import { beforeEach, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ComposerMode } from '../../src/components/ComposerMode';

beforeEach(() => localStorage.clear());

test('persists an explicit composer choice for that user and thread', async () => {
  const user = userEvent.setup();
  const changed: string[] = [];
  const { unmount } = render(
    <ComposerMode userId="u1" threadId="thread-a" defaultMode="team" onChange={(mode) => changed.push(mode)} />,
  );

  await user.click(screen.getByRole('button', { name: 'Agent mode' }));
  expect(screen.getByText('Agent')).toBeInTheDocument();
  expect(changed).toEqual(['agent']);
  unmount();

  render(<ComposerMode userId="u1" threadId="thread-a" defaultMode="team" onChange={() => {}} />);
  expect(screen.getByText('Agent')).toBeInTheDocument();
});

test('does not reuse a mode from a different thread', async () => {
  const user = userEvent.setup();
  const { unmount } = render(
    <ComposerMode userId="u1" threadId="thread-a" defaultMode="team" onChange={() => {}} />,
  );
  await user.click(screen.getByRole('button', { name: 'Agent mode' }));
  unmount();

  render(<ComposerMode userId="u1" threadId="thread-b" defaultMode="team" onChange={() => {}} />);
  expect(screen.getByText('Team')).toBeInTheDocument();
});
