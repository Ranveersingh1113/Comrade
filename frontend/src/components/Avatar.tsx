import { avatarColors, initialsOf } from '../lib/format';
import type { CSSProperties } from 'react';

export function Avatar({
  userId,
  name,
  size = 36,
}: {
  userId: string;
  name: string;
  size?: number;
}) {
  const { bg, fg } = avatarColors(userId);
  return (
    <span
      style={{
        display: 'flex',
        width: size,
        height: size,
        flex: 'none',
        borderRadius: '50%',
        background: bg,
        color: fg,
        fontSize: size <= 24 ? 9 : 12,
        fontWeight: 600,
        alignItems: 'center',
        justifyContent: 'center',
        border: '1px solid rgba(32,45,53,.14)',
        letterSpacing: '.02em',
      }}
      aria-label={name}
      title={name}
    >
      {initialsOf(name)}
    </span>
  );
}

export function OrbLogo({
  size = 36,
  breathing = false,
  style,
}: {
  size?: number;
  breathing?: boolean;
  style?: CSSProperties;
}) {
  return (
    <span
      className={`orb-logo${breathing ? ' breathing' : ''}`}
      style={{ width: size, height: size, ...style }}
    >
      <img src="/images/comrade-orb.png" alt="" aria-hidden />
    </span>
  );
}

export function AiOrb(props: { size?: number; breathing?: boolean; style?: CSSProperties }) {
  return <OrbLogo {...props} />;
}
