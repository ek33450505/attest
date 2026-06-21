# attest

**"DONE" is a claim, not proof. Grade the act, not the output.**

A Claude Code hook that verifies a subagent's `Status: DONE` / `## Handoff` claim against the **real git working-tree delta**, and flags any claimed file that never actually changed.

## The insight

Every eval and observability tool grades the *output* or asks the model "are you done?" — self-report, the one signal you can't take on trust. The git tree is the only ground truth. Verify the act, not the output. Deterministically. Zero LLM tokens.

## Status

Phase 1 complete — deterministic core + detect-and-print `SubagentStart`/`SubagentStop` hooks (264 tests). Enforcement (blocking a proven false `DONE`) is Phase 2.

## Usage

```bash
# Take a before-snapshot
python -m attest snapshot --repo /path/to/repo > before.json

# (run your agent)

# Verify the claim
python -m attest verify --claim-file agent-output.md --before before.json --repo /path/to/repo
```

## License

MIT
