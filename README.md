# attest

**"DONE" is a claim, not proof. Grade the act, not the output.**

A Claude Code hook that verifies a subagent's `Status: DONE` / `## Handoff` claim against the **real git working-tree delta**, and flags any claimed file that never actually changed.

## The insight

Every eval and observability tool grades the *output* or asks the model "are you done?" — self-report, the one signal you can't take on trust. The git tree is the only ground truth. Verify the act, not the output. Deterministically. Zero LLM tokens.

## Status

Phases 1–2 complete (235 tests: 220 Python + 15 BATS):

- **Phase 1** — deterministic core (`claim` / `gitdelta` / `verdict`) + detect-and-print
  `SubagentStart` / `SubagentStop` hooks.
- **Phase 2** — **enforcement**: a *proven* false `DONE` can be **blocked** at `SubagentStop`
  (the subagent is forced to continue and fix it). Gated behind `ATTEST_ENFORCE=1`, **off by default**.

> Enforcement is conservative by construction and **fails open on every doubt** (see below). It is
> still **experimental**: real `SubagentStop` payloads need a live `ATTEST_CAPTURE=1` capture to
> confirm the `agent_id` and block→continue assumptions before blocking is relied on in production.

## Usage (CLI)

```bash
# Take a before-snapshot
python -m attest snapshot --repo /path/to/repo > before.json

# (run your agent)

# Verify the claim
python -m attest verify --claim-file agent-output.md --before before.json --repo /path/to/repo
```

## Hooks

Wire the two shims into `~/.claude/settings.json` (`SubagentStart` snapshots the tree;
`SubagentStop` verifies the claim):

```json
{
  "SubagentStart": [{ "hooks": [{ "type": "command", "command": "bash /path/to/attest/hooks/attest-subagent-start.sh" }] }],
  "SubagentStop":  [{ "hooks": [{ "type": "command", "command": "bash /path/to/attest/hooks/attest-subagent-stop.sh" }] }]
}
```

By default the hooks only **detect and print** — they always exit 0 and never block.

## Enforcement (opt-in)

Set `ATTEST_ENFORCE=1` to let Attest **block** a proven false `DONE`. It blocks **only when every one**
of these holds — otherwise it allows the stop:

- the claim says `Status: DONE` (a missing or unparseable claim is never a false DONE);
- a claimed file is absent from the git delta **and** not present on disk — i.e. the work never landed;
- the working tree was **clean** at the agent's start (so the delta is cleanly attributable) and git was readable;
- a unique `agent_id` is present.

On a block it emits `{"decision":"block","reason":"…"}` naming the phantom file(s); Claude Code feeds
the reason back and the subagent continues to fix it.

**It never blocks on doubt.** A non-git directory, a git error, a `.gitignore`'d or already-on-disk
file, a path reported in a different form (bare basename, sub-directory-relative), a dirty start tree,
a missing snapshot, an absent `agent_id`, or any internal error — all **allow** the stop.

**It can't loop.** A per-agent retry cap and a session-wide backstop bound retries, and a block is
emitted only after the counter durably commits — so a failed write suppresses the block rather than
repeating it.

| Env var | Default | Meaning |
| --- | --- | --- |
| `ATTEST_ENFORCE` | off | `1` enables blocking; anything else is detect-only |
| `ATTEST_MAX_RETRIES` | `1` | per-agent blocks before failing open (`0` = never block) |
| `ATTEST_SESSION_BLOCK_CEILING` | `10` | session-wide block backstop |
| `ATTEST_STATE_DB` | `~/.attest/state.db` | snapshot + counter store |
| `ATTEST_CAPTURE` | off | `1` dumps real payloads + transcripts to `fixtures/captured/` |

## License

MIT
