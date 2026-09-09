"""A team's hourly budget, spent atomically.

🔴 The check this replaces read `count(*)` and `sum(tokens)` over agent_runs
and compared them to the cap. Two requests reading the same snapshot both
passed it, and nothing downstream stopped the second — the check and the spend
were different transactions, so the cap was advice. On a deployment where the
model key is the operator's, that is somebody else's money.

Everything here is one statement against one row. The cap lives in that
statement's WHERE clause, so the decision and the spend cannot be separated:
concurrent turns take the same row lock and serialise behind it.

A turn RESERVES an estimate before it runs and reconciles the truth when it
finishes. Reserving nothing up front would let a hundred simultaneous turns
each pass a cap none of them had spent against yet.
"""
import logging

from shared.config import settings
from shared.db import Role, team_session

logger = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """The team is out of turns or tokens for this hour."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _caps() -> tuple[int, int]:
    return settings.agent_turns_per_hour, settings.agent_tokens_per_hour


def reserve_turn(team_id: str, estimate: int | None = None) -> int:
    """Take one turn and `estimate` tokens from this hour, or refuse.

    Returns what was reserved, so the caller can hand the same number to
    `finalize_usage` and to `release_turn`. Raises BudgetExceeded when either
    cap would be passed — which cap is named, because "you have used your
    turns" sent to a team that is out of TOKENS points at the wrong thing.
    """
    turn_cap, token_cap = _caps()
    reservation = settings.agent_tokens_estimate if estimate is None else estimate
    if turn_cap <= 0 and token_cap <= 0:
        return reservation

    # A cap of 0-or-less means "no limit", and a WHERE clause cannot express
    # that with the same comparison. Substituting a number larger than any
    # real bucket keeps ONE statement rather than four variants of it.
    unlimited = 1 << 40
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, date_trunc('hour', now()), 1, %s)"
            " on conflict (team_id, bucket) do update"
            "   set turns = usage_buckets.turns + 1,"
            "       tokens = usage_buckets.tokens + excluded.tokens"
            # The cap, in the statement that spends against it.
            "   where usage_buckets.turns + 1 <= %s"
            "     and usage_buckets.tokens + excluded.tokens <= %s"
            " returning turns, tokens",
            (
                team_id, reservation,
                turn_cap if turn_cap > 0 else unlimited,
                token_cap if token_cap > 0 else unlimited,
            ),
        ).fetchone()
        if row is not None:
            return reservation
        # No row came back, so the conflicting row failed the WHERE. Read it
        # to say WHICH cap bound — a separate statement is fine here because
        # nothing is being decided any more, only explained.
        spent = conn.execute(
            "select turns, tokens from public.usage_buckets"
            " where team_id=%s and bucket=date_trunc('hour', now())",
            (team_id,),
        ).fetchone()

    turns, tokens = spent if spent else (turn_cap, token_cap)
    if turn_cap > 0 and turns + 1 > turn_cap:
        raise BudgetExceeded(
            f"This team has used its {turn_cap} agent turns for the hour."
            " Comrade will be available again shortly."
        )
    raise BudgetExceeded(
        f"This team has used its {token_cap:,} agent tokens for the hour"
        f" ({tokens:,} so far). Comrade will be available again shortly."
    )


def release_turn(team_id: str, reservation: int) -> None:
    """Give back a reservation whose turn never started.

    A turn that failed between reserving and persisting has spent nothing, and
    leaving its estimate on the bucket would refuse a team work they never
    had. Never takes the bucket below zero — an accounting bug must not become
    a way to earn budget.
    """
    turn_cap, token_cap = _caps()
    if turn_cap <= 0 and token_cap <= 0:
        return
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.usage_buckets"
            "   set turns = greatest(turns - 1, 0),"
            "       tokens = greatest(tokens - %s, 0)"
            " where team_id=%s and bucket=date_trunc('hour', now())",
            (reservation, team_id),
        )


def finalize_usage(
    team_id: str, run_id: str, actual_tokens: int,
    worker_id: str | None = None,
) -> None:
    """Replace this run's estimate with what it really cost. Exactly once.

    Idempotent by the `usage_finalized_at is null` guard, not by hoping the
    caller only calls once: the run finishes on the success path, the failure
    path and the cancellation path, and a double reconciliation would silently
    hand a team free budget.

    Adjusts the bucket the run was RESERVED in only if that is still the
    current one. A run that crosses an hour boundary is left alone rather than
    charged to an hour it did not start in — the estimate it took is small and
    the alternative is bookkeeping nobody will read.
    """
    turn_cap, token_cap = _caps()
    if turn_cap <= 0 and token_cap <= 0:
        return
    with team_session(Role.AGENT, team_id) as conn:
        claimed = conn.execute(
            "update public.agent_runs set usage_finalized_at = now()"
            " where id=%s and team_id=%s and usage_finalized_at is null"
            # 🔴 (fix.md F43) FENCED ON A TERMINAL STATUS. A worker that has
            # lost its lease still walks its exit path, and settling there
            # marked the run finalized while a REPLACEMENT was still running
            # it — so the real owner's settlement was skipped and the estimate
            # stayed on the bucket. A run that is still live is not one to
            # settle, whoever is asking.
            "   and status not in ('queued','running','waiting_for_permission',"
            "                      'waiting_for_user')"
            # 🔴 (fix.md F43, reopened.) AND IT HAS TO BE OURS. The terminal
            # check above blocks a stale worker only while the replacement is
            # still running; the moment the replacement FINISHES, the stale
            # worker's exit path finds a terminal run and settles its own
            # local totals. Measured shape: replacement commits 1,200 tokens,
            # stale worker's fenced `finish_run` is refused, `_finish` catches
            # that and settles 50 anyway, and the replacement's settlement is
            # then skipped because usage_finalized_at is set. The bucket keeps
            # 50 for a turn that cost 1,200 and releases budget really spent.
            #
            # 🔴 (fix.md F43, third pass.) `worker_id is null` used to be an
            # allowance for ANY caller, on the reasoning that cancellation
            # nulls it and the executing worker is the only thing that knows
            # what the turn cost. The first half is true and the conclusion is
            # not: a worker that LOST its lease is also holding totals, and it
            # reaches this line too. Cancel a replacement mid-run and the stale
            # worker settles 50 against the replacement's 1,200.
            #
            # `usage_owner` carries the identity forward when execution is
            # revoked (see 20260909130000), so the three cases stay distinct:
            #   * still owned  — the holder settles;
            #   * revoked      — the worker that WAS executing settles, and
            #                    nobody else;
            #   * never owned  — a queued cancellation, settled by the server,
            #                    which has no identity of its own to offer.
            "   and case when worker_id is not null"
            "             then %s::text is not distinct from worker_id"
            "             else %s::text is not distinct from usage_owner end"
            " returning coalesce(tokens_reserved, 0),"
            #  The hour this run's claims were charged to (fix.md F44), not
            #  whatever hour it happens to be when it finishes.
            "           coalesce(usage_bucket, date_trunc('hour', created_at))",
            (run_id, team_id, worker_id, worker_id),
        ).fetchone()
        if claimed is None:
            return
        reserved, bucket = claimed
        _release(conn, team_id, reserved, bucket, actual_tokens)


def _release(conn, team_id: str, reserved: int, bucket, actual_tokens: int) -> None:
    """Swap this run's claim on the hour for what it really cost."""
    conn.execute(
        "update public.usage_buckets"
        "   set tokens = greatest(tokens - %s + %s, 0)"
        " where team_id=%s and bucket=%s",
        (reserved, max(actual_tokens, 0), team_id, bucket),
    )


def settle_from_checkpoint(team_id: str, run_id: str) -> None:
    """Settle a terminal run that nobody is executing, from its own record.

    🔴 (fix.md F49) A permission wait checkpoints what the run has spent, drops
    the lease and RETURNS from the runtime. There is no worker left. Cancelling
    from there set a terminal status and settled nothing — the cancellation
    route only settled runs it found `queued` — so a 6,000-token reservation
    stayed charged against the hour after a turn that really spent 1,200 was
    stopped, and kept the team out of admission it was entitled to.

    `usage_owner` preserves who WAS executing, which is what stops a stale
    worker settling someone else's run. It does not conjure a caller: identity
    is not a settlement. So this one takes no identity at all, because it takes
    no totals either — the amount is the checkpoint on the run's own row,
    written by `pause_for_permission` in the statement that parked it.

    Fenced on `worker_id is null` as well as a terminal status: together those
    mean nobody is executing this run. A run someone is holding is still theirs
    to settle.

    🔴 (fix.md F49, sixth review) And that pair is NOT enough. `queued` does not
    mean the previous execution finished: `recover_expired_agent_runs` requeues
    an expired run and clears `worker_id` while the old worker may still have an
    in-flight model response that has been paid for and that nothing has
    checkpointed. Cancelling in that interval settled the stale totals — often
    zero — and the real settlement then lost to the `usage_finalized_at` already
    written, discarding the tokens.

    So the row also has to say its record is COMPLETE:

      * `usage_owner is null` — nothing ever executed this run, so zero is not
        a stale number, it is the true one;
      * `usage_checkpoint_at is not null` — a worker wrote its totals and gave
        up execution in the same statement (`pause_for_permission`), and no
        claim has invalidated that since.

    A lease-recovered run has neither, so its reservation is HELD until the
    worker that spent the tokens accounts for them. Holding costs the team
    admission for the rest of that hour; releasing loses the record of real
    spend. For a cap, holding is the safe direction.
    """
    with team_session(Role.AGENT, team_id) as conn:
        settle_cancelled(conn, team_id, run_id)


def settle_cancelled(conn, team_id: str, run_id: str) -> None:
    """`settle_from_checkpoint` on a caller's own transaction.

    🔴 (fix.md F49, sixth review) Cancellation used to commit before settlement
    opened its transaction, so a failed settlement left the run durably
    cancelled and still reserved — and the retry made it worse, because
    `cancel_run` then found nothing to cancel and the route skipped settlement
    entirely. No worker remains for a parked run, so nothing else was coming.

    Taking the caller's connection lets the terminal transition and the
    accounting for it commit or roll back together, which is what "settled
    exactly once" requires of a step that can fail.
    """
    turn_cap, token_cap = _caps()
    if turn_cap <= 0 and token_cap <= 0:
        return
    claimed = conn.execute(
        "update public.agent_runs set usage_finalized_at = now()"
        " where id=%s and team_id=%s and usage_finalized_at is null"
        "   and status not in ('queued','running','waiting_for_permission',"
        "                      'waiting_for_user')"
        "   and worker_id is null"
        "   and (usage_owner is null or usage_checkpoint_at is not null)"
        " returning coalesce(tokens_reserved, 0),"
        "           coalesce(usage_bucket, date_trunc('hour', created_at)),"
        #  What the run itself recorded, not what any caller believes.
        "           coalesce(input_tokens, 0) + coalesce(output_tokens, 0)",
        (run_id, team_id),
    ).fetchone()
    if claimed is None:
        return
    reserved, bucket, spent = claimed
    _release(conn, team_id, reserved, bucket, spent)


def claimed_tokens(team_id: str, run_id: str) -> int | None:
    """How much of the hour this run has taken so far. None when uncapped.

    This is `tokens_reserved`, which now means "claimed", not "estimated at
    admission" — the admission estimate is simply the first claim.
    """
    _, token_cap = _caps()
    if token_cap <= 0:
        return None
    with team_session(Role.AGENT, team_id) as conn:
        row = conn.execute(
            "select coalesce(tokens_reserved, 0) from public.agent_runs"
            " where id=%s and team_id=%s",
            (run_id, team_id),
        ).fetchone()
    return row[0] if row else 0


def claim_budget(team_id: str, run_id: str, chunk: int | None = None) -> bool:
    """Take another slice of the hour for this run, or refuse.

    🔴 (fix.md F30) The in-run brake used to ASK how much room the team had
    and let every active run treat the answer as its own. A run's real spend
    only reaches the bucket when it finalizes, so with a 500,000 cap and two
    turns holding 6,000 reservations, the bucket read 12,000 and each run was
    told it could spend 494,000. Both could, and the team spent 980,000.

    The brake was never wrong about ONE turn. It was wrong that a shared
    budget can be divided by reading it. Headroom has to be CLAIMED, in the
    same atomic conditional update admission already uses: the cap lives in the
    WHERE clause, so two runs asking at once serialise on the row lock and only
    one of them can take the last slice.

    Both statements are one transaction. A claim that charged the team and
    failed to record itself against the run would be budget spent by nobody.
    """
    _, token_cap = _caps()
    if token_cap <= 0:
        return True
    chunk = settings.agent_tokens_estimate if chunk is None else chunk
    with team_session(Role.AGENT, team_id) as conn:
        # 🔴 (fix.md F44) THE RUN'S OWN BUCKET, not "whatever hour it is now".
        #
        # This used to update `bucket = date_trunc('hour', now())` and treat a
        # missing row as "no room". Across an hour boundary that row often does
        # not exist yet — buckets are created at admission — so a run admitted
        # at 10:59 was refused at 11:00 with a full unused hour in front of it,
        # unless some unrelated request happened to be admitted first and
        # create the row. Whether a member's turn could continue depended on
        # other people's traffic.
        #
        # A run's claims belong to the hour it was ADMITTED in, for its whole
        # life. That hour's cap is the one bounding it, and finalization
        # reconciles the same one, so a boundary can neither refuse a turn nor
        # move a refund into an hour it did not spend in.
        bucket = conn.execute(
            "select coalesce(usage_bucket, date_trunc('hour', created_at))"
            " from public.agent_runs where id=%s and team_id=%s",
            (run_id, team_id),
        ).fetchone()
        if bucket is None:
            return False
        bucket = bucket[0]
        # Recorded on first use, so later claims and the settlement all agree
        # about which hour this run is spending.
        conn.execute(
            "update public.agent_runs set usage_bucket=%s"
            " where id=%s and team_id=%s and usage_bucket is null",
            (bucket, run_id, team_id),
        )
        row = conn.execute(
            # INSERT ... ON CONFLICT, so a bucket that does not exist yet is
            # created rather than read as an empty allowance. The cap lives in
            # the same statement that spends against it, on both arms.
            "insert into public.usage_buckets (team_id, bucket, turns, tokens)"
            " values (%s, %s, 0, %s)"
            " on conflict (team_id, bucket) do update"
            "   set tokens = public.usage_buckets.tokens + %s"
            "   where public.usage_buckets.tokens + %s <= %s"
            " returning tokens",
            (team_id, bucket, chunk, chunk, chunk, token_cap),
        ).fetchone()
        if row is None:
            # The hour has no room left.
            return False
        if row[0] > token_cap:
            # The INSERT arm has no `where`, so a first claim larger than the
            # whole cap would otherwise create the bucket over it. Undo and
            # refuse, in the same transaction that made it.
            conn.execute(
                "update public.usage_buckets set tokens = tokens - %s"
                " where team_id=%s and bucket=%s", (chunk, team_id, bucket),
            )
            return False
        conn.execute(
            "update public.agent_runs"
            "   set tokens_reserved = coalesce(tokens_reserved, 0) + %s"
            " where id=%s and team_id=%s",
            (chunk, run_id, team_id),
        )
    return True


def record_reservation(team_id: str, run_id: str, reservation: int) -> None:
    """Remember what this run took, so finalizing can give back the difference.

    Written after the run exists rather than as part of creating it: the
    reservation is against the TEAM's bucket and is already spent by then. If
    this write is lost the run simply reconciles nothing, which overcharges by
    the estimate rather than undercharging — the safe direction for a cap.
    """
    with team_session(Role.AGENT, team_id) as conn:
        conn.execute(
            "update public.agent_runs set tokens_reserved=%s"
            " where id=%s and team_id=%s",
            (reservation, run_id, team_id),
        )
