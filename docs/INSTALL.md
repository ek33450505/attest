# Installing Attest

Attest is a local, deterministic, zero-LLM Claude Code hook. It snapshots the
git working tree when a subagent starts, and when that subagent reports
`Status: DONE`, it checks whether the files the agent *claimed* to change
actually landed on disk. In enforce mode (opt-in) it can block a `DONE` whose
claimed files never appeared.

This guide covers installing the hooks, the synchronous-registration
requirement that makes blocking work, and every environment variable that tunes
behavior. For *why* the design fails open and what it deliberately does not do,
see [./DESIGN.md](./DESIGN.md) and [./LIMITATIONS.md](./LIMITATIONS.md). For the
empirical proof that `SubagentStop` can block on Claude Code v2.1.170, see
[./VALIDATION.md](./VALIDATION.md).

---

## Requirements

| Requirement | Why |
| --- | --- |
| **Python 3.9+** (stdlib only) | The hook handler is `python -m attest.hook`. No `pip install` — there are zero third-party dependencies. `install.sh` aborts if `python3` is not on `PATH`. |
| **git** | Attest computes its ground truth from `git status --porcelain` against `HEAD`. Outside a git repo, every verdict fails open (allows). |
| **Claude Code v2.1.170** | Enforcement (blocking a false `DONE`) relies on a `SubagentStop` command hook honoring a `{"decision":"block"}` stdout. This was empirically confirmed on **v2.1.170** (the exact version validated; see [./VALIDATION.md](./VALIDATION.md)). The behavior is **undocumented** and could change in a future release — if it does, enforce mode degrades cleanly to detect-only / fail-open. Detect mode works on any version. |

> Detect mode (the default) only reads. It never blocks, never writes to your
> repo, and is safe to run anywhere.

> **Windows / WSL note:** The hooks are bash scripts — both install paths register
> them as `bash ".../attest-subagent-*.sh"` (see `hooks/hooks.json` and
> `install.sh`). Native Windows without a POSIX shell on `PATH` cannot run them.
> The recommended path is to run Claude Code under **WSL** (Windows Subsystem for
> Linux) or any environment that provides `bash`, `git`, and `python3` on `PATH`,
> where Attest works normally. See also [LIMITATIONS.md §7](./LIMITATIONS.md#7-path-form-fail-open-cases)
> for the case-insensitive-filesystem caveat that applies on Windows (and macOS).

---

## Three ways to install

### a) Claude Code plugin (recommended)

```text
/plugin marketplace add https://github.com/ek33450505/attest
/plugin install attest@attest
```

The plugin ships `hooks/hooks.json`, which registers both `SubagentStart` and
`SubagentStop` automatically. The command paths resolve through the
`${CLAUDE_PLUGIN_ROOT}` variable, so there is nothing to edit by hand:

```json
"command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/attest-subagent-stop.sh\"",
"async": false,
"timeout": 30
```

The plugin sets `"defaultEnabled": false`, so installing it does **not** silently
turn on hooks — you enable it explicitly via the `/plugin` UI. The marketplace is
named `attest` and the plugin is `attest`, hence `attest@attest`.

### b) Homebrew

```bash
brew tap ek33450505/attest
brew install attest
```

One honest caveat: **Homebrew installs the CLI only — not the hooks.** The formula installs the
   `attest` command (used for `attest --version`, `attest snapshot`, `attest
   verify`). To wire the `SubagentStart`/`SubagentStop` hooks into Claude Code
   you still need the plugin (option **a**) or `install.sh` (option **c**). The
   formula's own `caveats` say exactly this.

### c) Manual `install.sh`

For users not on the plugin system, the installer merges the two hooks into
`~/.claude/settings.json`:

```bash
bash install.sh              # install (default)
bash install.sh --uninstall  # remove ONLY attest entries, preserve everything else
bash install.sh --help       # usage
```

What it does, verified against the script:

- **Idempotent.** It appends an `attest` hook entry only if no entry already
  carries that command, so re-running it will not create duplicates.
- **Preserves your existing hooks.** It reads the JSON, adds the two events
  under `hooks.SubagentStart` / `hooks.SubagentStop`, and leaves every other
  hook untouched.
- **Backs up first.** Before any write it copies your settings to
  `~/.claude/settings.json.attest.bak`.
- **Registers synchronously.** Both entries are written with `"async": false`
  and `"timeout": 30` (see the next section — this is load-bearing).
- **Honors `ATTEST_SETTINGS`.** Point it at a different settings file with
  `ATTEST_SETTINGS=/path/to/settings.json bash install.sh` (used by the test
  harness so it never touches your real config).

The installer prints a reminder that Attest runs in **detect mode by default**
and that you must **restart your Claude Code session** for the change to take
effect.

---

## The synchronous-hook requirement (important)

`SubagentStop` **must** be registered with `"async": false`.

Claude Code only reads a hook's stdout for a decision when the hook runs
synchronously. An `async` hook is fire-and-forget: its stdout is discarded, the
`{"decision":"block"}` payload is never read, and **enforcement silently
no-ops** — Attest would log a block it believes it issued while the subagent
sails right past it.

Both supported install paths already get this right:

- `hooks/hooks.json` sets `"async": false` on both events.
- `install.sh` writes `"async": false` (see `_make_entry` in the script).

If you hand-roll your own settings entry, copy the `async: false` exactly. Why
synchronous registration is *required* (and why the official docs say
`SubagentStop` cannot block at all) is documented with the deterministic
mechanism test in [./VALIDATION.md](./VALIDATION.md).

---

## Restart required

Hook registration is read when a Claude Code session starts. **Any change to
`settings.json` or to your installed plugins takes effect only after you restart
the session.** Install, then start a fresh session before expecting Attest to
fire. This applies to all three install paths.

---

## The stdout-JSON block contract

In **enforce** mode the `SubagentStop` hook's stdout must be **exactly one JSON
object** and nothing else:

```json
{"decision":"block","reason":"..."}
```

Claude Code parses the hook's *entire* stdout as a single JSON object. **Any
other byte on stdout voids the block.** A stray `echo`, a debug print, even a
trailing log line, and the decision is silently dropped.

This is why Attest splits its output streams by mode:

- **Enforce mode** — all human-readable diagnostics go to **stderr**, which the
  stop shim routes to `~/.claude/logs/attest-errors.log`. stdout carries only
  the single decision object (written by `_emit_block` as the final stdout
  action). Verified in `hooks/attest-subagent-stop.sh`
  (`2>>"$ATTEST_LOG"`, stdout flows through untouched) and in `attest/hook.py`.
- **Detect mode** — nothing is ever blocked, so the `attest: …` report lines go
  to stdout. Safe, because no decision is being parsed.

**If you write your own wrapper around the stop hook, do not echo anything to
stdout.** Let the Python handler's stdout pass through verbatim and send your
own logging to stderr or a file.

---

## Configuration (environment variables)

Enforcement is **OFF by default**. Attest observes and reports until you opt in
with `export ATTEST_ENFORCE=1`. Set these in your shell profile (they are read
at hook-fire time, inside the session's environment).

| Variable | Default | Meaning |
| --- | --- | --- |
| `ATTEST_ENFORCE` | *off* | `=1` enables blocking of proven false `DONE`s. Any other value (unset / empty / `0` / `true` / `yes`) is **detect-only**. |
| `ATTEST_MAX_RETRIES` | `1` | Per-agent blocks before Attest fails open for that agent. `0` = enforcement on but never blocks (a kill switch). |
| `ATTEST_SESSION_BLOCK_CEILING` | `10` | Session-wide block backstop, keyed on `(session_id, repo)`. Bounds a runaway even if the agent id churns. |
| `ATTEST_STATE_DB` | `~/.attest/state.db` | SQLite store for snapshots and the loop-safety counters. |
| `ATTEST_CAPTURE` | *off* | `=1` dumps the normalized payload **and** the verbatim raw stdin to `fixtures/captured/` (for debugging / fixture collection). |
| `ATTEST_CAPTURE_DIR` | *(repo `fixtures/captured/`)* | Redirects capture writes elsewhere — used by the harness to keep dumps **out of the repo tree** (otherwise they read as untracked changes and make the delta ambiguous). |
| `ATTEST_PYTHON` | `python3` | Overrides the python binary the shims invoke. |
| `ATTEST_SETTINGS` | `~/.claude/settings.json` | **`install.sh` only** — which settings file to merge into. |
| `CAST_DB_PATH` | `~/.claude/cast.db` | Optional CAST integration: if a `cast.db` exists at this path, verdicts are mirrored to an `attestations` table (best-effort; silent no-op otherwise). |

To turn enforcement on:

```bash
export ATTEST_ENFORCE=1
```

Then restart your session. Even with enforcement on, Attest blocks only when it
has *proof* — a refined false `DONE` over a reliably-read git delta, with both
loop-safety counters durably incremented. Every doubt (dirty start tree, git
unreadable, claimed file actually present on disk, missing claim, counter-write
failure) **allows**. The full allow/block truth table lives in
[./DESIGN.md](./DESIGN.md).

---

## Verifying the install

1. **Restart**, then start a session in a git repo with a clean working tree.
2. **Dispatch a subagent** (any Task subagent that ends with `Status: DONE`).
3. **Look for `attest:` report lines.** In detect mode the stop hook prints to
   stdout, for example:

   ```text
   attest: stop: <key>: CLAIMED [a.py] OBSERVED [a.py] -> OK [source=payload]
   attest: stop: <key>: CLAIMED [a.py, b.py] OBSERVED [a.py] -> MISMATCH: b.py claimed-but-unchanged (would block in enforce mode) [source=payload]
   attest: stop: <key>: claim source=none — cannot verify (never treating as false DONE)
   ```

4. **Check the log** at `~/.claude/logs/attest-errors.log`. In enforce mode the
   human-readable diagnostics land here (stdout is reserved for the decision
   JSON). Errors from the shims also log here. An empty or absent log on a clean
   run is normal.
5. **Capture raw payloads** for deeper debugging:

   ```bash
   export ATTEST_CAPTURE=1
   # (optionally redirect dumps out of your repo tree)
   export ATTEST_CAPTURE_DIR="$HOME/attest-captures"
   ```

   This dumps both the normalized payload and the verbatim raw stdin.

6. **Confirm the CLI** is reachable (Homebrew install, or via `bin/attest`):

   ```bash
   attest --version          # -> attest 0.2.0
   attest snapshot --repo .   # JSON snapshot of the working-tree delta
   ```

---

## Uninstall

**Manual install:**

```bash
bash install.sh --uninstall
```

This removes only the Attest hook entries from `settings.json` and leaves every
other hook in place. (Your pre-install backup remains at
`~/.claude/settings.json.attest.bak` if you want to restore it wholesale.)

**Plugin install:** disable or remove `attest` from the `/plugin` UI.

In both cases, **restart your session** for the removal to take effect.

---

## Related docs

- [./VALIDATION.md](./VALIDATION.md) — the live-capture evidence and the
  `SubagentStop`-can-block mechanism test on v2.1.170.
- [./DESIGN.md](./DESIGN.md) — architecture, the fail-open verdict logic, and
  the enforcement truth table.
- [./LIMITATIONS.md](./LIMITATIONS.md) — exactly what Attest does and does not
  guarantee.
- [../README.md](../README.md) — project overview.
- [../fixtures/README.md](../fixtures/README.md) — the real captured payload
  fixtures.
