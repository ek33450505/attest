# attest

[![CI](https://github.com/ek33450505/attest/actions/workflows/ci.yml/badge.svg)](https://github.com/ek33450505/attest/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

> **"DONE" is a claim, not proof. Grade the act, not the output.**

A local, deterministic, **zero-LLM** Claude Code hook that verifies a subagent's
`Status: DONE` / `## Handoff` claim against the **real git working-tree delta** —
and, opt-in, **blocks** a `DONE` whose claimed files never actually landed on disk.
It adds no tokens, cannot itself hallucinate, and **fails open on every doubt**.

## The insight

Every eval and observability tool grades the *output* or asks the model "are you done?" —
self-report, the **one signal you cannot take on trust**. The git tree is the only ground
truth. So Attest verifies the *act*, not the output: it diffs the working tree before and
after the subagent runs and checks whether the files a `DONE` claims it changed actually
changed. Deterministic, read-only, and — because it never calls a model — it cannot
fabricate its own verdict.

The real target is not a lying agent (well-trained agents resist that). It is the
**silent write-failure**: a `Write` tool call that returns success but never lands on disk,
behind a confident `Status: DONE`.

## Evidence

Attest was built proof-first and validated against **real Claude Code v2.1.170** — not mocks.

- **325 tests** — 304 Python (`unittest`) + 21 BATS (16 in `tests/hooks.bats`, 5 in
  `tests/install.bats`). The Python suite runs green (`Ran 304 tests … OK`).
- **Real captured payloads ship in the repo.** Four sanitized `SubagentStart`/`SubagentStop`
  fixtures plus a transcript sample live in [`fixtures/`](./fixtures/) and are pinned byte-for-byte
  by `tests/test_real_fixtures.py` — including the load-bearing safety case: an *honest* subagent
  that created nothing and explained why in prose (mentioning a path, a `files_changed:` line, and
  even the word `DONE`) from which the conservative parser correctly extracts **zero** claimed files.
- **A live empirical battery on real Claude Code.** End-to-end `claude -p` dispatches confirmed
  the boundary cases: an honest agent that changed nothing was correctly **not** blocked; a
  multi-line `Status: DONE` with a real file was allowed and parsed; and a genuine false `DONE`
  was **blocked — and the blocked subagent self-corrected.**

> **Honesty about that last result:** the self-correcting block is **non-deterministic** —
> well-trained agents resist fabricating claims, so the live "lie → block → fix" path can't be
> relied on to reproduce. The **deterministic** proof of blocking is the *mechanism test*
> (below) plus the unit suites (`tests/test_hook.py`, `tests/test_enforce.py`). Enforcement is
> **off by default**.

See **[docs/VALIDATION.md](./docs/VALIDATION.md)** for the full evidence dossier, and
**[scripts/live-capture-test.sh](./scripts/live-capture-test.sh)** to re-run the capture
harness yourself against your own Claude Code install.

## How it works

Two hooks, three pure layers, one source of truth (git):

1. **`SubagentStart`** snapshots the git working tree — `{path: sha256}` for every file that
   differs from `HEAD` (modified / added / untracked / deleted).
2. **`SubagentStop`** recomputes the delta, parses the subagent's final claim
   (`## Handoff` block first, then an anchored `Status:` / `Files changed:` fallback — *never*
   scraped from prose), and evaluates whether it is a **proven false `DONE`**: status `DONE`,
   a claim actually present, and a claimed file that is absent from the delta **and** not
   present on disk.
3. In **enforce mode** only, a proven false `DONE` is **blocked** — Claude Code feeds the reason
   back and the same subagent is forced to **continue and fix it**.

The claim parser is conservative by construction: a missing or prose-only claim yields
`status=None` and is **never** treated as a false `DONE`. A path mentioned in prose never
becomes a claimed file.

### What the report looks like

In detect mode (the default), the stop hook prints to stdout after every subagent completes:

```text
attest: stop: <key>: CLAIMED [a.py] OBSERVED [a.py] -> OK [source=payload]
attest: stop: <key>: CLAIMED [a.py, b.py] OBSERVED [a.py] -> MISMATCH: b.py claimed-but-unchanged (would block in enforce mode) [source=payload]
attest: stop: <key>: CLAIMED [a.py] OBSERVED [a.py, c.py] -> SCOPE_CREEP: c.py observed-but-unclaimed [source=payload]
attest: stop: <key>: claim source=none — cannot verify (never treating as false DONE)
```

`<key>` is the agent identifier; `source=payload` or `source=transcript` shows where the
claim was read from. In enforce mode (`ATTEST_ENFORCE=1`) the human-readable lines move to
stderr and the `(would block in enforce mode)` cases become real blocks.

## Install

Hooks take effect on a **new session** — restart Claude Code after installing. The
`SubagentStop` hook **must be synchronous** (`async: false`); an async hook cannot block.

**As a Claude Code plugin (recommended):**

```bash
/plugin marketplace add https://github.com/ek33450505/attest
/plugin install attest@attest
```

**Via Homebrew (CLI only — hooks still need the plugin or `install.sh`):**

```bash
brew tap ek33450505/attest
brew install attest
```

**Manually** — `install.sh` idempotently merges the two hooks into
`~/.claude/settings.json` (backing it up first), preserving any existing hooks:

```bash
./install.sh            # install (async:false, off by default)
./install.sh --uninstall # remove only attest's entries
```

Full details, overrides, and uninstall semantics: **[docs/INSTALL.md](./docs/INSTALL.md)**.

## Usage (CLI)

The CLI runs the same deterministic core standalone (stdlib only, Python 3):

```bash
# Take a before-snapshot of the working tree
python -m attest snapshot --repo /path/to/repo > before.json

# (run your agent / make changes)

# Verify a completion claim against the snapshot + current tree
python -m attest verify --claim-file agent-output.md --before before.json --repo /path/to/repo

# Version
python -m attest --version
```

`verify` prints the verdict as JSON; it exits 0 on success (and `1` on input
errors such as a missing file or a non-git repo) and never emits a block decision.

## Enforcement (opt-in)

Enforcement is **off by default**. Set `ATTEST_ENFORCE=1` to let Attest **block** a proven
false `DONE`. It blocks **only when every one** of these holds — any doubt returns *allow*
with a `reason_code`:

- enforcement is on (`ATTEST_ENFORCE=1`);
- the stopping agent's `agent_type` is in `ATTEST_ENFORCE_AGENTS` — when that allowlist is set (empty/unset imposes no agent-type restriction);
- a unique `agent_id` is present;
- the claim is a **refined** false `DONE` (status `DONE`, claim present, and a claimed file
  that is absent from the git delta **and** not on disk under any resolution);
- the git delta is **reliable** — both snapshots read git without error;
- the tree was **clean** at the agent's start (the delta is cleanly attributable);
- the per-agent retry cap is not yet reached (`block_count < ATTEST_MAX_RETRIES`);
- the session-wide backstop is not yet reached (`session_blocks < ATTEST_SESSION_BLOCK_CEILING`);
- `stop_hook_active` is not set (a subtractive fast-path — it can only *suppress* a block).

Otherwise: **allow**.

**It never blocks on doubt.** A non-git directory, a git error, a dirty start tree, a claimed
file already present on disk, a basename-matched changed file, a `.gitignore`'d or identical
rewrite, a missing/prose-only claim, a missing snapshot, an absent `agent_id`, a failed counter
write, or any internal exception — all **allow** the stop. Fail-open on doubt; fail-closed only
on proof.

**It cannot loop.** A per-agent retry cap and a session-wide ceiling bound retries, and a block
is emitted **only after both counters durably commit** — if either write can't be confirmed, the
block is suppressed rather than repeated (an *unrecorded* block is what loops). The hook shim
always exits 0; the block travels via pure stdout JSON
(`{"decision":"block","reason":"…"}`), never the exit code.

| Env var | Default | Meaning |
| --- | --- | --- |
| `ATTEST_ENFORCE` | off | `1` enables blocking; anything else (unset/`0`/`true`/`yes`) is detect-only |
| `ATTEST_ENFORCE_AGENTS` | (unset) | comma-separated `agent_type` allowlist; empty/unset = all agents eligible. When set (e.g. `code-writer,bash-specialist`), only those agent types are ever blocked — every other agent fails open. |
| `ATTEST_MAX_RETRIES` | `1` | per-agent blocks before failing open (`0` = on but never blocks — kill switch) |
| `ATTEST_SESSION_BLOCK_CEILING` | `10` | session-wide block backstop |
| `ATTEST_STATE_DB` | `~/.attest/state.db` | snapshot + counter store |
| `ATTEST_CAPTURE` | off | `1` dumps real payloads + transcripts (normalized + raw) to `fixtures/captured/` |
| `ATTEST_CAPTURE_DIR` | — | redirect capture writes (keeps dumps out of the repo tree) |
| `ATTEST_PYTHON` | `python3` | python binary the hook shims invoke |
| `ATTEST_SETTINGS` | `~/.claude/settings.json` | `install.sh` only — target settings file |
| `CAST_DB_PATH` | `~/.claude/cast.db` | mirror verdicts to a CAST `attestations` table — only if that DB file already exists (best-effort) |

## The headline finding

> **The official Claude Code docs mark `SubagentStop` as non-blocking.** On **v2.1.170**, that
> is empirically false: a **synchronous** (`async:false`) `SubagentStop` command hook whose sole
> stdout is `{"decision":"block","reason":…}` (exit 0) **does** force the subagent to continue.
>
> Proven by a deterministic **mechanism test** — a hook that blocks unconditionally exactly once on
> a trivial subagent produced `START → STOP(stop_hook_active=false) → [block] → STOP(stop_hook_active=true)`:
> one start, two stops. The flag flips `true` *only* because the framework continued a blocked agent.
> That is Attest proving its own thesis — documentation is a claim; the running system is ground truth.
>
> **Caveat:** this behavior is undocumented. `async:false` is required (async voids the block), and a
> future Claude Code version could drop it — in which case Attest degrades to detect-only / fail-open.
> Details: **[docs/VALIDATION.md](./docs/VALIDATION.md)**.

## What it does not do

Attest checks **one** thing deterministically: did the files a `DONE` claims it changed actually
land in the git tree. It does **not** judge correctness, run your tests, or detect semantic
wrongness. (`ran_tests` is parsed but informational — it never gates a block.)

## More

- **Limitations & honest trade-offs** — [docs/LIMITATIONS.md](./docs/LIMITATIONS.md)
- **Architecture & design rationale** — [docs/DESIGN.md](./docs/DESIGN.md)
- **License** — MIT
