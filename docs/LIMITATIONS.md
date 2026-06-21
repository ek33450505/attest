# Attest — Limitations

> Version 0.1.0 · validated against Claude Code v2.1.170

Attest verifies one thing, deterministically: **did the files a `DONE` claim says
changed actually land in the git working tree.** Everything it does *not* do is
listed below, in plain terms, with the reason. Read this as a design statement,
not an apology: nearly every limitation here is a place where Attest deliberately
chose to **allow** rather than risk a false block. The governing rule is
**fail-open-on-doubt, fail-closed-on-proof** — a missed detection is safe; a
false block is not.

For the architecture behind these choices see [./DESIGN.md](./DESIGN.md); for the
live evidence behind the empirical claims see [./VALIDATION.md](./VALIDATION.md).

---

## 1. It checks landing, not correctness

Attest compares the *claimed* `files_changed` against the *observed* git delta and
asks a single question: are any claimed files absent from the delta? That is the
whole verdict (`attest/verdict.py` → `evaluate`, where `false_done` is `True`
**iff** `status == 'DONE'` **and** the claim source is not `none` **and**
`claimed_but_unchanged` is non-empty).

It does **not**:

- judge whether the change is *correct*;
- run your tests, your linter, or your build;
- check whether anything passes or fails;
- detect semantic wrongness, regressions, or partial/incorrect edits.

A file that was edited badly still counts as "landed." Attest can tell you an
agent *touched* `auth.py`; it cannot tell you the auth logic is right.

`ran_tests` is parsed from the claim (`attest/claim.py`) but is **informational
only** — it is recorded and never consulted by `verdict.evaluate` or
`enforce.decide`. It does not gate a block. Treat Attest as a landing check that
sits *upstream* of your real test suite, never a replacement for it.

## 2. Detect-only by default

Out of the box, Attest blocks nothing. Enforcement is strictly opt-in:
`enforce.enforcement_enabled` returns `True` only when `ATTEST_ENFORCE == '1'`
(unset, empty, `0`, `true`, `yes` are all OFF). With enforcement off,
`enforce.decide` returns `allow('ALLOW_NOT_ENFORCING')` on every path and the hook
only prints a detect-mode report to stdout.

This means a fresh install observes and reports but never interrupts a subagent.
You must consciously turn enforcement on, and even then several further gates
(below) can still hold the block open. The bias is conservative by construction:
you opt in to blocking, never out of it.

## 3. A dirty / ambiguous tree is detect-only

If the working tree already had uncommitted changes at the moment the subagent
started, Attest cannot cleanly attribute the later delta to *that* agent — other
agents, the user, or prior work could have produced it. `gitdelta.delta` sets
`ambiguous=True` whenever the start snapshot was non-empty *and was read without
error* (an errored start snapshot is handled by the reliability path in §9, not the
ambiguity path), and `enforce.decide`
then returns `allow('ALLOW_AMBIGUOUS')`.

Practical consequence: Attest enforces most reliably from a **clean tree**. On a
mid-session, already-modified tree it degrades to detect-only and reports the
ambiguity rather than asserting false precision. This is intentional — attributing
a delta we cannot cleanly separate would be the exact kind of false block the
whole design avoids.

## 4. The single-line slash form is not parsed for files

The natural-language parser recognizes a files key **only at start-of-line**, and
its value terminates at a newline, `;`, or `|` — *not* at `/`, because real paths
contain directory slashes (`attest/claim.py`, `_FILES_KEY_SOL_RE` captures
`[^\n|;]*`). So this claim:

```text
Status: DONE / Files changed: a.txt
```

detects the **status** but extracts **no file** — the `/` is not a separator and
the rest of the line is not a start-of-line files key. The file is therefore not
verified, and with no claimed file there is nothing to contradict, so it cannot
trigger a block.

This is a deliberate trade-off from the BUG-4b hardening: separator anchoring
(`/ ; |`) was removed because it false-matched ordinary prose like
`"The previous implementation; status: DONE was broken."` To have your files
actually verified, use a multi-line form or a `## Handoff` block:

```text
## Handoff
status: DONE
files_changed: a.txt, b.py
```

```text
Status: DONE
Files changed: a.txt
```

A missed detection is safe; a false block is not.

## 5. Agents resist lying — the real target is silent write-failures

Attest's headline scenario — "an agent claims a file it never wrote" — is **rare
in practice**, because well-trained agents resist fabricating a `DONE`. We confirmed
this with a live capture: an honest subagent that created nothing *and said so*,
writing prose that literally contained `ghost.py`, the string `files_changed:
ghost.py`, the word `DONE`, and a `mkdir` command, was correctly **not blocked**.
The conservative parser extracted zero claimed files from that prose. (This is the
load-bearing safety proof, pinned by `fixtures/subagent_stop_false_done.json` and
`tests/test_real_fixtures.py`; see [./VALIDATION.md](./VALIDATION.md).)

So the deliberate-lie case, while it exists, is **non-deterministic** — you cannot
reliably reproduce it on demand because the model fights you. The *deterministic*
proof that the block path works comes from the unit tests (`tests/test_hook.py`,
`tests/test_enforce.py`) and the mechanism test, not from coaxing a real agent to
lie.

The real-world value of Attest is therefore the quieter failure: a **silent
write-failure** — a `Write`/`Edit` that returns success to the agent but never
lands on disk (interrupted tool call, swallowed error, wrong path, lost
worktree). The agent honestly believes it is `DONE` and honestly reports the file;
the file simply is not there. That mismatch is exactly what the git delta exposes.

## 6. SubagentStop blocking is officially undocumented

Enforcement depends on a behavior the official Claude Code documentation says is
**not** possible: a synchronous (`async:false`) `SubagentStop` hook whose sole
stdout is `{"decision":"block","reason":...}` forcing the subagent to continue.
The docs mark `SubagentStop` as non-blocking. On **v2.1.170** it empirically *does*
block — confirmed by a deterministic mechanism test (one `START`, two `STOP`s, with
`stop_hook_active` flipping `True` only on the post-block re-fire). The full
evidence is in [./VALIDATION.md](./VALIDATION.md).

The honest risk: this behavior is **undocumented and version-dependent**.

- `async:false` is **required** — an async hook's stdout is not read as a decision,
  so the block is voided.
- If a future Claude Code version removes or changes this behavior, Attest's block
  signal silently stops being honored. Because the hook always exits 0 and emits
  detect output regardless, Attest **degrades gracefully to detect-only /
  fail-open** — it never wedges your agents — but the enforcement guarantee
  evaporates. Re-validate against your installed version before relying on blocking.

## 7. Path-form fail-open cases

Even when a `DONE` is contradicted, Attest **removes** a claimed file from the
blockable set under several conditions, all of which only ever *suppress* a block
(`attest/hook.py` `on_stop`). A claimed file is dropped if **either**:

- **it exists on disk** — `gitdelta.path_on_disk` resolves the claimed path against
  the git toplevel, against the subagent's payload `cwd`, against the process cwd
  as a last resort, and as an absolute path.
  This catches gitignored writes, byte-identical rewrites (no delta but the file is
  real), prior-existing work, and cwd-relative claims from a subdirectory; **or**
- **an observed-changed file shares its basename** — e.g. the agent claimed
  `app.py` while `src/app.py` actually changed, or reported a bare basename from a
  subdirectory `cwd`. The work clearly happened; only the *path form* differs.

Both refinements are strictly subtractive: `refined_false_done = false_done AND
blockable non-empty`, and the two checks can only shrink `blockable`, never grow
it. A path-reporting imprecision can therefore never block real work.

What is **not** specially handled (and biases toward allowing):

- **Case-insensitive filesystems** — `path_on_disk` relies on `os.path.exists`, so
  on macOS/Windows a differently-cased name may read as present (over-allow). The
  basename comparison itself is case-sensitive, so the two mechanisms do not agree
  on case — but the net effect stays on the safe side: allow.
- **Submodule paths** — `git status` at the toplevel reports a submodule as a
  single path, not its internal files; changes *inside* a submodule are not
  attributed file-by-file.

In every one of these gaps the resolution is the same: when in doubt, allow.

## 8. Concurrency and fallback-key ambiguity

Attest v1 attributes deltas **per working tree**, not per agent in isolation.

- **Shared tree.** Concurrent subagents operate on one working tree, so the delta
  since a given agent's start is the *union* of everything that changed — possibly
  including a sibling agent's work. v1 does not try to slice this apart; it computes
  the union and, because the tree was non-clean at start for the later agent, marks
  the result **ambiguous** (see §3) rather than asserting precision. Worktree- or
  branch-isolated per-agent attribution is future work.
- **Fallback key collision.** The state store keys snapshots on `agent_id` when
  present, but falls back to `"{agent_type}:{session_id}"` when it is absent
  (`attest/state.py` `agent_key`). Two concurrent **same-type** agents in one
  session would then share a key and overwrite each other's snapshot. On the live
  v2.1.170 capture `agent_id` *was* present for plain Task subagents and stable
  across start→stop and block→continue, so the fallback is a safety net, not the
  normal path — but it is a real collision surface for unusual launch
  configurations. (When no `agent_id` is available at all, `enforce.decide` returns
  `allow('ALLOW_NO_AGENT_ID')` and never blocks.)

## 9. Reliability gating

Attest never confuses "nothing changed" with "I couldn't read git." `gitdelta.delta`
sets `reliable=True` **only** when *both* the start and stop snapshots read git
without an `_error` — it is derived solely from snapshot errors and is **never**
inferred from `len(changed)`. An empty `changed` set is genuinely ambiguous on its
own (it means *either* "the agent changed nothing" *or* "git was unreadable"), so
the two are kept strictly separate.

Consequences:

- A **non-git directory**, a missing `git` binary, or any `git status` failure makes
  the delta **unreliable**, and `enforce.decide` returns
  `allow('ALLOW_DELTA_UNRELIABLE')` — enforcement fails open.
- A clean run where the agent legitimately changed nothing yields an empty *but
  reliable* delta, which is treated as real data, not an error.

This is the difference between "I checked and there's nothing" and "I couldn't
check" — Attest only ever acts on the former.

---

## The through-line

Every limitation above is the same decision applied to a different surface:
**when Attest is not certain, it allows.** Detect-only by default; allow on a dirty
tree; allow when git is unreadable; allow when a claimed file might exist under a
different path or case; allow when the claim is missing or unparseable; allow when
there is no stable agent identity; allow when a counter write cannot be confirmed.
A block is emitted *only* on a positively proven, refined false `DONE`, against a
clean and reliable tree, with durable loop-safety counters committed first.

If you need Attest to catch *more*, the levers are: run from a clean tree, use a
`## Handoff` block or multi-line claims, and turn on `ATTEST_ENFORCE=1`. But the
ceiling is deliberate — Attest would rather miss a questionable case than wrongly
block honest work.

See also: [./DESIGN.md](./DESIGN.md) · [./VALIDATION.md](./VALIDATION.md) ·
[../README.md](../README.md)
