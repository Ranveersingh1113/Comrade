# Billing model — per-team subscription under flat authority — Design

Date: 2026-08-18 · Status: approved (owner, in-session)
First commercial design in the project. Nothing about payment existed in the
repo before this — no schema, no code, no prior doc.

## The problem

Comrade's differentiation is the flat/no-manager model: no admin, no approval
hierarchy, peer consent for the agent's actions. Payment appears to contradict
it. Somebody's card gets charged, and that somebody looks like an owner — which
is the exact role the product is positioned against.

The concrete questions: is the price divided among the team, and what happens
when a member leaves or a new one is invited?

## The separation that resolves it

Two different things were being merged.

- **Flat authority over the agent** — nobody approves another member's actions,
  nobody configures permissions, consent comes from peers. This is the product.
- **Flat authority over the commercial relationship** — nobody's card is on
  file. This was never claimed, and the moat does not rest on it.

The competitive read (project memory, 2026-07) identifies the incumbent
constraint as *"paid seats, a billing owner, and a persistent admin role,
**admin-configured permissions**."* The load-bearing part is the last one: an
admin who decides what other members may do. A person whose card is charged is
not that.

Precedent is universal — Discord has Nitro payers and flat chat; Figma has a
billing owner and flat file editing. The commercial layer being asymmetric does
not make the product asymmetric.

## The invariant

> **Paying buys continued service and nothing else.**
> No visibility, no vote, no ability to remove members, no override on consent,
> no access to private threads.

This is testable rather than aspirational: it is an RLS question, and RLS cannot
grant what no policy references. Checked against the product invariants:

| Invariant | Affected by a payer? |
|---|---|
| No rankings, no leaderboards | No |
| Quiet signals route privately | No |
| Private threads invisible to everyone | Only if paying bought visibility — it does not |
| AI acts as itself | No |
| Peer consent on agent actions | Only if paying bought a vote — it does not |
| Anyone can start, anyone can revert | No |

## Why per-team and not per-seat

Per-seat pricing won in B2B SaaS for five reasons. Three of them do not hold
here.

| Reason per-seat won | Holds for Comrade? |
|---|---|
| Value scales with headcount | **Partially.** True across 5→500. Comrade's segment spans roughly 3→10 — per-seat captures little across a 3× range |
| It is legible to finance and procurement | **Yes.** Genuine point in favour; the one real cost of not using it |
| Land and expand | **On a different axis.** Comrade's unit is the team (one room per team, by design). Growth inside an org is *more teams*, not more seats. Per-team expands across units instead of within them |
| Headcount proxies ability to pay | Mostly recoverable with size bands |
| Marginal cost per user ≈ zero | **No — and this is decisive.** Cost is tokens plus sandbox hours, which scale with *usage*. A 3-person team running the harness hard costs more than a 10-person team that barely touches it. Per-seat mis-prices in both directions |

A sixth reason is not usually listed and matters most here: **per-seat requires
someone to administer seats.** Whoever decides who gets one is an admin. This is
structural, not stylistic.

**Supporting evidence from the project's own research:** ClickUp Brain² is
recorded as *"$9–28/user/mo **+ credits**."* The "+ credits" is the tell —
per-seat kept for legibility and existing contracts, usage pricing bolted
underneath because seats do not cover AI cost. Cursor, Copilot and Devin have
converged on the same hybrid. The incumbents charging per seat are largely
pre-AI products that added AI to a model built when compute was free; they are
evidence that pricing is hard to change after enterprise contracts exist, not
that per-seat is right for an AI-native product.

**Accepted trade-offs.** Per-team is less legible to enterprise procurement, and
it invites gaming ("put 25 people in one team"). Bands address the second; the
first is a real cost, accepted, and is the argument that would justify revisiting
this if the segment moves toward enterprise sub-teams.

## The model

**One subscription per team.** Flat monthly price including a usage allowance,
with a **hard stop** at the allowance rather than metered overage. Size **bands**
rather than a hard member cap.

**Why a hard stop and not metered overage.** Metered overage bills a team more
than it agreed to, and in a flat team *nobody has the authority to approve that
charge* — there is no budget owner by construction. An unapproved bill is worse
here than a paused harness, and governance already treats money as the most
gated thing in the system. At the allowance the harness pauses; **any member**
can raise the band and it resumes immediately. Memory, room and coordination are
unaffected, since those are the free tier.

- Stripe customer per **team**, never per person. Comrade never stores card
  data; payment method capture and updates go through Stripe's hosted flows.
- **Any member** can attach payment. **Any member** can replace it. There is no
  billing-owner role, no transfer flow, and no permission to grant.
- Subscription state is team-visible (it is a fact about the team, not about a
  person, so it does not touch the no-blame invariant).

**Why bands rather than a hard cap:** a hard cap must be enforced when someone
tries to invite past it, which means somebody decides who is in — admin-shaped.
Bands move the price automatically instead. Within a band, invites and
departures change nothing; crossing one changes the price and every member can
see that it did.

**Price points are deliberately out of scope** (see below). This spec fixes the
*shape*; the numbers are a later commercial exercise.

## Free and paid

The boundary is drawn around **execution**, not volume, because that is where
the cost actually sits: a persistent per-team sandbox costs money whether or not
anyone uses it, while token spend is zero when idle.

| Tier | Contains |
|---|---|
| **Free** | Repo ingestion, the compiled wiki, tasks, the room, consent, nudges. Memory and coordination |
| **Paid** | The sandbox and the harness — code execution, the agent doing the work |

This maps onto the build order already recorded in the findings doc: §19 item 7
(GitHub ingestion → wiki) needs no sandbox and is the free tier; §19 item 11 (the
sandbox) is the paid one. The free tier is what gets built first regardless, and
the expensive capability sits behind the paywall by construction rather than by
policy.

It also makes failed payment degrade gracefully: the team drops to free and
keeps its memory, its room and its history. Nobody is locked out of their own
record.

## Lifecycle — the questions that prompted this

| Event | What happens |
|---|---|
| **Member invited** | Nothing, within the band. Price unchanged, no billing action, no approval |
| **Member leaves** | Nothing. Price unchanged |
| **Band crossed** | Price moves to the next band automatically; visible to all members |
| **The payer leaves** | The subscription belongs to the team and persists. Any remaining member can attach a new payment method — no transfer, no permission, no support ticket, because no authority was ever held |
| **Payment fails** | Grace period, then downgrade to free. Memory, room and history are retained. Any member can restore payment |
| **Team dissolves** | Out of scope for this spec |

The departure case is the point worth stating plainly: in a billing-owner model,
an owner leaving without transferring is the most common billing failure in team
SaaS and usually needs support intervention. Here it is a non-event. **Flat
authority is the reason the problem does not exist, not the cause of it.**

## Engineering consequences

- **New table `subscriptions`**, keyed by `team_id`: plan, band, status, Stripe
  customer/subscription ids, current period, allowance counters.
- **RLS:** members `select` their own team's row (`is_team_member(team_id)`).
  No member-level `update` that could confer authority; state changes arrive
  from Stripe webhooks, not from clients.
- **`comrade_agent` gets no grants on `subscriptions` at all.** Governance
  already rules that any spend is Tier 3 with a hard floor; the cleanest
  expression of that is that the agent has no billing capability to tier. The
  agent must not read the table either — plan state is not agent context.
- **A second unauthenticated route.** The Stripe webhook joins the GitHub
  webhook (findings §16.6) as an endpoint outside the JWT boundary. Same
  requirements: signature verification, and replay protection via the event id
  as `jobs.dedupe_key`.
- **Metering source.** Turn counts already exist in `agent_runs`; the hourly cap
  (`_check_turn_budget`, `server/app.py:106`) already counts them. Sandbox hours
  are new. The token/cost columns on `agent_runs` flagged in findings §3.1 stop
  being housekeeping and become the billing input.
- **No card data touches Comrade.** Stripe Checkout and the Customer Portal own
  capture and updates end to end.

## Out of scope

- **Price points, band sizes, and allowance amounts.** Owner decision: settle the
  shape now, the numbers when commercial pricing is taken up.
- Annual billing, discounts, trials, coupons, tax handling.
- Enterprise contracts, invoicing, procurement flows.
- Team dissolution and data export.
- A student/sponsored programme (the beachhead is served by the free tier here).

## Open questions

1. **Allowance unit.** Agent turns, sandbox hours, or both metered separately.
   Sandbox hours track cost more closely; turns are more legible to the buyer.
2. **Whether a team can opt into metered overage.** The default is a hard stop
   (above). A team that would rather be billed than paused could opt in — but
   that setting is itself a spending authorisation, so it would need to be a
   team-level decision under the consent protocol rather than one member's
   choice. Not designed here; flagged because the moment it exists, it is the
   first standing spending authority in the product.
3. **Band boundaries.** Where the member-count steps sit — needs pilot data on
   real team sizes.
4. **Whether the free tier includes the GitHub webhook ingest.** It is the
   differentiator and it is cheap, which argues yes; it is also the main
   ongoing cost of a non-paying team, which argues for a cap.

## Testing

1. A member can read their team's subscription row; a non-member cannot.
2. `comrade_agent` cannot read or write `subscriptions` (grant-level assertion,
   mirroring the existing memory sole-writer tests).
3. A Stripe webhook with an invalid signature is rejected; a replayed event id is
   a no-op.
4. Downgrade on failed payment leaves messages, memory and tasks intact and only
   removes sandbox access.
5. Adding and removing members within a band produces no billing change.
6. At the allowance the harness pauses and no charge is raised; a band increase
   by any member — including one who never touched billing before — resumes it.
