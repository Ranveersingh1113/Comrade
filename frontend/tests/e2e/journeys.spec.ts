/**
 * Critical journeys in a real browser against the real stack.
 * Seeded state comes from global-setup (.state.json).
 */
import { expect, test, type Browser, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

interface SeedState {
  teamId: string;
  leader: { id: string; email: string };
  member: { id: string; email: string };
  password: string;
}

const state: SeedState = JSON.parse(
  readFileSync(resolve(__dirname, '.state.json'), 'utf-8'),
);

async function signIn(page: Page, email: string): Promise<void> {
  await page.goto('/login');
  await page.getByRole('button', { name: 'USE A PASSWORD INSTEAD' }).click();
  await page.getByPlaceholder('you@university.edu').fill(email);
  await page.getByLabel('password').fill(state.password);
  await page.getByRole('button', { name: 'SIGN IN' }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15000 });
}

async function memberPage(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await signIn(page, state.member.email);
  await page.goto(`/t/${state.teamId}/room`);
  return page;
}

test.describe.serial('Comrade journeys', () => {
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    page = await (await browser.newContext()).newPage();
    await signIn(page, state.leader.email);
  });

  test('1. team gate → group room renders the roster', async () => {
    await page.goto('/teams');
    await page.getByRole('button', { name: 'ENTER →' }).first().click();
    await expect(page).toHaveURL(new RegExp(`/t/${state.teamId}/room`));
    // both members appear in the sidebar roster (emails' local parts)
    const leaderName = state.leader.email.split('@')[0];
    await expect(page.getByText(leaderName).first()).toBeVisible();
  });

  test('2. sending a group message shows it without a reload', async () => {
    await page.goto(`/t/${state.teamId}/room`);
    await page.getByPlaceholder(/Message the team/).fill('kickoff notes are up');
    await page.keyboard.press('Enter');
    await expect(page.getByText('kickoff notes are up')).toBeVisible({ timeout: 10000 });
  });

  test('3. only the assignee confirms a task (two contexts)', async ({ browser }) => {
    // Leader (not the assignee) sees waiting copy, no button.
    await page.goto(`/t/${state.teamId}/tasks`);
    await expect(page.getByText(/waiting on .* to confirm/)).toBeVisible();
    await expect(page.getByRole('button', { name: /CONFIRM — IT'S YOURS/ })).toHaveCount(0);

    // The member confirms in their own browser context.
    const member = await memberPage(browser);
    await member.goto(`/t/${state.teamId}/tasks`);
    await member.getByRole('button', { name: /CONFIRM — IT'S YOURS/ }).click();
    await expect(member.getByText('CONFIRMED', { exact: true })).toBeVisible({ timeout: 10000 });
    await member.context().close();
  });

  test('4. wiki: restoring a prior fact queues a revert', async () => {
    await page.goto(`/t/${state.teamId}/wiki`);
    await expect(page.getByText('Final demo is on Friday')).toBeVisible();
    await page.getByRole('button', { name: /1 REVISION/ }).click();
    await page.getByRole('button', { name: /restore/ }).click();
    await expect(page.getByText('REVERT QUEUED')).toBeVisible({ timeout: 10000 });
  });

  test('5. consent inbox: approving the T2 item executes it', async () => {
    await page.goto(`/t/${state.teamId}/inbox`);
    const t2Card = page
      .getByTestId('consent-card')
      .filter({ hasText: 'standup moved to 3pm' });
    await expect(t2Card.getByText('task_create').first()).toBeVisible();
    await t2Card.getByRole('button', { name: 'APPROVE' }).click();
    // executed items leave Pending and reappear as a compact History row
    await expect(page.getByText('EXECUTED', { exact: false }).first()).toBeVisible({
      timeout: 15000,
    });

    await page.goto(`/t/${state.teamId}/tasks`);
    await expect(page.getByText('Reminder: standup moved to 3pm.')).toBeVisible({
      timeout: 10000,
    });
  });

  test('7. suppressing an AI observation tombstones it', async () => {
    await page.goto(`/t/${state.teamId}/room`);
    const obs = page.getByText(/the API doc has not moved in a week/);
    await expect(obs).toBeVisible();
    await obs.hover();
    await page.getByRole('button', { name: /DON'T DO THIS AGAIN/ }).click();
    await expect(page.getByText(/removed a message — removed for everyone/)).toBeVisible({
      timeout: 10000,
    });
    await expect(page.getByText(/the API doc has not moved in a week/)).toHaveCount(0);
  });

  test('8. private thread agent turn (needs GEMINI_API_KEY)', async () => {
    test.skip(!process.env.GEMINI_API_KEY, 'live agent turn needs a Gemini key');
    await page.goto(`/t/${state.teamId}/thread`);
    await page.getByPlaceholder(/this stays private/).fill('What tasks are open right now?');
    await page.getByRole('button', { name: 'SEND' }).click();
    // the user's message and a non-empty AI reply both arrive via the server
    await expect(page.getByText('What tasks are open right now?')).toBeVisible({
      timeout: 30000,
    });
    // Assert a reply ARRIVED, not that it used a particular word. The prior
    // version looked for a second element matching /task/i, which depends on
    // how the model phrases itself — "Nothing is open right now" is a correct
    // answer containing no "task" — and it flaked three times across this
    // session's runs. What the test is actually for is that a live turn
    // round-trips through the server and renders.
    await expect(page.locator('[data-sender="ai"]').last()).toBeVisible({
      timeout: 30000,
    });
    await expect(page.locator('[data-sender="ai"]').last()).not.toBeEmpty();
  });
});

test('9. no screen scrolls sideways on a phone', async ({ browser }) => {
  // The objective half of the responsive work. The product shipped with zero
  // breakpoints: a 250px sidebar plus a 296px rail on a 375px viewport pushed
  // every screen into horizontal scroll. Eyes catch that once; this catches it
  // every run.
  const ctx = await browser.newContext({ viewport: { width: 375, height: 812 } });
  const phone = await ctx.newPage();
  await signIn(phone, state.leader.email);

  for (const path of ['room', 'tasks', 'wiki', 'docs', 'inbox', 'thread']) {
    await phone.goto(`/t/${state.teamId}/${path}`);
    await phone.waitForLoadState('networkidle');
    const overflows = await phone.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(overflows, `/${path} scrolls sideways at 375px`).toBe(false);
  }
  await ctx.close();
});
