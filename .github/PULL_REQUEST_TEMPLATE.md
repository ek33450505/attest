<!--
Thanks for contributing to attest. attest is a local, deterministic, zero-LLM hook —
its value is that it can be trusted not to lie and not to wedge the session. Please keep
that bar. Describe your change, then confirm each item below.
-->

## What does this change do?

<!-- A short, plain description of the change and why. Link any related issue (e.g. Closes #123). -->

## Why?

<!-- Motivation / context. If this changes observable behavior, say so explicitly. -->

## Contributor checklist

- [ ] All 290 tests pass locally — Python (`python3 -m unittest discover -s tests -p 'test_*.py'`, expect `Ran 270 tests ... OK`) and BATS (`bats tests/*.bats`, expect 20 `ok`).
- [ ] CI is green on this branch.
- [ ] Fail-open behavior is preserved — the hook always exits 0, can never wedge or stall the parent session, and blocks only on proof (a proven false DONE). Any doubt or internal error falls open.
- [ ] No new runtime dependencies — stdlib-only Python 3, zero network, no third-party packages added.
- [ ] CHANGELOG `[Unreleased]` section updated.
- [ ] Docs updated if behavior changed (README / docs/DESIGN.md / docs/INSTALL.md / docs/LIMITATIONS.md / docs/VALIDATION.md as applicable).
