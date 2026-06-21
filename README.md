# attest

**Your agents lie about DONE. Grade the act, not the output.**

A Claude Code hook that verifies a subagent's `Status: DONE` / `## Handoff` claim against the **real git working-tree delta**, and flags any claimed file that never actually changed.

## The insight

Every eval and observability tool grades the *output* or asks the model "are you done?" — self-report, the thing that lies. The git tree is the only trustworthy ground truth. Verify the act, not the output. Deterministically. Zero LLM tokens.

## Status

Phase 1a — pure core + unit tests. Hook wiring is a later phase.

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
