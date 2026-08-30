"""The codebase's first unauthenticated door.

findings §16.6: the webhook endpoint is the first route with no JWT behind it.
Its only credential is an HMAC-SHA256 signature over the raw body, so these are
the tests that stand between a stranger and the job queue.

§23.4-2 notes the Stripe webhook will join it — "worth one shared helper rather
than two implementations" — so this is deliberately generic over the secret and
the body, with GitHub's header format as the only GitHub-specific part.
"""
import hashlib
import hmac

import pytest

from server.webhooks import verify_signature

SECRET = "s3cr3t-webhook-key"
BODY = b'{"action":"opened","number":7}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


def test_a_correctly_signed_body_verifies():
    assert verify_signature(SECRET, BODY, _sign(SECRET, BODY)) is True


def test_one_altered_byte_does_not_verify():
    assert verify_signature(SECRET, BODY + b" ", _sign(SECRET, BODY)) is False


def test_a_signature_for_another_secret_does_not_verify():
    assert verify_signature(SECRET, BODY, _sign("not-the-secret", BODY)) is False


@pytest.mark.parametrize(
    "header",
    [None, "", "garbage", "sha256=", "sha256=zzzz", "sha1=" + "a" * 40,
     hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()],
)
def test_a_missing_or_malformed_header_does_not_verify(header):
    """A bad header is a failed auth, not a server error — never raises."""
    assert verify_signature(SECRET, BODY, header) is False


def test_an_empty_secret_refuses_everything():
    """The trap: hmac will happily sign with b"", so an unconfigured deployment
    would otherwise accept anything a caller signed with the empty string.

    Phase 1 fixed the same class of bug in the opposite direction, where
    user_session fell back to a BYPASSRLS connection when its URL was unset.
    Missing configuration must fail closed, not open.
    """
    assert verify_signature("", BODY, _sign("", BODY)) is False
    assert verify_signature("", BODY, _sign(SECRET, BODY)) is False
    assert verify_signature(None, BODY, _sign("", BODY)) is False


def test_an_empty_body_still_verifies_when_correctly_signed():
    """GitHub sends pings with small bodies; an empty one is not special."""
    assert verify_signature(SECRET, b"", _sign(SECRET, b"")) is True


def test_the_comparison_is_constant_time():
    """A `==` here is a timing oracle: an attacker with the endpoint and enough
    patience recovers a valid signature byte by byte.

    Pinned against the compiled code object, not the source text. The first
    version of this test grepped `inspect.getsource` for "compare_digest" and
    passed happily after the call was replaced with `==` — because the string
    still appeared in the docstring. Mutation-testing it is what caught that;
    a security test that cannot fail is worse than no test, because it reads
    as coverage.
    """
    assert "compare_digest" in verify_signature.__code__.co_names, (
        "verify_signature must use hmac.compare_digest, not =="
    )
