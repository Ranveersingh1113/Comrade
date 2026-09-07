-- A document with no thread is a team document.
--
-- The column default is `turn_context`, which is right for an ATTACHMENT: a
-- member dropping a file into a conversation is not publishing it. But the
-- team documents screen inserts with no thread at all, and those uploads mean
-- exactly the opposite — somebody went to the team's shared files and added
-- one. Defaulting those to turn_context would have quietly stopped the whole
-- documents feature compiling anything, which is how a privacy default turns
-- into a silent outage.
--
-- So the rule is about the SHAPE of the row rather than the column default:
-- attached to a thread means scoped to that thread; attached to nothing means
-- shared with the team.
create or replace function public.trg_document_default_purpose()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.thread_id is null and new.purpose = 'turn_context' then
    new.purpose := 'team_knowledge';
  end if;
  return new;
end;
$$;

drop trigger if exists trg_documents_default_purpose on public.documents;
create trigger trg_documents_default_purpose
  before insert on public.documents
  for each row execute function public.trg_document_default_purpose();
