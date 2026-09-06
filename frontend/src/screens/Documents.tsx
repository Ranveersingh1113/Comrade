import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { AgentApiError, ingestDocument } from '../lib/agentApi';
import { messageTime } from '../lib/format';
import type { DocumentKind, DocumentOpen, DocumentRow } from '../lib/types';
import { isConfirmedEmpty } from '../lib/listState';
import { useTeam } from '../state/TeamContext';

const STORAGE_BUCKET = 'documents';

function kindOf(filename: string): DocumentKind {
  const ext = filename.toLowerCase().split('.').pop() ?? '';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'docx' || ext === 'doc') return 'docx';
  if (ext === 'txt' && filename.toLowerCase().includes('whatsapp')) return 'whatsapp';
  return 'text';
}

export function Documents() {
  const { team, myUserId, profileOf } = useTeam();
  const teamId = team?.id ?? '';
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [myOpens, setMyOpens] = useState<Map<string, DocumentOpen>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (!teamId) return;
    const [{ data: rows, error: dErr }, { data: opens }] = await Promise.all([
      supabase
        .from('documents')
        .select('*')
        .eq('team_id', teamId)
        .is('deleted_at', null)
        .order('created_at', { ascending: false }),
      supabase.from('document_opens').select('*').eq('user_id', myUserId),
    ]);
    if (dErr) {
      setError(dErr.message);
      return;
    }
    setDocs((rows as DocumentRow[] | null) ?? []);
    setMyOpens(
      new Map(((opens as DocumentOpen[] | null) ?? []).map((o) => [o.document_id, o])),
    );
  }, [teamId, myUserId]);

  useEffect(() => {
    void load();
  }, [load]);

  const upload = async (file: File) => {
    setUploading(true);
    setError(null);
    setNotice(null);
    try {
      const path = `${teamId}/${crypto.randomUUID()}-${file.name}`;
      const { error: upErr } = await supabase.storage.from(STORAGE_BUCKET).upload(path, file);
      if (upErr) throw new Error(`Storage upload failed: ${upErr.message}`);
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
      if (insErr || !row) throw new Error(insErr?.message ?? 'documents insert failed');
      try {
        await ingestDocument((row as DocumentRow).id, teamId, file);
        setNotice(`${file.name} uploaded — compiling into the wiki.`);
      } catch (e) {
        // The row and the stored file exist either way; only parsing failed.
        setNotice(
          `${file.name} uploaded, but ingestion did not start: ${
            e instanceof AgentApiError ? `${e.status} ${e.message}` : (e as Error).message
          }`,
        );
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const markOpened = async (doc: DocumentRow) => {
    const existing = myOpens.get(doc.id);
    if (existing?.first_opened_at) return;
    await supabase.from('document_opens').upsert(
      {
        document_id: doc.id,
        user_id: myUserId,
        first_opened_at: new Date().toISOString(),
        expanded: true,
      },
      { onConflict: 'document_id,user_id' },
    );
    await load();
  };

  const openDoc = async (doc: DocumentRow) => {
    void markOpened(doc);
    if (!doc.storage_path) return;
    const { data, error: sErr } = await supabase.storage
      .from(STORAGE_BUCKET)
      .createSignedUrl(doc.storage_path, 60 * 10);
    if (sErr || !data?.signedUrl) {
      setError(`Could not open file: ${sErr?.message ?? 'no URL'}`);
      return;
    }
    window.open(data.signedUrl, '_blank', 'noopener');
  };

  return (
    <main className="page-scroll documents-page">
      <header className="screen-header">
        <div className="display">Documents</div>
        <div className="sub">Everything shared here is compiled into the team wiki</div>
      </header>
      <div
        className="content-column"
        style={{
          maxWidth: 780,
          margin: '0 auto',
          padding: '28px 32px 44px',
          display: 'flex',
          flexDirection: 'column',
          gap: 30,
        }}
      >
        <div>
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              marginBottom: 13,
            }}
          >
            <div className="micro-label">Shared documents</div>
            <button
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              style={{
                border: 'none',
                background: 'transparent',
                fontSize: 12,
                color: 'var(--terracotta)',
                cursor: 'pointer',
                fontWeight: 600,
              }}
            >
              {uploading ? 'Uploading…' : '+ Upload'}
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
          </div>

          {error && (
            <div style={{ fontSize: 12.5, color: 'var(--terracotta)', marginBottom: 10 }}>
              {error}
            </div>
          )}
          {notice && (
            <div style={{ fontSize: 12.5, color: 'var(--sage)', marginBottom: 10 }}>{notice}</div>
          )}

          <div className="card" style={{ overflow: 'hidden' }}>
            {isConfirmedEmpty(error, docs === null, docs.length) && (
              <div style={{ padding: '18px 19px', fontSize: 13, color: 'var(--faint)' }}>
                Nothing shared yet — upload a brief, proposal, or WhatsApp export.
              </div>
            )}
            {docs.map((d) => {
              const opened = myOpens.get(d.id)?.first_opened_at;
              const uploader = profileOf(d.uploader_id);
              return (
                <button
                  key={d.id}
                  onClick={() => void openDoc(d)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 14,
                    padding: '15px 19px',
                    borderBottom: '1px solid rgba(35,33,48,0.08)',
                    width: '100%',
                    border: 'none',
                    background: 'transparent',
                    cursor: 'pointer',
                    textAlign: 'left',
                  }}
                >
                  <span
                    style={{
                      width: 34,
                      height: 34,
                      flex: 'none',
                      borderRadius: 3,
                      background: 'var(--ink)',
                      color: 'var(--paper)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 14,
                    }}
                  >
                    <span className="mono" style={{ fontSize: 10 }}>DOC</span>
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{d.filename ?? d.kind}</div>
                    <div className="mono" style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 3 }}>
                      {(uploader?.display_name ?? 'unknown').toUpperCase()} ·{' '}
                      {messageTime(d.created_at).toUpperCase()} ·{' '}
                      {d.status === 'ready' ? 'IN WIKI' : d.status.toUpperCase()}
                    </div>
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div
                      style={{
                        fontSize: 11.5,
                        fontWeight: 600,
                        color: opened ? 'var(--sage)' : 'var(--terracotta)',
                      }}
                    >
                      {opened ? 'Opened by you' : 'Not opened yet'}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
          <div style={{ fontSize: 11, color: 'var(--faint)', marginTop: 10, lineHeight: 1.6 }}>
            Comrade tracks who opens each doc. After 48 hours, anyone who hasn't gets a gentle
            private nudge — no names, no guilt. (Teammates' open-states stay private to them; only
            Comrade sees the whole picture.)
          </div>
        </div>

        <Link
          to="../wiki"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 14,
            background: 'var(--ink)',
            border: 'none',
            borderRadius: 3,
            padding: '17px 21px',
            boxShadow: '4px 4px 0 rgba(35,33,48,0.15)',
            cursor: 'pointer',
            textAlign: 'left',
            textDecoration: 'none',
          }}
        >
          <span className="mono" style={{ fontSize: 10, color: 'var(--peach-pale)' }}>WIKI</span>
          <span style={{ flex: 1 }}>
            <span style={{ display: 'block', fontSize: 13, fontWeight: 600, color: 'var(--paper)' }}>
              Project memory lives in the team wiki
            </span>
            <span style={{ display: 'block', fontSize: 11, color: 'var(--ink-muted)', marginTop: 3 }}>
              compiled from these documents and chat · every fact citable and revertible
            </span>
          </span>
          <span style={{ color: 'var(--peach-pale)', fontSize: 14 }}>→</span>
        </Link>
      </div>
    </main>
  );
}
