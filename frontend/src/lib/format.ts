// Presentation helpers shared across screens.

const AVATAR_PALETTE: Array<{ bg: string; fg: string }> = [
  { bg: '#E3D5C3', fg: '#6B4E2E' },
  { bg: '#CDD9E3', fg: '#3A566E' },
  { bg: '#D3E0D8', fg: '#35513F' },
  { bg: '#E0D2DC', fg: '#5F4257' },
  { bg: '#DCD8E8', fg: '#4A4468' },
  { bg: '#E8DDCB', fg: '#6E5837' },
];

export function avatarColors(userId: string): { bg: string; fg: string } {
  let h = 0;
  for (let i = 0; i < userId.length; i += 1) {
    h = (h * 31 + userId.charCodeAt(i)) >>> 0;
  }
  return AVATAR_PALETTE[h % AVATAR_PALETTE.length];
}

export function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function firstNameOf(name: string): string {
  return name.trim().split(/\s+/)[0] ?? name;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** "9:15 AM" today, "YD 6:12 PM" yesterday, "JUL 11" earlier. */
export function messageTime(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  const clock = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (t >= startOfToday) return clock;
  if (t >= startOfToday - DAY_MS) return `YD ${clock}`;
  return d
    .toLocaleDateString([], { month: 'short', day: 'numeric' })
    .toUpperCase();
}

export function shortDate(iso: string): string {
  return new Date(iso)
    .toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
    .toUpperCase()
    .replace(/,/g, ' ·');
}

export function daysUntil(iso: string): number {
  const due = new Date(iso).getTime();
  return Math.max(0, Math.ceil((due - Date.now()) / DAY_MS));
}

/** "6D 21H" style countdown for consent expiry. */
export function countdown(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return 'EXPIRED';
  const hours = Math.floor(ms / (60 * 60 * 1000));
  const days = Math.floor(hours / 24);
  return `${days}D ${String(hours % 24).padStart(2, '0')}H`;
}

export function shortHash(hash: string): string {
  const clean = hash.replace(/^sha256:/, '');
  if (clean.length <= 12) return clean;
  return `${clean.slice(0, 4)}…${clean.slice(-4)}`;
}
