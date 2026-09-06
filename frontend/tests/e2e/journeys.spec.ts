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
  generalThreadId: string;
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
  await page.goto(`/t/${state.teamId}/threads`);
  return page;
}

test.describe.serial('Comrade journeys', () => {
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    page = await (await browser.newContext()).newPage();
    await signIn(page, state.leader.email);
  });

  test('1. team gate → threads renders the roster', async () => {
    await page.goto('/teams');
    await page.getByRole('button', { name: 'ENTER →' }).first().click();
    await expect(page).toHaveURL(new RegExp(`/t/${state.teamId}/threads`));
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

  test('5. inline consent: approving the T2 item executes it', async () => {
    await page.goto(`/t/${state.teamId}/threads/${state.generalThreadId}`);
    const t2Card = page
      .getByTestId('consent-card')
      .filter({ hasText: 'standup moved to 3pm' });
    await expect(t2Card.getByText(/New task/i).first()).toBeVisible();
    await t2Card.getByRole('button', { name: 'Approve once' }).click();
    await expect(page.getByText('COMPLETED', { exact: false }).first()).toBeVisible({
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

  test('8. thread agent turn (needs GEMINI_API_KEY)', async () => {
    test.skip(!process.env.GEMINI_API_KEY, 'live agent turn needs a Gemini key');
    await page.goto(`/t/${state.teamId}/threads`);
    await page.getByText('General', { exact: true }).click();
    await page.getByRole('button', { name: 'Comrade mode' }).click();
    await page.getByPlaceholder('Ask Comrade…').fill('What tasks are open right now?');
    await page.getByRole('button', { name: 'SEND' }).click();
    // the user's message and a non-empty AI reply both arrive via the server
    await expect(page.getByText('What tasks are open right now?')).toBeVisible({
      timeout: 30000,
    });
    // Assert the member GETS AN ANSWER OF SOME KIND, not that the model spoke.
    //
    // Two earlier versions of this assertion were too strong and I chased both.
    // It first looked for a second element matching /task/i, which depends on
    // how the model phrases itself. It then required an AI message, which
    // depends on the model producing one at all — and measurement says it
    // often does not: six of fourteen live turns came back with no events
    // whatsoever, which is why agent/runtime.py now emits an `empty` frame and
    // marks the run failed instead of reporting a silent success.
    //
    // So a bare AI-message assertion is testing the model, not the product.
    // What the product guarantees — and what this now checks — is that a live
    // turn round-trips through the server and the member is told something
    // either way. A blank screen fails both branches, which is the regression
    // worth catching.
    //
    // The explanation is matched by its SLOT, not its wording. There are two
    // of them — "the model came back empty" and "attempted <tools> and then
    // stopped" — and which one the member gets depends on whether the model
    // called a tool before going quiet. Matching one sentence made this assert
    // on model behaviour again, the exact thing the paragraph above rejects.
    const answered = page.locator('[data-sender="ai"]').last();
    const explained = page.locator('[data-agent-note]');
    await expect(answered.or(explained)).toBeVisible({ timeout: 30000 });
    if (await answered.count()) {
      await expect(answered).not.toBeEmpty();
    }
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

  for (const path of ['threads', 'tasks', 'wiki', 'docs', 'thread']) {
    await phone.goto(`/t/${state.teamId}/${path}`);
    await phone.waitForLoadState('networkidle');
    const overflows = await phone.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(overflows, `/${path} scrolls sideways at 375px`).toBe(false);
  }
  await ctx.close();
});
