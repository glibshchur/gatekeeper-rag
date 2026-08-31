"""Authentication: proving *who* is asking, before authorization decides what they see.

Everything before this phase took the principal on trust. The console accepted a handle in
the request body, and the MCP server read one from an environment variable — appropriate
for a demo and a local subprocess respectively, and both said so, but neither is
authentication.

**The decision that matters here: the token asserts identity, the database owns
entitlements.** A verified token establishes *who* the caller is and nothing more; groups,
clearance, need-to-know and region are read from `principals` on every request. Taking
those from token claims is common and, in this system, would be self-defeating — the
entire project rests on the database deciding what a principal may read, and a system that
accepts `"groups": ["executives"]` from a signed blob has moved that decision to whoever
can mint blobs. An IdP compromise should cost you impersonation of one user, not the
ability to invent an entitlement that never existed.

Two verification modes, because a project whose promise is `make bootstrap` on a laptop
cannot require an identity provider:

* **dev** — HS256 with a local secret, issued by `gatekeeper token`. Real signature
  verification, real expiry, no external dependency. Refuses to run outside development.
* **oidc** — RS256 verified against a JWKS endpoint, with issuer and audience checked.
  Keycloak is wired up behind a compose profile.

The dev issuer is a genuine risk: a hardcoded secret that reaches production is a
forge-any-identity bug. `Settings` therefore fails closed — `auth_mode="dev"` with a
non-default secret is allowed, `auth_mode="dev"` with the shipped default secret raises
unless `GK_ALLOW_INSECURE_DEV_AUTH=1` is set explicitly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jwt
from jwt import PyJWKClient

from gatekeeper.config import get_settings

if TYPE_CHECKING:
    from gatekeeper.core.principal import Principal

# Anyone who knows this can mint a token, which is why using it is gated.
INSECURE_DEV_SECRET = "gatekeeper-dev-secret-do-not-use-in-production"

DEV_ISSUER = "gatekeeper-dev"
DEV_AUDIENCE = "gatekeeper"


class AuthError(Exception):
    """Rejected credential. The message is safe to return to the caller.

    Deliberately uniform in shape: "expired", "bad signature" and "unknown issuer" are all
    just refusals to the client. Distinguishing them in a response body tells an attacker
    which half of a forgery attempt worked.
    """


@dataclass(frozen=True)
class Identity:
    """What a verified token establishes. Notably *not* what the caller may read."""

    subject: str
    """Maps to `principals.external_id`. The only claim with authority here."""
    issuer: str
    expires_at: int
    can_impersonate: bool = False
    """A privilege, carried as a claim, and audited when used. The demo console needs
    side-by-side comparison; that is a capability, not a default."""


def _dev_secret() -> str:
    settings = get_settings()
    secret = settings.auth_dev_secret
    if secret == INSECURE_DEV_SECRET and not settings.allow_insecure_dev_auth:
        raise AuthError(
            "refusing to use the shipped development signing secret. Set GK_AUTH_DEV_SECRET "
            "to something private, or GK_ALLOW_INSECURE_DEV_AUTH=1 to accept the risk."
        )
    return secret


def issue_dev_token(subject: str, ttl_seconds: int = 3600, can_impersonate: bool = False) -> str:
    """Mint a development token. Never available in oidc mode."""
    if get_settings().auth_mode != "dev":
        raise AuthError("dev tokens can only be issued in dev auth mode")
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": DEV_ISSUER,
        "aud": DEV_AUDIENCE,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if can_impersonate:
        payload["gatekeeper.impersonate"] = True
    return jwt.encode(payload, _dev_secret(), algorithm="HS256")


_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        settings = get_settings()
        if not settings.oidc_jwks_url:
            raise AuthError("oidc auth mode requires GK_OIDC_JWKS_URL")
        # PyJWKClient caches keys and refreshes on an unknown kid, which is what makes
        # key rotation a non-event rather than an outage.
        _jwks_client = PyJWKClient(settings.oidc_jwks_url, cache_keys=True)
    return _jwks_client


def verify(token: str) -> Identity:
    """Verify a bearer token and return the identity it establishes.

    Raises :class:`AuthError` for anything that is not a valid, unexpired token from the
    configured issuer. Signature, expiry, issuer and audience are all checked — omitting
    audience is the classic way a token minted for one service is replayed against another.
    """
    settings = get_settings()
    try:
        if settings.auth_mode == "dev":
            claims = jwt.decode(
                token,
                _dev_secret(),
                algorithms=["HS256"],
                audience=DEV_AUDIENCE,
                issuer=DEV_ISSUER,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        else:
            signing_key = _jwks().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                # RS256 only. Accepting a list that includes HS256 is the algorithm
                # confusion attack: the verifier is handed a public key it will happily
                # use as an HMAC secret, and the public key is public.
                algorithms=["RS256"],
                audience=settings.oidc_audience,
                issuer=settings.oidc_issuer,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
    except jwt.PyJWTError as exc:
        raise AuthError("invalid or expired token") from exc

    subject = claims.get(settings.oidc_subject_claim) or claims.get("sub")
    if not subject:
        raise AuthError("token carries no subject")

    return Identity(
        subject=str(subject),
        issuer=str(claims.get("iss", "")),
        expires_at=int(claims.get("exp", 0)),
        can_impersonate=bool(claims.get("gatekeeper.impersonate", False)),
    )


async def resolve(identity: Identity, tenant_slug: str | None = None) -> Principal:
    """Turn a verified identity into a Principal, reading entitlements from the database.

    This is the function that keeps the token from being able to grant anything. The
    subject selects a row; every attribute the policy consults comes from that row.
    """
    from gatekeeper.ingest import seed

    slug = tenant_slug or seed.TENANT_SLUG
    try:
        principal = await seed.load_principal(identity.subject, slug)
    except LookupError as exc:
        # A valid token for a principal that no longer exists is a deprovisioned user,
        # which must fail like any other unauthenticated request.
        raise AuthError("no such principal") from exc

    if principal.is_expired:
        raise AuthError("grant expired")
    return principal
