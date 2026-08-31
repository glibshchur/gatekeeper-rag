# Threat model

What this system defends, against whom, and — the part most threat models skip — **what it
does not defend**. Every mitigation below names the measurement that backs it or admits
there isn't one.

Diagrams: [trust boundaries](diagrams/trust-boundaries.md).

---

## 1. Assets

| Asset | Why it matters | Where it lives |
|---|---|---|
| Restricted document content | Compensation, board material, security runbooks, per-country employment terms | `chunks.content`, `documents` |
| The *existence* of a document | For restricted material the title is usually the secret — "Acquisition of X — Diligence" discloses the deal | `documents.path`, `documents.title` |
| Entitlement data | Who is in which group, at what clearance | `principals`, `principal_claims` |
| The audit trail | Its value is that it cannot be quietly edited | `audit_log` (hash-chained) |
| Query history | What the CFO searched for is sensitive even when every result was permitted | `audit_log.query_text`, and — deliberately not — traces |

## 2. Adversaries

| # | Adversary | Capability | Motivation |
|---|---|---|---|
| A1 | **Curious insider** | A valid account with real, ordinary entitlements. Can phrase any query. | Read material above their clearance |
| A2 | **Malicious content author** | Can write handbook pages other employees will read | Plant an injection that makes the model fetch or reveal restricted material |
| A3 | **Compromised application process** | Code execution in the API/worker, holds the app DB credential | Read the whole corpus |
| A4 | **Token forger** | Can craft and present arbitrary JWTs | Impersonate a principal, or self-grant entitlements |
| A5 | **Operator with telemetry access** | Reads traces, logs, metrics — but has no database grant | Read query text and results out of band |
| A6 | **Other tenant** | A legitimate principal in a different tenant | Cross-tenant read |

A1 and A2 are the realistic ones and get the most attention. A3 is the assumption most
RAG demos quietly fail.

## 3. Mitigations, and the evidence for each

### A1 — Curious insider

**Authorization runs inside the query, as a row-level security predicate on the same
relation as the vector index.** The candidate row is never returned to application memory
and then filtered; it is never returned. Ranking and authorization are one scan
([ADR 0002](adr/0002-row-level-security-over-application-filtering.md),
[ADR 0004](adr/0004-embedding-columns-on-chunks.md)).

**Evidence.** An independent Python implementation of the same written spec is reconciled
against the database over **every (principal, chunk) pair — 442,782 of them — with 0
disagreements**. Asking the database whether the database got it right proves nothing;
two implementations built from one spec disagreeing loudly is the point
([`redteam/oracle.py`](../src/gatekeeper/redteam/oracle.py)). Plus 360 adversarial probes
across 8 categories, direct primary-key fetch, aggregate enumeration, and boundary probes:
**0 leaks**.

**Residual risk.** The over-block rate is **1.15%** — entitled results withheld. Erring
toward denial is the right direction, but it is not zero and it is measured rather than
assumed.

### A2 — Malicious content author (indirect prompt injection)

**Containment is structural and does not depend on detection.** Grants live in a
transaction-local GUC set by `set_config(..., true)` before the query runs. Nothing the
model emits can write it. An injection that perfectly persuades the model still cannot
make the database return a row.

Detection is the *second* line: chunks are scored at ingest, stored, and re-scorable in
15 seconds without re-embedding. A flagged source is **annotated for the model, never
silently withheld** — withholding it would hide the attack from the person best placed to
notice it.

**Evidence.** Red team v2 plants all 19 payloads as readable documents **in the live
73,801-chunk corpus** and attacks through the real pipeline. 13 reached the model, **0
widened access — including 2 the classifier missed entirely**. That last clause is the
whole argument; if containment only held where detection worked, this system's security
would rest on regular expressions ([ADR 0010](adr/0010-injection-detection-is-the-second-line.md)).

**Residual risk.** An injection can still make the model *lie about* material it was
legitimately shown, or refuse, or emit an alarming answer. Containment bounds disclosure,
not truthfulness. Groundedness verification is the partial answer and it has its own
blind spot (below).

### A3 — Compromised application process

**The query role cannot bypass the policy.** `gatekeeper_app` is `NOSUPERUSER`
`NOBYPASSRLS`, and policies are `FORCE`d so even the table owner is subject to them.
Migrations and ingestion use a separate, privileged connection that the request path never
holds.

**Residual risk, and it is larger than the split-privilege story suggests.** The API
process **holds and routinely uses an RLS-bypassing connection**. Three parts of the
request path open one, each for a defensible reason:

| Use | Why | What it touches |
|---|---|---|
| `count_withheld` baseline | The comparison must be genuinely unfiltered, or the denied-row count silently under-reports every denial the policy caused | Chunk ids and embeddings of rows the caller *cannot* read |
| Query cache | Entitlement fingerprints and chunk ids; the cache is not a tenant-scoped relation | No content |
| `/api/jobs` | Operator plane, gated on `gatekeeper.admin` | Job rows, including document paths |

Chunk **content** is never read through it. But an attacker with code execution in the API
process inherits that credential and can then read the entire corpus directly — RLS does
not bound them at all. It bounds a *logic* bug in the request path, not a code-execution
compromise of the process holding the owner URL.

The honest hardening is to move the owner credential out of the API process entirely: run
the withheld-count baseline as a `SECURITY DEFINER` function that returns only a count,
give the cache its own least-privilege role, and move `/api/jobs` behind the worker. **This
is not done.** It is the largest known gap between what the split-privilege design claims
and what it currently delivers.

### A4 — Token forger

**A token asserts identity and nothing else.** `Identity` has no field for groups or
clearance — there is nowhere to put a forged entitlement. Groups, clearance, need-to-know
and region are read from Postgres on every request ([ADR 0013](adr/0013-tokens-assert-identity-not-entitlement.md)).

Signature, issuer, **audience** and expiry are all checked. `RS256` is pinned in oidc mode:
accepting an algorithm list that includes HS256 is the confusion attack, where the verifier
is handed a public key it will happily use as an HMAC secret. The shipped development
secret is refused unless `GK_ALLOW_INSECURE_DEV_AUTH=1`.

**Residual risk.** **There is no revocation.** A token is valid until it expires; a
compromised token cannot be recalled. For one-hour dev tokens that is tolerable, and for
anything real it is the next thing to build. `/api/dev-login` is an unauthenticated token
mint — it is refused outside `dev` mode and `issue_dev_token` refuses independently rather
than trusting the endpoint's check, but it remains the most dangerous endpoint here.

### A5 — Operator with telemetry access

This is the adversary that instrumenting a RAG system the ordinary way creates. Recording
query text and matched document titles builds a **second copy of the corpus in a store with
a weaker access policy**, and RLS never sees those reads.

**Spans carry shapes, never contents**: the query's length, not its text; the entitlement
fingerprint, not the principal; counts, not titles. `telemetry.attributes()` **raises** on
a deny-listed key rather than dropping it, and a test walks the AST of every module so a
span on a rarely-taken path cannot fail for the first time in production
([ADR 0015](adr/0015-traces-carry-shapes-not-contents.md)).

**Residual risk, and it is real.** The **audit log deliberately stores `query_text`** —
that is its job, and it is the one place query content is retained. Anyone with a database
grant on `audit_log` can read what everyone searched for. The hash chain makes tampering
evident; it does nothing about reading. Access to `audit_log` is a privileged operation and
is not further restricted here.

Application **logs** are not audited for content the way spans are. A stack trace can carry
a chunk excerpt.

### A6 — Other tenant

`tenant_id` is the first clause of the policy and an explicit predicate on every query.
The withheld count is scoped to the principal's own tenant — an early version compared
against the whole database, which turned a transparency feature into a side channel
disclosing that other corpora existed.

**Evidence.** Cross-tenant probes in the adversarial suite; the oracle reconciliation
covers cross-tenant pairs.

---

## 4. Disclosure through side channels

Denial has to be observable to be trustworthy, and every observable is a channel.

| Channel | Handling |
|---|---|
| **Withheld results** | Reported as a **count, never an identity**. Hand a model the title of a withheld document and it writes that title into its answer — and for restricted material the title is the secret ([ADR 0009](adr/0009-mcp-surface-and-one-principal-per-process.md)) |
| **`get_document` on an unreadable path** | Byte-identical to a nonexistent path. Distinguishing them turns the tool into an oracle for enumerating the corpus |
| **Query cache** | Keyed on a hash of exactly the attributes the policy consults, and stores chunk **ids** so every hit is re-authorized. Two principals whose entitlements differ in any respect the policy reads cannot collide ([ADR 0011](adr/0011-cache-by-entitlement-not-identity.md)) |
| **Timing** | **Not mitigated.** A restricted-heavy query and a permitted one take measurably different times, and the partial indexes make that worse, not better. An adversary with many queries and a clock can learn something about what exists. Accepted; noted rather than hidden |
| **Result counts** | A principal learns *how many* rows they were denied. Deliberate — the alternative is a system whose denials are invisible — but it is disclosure |

## 5. What is out of scope

Stated so their absence is not mistaken for coverage:

- **Denial of service.** No rate limiting, no query cost budget. A single expensive query can occupy a connection.
- **Encryption at rest / in transit.** Local compose, plaintext. A deployment concern that this project does not model.
- **Secret management.** `.env` on disk. No vault, no rotation.
- **Supply chain.** Dependencies are pinned via `uv.lock` and not otherwise verified. The corpus is cloned from GitLab at build time and trusted.
- **The blob store.** MinIO holds raw source and is **not** RLS-governed. It currently holds only public handbook content and nothing in the read path consults it — the git clone is the source of truth. If it ever serves content, it needs its own authorization.
- **Multi-hop agentic retrieval.** Not built. Each hop is a fresh authorized query, so the model extends but does not obviously break; untested, therefore unclaimed.

## 6. Summary

| Adversary | Status | Backed by |
|---|---|---|
| A1 curious insider | **Mitigated** | 0 leaks / 360 probes; 0 disagreements / 442,782 pairs |
| A2 content author | **Contained** | 0 of 13 reaching payloads widened access, including 2 undetected |
| A3 compromised process | **Weak** — the API process holds an RLS-bypassing credential | Split-privilege roles exist; the request path still opens owner connections |
| A4 token forger | **Mitigated for forgery, open for revocation** | No entitlement claims exist to forge; no revocation list |
| A5 telemetry operator | **Mitigated for traces, open for the audit log** | AST test over every span; `audit_log.query_text` is by design |
| A6 other tenant | **Mitigated** | Policy clause + explicit predicate + oracle reconciliation |
