# 0013 — A token asserts identity; the database owns entitlement

**Status:** Accepted · **Date:** 2026-08-30 · **Phase:** 5

## Context

Until Phase 5 nothing in this system authenticated. The console took a principal handle in
the request body and believed it; the MCP server read one from an environment variable.
Both were documented as demo surfaces, and [ADR 0009](0009-mcp-surface-and-one-principal-per-process.md)
argued the MCP case was actually correct for a stdio subprocess. The console's was not — it
was the single largest gap between what the project claimed and what it enforced.

## Decision

Bearer tokens, verified for signature, expiry, issuer **and** audience. Two modes: `dev`
(HS256, local secret, `gatekeeper token`) so the system runs with no identity provider, and
`oidc` (RS256 against a JWKS endpoint).

**The load-bearing decision is what a token is allowed to say.** A verified token
establishes a *subject* and one privilege flag. Groups, clearance, need-to-know and region
are read from `principals` on every request. `Identity` has nowhere to put a group, and a
test asserts that a token carrying `groups: [executives], clearance: 3` grants none of it.

Taking entitlements from token claims is common and would be self-defeating here. This
entire project rests on the database deciding what a principal may read; a system that
accepts a signed blob saying `executives` has moved that decision to whoever can mint
blobs. An IdP compromise should cost impersonation of one existing user, not the power to
invent an entitlement that never existed in the database.

**Impersonation survives as a capability.** The console's side-by-side comparison is its
whole point, so a token may carry `gatekeeper.impersonate`. Without the claim, a request
naming another principal gets a 403 rather than being silently narrowed — answering a
different question than the one asked is worse than refusing. Every use is written to the
audit chain under the **real** principal, because attributing it to the impersonated
identity would make the log say the CFO read her own compensation data.

## Consequences

`/api/dev-login` is an unauthenticated token mint and is the most dangerous thing in this
codebase if it ever ships enabled. It is refused outside `dev` mode, `issue_dev_token`
refuses independently rather than trusting the endpoint's check, and the console banner
says plainly what it is. The shipped signing secret is also refused unless
`GK_ALLOW_INSECURE_DEV_AUTH=1` is set — a hardcoded secret reaching production is a
forge-any-identity bug, so using it has to be deliberate.

`RS256` is pinned in oidc mode. Accepting a list that includes HS256 is the algorithm
confusion attack: the verifier is handed a public key it will happily use as an HMAC
secret, and the public key is public.

Audience is checked, not just issuer. Omitting it is how a token minted for one service
gets replayed against another that shares a signing key.

**What this does not do.** There is no refresh flow, no revocation list, and no session
management: a token is valid until it expires. For a demo with one-hour tokens that is
adequate; for anything real, revocation is the next thing to build. The MCP server still
binds one principal by environment variable — ADR 0009's reasoning stands, and it now has
the option of a token instead.
