// HTTP surface of the FastAPI agent runtime (built in parallel — see repo HANDOFF).
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

export interface AgentTurnResult {
  run_id: string;
  reply: string;
  user_message_id: string;
  reply_message_id: string;
}

export function agentTurn(
  teamId: string,
  text: string,
  threadType: 'private' | 'group',
): Promise<AgentTurnResult> {
  return request('/agent/turn', { team_id: teamId, text, thread_type: threadType });
}

export interface ConsentActionResult {
  status: string;
  result?: unknown;
}

export function approveConsent(consentId: string, teamId: string) {
  return request<ConsentActionResult>(`/consent/${consentId}/approve`, { team_id: teamId });
}

export function rejectConsent(consentId: string, teamId: string) {
  return request<{ status: string }>(`/consent/${consentId}/reject`, { team_id: teamId });
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
