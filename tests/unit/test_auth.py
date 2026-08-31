"""Token verification.

The property these tests exist for is narrow and load-bearing: **a token establishes who
you are and cannot establish what you may read.** Everything else here is the standard
JWT footguns — algorithm confusion, missing audience, unchecked expiry — which are worth
pinning precisely because they are the ones people get wrong by omission rather than by
reasoning.
"""

from __future__ import annotations

import time

import jwt
import pytest

from gatekeeper.config import get_settings
from gatekeeper.core import auth


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GK_AUTH_MODE", "dev")
    # >= 32 bytes: PyJWT warns below that for SHA256, and a test suite that emits
    # security warnings trains people to ignore security warnings.
    monkeypatch.setenv("GK_AUTH_DEV_SECRET", "test-secret-not-the-shipped-one-32b")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- the happy path --------------------------------------------------------


def test_a_minted_token_verifies() -> None:
    identity = auth.verify(auth.issue_dev_token("raj"))
    assert identity.subject == "raj"
    assert identity.can_impersonate is False


def test_impersonation_is_a_claim_not_a_default() -> None:
    assert auth.verify(auth.issue_dev_token("raj")).can_impersonate is False
    assert auth.verify(auth.issue_dev_token("raj", can_impersonate=True)).can_impersonate


# --- forgery ---------------------------------------------------------------


def test_a_token_signed_with_another_secret_is_refused() -> None:
    forged = jwt.encode(
        {
            "sub": "mira",
            "iss": auth.DEV_ISSUER,
            "aud": auth.DEV_AUDIENCE,
            "exp": int(time.time()) + 600,
        },
        "attacker-secret-also-long-enough-32bytes",
        algorithm="HS256",
    )
    with pytest.raises(auth.AuthError):
        auth.verify(forged)


def test_an_expired_token_is_refused() -> None:
    with pytest.raises(auth.AuthError):
        auth.verify(auth.issue_dev_token("raj", ttl_seconds=-1))


def test_an_unsigned_token_is_refused() -> None:
    """`alg: none` is the oldest JWT attack there is: a library that honours the header's
    algorithm choice will accept a token with no signature at all."""
    unsigned = jwt.encode(
        {
            "sub": "mira",
            "iss": auth.DEV_ISSUER,
            "aud": auth.DEV_AUDIENCE,
            "exp": int(time.time()) + 600,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(auth.AuthError):
        auth.verify(unsigned)


@pytest.mark.parametrize(
    ("field", "value"),
    [("iss", "some-other-issuer"), ("aud", "some-other-service")],
)
def test_issuer_and_audience_are_both_checked(field: str, value: str) -> None:
    """Omitting the audience check is how a token minted for one service gets replayed
    against another that happens to share a signing key."""
    claims = {
        "sub": "raj",
        "iss": auth.DEV_ISSUER,
        "aud": auth.DEV_AUDIENCE,
        "exp": int(time.time()) + 600,
    }
    claims[field] = value
    token = jwt.encode(claims, get_settings().auth_dev_secret, algorithm="HS256")
    with pytest.raises(auth.AuthError):
        auth.verify(token)


def test_a_token_without_a_subject_is_refused() -> None:
    token = jwt.encode(
        {"iss": auth.DEV_ISSUER, "aud": auth.DEV_AUDIENCE, "exp": int(time.time()) + 600},
        get_settings().auth_dev_secret,
        algorithm="HS256",
    )
    with pytest.raises(auth.AuthError):
        auth.verify(token)


# --- the property that matters ---------------------------------------------


def test_a_token_cannot_assert_entitlements() -> None:
    """The decision this module exists to encode.

    A token carrying `groups: [executives]` and `clearance: 3` establishes nothing beyond
    its subject. Entitlements are read from `principals` by `resolve()`, so an IdP
    compromise costs you impersonation of one user — not the ability to invent an
    entitlement that never existed in the database.
    """
    token = jwt.encode(
        {
            "sub": "raj",
            "iss": auth.DEV_ISSUER,
            "aud": auth.DEV_AUDIENCE,
            "exp": int(time.time()) + 600,
            "groups": ["executives", "comp-committee"],
            "clearance": 3,
            "need_to_know": ["compensation"],
        },
        get_settings().auth_dev_secret,
        algorithm="HS256",
    )
    identity = auth.verify(token)
    # Identity has room for a subject, an issuer, an expiry and one privilege flag.
    # There is nowhere for a group to go, which is the point.
    assert identity.subject == "raj"
    assert not hasattr(identity, "groups")
    assert not hasattr(identity, "clearance")


# --- the dev-secret guard --------------------------------------------------


def test_the_shipped_development_secret_is_refused_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hardcoded signing secret that reaches production is a forge-any-identity bug, so
    using the one in the repo has to be a deliberate act."""
    monkeypatch.setenv("GK_AUTH_DEV_SECRET", auth.INSECURE_DEV_SECRET)
    # Set to "0" rather than deleted: Settings also reads `.env`, so unsetting the
    # process env is not enough to unset the value. That is worth knowing about the guard
    # itself — a stray line in a deployed .env defeats it exactly this way.
    monkeypatch.setenv("GK_ALLOW_INSECURE_DEV_AUTH", "0")
    get_settings.cache_clear()
    with pytest.raises(auth.AuthError, match="development signing secret"):
        auth.issue_dev_token("raj")


def test_the_shipped_secret_is_allowed_when_explicitly_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GK_AUTH_DEV_SECRET", auth.INSECURE_DEV_SECRET)
    monkeypatch.setenv("GK_ALLOW_INSECURE_DEV_AUTH", "1")
    get_settings.cache_clear()
    assert auth.verify(auth.issue_dev_token("raj")).subject == "raj"


def test_dev_tokens_cannot_be_minted_in_oidc_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GK_AUTH_MODE", "oidc")
    get_settings.cache_clear()
    with pytest.raises(auth.AuthError, match="dev auth mode"):
        auth.issue_dev_token("raj")
