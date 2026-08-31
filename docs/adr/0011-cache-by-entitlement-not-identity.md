# 0011 — The query cache is keyed by entitlement, and stores decisions rather than data

**Status:** Accepted · **Date:** 2026-08-30 · **Phase:** 4

## Context

A retrieval cache is the most natural place in this system to reintroduce the exact bug
the rest of it exists to prevent. Key it on the query — text or embedding, it makes no
difference — and the first person to ask "what is the equity refresh policy" populates an
entry the next person receives, regardless of what either is cleared to read. The failure
is silent, it presents as a performance win, and it survives every test that measures only
latency.

The obvious safe answer is to key on `principal_id`. That is also nearly useless: in an
enterprise hundreds of people share a role, and a per-person cache almost never hits.

## Decision

**1. Cache the decision, not the data.** An entry stores chunk *ids*. A hit re-fetches them
through `principal_session`, so the RLS policy runs again on every hit. Correctness
therefore does not depend on the key being right — a wrong key degrades to a wasted lookup,
not a disclosure. `test_a_forged_entry_cannot_leak_because_hits_are_re_authorized` writes an
entry containing a restricted chunk id directly into a principal's own bucket; the hit is
served and still returns nothing.

This costs almost nothing, because the authorized fetch is 2-6 ms of a ~215 ms query. What
the cache actually saves is the embedding pass and the cross-encoder.

**2. Key on entitlement, not identity.** The key is a hash of exactly the attributes
`gatekeeper.authorize()` consults: tenant, clearance, groups, need-to-know, region,
employment type. Two principals with identical entitlements share entries; two differing in
any respect the policy reads cannot collide, because the difference is in the key. A
parametrised test asserts that changing each one of those six attributes changes the
fingerprint — the test exists so that a future attribute added to the policy and forgotten
here fails loudly.

Deliberately absent: `department` (no policy reads it, so including it would fragment the
cache for nothing) and `valid_until` (expiry is enforced by the re-fetch, and a timestamp
in a cache key defeats the cache).

**3. An explicit visibility epoch.** Anything that changes what anyone can see — corpus
load, `index reacl`, a policy change — bumps a counter that is part of the key, retiring
the cache. Deriving the epoch from timestamps was considered and rejected: `index reacl`
rewrites chunk ACLs without touching any `updated_at`, so a derived epoch would have missed
the most likely reason for a visibility change. `cache_epochs` keeps a row per bump with a
reason, so a collapsed hit rate can be attributed.

## Consequences

**267 ms → 12 ms on a hit, a 21.7x speedup**, measured on the shipped `dense+rerank` config.

**The cache is off by default** (`use_cache=False`). It is safe, but the *semantic* part —
matching similar queries rather than identical ones — means it can answer a question nobody
asked, and do so silently. The threshold is tight (0.97 cosine) and `SearchResult.cached_query`
reports which query was actually reused, so a caller can log it. That is a correctness trade
a caller should opt into rather than inherit.

The cache table is admin-plane only; `gatekeeper_app` has no access to it. It holds no
tenant data — a chunk id is meaningless without a subsequent authorized fetch — but giving
the query plane a path to chunk ids that never passes through a policy would be a
gratuitous second door.
