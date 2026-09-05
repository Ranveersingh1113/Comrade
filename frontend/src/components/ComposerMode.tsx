import { useEffect, useState } from 'react';

export type ComposerModeValue = 'team' | 'agent';

function storageKey(userId: string, threadId: string): string {
  return `comrade.composerMode.${userId}.${threadId}`;
}

function savedComposerMode(userId: string, threadId: string, fallback: ComposerModeValue): ComposerModeValue {
  return (localStorage.getItem(storageKey(userId, threadId)) as ComposerModeValue | null) ?? fallback;
}

export function ComposerMode({
  userId, threadId, defaultMode, onChange,
}: {
  userId: string;
  threadId: string;
  defaultMode: ComposerModeValue;
  onChange: (mode: ComposerModeValue) => void;
}) {
  const [mode, setMode] = useState<ComposerModeValue>(
    () => savedComposerMode(userId, threadId, defaultMode),
  );
  useEffect(() => {
    setMode(savedComposerMode(userId, threadId, defaultMode));
  }, [userId, threadId, defaultMode]);
  const choose = (next: ComposerModeValue) => {
    setMode(next);
    localStorage.setItem(storageKey(userId, threadId), next);
    onChange(next);
  };
  return (
    <span aria-label={`Composer mode: ${mode}`} style={{ display: 'inline-flex', gap: 3 }}>
      <button type="button" aria-label="Team mode" aria-pressed={mode === 'team'} onClick={() => choose('team')}>Team</button>
      <button type="button" aria-label="Agent mode" aria-pressed={mode === 'agent'} onClick={() => choose('agent')}>Agent</button>
    </span>
  );
}
