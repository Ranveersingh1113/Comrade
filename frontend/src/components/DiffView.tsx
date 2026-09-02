// A unified diff a person can actually read.
//
// The consent protocol's promise is "what you approve is exactly what runs",
// and repo_open_pr carries the literal patch in its consent row so the card
// shows the change itself rather than a pointer to a directory that a later
// turn may already have reset.
//
// That promise was hollow in the UI. ConsentCard renders every tool argument
// as `{key} {value}`, so the patch arrived as one unwrapped line containing
// literal backslash-n characters — technically present, and unreadable. A
// reviewer who cannot see the change is not reviewing it, they are clicking
// approve because the agent seems to know what it is doing, which is the exact
// failure mode consent exists to prevent.

interface Props {
  patch: string;
}

//: A diff longer than this is not being read line by line anyway, and mounting
//: tens of thousands of nodes makes the card janky to scroll. The pull request
//: itself is the place to review something that large — the cap says so rather
//: than silently showing a prefix.
const MAX_LINES = 600;

function colorFor(line: string): string | undefined {
  if (line.startsWith('+++') || line.startsWith('---')) return 'var(--ink-faint)';
  if (line.startsWith('+')) return '#7fb886';
  if (line.startsWith('-')) return '#d98b82';
  if (line.startsWith('@@')) return 'var(--peach)';
  if (line.startsWith('diff ') || line.startsWith('index ')
      || line.startsWith('new file') || line.startsWith('deleted file')) {
    return 'var(--ink-faint)';
  }
  return undefined;
}

export function DiffView({ patch }: Props) {
  const all = patch.split('\n');
  const lines = all.slice(0, MAX_LINES);
  const dropped = all.length - lines.length;

  const files = all.filter((l) => l.startsWith('diff --git ')).length;
  const added = all.filter((l) => l.startsWith('+') && !l.startsWith('+++')).length;
  const removed = all.filter((l) => l.startsWith('-') && !l.startsWith('---')).length;

  return (
    <div style={{ margin: '9px 0 0' }}>
      <div
        className="mono"
        style={{ fontSize: 10.5, color: 'var(--ink-faint)', marginBottom: 5 }}
      >
        {files} file{files === 1 ? '' : 's'}{' '}
        <span style={{ color: '#7fb886' }}>+{added}</span>{' '}
        <span style={{ color: '#d98b82' }}>−{removed}</span>
      </div>
      <pre
        className="mono"
        style={{
          margin: 0,
          background: 'var(--ink)',
          borderRadius: 3,
          padding: '11px 13px',
          fontSize: 11,
          lineHeight: 1.55,
          color: '#C6C2CE',
          overflowX: 'auto',
          maxHeight: 340,
          overflowY: 'auto',
          whiteSpace: 'pre',
        }}
      >
        {lines.map((line, i) => (
          <div key={i} style={{ color: colorFor(line) }}>
            {line === '' ? ' ' : line}
          </div>
        ))}
        {dropped > 0 && (
          <div style={{ color: 'var(--peach)', paddingTop: 6 }}>
            … {dropped} more lines. Review the rest on the pull request.
          </div>
        )}
      </pre>
    </div>
  );
}
