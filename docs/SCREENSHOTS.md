# Running it, and shooting it

Every command here was run against the live 73,801-chunk corpus and produced the output
shown. Where a number appears, it is the number that actually came back.

The console shots are **scripted** rather than taken by hand
([`scripts/screenshots.mjs`](../scripts/screenshots.mjs)) — a re-shoot after a UI change
produces the same frames at the same sizes. The terminal shots are manual, and the exact
commands are below.

---

## 1. Bring it up

```bash
make install          # venv + dependencies (once)
make bootstrap        # infra, migrations, clone the handbook, seed, embed
```

`make bootstrap` takes roughly 45 minutes the first time — it embeds 73,801 chunks on CPU.
After that everything is incremental.

Already bootstrapped, or coming back after a reboot:

```bash
make up               # postgres, redis, minio
make ui               # console on http://127.0.0.1:8077
```

Check it is really up before shooting anything:

```bash
make jobs
```

Answer *generation* needs an API key in `.env` (`GK_ANTHROPIC_API_KEY` or
`GK_OPENAI_API_KEY`). Without one the console still retrieves and says so — the shots just
show sources instead of answers. **The answer panels in the committed screenshots were
generated with a real key.**

---

## 2. The console shots (scripted)

```bash
npm install                     # once — puppeteer-core, drives your installed Chrome
node scripts/screenshots.mjs    # writes docs/assets/*.png at 2x
node scripts/screenshots.mjs --open
```

Fifteen frames, about four minutes:

| File | What it shows |
|---|---|
| **`00-hero.png`** | **The main image.** One question, four principals, four different amounts of truth |
| `01-signed-out.png` | The principal picker before sign-in |
| `02-clearance-is-a-ceiling.png` | Three principals; the lowest clearance reads the most restricted material |
| `03-access-surface.png` | Sidebar alone: how much of the corpus each principal can reach |
| `04-source-detail.png` | A source opened, with its sensitivity tag and real handbook path |
| `05-withheld-counts.png` | Withheld counts differing across principals |
| `06-dark.png` | Dark theme |
| `07-mobile.png` | 390px, showing it reflows |
| `08-four-principals-one-question.png` | The full page the hero is cropped from |
| `10-expired-grant.png` | A single column: access that expired, and the date it expired on |
| `11-guest-vs-cfo.png` | Anonymous visitor beside the CFO on the same question |
| `12-jurisdiction.png` | Per-country employment policy, scoped by region rather than rank |
| `13-sensitivity-tags.png` | One source list spanning internal, confidential and restricted |
| `14-tablet.png` | 834px, two columns |
| `15-mobile-dark.png` | Mobile, dark theme |

### The hero

`00-hero.png` (3008×690, a 4.4:1 banner) is the one to lead with. One question — *"How much
can I expense for a meal on a business trip?"* — and four outcomes:

| | clearance | withheld | what they get |
|---|---:|---:|---|
| Sam Okafor, Security Engineer | 1 | 2 | the actual limits |
| Unauthenticated Guest | 0 | **8** | "None of the provided sources address meal expense limits" |
| Mira Lindqvist, CFO | 3 | 1 | the actual limits, in more detail |
| Wren Adeyemi, External Auditor | 2 | — | **grant expired 2026-08-26 — no claims issued** |

What makes it work is that the guest's non-answer sits directly beneath *"8 results withheld
by authorization"*. Cause and effect are in the same frame: the assistant is not failing,
it is being prevented. And Wren shows access is time-bound, not just role-bound.

The equity-refresh question was tried first and rejected — it withholds more, but even the
CFO's answer comes back "no specific policy described", so three of four columns read as an
assistant that cannot answer rather than an access model that works. If you re-shoot, check
the answers are substantive before shipping the frame.

### Why `02` is the strongest supporting shot

Same question — *"How do I report a security incident?"* — asked as three people:

| | clearance | withheld | reads the restricted runbooks? |
|---|---:|---:|---|
| Sam Okafor, Security Engineer | 1 | 3 | **yes** |
| Raj Mehta, Backend Engineer | 1 | 4 | no |
| Mira Lindqvist, CFO | 3 | 4 | no |

The CFO sits two clearance levels above Sam and still cannot read what Sam reads, because
clearance is a **ceiling** and the `security` group is the **key**. That is counterintuitive
on sight, which is exactly why it is worth a screenshot — it shows the access model is real
rather than a single privilege dial.

If you re-shoot with different principals, keep a pair where the *lower* clearance sees
more. A shot where the CFO simply sees everything demonstrates nothing.

### Overriding

```bash
GK_CONSOLE=http://localhost:9000 node scripts/screenshots.mjs
CHROME_PATH="/Applications/Chromium.app/Contents/MacOS/Chromium" node scripts/screenshots.mjs
```

---

## 3. The terminal shots (manual)

Terminal at **16–18pt**, ~100 columns, dark theme. On macOS, `⌘⇧4` then `Space` captures a
single window cleanly, or:

```bash
screencapture -w -o ~/Desktop/shot.png
```

`-w` picks a window, `-o` drops the drop-shadow. Set the width first so the tables do not
wrap:

```bash
export COLUMNS=100
```

### 3a. The same question, two people

```bash
make ask Q="What is the board meeting cadence and who attends?" WHO=raj
make ask Q="What is the board meeting cadence and who attends?" WHO=mira
```

Raj gets **8 sources · 4 withheld by authorization**; Mira gets 8 with nothing withheld and
`confidential` SME cadence material Raj never sees. Capture both in one frame if you can —
the contrast is the point.

### 3b. What one principal can reach

```bash
make whoami WHO=sam
```

> Sam Okafor — Security Engineer  clearance=1 region=US groups=all-employees, engineering, security
> sees 4,094 of 4,586 documents (492 withheld by the database)

Plus the per-sensitivity table and a sample of restricted material. Good "the model is real"
evidence.

### 3c. The security claim

```bash
make redteam
```

> (principal, chunk) pairs reconciled | 442,806
> leaks | 0
> over-block rate | 0.00%
>
> No leaks. Database and oracle agree on every pair.

This is the single strongest frame in the project. Takes about a minute.

### 3d. Prompt injection contained

```bash
make redteam-indirect
```

Plants 19 payloads as readable documents in the live corpus, attacks through the real
pipeline, removes them afterwards. The line to capture is that payloads reached the model
and **none widened access, including ones the classifier missed**.

### 3e. Throughput

```bash
make load
```

Or just screenshot the committed table in [`docs/LOAD.md`](LOAD.md) — 1,207 q/s on the
authorized database path against 201 for the full request path. Re-running takes ~3 minutes.

### 3f. Background ingestion

```bash
make worker      # leave running in a second pane
make jobs
```

Shows the job table with a `partial` row and its failure count — evidence that failure
isolation and retry are real, not aspirational.

---

## 4. Claude Code hitting the MCP server

The repo ships a [`.mcp.json`](../.mcp.json), so a client in this directory picks the server
up. Verify it boots first:

```bash
make mcp WHO=raj
```

Then in Claude Code, in this directory, ask:

> Search the handbook for the executive compensation review process.

Capture the model reporting **what it found and how many matches it withheld**. That
behaviour — counts, never titles — is the point of
[ADR 0009](adr/0009-mcp-surface-and-one-principal-per-process.md).

If the client reports the server as disconnected, it is almost always because Postgres was
down when the client started it. `make up`, then restart the client.

---

## 5. Tracing

```bash
make trace       # starts Jaeger on http://localhost:16686
GK_OTEL_ENDPOINT=http://localhost:4317 make ask Q="expense limit?" WHO=dana
```

Open the trace and screenshot the span breakdown. Worth capturing because of what the spans
*do not* contain: the query's length, never its text; an entitlement fingerprint, never a
principal ([ADR 0015](adr/0015-traces-carry-shapes-not-contents.md)).

---

## 6. Using them

- **README hero / GitHub social preview:** `00-hero.png`, full width, directly under the pitch.
- **Upwork portfolio main image:** `00-hero.png`. Use `08-four-principals-one-question.png` where a taller, less banner-shaped image fits better.
- **Second image:** `02-clearance-is-a-ceiling.png`, then the `make redteam` terminal shot. Outcome first, proof second.
- **Dark-mode viewers:** GitHub honours `<picture>` with `prefers-color-scheme`, so `02` and `06` can be paired.

Keep each file under ~1 MB or GitHub is slow to render them. The 2x captures land between
200 KB and 750 KB, so no compression is needed.

## Things to avoid

- Do not shoot with a stale session. The console keeps a token in `sessionStorage`; sign out first, or the picker is already in comparison mode and the identity is whoever signed in last. The script clears it for you.
- `raj` and `mira` are pre-selected on every boot. Any other pairing has to be set explicitly.
- Do not crop out the withheld badges to fit. They are the product.
- Do not show `.env`, a token, or the dev-login response body.
