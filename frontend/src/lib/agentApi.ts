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

/** Human-readable text for a failed agent call. */
export function agentErrorText(e: unknown): string {
  if (e instanceof AgentApiError) {
    if (e.status === 401) return 'Your session expired — sign in again.';
    if (e.status === 403) return "You're not an active member of this team.";
    if (e.status === 404) return 'Agent endpoint not reachable — is the backend running on :8000?';
    if (e.status === 422) return `The runtime rejected that request: ${e.message}`;
    return `${e.status} — ${e.message}`;
  }
  // fetch() rejects with a TypeError when the host is unreachable / CORS-blocked.
  if (e instanceof TypeError) {
    return `Can't reach Comrade's runtime at ${BASE} — is the backend running?`;
  }
  return e instanceof Error ? e.message : 'Send failed';
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

export interface StreamFrame {
  // 'busy' was missing here, which is how it came to be silently dropped:
  // stream_turn emits it and returns when a group room's turn lock is held
  // (decision Q6), and GroupRoom matched neither 'text' nor 'error', so the
  // member's message sat unanswered with no explanation at all — the exact
  // outcome that decision was written to prevent.
  type:
    | 'run'
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
  tool?: string;
  text?: string;
  /** Carried by 'busy', 'empty', 'error', and a terminal 'done'
   *  whose run recorded a reason (agent_runs.last_error). */
  detail?: string;
  user_message_id?: string;
  reply_message_id?: string | null;
}

/**
 * Stream one agent turn, calling `onFrame` per NDJSON line.
 * fetch (not EventSource) because the turn needs the Authorization header.
 */
export async function streamTurn(
  teamId: string,
  text: string,
  threadId: string,
  onFrame: (frame: StreamFrame) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/agent/turn/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: await authHeader(),
    },
    body: JSON.stringify({ team_id: teamId, text, thread_id: threadId }),
  });
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
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? ''; // keep the partial line
    for (const line of lines) {
      if (line.trim()) onFrame(JSON.parse(line) as StreamFrame);
    }
  }
  if (buffer.trim()) onFrame(JSON.parse(buffer) as StreamFrame);
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
