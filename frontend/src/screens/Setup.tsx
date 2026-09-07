import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { AgentApiError, ingestDocument } from '../lib/agentApi';
import type { DocumentKind, DocumentRow, Profile } from '../lib/types';
import { useTeam } from '../state/TeamContext';
import { GitHubConnect } from '../components/GitHubConnect';
import { OrbLogo } from '../components/Avatar';

const STORAGE_BUCKET = 'documents';

function kindOf(filename: string): DocumentKind {
  const ext = filename.toLowerCase().split('.').pop() ?? '';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'docx' || ext === 'doc') return 'docx';
  if (ext === 'txt' && filename.toLowerCase().includes('whatsapp')) return 'whatsapp';
  return 'text';
}

export function Setup() {
  const { team, myUserId, isLeader, refreshRoster } = useTeam();
  const navigate = useNavigate();
  const teamId = team?.id ?? '';
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadNote, setUploadNote] = useState<string | null>(null);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteNote, setInviteNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const upload = async (file: File) => {
    setBusy(true);
    setUploadNote(null);
    try {
      const path = `${teamId}/${crypto.randomUUID()}-${file.name}`;
      const { error: upErr } = await supabase.storage.from(STORAGE_BUCKET).upload(path, file);
      if (upErr) throw new Error(upErr.message);
      const { data: row, error: insErr } = await supabase
        .from('documents')
        .insert({
          team_id: teamId,
          uploader_id: myUserId,
          kind: kindOf(file.name),
          filename: file.name,
          storage_path: path,
          status: 'uploaded',
        })
        .select()
        .single();
      if (insErr || !row) throw new Error(insErr?.message ?? 'insert failed');
      try {
        await ingestDocument((row as DocumentRow).id, teamId);
        setUploadNote(`${file.name} uploaded — seeding the wiki.`);
      } catch (e) {
        setUploadNote(
          `${file.name} uploaded, but ingestion did not start: ${
            e instanceof AgentApiError ? `${e.status} ${e.message}` : (e as Error).message
          }`,
        );
      }
    } catch (e) {
      setUploadNote(e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setBusy(false);
    }
  };

  const invite = async () => {
    const email = inviteEmail.trim().toLowerCase();
    if (!email) return;
    if (!isLeader) {
      setInviteNote('Only the team leader can invite members.');
      return;
    }
    setBusy(true);
    setInviteNote(null);
    // RLS caveat: profiles are only visible if you already share a team, so
    // this lookup finds returning teammates but not brand-new users. A proper
    // email-invite flow needs a backend endpoint (known gap — flagged).
    const { data: profile } = await supabase
      .from('profiles')
      .select('*')
      .eq('email', email)
      .maybeSingle();
    if (!profile) {
      setInviteNote(
        `No visible profile for ${email}. Ask them to sign in once, then invite again — email invites to brand-new users need a backend endpoint that doesn't exist yet.`,
      );
      setBusy(false);
      return;
    }
    const { error: mErr } = await supabase.from('memberships').insert({
      team_id: teamId,
      user_id: (profile as Profile).id,
      role: 'member',
      status: 'invited',
    });
    setBusy(false);
    if (mErr) setInviteNote(mErr.message);
    else {
      setInviteNote(`Invited ${(profile as Profile).display_name} — they accept from their team list.`);
      setInviteEmail('');
      await refreshRoster();
    }
  };

  return (
    <main className="page-scroll setup-page">
      <div className="content-column" style={{ maxWidth: 580, margin: '0 auto', padding: '52px 32px 60px' }}>
        <OrbLogo size={64} breathing style={{ marginBottom: 20 }} />
        <div className="display" style={{ fontSize: 40, lineHeight: 1.05, marginTop: 20 }}>
          Give Comrade
          <br />
          the context.
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 10, lineHeight: 1.6 }}>
          Three steps, and it can help from day one.
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16, marginTop: 30 }}>
          <div className="card" style={{ padding: '19px 21px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
              <span className="display" style={{ fontSize: 24, color: 'var(--terracotta)', width: 24 }}>
                1
              </span>
              <span style={{ fontSize: 14, fontWeight: 700 }}>Upload project documents</span>
            </div>
            <div
              style={{
                fontSize: 12.5,
                color: 'var(--muted)',
                marginTop: 7,
                lineHeight: 1.55,
                paddingLeft: 35,
              }}
            >
              Brief, proposal, rubric — PDF, .docx, or a WhatsApp export. These seed the team wiki.
            </div>
            <button
              onClick={() => fileRef.current?.click()}
              disabled={busy}
              className="mono"
              style={{
                margin: '13px 0 0 35px',
                width: 'calc(100% - 35px)',
                border: '1.5px dashed rgba(35,33,48,0.28)',
                background: 'transparent',
                borderRadius: 3,
                padding: 24,
                textAlign: 'center',
                fontSize: 11.5,
                color: 'var(--faint)',
                cursor: 'pointer',
              }}
            >
              {busy ? 'uploading…' : 'drop files here or browse'}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.doc,.txt"
              style={{ display: 'none' }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void upload(f);
                e.target.value = '';
              }}
            />
            {uploadNote && (
              <div style={{ fontSize: 12, color: 'var(--text-soft)', margin: '10px 0 0 35px' }}>
                {uploadNote}
              </div>
            )}
          </div>

          <div className="card" style={{ padding: '19px 21px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
              <span className="display" style={{ fontSize: 24, color: 'var(--terracotta)', width: 24 }}>
                2
              </span>
              <span style={{ fontSize: 14, fontWeight: 700 }}>Connect GitHub</span>
            </div>
            <div
              style={{
                fontSize: 12.5,
                color: 'var(--muted)',
                margin: '7px 0 13px',
                lineHeight: 1.55,
                paddingLeft: 35,
              }}
            >
              Comrade reads merged PRs, reviews and issues to keep the wiki honest, and
              reads the code itself to answer questions about it. It can also propose
              changes — those arrive as a pull request on a comrade/ branch that a member
              approves after seeing the diff, and nothing is ever pushed to your default
              branch.
            </div>
            {/* The steering mechanism nobody was told about. repo_guide() has
                read this file since the repo tools shipped, and no screen, doc
                or setup step ever mentioned it — so the strongest lever a team
                has over Comrade's behaviour on their code was invisible. */}
            <div
              style={{
                fontSize: 12.5,
                color: 'var(--muted)',
                margin: '9px 0 13px',
                lineHeight: 1.55,
                paddingLeft: 35,
              }}
            >
              If your repository has an <code>AGENTS.md</code>, <code>CLAUDE.md</code> or{' '}
              <code>.cursorrules</code>, Comrade reads it and follows your conventions —
              the same file your other coding tools use. It informs Comrade; it can&apos;t
              override the consent rules, so a stranger&apos;s pull request can&apos;t
              rewrite them.
            </div>
            <GitHubConnect teamId={teamId} isLeader={isLeader} />
          </div>

          <div className="card" style={{ padding: '19px 21px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
              <span className="display" style={{ fontSize: 24, color: 'var(--terracotta)', width: 24 }}>
                3
              </span>
              <span style={{ fontSize: 14, fontWeight: 700 }}>Invite your team</span>
            </div>
            <div style={{ display: 'flex', gap: 9, margin: '13px 0 0 35px' }}>
              <input
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void invite();
                }}
                placeholder="teammate@university.edu"
                style={{
                  flex: 1,
                  border: '1px solid rgba(35,33,48,0.22)',
                  borderRadius: 3,
                  padding: '10px 13px',
                  fontSize: 12.5,
                  fontFamily: 'inherit',
                  outline: 'none',
                  background: '#fff',
                }}
              />
              <button className="btn-ink" disabled={busy} onClick={() => void invite()}>
                INVITE
              </button>
            </div>
            {inviteNote && (
              <div style={{ fontSize: 12, color: 'var(--text-soft)', margin: '10px 0 0 35px', lineHeight: 1.5 }}>
                {inviteNote}
              </div>
            )}
          </div>

          <div
            style={{
              background: 'var(--ink)',
              borderRadius: 3,
              padding: '18px 21px',
              fontSize: 12.5,
              lineHeight: 1.65,
              color: 'var(--ink-roster)',
              boxShadow: '4px 4px 0 rgba(35,33,48,0.15)',
            }}
          >
            <span style={{ color: 'var(--peach)', fontWeight: 700 }}>
              What Comrade does on its own:
            </span>{' '}
            reads everything shared in the room, compiles the team wiki, tracks deadlines and doc
            opens, and sends you <b style={{ color: 'var(--paper)' }}>private nudges</b> when
            something slips. It <b style={{ color: 'var(--paper)' }}>never posts to the group</b> or
            acts in your name without consent — every action shows its literal tool and arguments
            first.
          </div>

          <button
            className="btn-primary"
            style={{ padding: 14, fontSize: 13, letterSpacing: '0.08em', marginTop: 4 }}
            onClick={() => navigate('../room')}
          >
            ENTER THE ROOM →
          </button>
        </div>
      </div>
    </main>
  );
}
