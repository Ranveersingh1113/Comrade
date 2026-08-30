import { avatarColors, initialsOf } from '../lib/format';

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
      }}
    >
      {initialsOf(name)}
    </span>
  );
}

export function AiOrb({ size = 36, breathing = false }: { size?: number; breathing?: boolean }) {
  return (
    <span
      className={`orb${breathing ? ' breathing' : ''}`}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.36) }}
    >
      ◈
    </span>
  );
}
