# Contributing to Attest

Thanks for your interest in improving Attest. This is a small, deliberately
conservative project: a local, deterministic, zero-LLM Claude Code hook that
verifies a subagent's `DONE` claim against the real git working-tree delta. The
bar for changes is correctness and restraint, not feature surface.

Please read [`docs/DESIGN.md`](docs/DESIGN.md) for how the hook works and
[`docs/VALIDATION.md`](docs/VALIDATION.md) for the evidence dossier behind every
load-bearing claim before opening a non-trivial PR.

## Ethos (read this first)

Attest grades the act, not the claim. "DONE is a claim, not proof." Three
principles govern every change:

1. **Verify the act, not the claim.** The hook compares what an agent *says* it
   did against the actual git delta and the filesystem. Never trust prose.
2. **Deterministic and zero-LLM.** No network calls, no model invocations, no
   nondeterminism. Same inputs must always produce the same verdict. The runtime
   is stdlib-only Python 3 with no third-party dependencies.
3. **Fail open on every doubt; fail closed only on proof.** A block happens
   *only* on a proven false DONE (status `DONE`, a claim present, and a claimed
   file absent from both the delta and disk). Anything ambiguous passes through.

A contribution must **NEVER** make the hook able to wedge the parent session,
and must **NEVER** block on anything short of proof. Concretely:

- The hook always exits `0`. A block is delivered as a stdout JSON payload
  (`{"decision":"block", ...}`) with **exit code 0** — never via `exit 2`, never
  via a non-zero exit. A broken, slow, or crashing hook must degrade to a no-op,
  not a wedged session.
- The bash shims (`hooks/attest-subagent-start.sh`, `hooks/attest-subagent-stop.sh`)
  use `set +e` and are fail-open by construction. Keep them that way.
- The loop-safety retry cap must remain: a second consecutive false DONE must not
  be able to block forever.

If a change weakens any of these guarantees, it will not be merged regardless of
what else it improves.

## Dev setup

There is **no build step and no install step** to develop or run the tests.

- **Python 3** (stdlib only) — already on your machine. No `pip install`, no
  virtualenv required to run the suite.
- **bats** — needed only for the shell tests:
  - macOS: `brew install bats-core`
  - Debian/Ubuntu: `apt-get install bats`

Clone the repo and you are ready to run the tests.

## Running the tests

Run both suites **from the repository root**.

The quickest way is:

```sh
make test
```

This runs the Python suite first, then BATS. It exits non-zero if either suite
fails. The individual targets are also available (`make test-py`, `make test-bats`).

The raw commands — mirrored exactly by `make test` — are:

Python (282 tests):

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Expect `Ran 282 tests ... OK`.

BATS (20 tests):

```sh
bats tests/*.bats
```

Expect 20 `ok` lines.

That is **302 tests total** (282 Python unittest + 20 BATS). CI runs both suites
on every push and pull request; a PR cannot merge with a red suite.

## Linting

The shell scripts (`hooks/*.sh`, `install.sh`, `scripts/*.sh`) are linted with
[ShellCheck](https://www.shellcheck.net/) in CI. To run it locally:

```sh
shellcheck hooks/*.sh install.sh scripts/*.sh
```

This must pass with **no findings**. The hook shims deliberately use `set +e` and
a fail-open `A && B || C` idiom when resolving their own directory; that single
SC2015 case carries a narrowly-scoped `# shellcheck disable=SC2015` with a
justifying comment at the line. Do not add blanket suppressions — disable a
specific code at a specific line, with a comment explaining why.

## Test conventions

These rules exist because the hook touches `$HOME` and the live runtime in
production. Tests must never have side effects on the real environment.

- **BATS tests MUST isolate `$HOME`.** Use `setup_temp_home` / `teardown_temp_home`
  from [`tests/helpers/setup.bash`](tests/helpers/setup.bash) so the test operates
  on a throwaway temp HOME. A test must **never** read from or write to the real
  `~/.claude`.
- **BATS tests MUST shim notifications.** Any script path that could emit a desktop
  notification, play a sound, or open a URL/app (`osascript`, `open`, etc.) must be
  PATH-shimmed to a no-op stub in `setup()`. Tests produce zero real GUI side
  effects.
- **Python tests self-configure git identity** inside their own temp repos
  (`git -C <tmp> config user.email/user.name`). Do not rely on a global git
  identity, and do not run git commands against any repo outside the test's temp
  directory. This keeps CI green on a machine with no global git config.

## The conservative-parser invariant (do not break this)

The claim parser is the most safety-critical code in the project. It reads the
subagent's final message and extracts a claim — preferring the `## Handoff`
block, then falling back to an anchored `Status:` / `Files changed:` form. It
**never** scrapes file paths out of free-form prose.

Two invariants every parsing change must preserve:

1. **A value scraped from prose must NEVER become a claimed file.** A path,
   command, or the word "DONE" appearing in an agent's narrative explanation must
   yield zero claimed files. An honest agent that did nothing and explained why in
   prose must produce `files_changed == []`, so it can never trigger a false block.
2. **`status` of `None` is never a false DONE.** If no anchored status was parsed,
   there is no DONE to falsify — the verdict must pass through.

Both invariants are pinned by regression tests (see
[`tests/test_real_fixtures.py`](tests/test_real_fixtures.py),
`TestRealFalseDoneRegression`, captured from real Claude Code v2.1.170 output).
New parsing code must keep these tests green and add coverage for any new shape it
introduces.

## Adding a captured fixture

When you capture a new real payload that exposes a behavior worth pinning:

1. **Sanitize it.** Rewrite the local username and any real repo path to disposable
   placeholders (e.g. `/tmp/attest-test-repo`, `/Users/dev`). Opaque run ids
   (`agent_id`, `session_id`, uuids) are ephemeral and may stay verbatim. Never
   commit anything from `fixtures/captured/` — that directory is gitignored because
   it may hold session content.
2. **Drop the sanitized payload under [`fixtures/`](fixtures/)** and describe it in
   [`fixtures/README.md`](fixtures/README.md).
3. **Pin it byte-for-byte in [`tests/test_real_fixtures.py`](tests/test_real_fixtures.py).**
   These tests assert the parser and hook normalization against ground truth — if
   Claude Code changes its payload schema, they break, which is the point.

## Pull request process

1. **Open an issue first** for any non-trivial change (new behavior, parser
   changes, schema assumptions). It is cheap to discuss the approach before the
   code, and Attest's restraint bar means some otherwise-reasonable features will
   be declined. Typo and doc fixes can go straight to a PR.
2. **Keep changes scoped.** One logical change per PR. No "while I'm here" edits to
   unrelated files. If you spot something out of scope, surface it in the issue or
   PR description rather than fixing it inline.
3. **Keep all 302 tests green** and add tests for new behavior — especially any
   change to the parser or the enforce/block path. CI must pass before merge.
4. **Update the `[Unreleased]` section of [`CHANGELOG.md`](CHANGELOG.md)** under the
   appropriate heading (`Added` / `Changed` / `Fixed`).
5. **Write imperative commit messages** (`Add ...`, `Fix ...`, `Correct ...`), and
   keep each commit a single logical unit.

## Where to read more

- [`docs/DESIGN.md`](docs/DESIGN.md) — how the snapshot/delta/verdict pipeline
  works and why it is built this way.
- [`docs/VALIDATION.md`](docs/VALIDATION.md) — the evidence dossier: what was
  empirically verified against live Claude Code, including the SubagentStop
  blocking behavior the official docs do not promise.
