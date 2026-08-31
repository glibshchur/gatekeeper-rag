# Demo recording script

A four-minute walkthrough and three GIFs. **I can't record these** — they need a screen
capture — so this is the shot list, with the exact commands, the timings, and what to say
over each one.

Everything here has been run and produces the output described. If a shot doesn't look
like this, something regressed.

## Setup, once

```bash
make bootstrap          # ~45 min the first time: clone, seed, embed 73,801 chunks
make ui                 # leave running on http://127.0.0.1:8077
```

Terminal at **16–18pt**, dark theme, ~100 columns. Browser at 1280×800. Close every other
tab — a portfolio video with someone's inbox in the tab bar reads as careless.

---

## The three GIFs

Short, silent, looping. These carry the README, so they matter more than the video —
most people will never press play on a video, and everyone scrolls past a GIF.

### GIF 1 — the same question, two people (~12s)

The single most important shot in the project. Split terminal, or two takes stitched.

```bash
make ask Q="What is the board meeting cadence and who attends?" WHO=raj
make ask Q="What is the board meeting cadence and who attends?" WHO=mira
```

Raj gets public sources and a `1 withheld by authorization` line in red. Mira gets the
restricted board material. **Hold on the withheld line for a full second** — that is the
whole product in one line of output.

### GIF 2 — clearance is a ceiling, not a key (~15s)

In the console at `http://127.0.0.1:8077`. Sign in, select **Sam** and **Mira**, ask:

> How do I report a security incident?

Sam (security engineer, clearance **1**) gets a `restricted` incident-response guide. Mira
(CFO, clearance **3**) does not.

This is counterintuitive on sight, which is exactly why it belongs in a GIF: high clearance
without the right group grants nothing. Let the two columns sit side by side for two
seconds before cutting.

### GIF 3 — Claude Code hitting the MCP server (~20s)

```bash
make mcp WHO=raj
```

Then in Claude Code, in this directory:

> Search the handbook for the executive compensation review process.

The tool reports what it found **and** how many matches it withheld. Capture the model
saying so honestly rather than implying the corpus is empty on the topic — that behaviour
is the point of returning counts rather than titles.

---

## The four-minute video

Times are cumulative. Rehearse once; a second take is always better and this is short.

### 0:00–0:25 — the problem

*Slide or plain terminal. No code yet.*

> Most internal chatbots handle permissions by filtering search results in application
> code, after retrieval. That means the restricted document already left the database. One
> bug, one injected prompt, one compromised process, and it's disclosed.
>
> This is what it looks like to put the access policy in the database instead.

### 0:25–1:10 — the demo (GIF 1, live)

Run both `make ask` commands. While they run:

> Same question, same corpus, same ranking function. Raj is a backend engineer; Mira is
> the CFO. Raj gets public sources and a note that one result was withheld. He isn't
> filtered out of a list he was shown — the row never left Postgres.

Then the console, Sam vs Mira on the security incident question:

> And it isn't a simple hierarchy. Sam has clearance 1 and reads a restricted security
> runbook the CFO can't. Clearance is a ceiling, not a key.

### 1:10–2:00 — how

*Show [docs/diagrams/authorization.md](diagrams/authorization.md) or the policy SQL.*

> Claims go into a transaction-local Postgres setting before the query runs. A row-level
> security policy reads them, and the query layer connects as a role that is `NOSUPERUSER`
> and `NOBYPASSRLS` — it cannot opt out. The ACL columns live on the same table as the
> HNSW vector index, so ranking and authorization are one scan, not a filter applied to
> the output of one.

### 2:00–2:50 — how I know it works

```bash
make redteam
```

> I don't trust a system to grade itself. There's a second, independent Python
> implementation of the same written spec, and the two get reconciled across every
> principal-document pair — 442,782 of them. Zero disagreements. Plus 360 adversarial
> probes: zero leaks.

Then:

```bash
make redteam-indirect
```

> And this plants 19 prompt injections as real, readable documents in the live corpus and
> attacks through the actual pipeline. Thirteen reached the model. None widened access —
> including two that my own classifier completely failed to detect. That's the part that
> matters: containment doesn't depend on detection working.

### 2:50–3:30 — measured, including what failed

*Show [docs/ABLATION.md](ABLATION.md) and [docs/LOAD.md](LOAD.md).*

> 58 hand-written questions — not generated from the chunk text, which would make the eval
> circular. nDCG at 10 is 0.796.
>
> I also built hybrid search, measured it at 0.795 against 0.796 for 56% more latency, and
> turned it off. That's in the README as prominently as the wins.
>
> Under load the authorized database path does 1,207 queries a second. The full request
> path caps at 201 — so the bottleneck is the embedding model, not row-level security. I
> keep the control arm in the benchmark permanently so that number is never quotable
> without it.

### 3:30–4:00 — close

*Show [docs/adr/](adr/) and the [threat model](THREAT_MODEL.md).*

> Sixteen decision records, including the ones where I was wrong — a proposed fix on the
> wrong axis, a component I built and deleted, a benchmark that was measuring itself.
>
> And a threat model that says what's still weak: no token revocation, timing side
> channels unmitigated, and the API process still holding a connection that can bypass the
> policy.
>
> One command from nothing to a running system. Link's in the description.

---

## Publishing

- **YouTube, unlisted.** Link at the top of the README and in the Upwork profile.
- Title: `gatekeeper-rag — RAG where the database enforces who can read what`
- GIFs: `docs/assets/`, referenced from the README. Keep each under 5 MB or GitHub is slow to load them.

## Things to avoid

- Don't narrate what's visibly happening. Say *why*, not *what*.
- Don't apologise for the local-only demo. One-command reproducibility is a feature.
- Don't skip the negative results — they're the most credible thing in the project, and the reason a technical viewer keeps watching.
- Don't show `.env`, tokens, or the dev-login endpoint minting a token without saying what `GK_AUTH_MODE=dev` means.
