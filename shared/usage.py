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


def finalize_usage(team_id: str, run_id: str, actual_tokens: int) -> None:
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
            " returning coalesce(tokens_reserved, 0),"
            "           created_at >= date_trunc('hour', now())",
            (run_id, team_id),
        ).fetchone()
        if claimed is None:
            return
        reserved, same_hour = claimed
        if not same_hour:
            return
        conn.execute(
            "update public.usage_buckets"
            "   set tokens = greatest(tokens - %s + %s, 0)"
            " where team_id=%s and bucket=date_trunc('hour', now())",
            (reserved, max(actual_tokens, 0), team_id),
        )


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
