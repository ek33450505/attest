# Validation

> Evidence that Attest works against **real Claude Code**, not mocks.
>
> Validated against Claude Code **v2.1.170** (the exact installed version). Attest
> version **0.1.0**.

## Why this document

Attest's thesis is simple and a little uncomfortable: **`DONE` is a claim, not
proof.** A subagent's self-report is the one signal you cannot trust, so Attest
grades the *act* (the real git working-tree delta) rather than the *output* (the
agent's words).

This document holds Attest to its own standard. It would be hypocritical to claim
"verify the act, not the report" and then validate the verifier with mocked
payloads and synthetic transcripts. So the evidence below comes from **live
Claude Code dispatches** — real `claude -p` runs that produced real
`SubagentStart`/`SubagentStop` payloads, captured verbatim and pinned by tests.

The headline finding (see below) is itself an instance of the thesis: the official
docs say one thing about `SubagentStop`; the running system on v2.1.170 does
another. **Documentation is a claim; the running system is ground truth.** We
trusted the running system.

A short note on honesty up front, because a skeptic should know the shape of the
evidence before reading it:

- **Enforcement is OFF by default** (`ATTEST_ENFORCE != 1`). Everything here is
  about what the system *can* do when explicitly enabled.
- The undocumented `SubagentStop` blocking behavior is **empirical on v2.1.170**
  and could change in a future Claude Code release. See [`./LIMITATIONS.md`](./LIMITATIONS.md).
- The "agent caught lying and self-corrected" scenario is **non-deterministic** —
  well-trained agents resist fabricating claims. The *deterministic* proof of
  blocking is the mechanism test plus the unit suite, not the lie-catch.

## Capture methodology

The capture is an **isolated headless run** designed to touch nothing on the host
but a scratch repo. It leaves the real `~/.claude/settings.json` and every other
installed hook **completely untouched**.

The recipe:

1. **Scratch git repo, clean tree.** Create a throway git repo and commit it so
   `git status --porcelain` is empty. A clean start tree is required — Attest
   treats a dirty start tree as *ambiguous* and suppresses blocks by design (it
   cannot cleanly attribute a delta it didn't establish a baseline for).

2. **Project-scoped hooks only.** Write a `.claude/settings.json` **inside the
   scratch repo** that wires the two attest shims (`SubagentStart` and
   `SubagentStop`), both `type: command`, both `async: false`. Because we load
   with `--setting-sources project`, the user-level `~/.claude/settings.json` is
   never read and never modified.

3. **Invoke headless, sandbox-aware:**

   ```bash
   env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
     CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 \
     claude -p "<prompt that dispatches a Task subagent>" \
       --setting-sources project \
       --permission-mode bypassPermissions \
       < /dev/null
   ```

   - `env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT` strips the inherited
     "I'm already inside Claude Code" markers so the nested run behaves like a
     fresh top-level session.
   - `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0` is **required** — without it,
     `bypassPermissions` is silently downgraded to the default permission mode and
     the subagent can't freely write files.
   - `--setting-sources project` is what guarantees only the scratch repo's hooks
     fire.

4. **For ENFORCE/block runs:** set `ATTEST_ENFORCE=1`, and configure the
   `SubagentStop` hook command so Python **stdout flows through to Claude Code**
   (the pure-JSON block decision) while Python **stderr is routed to a log**. In
   enforce mode all human-readable diagnostics go to stderr precisely so stdout
   stays a single JSON object — any stray byte on stdout voids the block.

### The critical gotcha

> **All dumper output — the state DB, the capture directory, and logs — MUST live
> OUTSIDE the scratch repo.**

Attest snapshots the scratch repo's tree at `SubagentStart` and diffs it at
`SubagentStop`. If `state.db`, the capture dir, or a log file is written *inside*
the repo, the stop-time snapshot sees them as **untracked changes**. That makes
the delta **ambiguous**, and an ambiguous delta suppresses the block
(`ALLOW_AMBIGUOUS`) — by design, but it will quietly defeat your capture run if you
don't notice. Point `ATTEST_STATE_DB` and `ATTEST_CAPTURE_DIR` at a sibling
working directory (e.g. `../work/`) outside the repo tree.

The committed, runnable version of this entire procedure is
[`../scripts/live-capture-test.sh`](../scripts/live-capture-test.sh) — it rebuilds
the scratch repo, wires the project-scoped hooks, runs the dispatch, and writes all
dumper output to a sibling directory.

## Real payload schema (ground truth)

The four captured fixtures (sanitized: local username and `$TMPDIR` repo path
rewritten to `/Users/dev` and `/tmp/attest-test-repo`; opaque run ids kept
verbatim) pin the schema below. This replaces earlier *synthetic* assumptions —
where the synthetic fixtures guessed wrong, the real capture corrected them.

**`SubagentStart`** carries identity plus the snapshot boundary — and nothing more:

| Field | Notes |
|-------|-------|
| `session_id` | parent session id |
| `transcript_path` | the **parent** session jsonl |
| `cwd` | the subagent's working dir |
| `agent_id` | stable identity (see below) |
| `agent_type` | `general-purpose` for a plain Task subagent |
| `hook_event_name` | `SubagentStart` |

There is **no `agent_transcript_path`** and **no `stop_hook_active`** at start.

**`SubagentStop`** carries the full picture, including the claim itself:

| Field | Notes |
|-------|-------|
| `session_id` | parent session id |
| `transcript_path` | the **parent** session jsonl |
| `agent_transcript_path` | the **subagent's own** jsonl, at `.../subagents/agent-<agent_id>.jsonl` |
| `cwd` | subagent working dir |
| `permission_mode` | e.g. `bypassPermissions` |
| `agent_id` | identical to the start payload |
| `agent_type` | `general-purpose` |
| `effort.level` | e.g. `high` |
| `hook_event_name` | `SubagentStop` |
| `stop_hook_active` | `false` on first stop, `true` on a post-block re-fire |
| `last_assistant_message` | the subagent's final text — **this is the claim** |
| `background_tasks` | `[]` in captures |
| `session_crons` | `[]` in captures |

### Two transcripts — don't confuse them

`transcript_path` is the **parent** (orchestrating) session's jsonl.
`agent_transcript_path` is the **subagent's own** jsonl. The subagent's completion
message lives in the subagent's file, not the parent's — so when a transcript read
is needed, Attest prefers `agent_transcript_path` over `transcript_path`
(`attest/hook.py`, `on_stop`).

### The claim fast-path

In the common case **no transcript read happens at all.** The subagent's final
text rides directly in the payload as `last_assistant_message`. Attest uses that
as the fast-path (`payload_text`) and only falls back to reading a transcript file
when the payload text is empty.

### Correction: there is no `stop_reason` on a normal stop

The earlier *synthetic* fixture invented a `stop_reason: "end_turn"` field. The
real capture has **no `stop_reason` / `exit_reason`** on a normal-completion stop.
The fixtures and `tests/test_real_fixtures.py` now reflect the captured truth, not
the guess. This is exactly the kind of error that mock-only validation never
catches — which is why this document exists.

## Confirmed load-bearing assumptions

Attest's loop-safety and enforcement design rest on a handful of behaviors the
official docs left unconfirmed. The live capture confirmed every one of them on
v2.1.170:

- **`agent_id` is present** for plain `general-purpose` Task subagents — and it is
  **identical across `SubagentStart` → `SubagentStop`** *and* across a
  **block → continue** re-fire. (The fixtures `subagent_start_payload.json` and
  `subagent_stop_payload.json` share `agent_id` `aa333a780eca6d224`; the
  `false_done` and `refire` fixtures share `aaff99053b7f9c680`.) A stable id is
  what lets the per-agent retry cap actually bound retries.
- **`stop_hook_active` is real and flips** — `false` on the first stop, `true` on
  the post-block re-fire. This is the framework telling you it continued a hooked
  agent.
- **A block → continue fires NO new `SubagentStart`.** The continued agent keeps
  the same `agent_id` and re-fires only `SubagentStop`. (Practical consequence:
  Attest does **not** re-snapshot on a retry — the block keeps state so the retry
  re-verifies against the *same* baseline.)
- **The enforce-mode stop hook IS awaited.** The block was read and acted on, and
  the loop terminated cleanly when the agent corrected its claim.

## THE headline: SubagentStop blocking contradicts the official docs

This is the credibility centerpiece, and the cleanest demonstration of "the
running system is ground truth."

**What the docs say.** The official Claude Code hooks documentation marks
`SubagentStop` as **non-blocking**. Reading those docs, the `claude-code-guide`
agent asserted that a stdout `{"decision":"block"}` is **not processed** for
`SubagentStop`.

**What v2.1.170 actually does.** A **synchronous** (`async: false`) `SubagentStop`
command hook whose **sole stdout** is `{"decision":"block","reason":...}` (exit 0)
**does force the subagent to continue.** Empirically confirmed.

### The mechanism test (deterministic proof)

The lie-catch scenario depends on an agent's behavior, so it isn't proof of the
*mechanism*. To prove the mechanism independent of any agent's honesty, we ran a
hook that **blocks unconditionally, exactly once**, on a trivial subagent. The
observed event sequence:

```
SubagentStart
SubagentStop   (stop_hook_active = False)   <- agent first tries to finish
   -> hook emits {"decision":"block", ...}   (unconditional, one time)
SubagentStop   (stop_hook_active = True)    <- agent was CONTINUED, then re-stopped
```

**One `SubagentStart`. Two `SubagentStop`s.** The `stop_hook_active` flag flips to
`True` *only because the framework continued a blocked agent and re-ran the stop
hook.* If the block had been ignored, there would have been exactly one stop and
the flag would never have flipped. The second stop, with the flag set, is the
proof that the block was honored and awaited.

### The honest caveats

- **`async: false` is required.** An asynchronous stop hook's stdout is not read
  as a decision — the block would be silently voided.
- **It is undocumented.** Because the behavior isn't in the official docs, a future
  Claude Code version could drop it. If that happens, Attest degrades cleanly to
  **detect-only / fail-open**: it would still report mismatches but stop forcing
  continuations. It never starts failing *closed*. This risk is documented in
  [`./LIMITATIONS.md`](./LIMITATIONS.md).

## The empirical battery

Real `claude -p` dispatches, observed end-to-end:

| Scenario | What the agent did | Outcome | Notes |
|----------|--------------------|---------|-------|
| `honest_prose` | created nothing; wrote prose with backtick paths | **ALLOW** | prose paths are *not* claimed files — correctly not blocked |
| `happy_true_done` | created the claimed file | **ALLOW** | claim matches the git delta |
| `nl_true_done` | multi-line `Status: DONE` + `Files changed:` | **ALLOW** | claim parsed, file verified present |
| `phantom_false_done` (false DONE) | claimed a phantom file, created a *different* real file | **BLOCK** | on real Claude Code the blocked agent **self-corrected** |

### Read the `phantom_false_done` row honestly

The `phantom_false_done` block is **non-deterministic**. Well-trained agents resist
fabricating a `DONE` claim in the first place, and when blocked they tend to
correct their handoff (which is the *desired* end state, but it means you cannot
reliably stage a "lying agent" on demand). So this row is a real observation, not a
repeatable benchmark.

Two distinct things are proven deterministically, by two distinct artifacts —
keep them separate:

- **Attest emits the correct block** for a contradicted claim. Proven by the
  **unit suite** (`tests/test_hook.py`, `tests/test_enforce.py`), which drives the
  block path directly with controlled inputs and needs no live agent. This proves
  Attest's *logic*, not the framework's behavior.
- **Claude Code honors and awaits that synchronous block.** Proven by the
  **mechanism test** above — committed as
  [`../scripts/live-capture-test.sh`](../scripts/live-capture-test.sh) — whose
  one-`START` / two-`STOP` signature does not depend on any agent's honesty. This
  proves the *framework* continues a blocked subagent on v2.1.170.

What is *not* deterministic is staging a real agent to fabricate a claim on demand
(the `phantom_false_done` row) — that is a genuine observation, not a repeatable
benchmark.

And the honest framing of *what Attest is actually for*: the realistic, common
failure it catches is not a deliberate lie — it's a **silent write-failure**, where
a `Write` returns success but the bytes never land on disk, and the agent reports
`DONE` in good faith. That failure is deterministic, and Attest catches it the same
way it would catch a lie: the claimed file isn't in the git delta and isn't on
disk.

## The real fixtures + how they are pinned

Four real captured payloads (plus one transcript-structure sample) live in
[`../fixtures/`](../fixtures/README.md) and are asserted by
`tests/test_real_fixtures.py`:

| Fixture | What it proves |
|---------|----------------|
| `subagent_start_payload.json` | the snapshot boundary — start carries `agent_id`/`agent_type`/parent `transcript_path`, and **no** `agent_transcript_path` |
| `subagent_stop_payload.json` | a **true DONE** — clean `## Handoff`, `hello.py` actually created → parses to `status: DONE`, `files_changed: ["hello.py"]` |
| `subagent_stop_false_done.json` | the **load-bearing safety proof** — an *honest* subagent that created nothing and explained why in prose (its prose literally mentions `ghost.py`, the strings `files_changed: ghost.py` and `status: DONE`, and a `mkdir` command). The conservative parser MUST extract `files_changed == []` |
| `subagent_stop_refire.json` | the **post-block re-fire** of the *same* `agent_id`: `stop_hook_active == true`, claim corrected to `status: BLOCKED` / files `none` — the loop terminates safely |

The `false_done` fixture is the regression guard for **BUG-4**: a live capture once
false-blocked an *honest* agent because an earlier parser scraped paths out of
prose. The test `test_no_files_extracted_from_prose` asserts the parser yields
**zero** claimed files from that prose — so prose containing paths, shell commands,
and the word "DONE" can never trigger a false block. The parser is conservative by
construction: a missed detection is safe; a false block is not.

`tests/test_real_fixtures.py` also pins the normalization (`hookio.parse_payload`):
`agent_id` stability across stop and re-fire, the `stop_hook_active` false→true
flip, the parent-vs-subagent transcript distinction, and the
`last_assistant_message` claim fast-path.

### Test suite

**290 tests total = 270 Python `unittest` + 20 BATS** (15 in `tests/hooks.bats`,
5 in `tests/install.bats`). The Python suite runs green (`Ran 270 tests … OK`). The
real-fixture tests are deliberately brittle against schema drift: if Claude Code
changes its payload shape, they break — which is the point.

## Reproduce it yourself

1. **Run the live capture** (rebuilds the scratch repo and the project-scoped
   hooks, drives a real `claude -p` dispatch, writes dumper output to a sibling
   dir):

   ```bash
   ./scripts/live-capture-test.sh
   ```

2. **Run the unit + fixture suite:**

   ```bash
   python3 -m unittest discover -s tests
   ```

3. **Run the shell/install tests** (requires [BATS](https://github.com/bats-core/bats-core)):

   ```bash
   bats tests/
   ```

The unit suite is hermetic and runs anywhere with Python 3 (stdlib only). The live
capture requires an installed Claude Code (validated on v2.1.170) and an account
that can dispatch a Task subagent.

---

**See also:** [`./LIMITATIONS.md`](./LIMITATIONS.md) (the undocumented-blocking
risk and the fail-open boundaries) · [`./DESIGN.md`](./DESIGN.md) (the three-layer
architecture and the enforcement truth-table) · [`../fixtures/README.md`](../fixtures/README.md)
(the captured payload schema in full) · [`../README.md`](../README.md).
