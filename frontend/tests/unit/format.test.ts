import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import {
  avatarColors, countdown, daysUntil, firstNameOf, initialsOf,
  messageTime, shortHash,
} from '../../src/lib/format';

describe('initialsOf', () => {
  test('two names -> first+last initials', () => {
    expect(initialsOf('Priya Sharma')).toBe('PS');
  });
  test('single name -> first two letters', () => {
    expect(initialsOf('Marcus')).toBe('MA');
  });
  test('empty -> ?', () => {
    expect(initialsOf('   ')).toBe('?');
  });
  test('three names -> first and LAST initial', () => {
    expect(initialsOf('Ana de Souza')).toBe('AS');
  });
});

describe('firstNameOf', () => {
  test('takes the first word', () => {
    expect(firstNameOf('Priya Sharma')).toBe('Priya');
  });
});

describe('avatarColors', () => {
  test('deterministic for the same id', () => {
    expect(avatarColors('user-1')).toEqual(avatarColors('user-1'));
  });
  test('returns a palette entry with bg and fg', () => {
    const c = avatarColors('anything');
    expect(c.bg).toMatch(/^#/);
    expect(c.fg).toMatch(/^#/);
  });
});

describe('time formatting (frozen clock)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-20T15:00:00'));
  });
  afterEach(() => vi.useRealTimers());

  test('messageTime today -> bare clock', () => {
    expect(messageTime('2026-07-20T09:15:00')).not.toMatch(/^YD|^[A-Z]{3} /);
  });
  test('messageTime yesterday -> YD prefix', () => {
    expect(messageTime('2026-07-19T18:12:00')).toMatch(/^YD /);
  });
  test('messageTime older -> UPPER month-day', () => {
    expect(messageTime('2026-07-11T10:00:00')).toMatch(/^[A-Z]{3} \d+$/);
  });
  test('daysUntil clamps past dates to 0', () => {
    expect(daysUntil('2026-07-01T00:00:00')).toBe(0);
  });
  test('daysUntil counts up', () => {
    expect(daysUntil('2026-07-23T15:00:00')).toBe(3);
  });
  test('countdown formats days+hours', () => {
    expect(countdown('2026-07-27T12:00:00')).toBe('6D 21H');
  });
  test('countdown past -> EXPIRED', () => {
    expect(countdown('2026-07-19T00:00:00')).toBe('EXPIRED');
  });
});

describe('shortHash', () => {
  test('long hash -> 4…4 with prefix stripped', () => {
    expect(shortHash('sha256:abcdef1234567890')).toBe('abcd…7890');
  });
  test('short hash unchanged', () => {
    expect(shortHash('abc123')).toBe('abc123');
  });
});
