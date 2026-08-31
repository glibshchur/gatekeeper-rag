# Ingestion

4,586 Markdown files to 73,801 authorized, embedded chunks. The access model is
**derived from the corpus's own structure**, not hand-labelled per document.

```mermaid
flowchart TB
    CLONE["git clone<br/>GitLab Handbook · 4,586 files"] --> SEED
    RULES["corpus/acl_rules.yaml<br/>19 rules · first match wins"] --> SEED
    SEED["seed<br/>path → sensitivity, groups, clearance,<br/>need-to-know, jurisdiction"]
    SEED --> DOCS[("documents")]

    DOCS --> SKIP{"content hash and<br/>embedding space unchanged?"}
    SKIP -- yes --> DONE(["skip — one query"])
    SKIP -- no --> CHUNK["structure-aware chunking<br/>heading path prefixed · tables atomic"]

    CHUNK --> BLOB["blob → MinIO"]
    CHUNK --> SCORE["injection scoring"]
    CHUNK --> EMBED["embed · ONNX CPU"]

    EMBED --> CHUNKS[("chunks<br/>ACLs inherited, then overridden")]
    SCORE --> CHUNKS
    CHUNKS --> HNSW["partial HNSW indexes<br/>one per sensitivity tier"]

    style SKIP stroke:#58a6ff
    style CHUNKS stroke:#3fb950,stroke-width:2px
```

**ACLs are columns on `chunks`, not a join.** The RLS policy and the vector index live on
the same relation, so ranking and authorization are one scan rather than a filter applied
to the output of one ([ADR 0004](../adr/0004-embedding-columns-on-chunks.md)).

**The rules file is the access model.** 19 path patterns, first match wins, reviewable
without reading Python. Three chunk-level overrides handle the case a path rule cannot:
a compensation table inside an otherwise-internal handbook page.

**The skip check is what makes iteration affordable.** Change a chunking parameter and
pass `--force`; change nothing and re-running the whole corpus costs one query per
document.

## Background jobs

```mermaid
flowchart TB
    ENQ["index build --background"] --> ROW[("ingest_jobs row<br/>written first")]
    ROW --> MSG["Redis message<br/>carries only the job id"] --> W["worker"]
    W --> READ["read parameters from the row"]
    READ --> LOOP["per document: its own transaction"]
    LOOP -- ok --> PROG["progress → row, every 10 documents"]
    LOOP -- raises --> FAIL["append to failures<br/>the batch continues"]
    PROG --> STATUS{"any failures?"}
    FAIL --> STATUS
    STATUS -- no --> OK(["succeeded"])
    STATUS -- yes --> PARTIAL(["partial"])
    PARTIAL --> RETRY["jobs retry<br/>re-enqueues only what failed"]

    style ROW stroke:#3fb950,stroke-width:2px
    style LOOP stroke:#58a6ff,stroke-width:2px
    style PARTIAL stroke:#d29922
```

The row is written **before** the message. A crash between the two leaves a visible
`queued` job; the other order leaves work running that nothing knows about.

The message carries **only an id**. An operator debugging a stuck reindex answers "what is
it doing and how far has it got" with a `SELECT`, not by decoding a Redis value that the
incident may already have flushed ([ADR 0014](../adr/0014-jobs-are-rows-not-just-messages.md)).

Failure isolation is **a transaction boundary, not a `try` block**. One transaction around
the loop would still roll back every committed document when a later one failed — which is
exactly how the foreground path used to lose twenty-five minutes of work to a single bad
blob upload.
