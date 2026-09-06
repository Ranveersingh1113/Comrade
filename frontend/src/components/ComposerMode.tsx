import { useEffect, useState } from 'react';
import { OrbLogo } from './Avatar';

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
    <span className="composer-mode">
      <button
        type="button"
        aria-label="Comrade mode"
        aria-pressed={mode === 'agent'}
        title={mode === 'agent' ? 'Comrade is on' : 'Comrade is off'}
        onClick={() => choose(mode === 'agent' ? 'team' : 'agent')}
      >
        <OrbLogo size={22} />
        <span className="composer-mode-label">Comrade</span>
      </button>
    </span>
  );
}
