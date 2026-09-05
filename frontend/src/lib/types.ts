// Hand-written row types for the tables the frontend touches.
// Ground truth: supabase/migrations/20260612094142_init.sql and later migrations.

export interface Profile {
  id: string;
  display_name: string;
  email: string | null;
  github_username: string | null;
  created_at: string;
}

export interface Team {
  id: string;
  name: string;
  created_by: string | null;
  created_at: string;
  /** Stamped when the last active member leaves; cleared if anyone rejoins. */
  archived_at: string | null;
}

export type MembershipRole = 'leader' | 'member';
// 'left' is a state, not a deletion — the row survives so a departed
// member's messages and tasks keep an author the roster can still name.
export type MembershipStatus = 'invited' | 'active' | 'left';

export interface Membership {
  id: string;
  team_id: string;
  user_id: string;
  role: MembershipRole;
  status: MembershipStatus;
  joined_at: string | null;
  left_at: string | null;
  created_at: string;
}

export type ThreadVisibility = 'team' | 'restricted';
export type ThreadKind = 'discussion' | 'work';

export interface Thread {
  id: string;
  team_id: string;
  title: string;
  visibility: ThreadVisibility;
  kind: ThreadKind;
  work_state: 'planned' | 'active' | 'waiting' | 'review' | 'done' | null;
  owner_id: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface ThreadParticipant {
  thread_id: string;
  team_id: string;
  user_id: string;
  added_by: string;
  joined_at: string;
}
export type SenderKind = 'user' | 'ai';
export type DeletedScope = 'everyone' | 'me' | null;

export interface Message {
  id: string;
  team_id: string;
  thread_id: string;
  sender_kind: SenderKind;
  sender_id: string | null;
  body: string;
  deleted_scope: DeletedScope;
  deleted_by: string | null;
  deleted_at: string | null;
  /**
   * The member drafted this with Comrade in their private thread and published
   * it themselves (findings §13.4). Provenance, not authorship — attribution
   * stays with the member, which is the point of the whole shape.
   */
  ai_assisted: boolean;
  created_at: string;
}

export type DocumentKind = 'pdf' | 'docx' | 'whatsapp' | 'link' | 'text';
export type DocumentStatus = 'uploaded' | 'parsing' | 'ready' | 'failed';

export interface DocumentRow {
  id: string;
  team_id: string;
  uploader_id: string | null;
  kind: DocumentKind;
  filename: string | null;
  storage_path: string | null;
  status: DocumentStatus;
  summary: string | null;
  deleted_at: string | null;
  deleted_by: string | null;
  created_at: string;
}

export interface DocumentOpen {
  id: string;
  document_id: string;
  user_id: string;
  first_opened_at: string | null;
  expanded: boolean;
  questions_asked: number;
}

// 'fact' = things that are true about the project; 'skill' = how the team
// does something (findings §24.2 — the standard that replaces the handoff).
export type MemoryPageKind = 'fact' | 'skill';

export interface MemoryPage {
  id: string;
  team_id: string;
  title: string;
  description: string;
  kind: MemoryPageKind;
  created_at: string;
  updated_at: string;
}

export interface MemoryEntry {
  id: string;
  team_id: string;
  page_id: string | null;
  archived: boolean;
  created_at: string;
}

// 'invalidated' added by migration 20260703090000_compiler_v2.sql
export type MemoryChangeType = 'added' | 'revised' | 'invalidated' | 'reverted';

export interface MemoryVersion {
  id: string;
  entry_id: string;
  team_id: string;
  compilation_id: string | null;
  fact: string;
  change_type: MemoryChangeType;
  is_active: boolean;
  valid_from: string;
  valid_until: string | null;
  created_at: string;
}

export interface MemoryCitation {
  id: string;
  version_id: string;
  source_kind: 'message' | 'document' | 'github';
  source_id: string;
  excerpt: string | null;
  created_at: string;
}

export interface MemoryCompilation {
  id: string;
  team_id: string;
  trigger: 'scheduled' | 'on_demand' | 'flagged';
  status: 'running' | 'done' | 'failed';
  entries_added: number;
  entries_revised: number;
  entries_removed: number;
  diff_message_id: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface MemoryRevert {
  id: string;
  entry_id: string;
  team_id: string;
  member_id: string;
  reverted_version_id: string | null;
  created_at: string;
}

export type TaskStatus = 'proposed' | 'confirmed' | 'in_progress' | 'done';

export interface Task {
  id: string;
  team_id: string;
  assignee_id: string | null;
  title: string;
  description: string | null;
  deadline: string | null;
  status: TaskStatus;
  created_by_kind: 'user' | 'ai';
  created_by_id: string | null;
  confirmed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Milestone {
  id: string;
  team_id: string;
  title: string;
  due_at: string | null;
  created_by: string | null;
  created_at: string;
}

export type ConsentStatus =
  | 'pending'
  | 'approved'
  | 'edited'
  | 'cancelled'
  | 'executed'
  | 'rejected';

/** Blast-radius tier — informational since the T3 removal (findings §10). */
export type ConsentTier = 'T0' | 'T1' | 'T2';

export interface ConsentItem {
  id: string;
  team_id: string;
  requesting_member_id: string | null;
  tool_name: string;
  tool_args: Record<string, unknown>;
  source_snippet: string | null;
  action_hash: string;
  status: ConsentStatus;
  reversible: boolean;
  tier: ConsentTier;
  expires_at: string | null;
  created_at: string;
  resolved_at: string | null;
  thread_id: string | null;
  agent_run_id: string | null;
  resolution_reason: string | null;
}

export interface ContributionRow {
  team_id: string;
  user_id: string;
  tasks_done: number;
  tasks_active: number;
  github_events: number;
  group_messages: number;
}

/**
 * Member discussion anchored to a memory ENTRY (findings §6.3-6).
 *
 * The entry, not the version: consolidation replaces versions routinely, and a
 * comment anchored to one would detach from the fact it argues with at exactly
 * the moment the fact changes.
 */
export interface MemoryComment {
  id: string;
  entry_id: string;
  team_id: string;
  author_id: string;
  body: string;
  created_at: string;
}
