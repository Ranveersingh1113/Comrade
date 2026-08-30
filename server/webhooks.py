"""Signature verification for inbound webhooks.

findings §16.6: the GitHub webhook is the first route in this codebase with no
JWT behind it. GitHub will not present one — its only credential is an
HMAC-SHA256 signature over the raw request body. §23.4-2 records that the
Stripe webhook will join it, and that two unauthenticated routes are "a
pattern, not two one-offs": both need signature verification and replay
protection, and both should share one implementation rather than growing two.

Replay protection lives at the queue, not here: the delivery id becomes
`jobs.dedupe_key`, and the partial unique index already on that table makes a
retried delivery a no-op instead of a second job.
"""
import hashlib
import hmac

_PREFIX = "sha256="


def verify_signature(secret: str | None, raw_body: bytes, header: str | None) -> bool:
    """True only if `header` is a valid HMAC-SHA256 of `raw_body` under `secret`.

    Three properties this function exists to guarantee:

    1. **It fails closed on missing configuration.** An empty or unset secret
       refuses everything, including a body signed with the empty string —
       `hmac` will happily sign with `b""`, so a deployment that forgot the
       variable would otherwise accept whatever a caller signed with it. Phase
       1 fixed this same class of bug in the opposite direction, where
       `user_session` silently fell back to a connection that bypassed RLS.

    2. **The comparison is constant-time.** `hmac.compare_digest`, never `==`.
       A short-circuiting comparison here is a timing oracle: an attacker with
       the endpoint and patience recovers a valid signature byte by byte.

    3. **A malformed header is a failed auth, not a crash.** Anything
       unparseable returns False rather than raising, so a stranger cannot turn
       a bad header into a 500 and learn something from it.

    The caller must pass the RAW bytes, before any JSON parsing. Parse-then-
    verify would hand the parser unsigned input, which is the whole attack.
    """
    if not secret or not header:
        return False
    if not header.startswith(_PREFIX):
        return False
    sent = header[len(_PREFIX):]
    if not sent:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)
