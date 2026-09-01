# 0009 — The MCP server binds one principal per process, and counts what it withholds

**Status:** Accepted · **Date:** 2026-08-29 · **Phase:** 4

## Context

An agent that can search a corpus is only as safe as that corpus's access control, and an
MCP server is the surface where an access model is most likely to be quietly bypassed: the
tools are new code, the caller is a language model, and the output is prose rather than
rows. Two questions have to be answered before writing any of it — who the server is
acting as, and what its output may say about what it refused.

## Decision 1: one principal, bound at launch, for the process lifetime

The handle comes from `GK_MCP_PRINCIPAL` (or `--as`) and the server refuses to start
without a valid, unexpired one.

This is not the compromise the demo console makes. The console impersonates freely and
says so, because its purpose is side-by-side comparison. Here, binding to one
identity is *correct*, not a shortcut: an MCP server over stdio is a subprocess launched
by one user's client, there is no request envelope to carry a token, and there is no
second principal it could legitimately serve. Making the binding a launch-time
configuration rather than a per-call argument also means no tool takes a principal
parameter — so no prompt injection can ask for a different one.

Failing at startup rather than at first call is deliberate. A server that starts cleanly
and then refuses everything looks like an empty corpus, and an agent will confidently
report it as one.

## Decision 2: a withheld result is a count, never an identity

`search_knowledge_base` reports "3 additional match(es) exist that this principal is not
authorised to read". It does not say which.

The distinction matters more here than in a UI. A model relays what it is given: hand it
the title *Equity Compensation* as a withheld item and it will write that title into its
answer, and for restricted material the title is very often the secret. A count reveals
density, not content — the same figure the console already shows — and it lets the agent
say "your answer may be incomplete" instead of implying the corpus is silent on a topic.

`get_document` follows the same rule harder: an unreadable path and a nonexistent path
return **byte-identical** responses. Distinguishing them would turn the tool into an
oracle for enumerating the corpus by probing paths, which is slower than reading it and
just as effective.

## Consequences

Switching principals means editing `.mcp.json` and restarting the client. That is the
intended friction.

Every tool goes through `retrieve()` and `principal_session()` — the same code path as the
CLI and the console, with no agent-specific branch. The red-team suite's guarantees
therefore cover this surface without re-testing it, which is the main argument for not
giving the agent its own query layer no matter how convenient that would be.

Model load (~18 s for the embedder, plus the cross-encoder) happens at startup, so the
first tool call is fast and `claude mcp` shows a slow connect instead of a slow first
answer. `--no-rerank` skips the cross-encoder and drops to the dense-only config —
**derived from what actually loaded, not assumed**, after an earlier version kept the
reranking default and raised on every single search.

## What this does not do

There is no authentication. The server trusts `GK_MCP_PRINCIPAL` completely, which is
appropriate for a local subprocess whose parent already has the user's shell, and wholly
inappropriate for anything reachable over a network. An HTTP transport would need a real
token exchange, and the per-process binding above would have to become per-request — at
which point the console's impersonation problem returns and has to be solved properly.
