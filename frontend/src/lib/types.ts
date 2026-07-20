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
}

export type MembershipRole = 'leader' | 'member';
export type MembershipStatus = 'invited' | 'active';

export interface Membership {
  id: string;
  team_id: string;
  user_id: string;
  role: MembershipRole;
  status: MembershipStatus;
  joined_at: string | null;
  created_at: string;
}

export type ThreadType = 'group' | 'private';
export type SenderKind = 'user' | 'ai';
export type DeletedScope = 'everyone' | 'me' | null;

export interface Message {
  id: string;
  team_id: string;
  thread_type: ThreadType;
  thread_owner_id: string | null;
  sender_kind: SenderKind;
  sender_id: string | null;
  body: string;
  deleted_scope: DeletedScope;
  deleted_by: string | null;
  deleted_at: string | null;
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

export interface MemoryPage {
  id: string;
  team_id: string;
  title: string;
  description: string;
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

/** Blast-radius tier (migration 20260719130000): T3 needs a second key. */
export type ConsentTier = 'T0' | 'T1' | 'T2' | 'T3';

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
  second_approver_id: string | null;
  second_approved_at: string | null;
  expires_at: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface ContributionRow {
  team_id: string;
  user_id: string;
  tasks_done: number;
  tasks_active: number;
  github_events: number;
  group_messages: number;
}
