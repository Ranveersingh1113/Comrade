// HTTP surface of the FastAPI agent runtime.
// Every request carries the Supabase session JWT; identity comes from the token.

import { supabase } from './supabase';

const BASE =
  (import.meta.env.VITE_AGENT_API_URL as string | undefined) ?? 'http://localhost:8000';

export class AgentApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'AgentApiError';
    this.status = status;
  }
}

async function authHeader(): Promise<string> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new AgentApiError(401, 'Not signed in');
  return `Bearer ${token}`;
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: await authHeader(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new AgentApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/**
 * Log the technical detail and return a short reference for the member.
 *
 * 🔴 The detail used to go in the message. "Agent endpoint not reachable — is
 * the backend running on :8000?" is a sentence about somebody else's laptop:
 * it tells the person reading it nothing they can act on, and it tells anyone
 * helping them nothing they can search for. The detail belongs in the console
 * where support can ask for it; the member gets something to DO and a short
 * code to quote.
 */
export function failureReference(e: unknown): string {
  const reference = Math.random().toString(36).slice(2, 8).toUpperCase();
  // console.error, not a swallowed log: this is the only copy of the cause.
  console.error(`[comrade ${reference}]`, e);
  return reference;
}

/** What to tell the member, and what to do about it. */
export function agentErrorText(e: unknown): string {
  const reference = failureReference(e);
  const tail = ` (reference ${reference})`;
  if (e instanceof AgentApiError) {
    if (e.status === 401) return 'Your session has expired. Sign in again to carry on.';
    if (e.status === 403) return "You're not an active member of this team, so this thread is read-only for you.";
    if (e.status === 429) return `This team has used its turns for the hour. Try again a little later.${tail}`;
    if (e.status >= 500) return `Comrade hit a problem on its side. Try again in a moment.${tail}`;
    // Other 4xx: the server wrote that sentence FOR the member — a refused
    // GitHub installation, a document with nothing stored, a rejected
    // argument. Replacing it with "something went wrong" would throw away the
    // one part of the failure that is actually actionable. Generic text is
    // for failures nobody wrote a sentence for.
    if (e.message && e.message !== 'Not Found' && e.message !== 'Bad Request') {
      return `${e.message}${tail}`;
    }
    return `Comrade could not do that. Reload and try again; if it keeps happening, tell whoever runs this deployment.${tail}`;
  }
  // fetch() rejects with a TypeError when the host is unreachable or the
  // request was blocked — from here that is indistinguishable from being
  // offline, and both have the same answer.
  if (e instanceof TypeError) {
    return `No connection to Comrade. Check your network and try again — nothing you typed has been lost.${tail}`;
  }
  return `Something went wrong. Try again.${tail}`;
}

export interface ConsentActionResult {
  status: string;
  result?: unknown;
}

export function approveConsent(consentId: string, teamId: string, grantForThread = false) {
  return request<ConsentActionResult>(`/consent/${consentId}/approve`, grantForThread
    ? { team_id: teamId, grant_for_thread: true }
    : { team_id: teamId });
}

export function rejectConsent(consentId: string, teamId: string, reason?: string) {
  return request<{ status: string }>(`/consent/${consentId}/reject`, {
    team_id: teamId,
    reason,
  });
}

/** One tap: tombstone a proactive AI observation + record "don't do this again". */
export function suppressObservation(messageId: string, teamId: string, kind: string) {
  return request<{ suppression_id: string; kind: string }>(
    `/observations/${messageId}/suppress`,
    { team_id: teamId, kind },
  );
}

export function editAndApproveConsent(
  consentId: string,
  teamId: string,
  args: Record<string, unknown>,
) {
  return request<ConsentActionResult>(`/consent/${consentId}/edit_and_approve`, {
    team_id: teamId,
    args,
  });
}

/**
 * Kick off parsing for an already-inserted `documents` row.
 * Contract: team_id is a QUERY parameter, the file is multipart `file`.
 * Insert the documents row under RLS first, then call this.
 */
export async function ingestDocument(
  documentId: string,
  teamId: string,
  file: File,
): Promise<{ job_id: string }> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(
    `${BASE}/documents/${documentId}/ingest?team_id=${encodeURIComponent(teamId)}`,
    {
      method: 'POST',
      headers: { Authorization: await authHeader() }, // no Content-Type: the browser sets the boundary
      body: form,
    },
  );
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new AgentApiError(res.status, detail);
  }
  return (await res.json()) as { job_id: string };
}

/**
 * Parse an already-uploaded document again.
 *
 * 🔴 The only retry was `/ingest`, which takes the bytes as multipart — so a
 * member whose ingestion failed had to find the file and upload it a second
 * time, and after a reload the browser no longer had it. This reads back what
 * is already stored.
 */
export function reingestDocument(documentId: string, teamId: string) {
  return request<{ job_id: string }>(
    `/documents/${encodeURIComponent(documentId)}/reingest`, { team_id: teamId },
  );
}

export interface StreamFrame {
  // 'busy' was missing here, which is how it came to be silently dropped:
  // stream_turn emits it and returns when a group room's turn lock is held
  // (decision Q6), and GroupRoom matched neither 'text' nor 'error', so the
  // member's message sat unanswered with no explanation at all — the exact
  // outcome that decision was written to prevent.
  type:
    | 'run'
    // Lifecycle, on its own frame type. Queued, running and the two waiting
    // states are not content, and folding them into 'done' meant a run parked
    // on a consent card read to the member as a turn that failed in silence.
    | 'status'
    // Sent while a run is quiet, so a dead connection can be told apart from a
    // slow one now that the poll backs off. Carries nothing and needs no
    // handling beyond arriving.
    | 'heartbeat'
    | 'busy'
    // The model came back with nothing at all. Was reported as a successful
    // turn that simply rendered no reply — see agent/runtime.py.
    | 'empty'
    | 'tool_call'
    | 'tool_result'
    | 'text'
    | 'done'
    | 'error';
  run_id?: string;
  /** Carried by 'run' and 'status': the run's server-side lifecycle state. */
  status?: string;
  tool?: string;
  text?: string;
  /** Carried by 'busy', 'empty', 'error', and a terminal 'done'
   *  whose run recorded a reason (agent_runs.last_error). */
  detail?: string;
  user_message_id?: string;
  reply_message_id?: string | null;
  seq?: number;
  args?: Record<string, unknown>;
  response?: unknown;
}

export type AgentStep = Pick<StreamFrame, 'seq' | 'type' | 'tool' | 'args' | 'response'>;

export interface AgentRun {
  id: string;
  status?: string;
  /** Who asked for this turn. Only they may stop it. */
  requester_id?: string | null;
  steps: AgentStep[];
}

/** Statuses that mean a run is still doing work worth watching. */
export const ACTIVE_RUN_STATUSES = new Set(['queued', 'running']);

/** A run parked on a human decision. Not finished, and not failed either. */
export const WAITING_RUN_STATUSES = new Set([
  'waiting_for_permission', 'waiting_for_user',
]);

export interface TurnAccepted {
  run_id: string;
  /** 'queued' | 'steering' | 'duplicate'. */
  status: string;
}

/**
 * Submit a turn. Returns as soon as it is DURABLE, not when it is answered.
 *
 * `clientRequestId` identifies the attempt and must survive a retry: an
 * accepted POST whose connection then died is indistinguishable from one that
 * never arrived, and without the id the only safe options were to lose the
 * member's message or to send it twice. With it, the retry comes back as
 * `duplicate` carrying the run already accepted.
 */
export function startTurn(
  teamId: string, text: string, threadId: string, clientRequestId: string,
): Promise<TurnAccepted> {
  return request<TurnAccepted>('/agent/turn', {
    team_id: teamId, text, thread_id: threadId,
    client_request_id: clientRequestId,
  });
}

/**
 * Stop a turn.
 *
 * There was no way to before this: a member who asked the wrong question, or
 * watched a turn head somewhere expensive, could only wait it out. The server
 * requires that the caller be the member who ASKED for the run — seeing a
 * teammate's turn is not standing for them.
 */
export function cancelRun(teamId: string, runId: string) {
  return request<{ status: string; already_finished: boolean }>(
    `/agent/runs/${encodeURIComponent(runId)}/cancel`, { team_id: teamId },
  );
}

/**
 * How a follow ENDED, which is the whole point of returning anything.
 *
 * 🔴 `truncated` is the case that did not exist before. The old reader
 * returned normally whenever the body ended, so a severed connection and a
 * finished turn looked identical: the indicator stopped, no reply arrived, and
 * the run carried on answering into a browser that had stopped listening.
 */
export type FollowOutcome = 'done' | 'waiting' | 'truncated' | 'aborted';

/**
 * Follow a durable run over GET, from a cursor.
 *
 * GET rather than the POST that started it, because reattaching after a
 * refresh or a dropped connection is then the SAME call as attaching — one
 * path instead of a live path and a lost one.
 */
export async function followRun(
  teamId: string,
  runId: string,
  onFrame: (frame: StreamFrame) => void,
  opts: { afterSeq?: number; signal?: AbortSignal } = {},
): Promise<FollowOutcome> {
  const afterSeq = opts.afterSeq ?? -1;
  const url =
    `${BASE}/agent/runs/${encodeURIComponent(runId)}/stream`
    + `?team_id=${encodeURIComponent(teamId)}&after_seq=${afterSeq}`;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Authorization: await authHeader() },
      signal: opts.signal,
    });
  } catch (e) {
    if (opts.signal?.aborted) return 'aborted';
    throw e;
  }
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new AgentApiError(res.status, detail);
  }
  // Truncated until proven otherwise. Only a terminal frame is evidence the
  // turn is over; a body that simply stops is exactly the ambiguous case.
  let outcome: FollowOutcome = 'truncated';
  const emit = (line: string) => {
    if (!line.trim()) return;
    const frame = JSON.parse(line) as StreamFrame;
    if (frame.type === 'done') outcome = 'done';
    else if (frame.type === 'status' && WAITING_RUN_STATUSES.has(frame.status ?? '')) {
      outcome = 'waiting';
    }
    onFrame(frame);
  };
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? ''; // keep the partial line
      for (const line of lines) emit(line);
    }
    emit(buffer);
  } catch (e) {
    if (opts.signal?.aborted) return 'aborted';
    throw e;
  }
  return outcome;
}


// ---------------------------------------------------------------------------
// Team lifecycle (D4)
// ---------------------------------------------------------------------------

/**
 * Ask a teammate to leave. This does NOT remove them.
 *
 * It files a consent card in THEIR private thread, which only they can see and only
 * they can approve — §23.1's "nobody configures another member's
 * participation", implemented with the mechanism already here rather than a
 * remove button with a confirmation dialog on it.
 */
export async function askToLeave(
  teamId: string,
  memberId: string,
  reason?: string,
): Promise<{ consent_id: string; status: string }> {
  return request(`/teams/${teamId}/members/${memberId}/departure-request`, {
    reason: reason?.trim() || null,
  });
}

/**
 * Download the team's history as JSON — everything THIS member can read.
 *
 * Not routed through `request`, which is POST-and-JSON: this is a GET whose
 * body is a file. Do it before leaving; afterwards RLS returns nothing and
 * there is nothing to export.
 */
export async function downloadTeamExport(teamId: string): Promise<void> {
  const res = await fetch(`${BASE}/teams/${teamId}/export`, {
    headers: { Authorization: await authHeader() },
  });
  if (!res.ok) throw new AgentApiError(res.status, res.statusText);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = `comrade-${teamId}.json`;
  a.click();
  // Revoked on the next tick, not in a `finally`. The download starts
  // asynchronously after click() returns, so revoking immediately races it and
  // can cancel a save that appeared to work. The blob is a few megabytes of
  // team history; leaving it pinned for one tick is the cheaper mistake.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}


/**
 * Ask Comrade to remember something that was said (findings §20.7.1).
 *
 * Does NOT write a fact. Members cannot write memory at all — it queues a
 * compile of this one message, which then earns a citation, a diff card and a
 * one-tap revert like every other fact. Group messages only: private threads
 * never reach memory.
 */
export async function rememberMessage(
  teamId: string,
  messageId: string,
): Promise<{ job_id: string }> {
  return request(`/teams/${teamId}/messages/${messageId}/remember`);
}

// ---------- GitHub ----------
//
// Only what the browser cannot do itself lives behind these. Connecting and
// disconnecting a repository are writes straight to Supabase, because RLS is
// what decides them (au_github_repos_insert requires team leadership AND an
// installation the same team owns) — routing them through the API would be a
// second place to get the same rule right, and the weaker place, since anyone
// can post to PostgREST directly.

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { Authorization: await authHeader() },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new AgentApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Durable activity is server-read: direct browser access to agent_steps is forbidden. */
export function getThreadRuns(teamId: string, threadId: string) {
  return getJson<AgentRun[]>(`/threads/${encodeURIComponent(threadId)}/agent-runs?team_id=${encodeURIComponent(teamId)}`);
}

export interface InstallLink {
  configured: boolean;
  url?: string;
  reason?: string;
}

/** Where to send someone to install the App, or why this deployment cannot. */
export function githubInstallLink(teamId: string) {
  return getJson<InstallLink>(`/teams/${teamId}/github/install`);
}

export interface ConnectableRepo {
  full_name: string;
  connected: boolean;
  /** null until the first successful clone. */
  cloned_at: string | null;
  /** Why the last clone failed, if it did. */
  sync_error: string | null;
  /**
   * The dependency environment, for connected repositories only.
   *
   * Derived on the SERVER, not from the row: "stale" is the stored key against
   * what the checkout would produce now, and the checkout is on the server.
   * A status computed in the browser could say "ready" about an environment
   * built from code two weeks old.
   */
  environment?: RepoEnvironment;
}

export type RepoEnvironmentStatus =
  | 'disabled' | 'none' | 'building' | 'ready' | 'failed' | 'stale' | 'unknown';

export interface RepoEnvironment {
  status: RepoEnvironmentStatus;
  detail: string;
}

export interface InstallationRepos {
  installation_id: number;
  account_login: string;
  repositories: ConnectableRepo[];
  error?: string;
}

/** What this team's installations can reach — the picker's contents. */
export function githubRepositories(teamId: string) {
  return getJson<{ installations: InstallationRepos[] }>(
    `/teams/${teamId}/github/repositories`,
  );
}

/**
 * Finish an install. `state` and `code` come back from GitHub in the redirect;
 * both are required, because installation_id alone is an unauthenticated
 * number in a URL and installation ids are sequential.
 */
export function githubRecordInstallation(
  installationId: number,
  state: string,
  code: string,
) {
  // No team in the path. A GitHub App has ONE fixed Callback URL, so the redirect
  // cannot carry a team — and the signed state token is the only trustworthy
  // place for it anyway. The response says which team it landed in.
  return request<{ team_id: string; installation_id: number; account_login: string }>(
    '/github/installations',
    { installation_id: installationId, state, code },
  );
}
