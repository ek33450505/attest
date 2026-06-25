# Attest — Design & Rationale

> Version 0.3.0. This document explains **why** Attest is built the way it is, for a
> contributor or a skeptic reading the source. Every behavioural claim below is grounded
> in a specific module under [`../attest/`](../attest/). Where a claim is subtle, the
> relevant function is named so you can check it against the code.
>
> For the empirical proof that the design holds on real Claude Code, see
> [`./VALIDATION.md`](./VALIDATION.md). For the honest list of what this design cannot do,
> see [`./LIMITATIONS.md`](./LIMITATIONS.md).

Attest is a local, deterministic, zero-LLM Claude Code hook. On `SubagentStart` it snapshots
the git working tree; on `SubagentStop` it compares the subagent's completion **claim**
(`Status: DONE`, `## Handoff` `files_changed`) against the **real** working-tree delta, and —
when explicitly enabled — blocks a `DONE` whose claimed files never landed on disk.

---

## 1. Core principle — verify the act, not the output

> **DONE is a claim, not proof. Grade the act, not the output.**

A subagent's final message is a self-report, and a self-report is the one signal you cannot
trust to audit itself. An agent that says `Status: DONE / files_changed: auth.py` is asserting
a fact about the world. Attest treats that assertion as a hypothesis and tests it against the
only artifact at a handoff that cannot lie: **the git working tree.**

This is not paranoia about dishonest agents. The real failure mode is more mundane and more
dangerous: a **silent write failure.** A `Write` tool call can return success and yet never
land on disk — a backgrounded write whose permission auto-denies, a path that resolved
somewhere unexpected, a tool that reported completion before the bytes were flushed. In all of
these the agent's own tool-ledger says "I wrote the file" in perfect good faith, and the agent
honestly reports `DONE`. The ledger is a claim too. The git tree is the only ground truth.

Two design consequences follow:

- **Deterministic and zero-LLM.** Attest runs `git status --porcelain=v1 -z`, hashes file
  contents with SHA-256, and parses claim text with anchored regexes
  ([`../attest/gitdelta.py`](../attest/gitdelta.py),
  [`../attest/claim.py`](../attest/claim.py)). There is no model in the loop. A verifier that
  used an LLM to judge completion would inherit exactly the hallucination problem it is
  supposed to catch. Attest **cannot itself hallucinate** — given the same tree and the same
  claim text it always returns the same verdict.
- **It judges one thing only.** Attest does not check correctness, run your tests, or detect
  semantic wrongness. It answers a single falsifiable question: *did the files a `DONE` claim
  named actually change in the git tree?* `ran_tests` is parsed but is informational only and
  never gates a block (see [`../attest/claim.py`](../attest/claim.py) `_RAN_TESTS_RE`). The
  full scope boundary is in [`./LIMITATIONS.md`](./LIMITATIONS.md).

---

## 2. Three-layer separation — SEMANTICS / POLICY / I/O

The code is split into three layers so that the entire block/allow decision is a
**unit-testable truth-table** with no side effects.

| Layer | Module | Purity | Responsibility |
|-------|--------|--------|----------------|
| **SEMANTICS** | [`../attest/verdict.py`](../attest/verdict.py) | pure | *Is this a proven false DONE?* Compares a parsed claim to an observed delta. |
| **POLICY** | [`../attest/enforce.py`](../attest/enforce.py) | pure, total | *Given the verdict + config + loop state, BLOCK or allow?* |
| **I/O** | [`../attest/hook.py`](../attest/hook.py) | impure | Load state, run git, parse the claim, refine the verdict, emit the decision. |

`verdict.evaluate(claim, observed, repo_root)` and `enforce.decide(...)` take plain
dicts/booleans/ints and return plain dicts. Neither touches git, sqlite, stdout, or `os.environ`
(the `enforce` config readers read env, but `decide` itself does not). The module docstring of
`enforce.py` states the invariant directly:

```text
Every function here is pure and total — no git, no sqlite, no stdout, no env
mutation — so the entire block/allow decision is a unit-testable truth-table.
```

**Why purity matters here specifically:** the hardest thing to get right in this system is the
*decision*, not the git plumbing. Loop-safety bugs (block-without-recording an infinite loop),
fail-open bugs (blocking on a git error), and false-positive bugs (blocking honest prose) all
live in the decision logic. By making `decide()` a pure function of ten named arguments
(`enforce`, `false_done`, `reliable`, `ambiguous`, `agent_id_present`, `stop_hook_active`,
`block_count`, `max_retries`, `session_blocks`, `session_ceiling`), every branch of the truth-table
is enumerable in a unit test with no fixtures, no temp repo, and no sqlite file. The I/O layer in
`hook.py` then has exactly two jobs the pure layer cannot do: durably commit the counters and
emit pure-JSON stdout. Both are described below.

---

## 3. The verdict and the refinement

### The base verdict (`verdict.py`, pure)

`verdict.evaluate` declares a **false DONE** if and only if all three hold
([`../attest/verdict.py`](../attest/verdict.py), lines 108–112):

```python
false_done = (
    status == 'DONE'
    and source != 'none'        # a claim is actually present
    and bool(claimed_but_unchanged)  # at least one claimed file is absent from the delta
)
```

`claimed_but_unchanged` is computed by normalizing both the claimed paths and the observed
changed paths to a repo-relative form (`_normalize_path` handles absolute-vs-relative and
strips `./`), then taking the claimed paths whose normalized form is **not** in the observed
delta. Note the second clause: a missing claim (`source == 'none'`) can **never** be a false
DONE, no matter what the tree looks like. That rule is load-bearing and is enforced again
upstream in `hook.py`.

### The refinement (`hook.py`, fail-open only)

`hook.py` does **not** block on `verdict.false_done` directly. It first computes a narrower
**blockable set** that removes any claimed-but-unchanged file showing *any* evidence of work
([`../attest/hook.py`](../attest/hook.py), lines 262–270):

```python
observed_basenames = {os.path.basename(p.rstrip('/')) for p in observed.get('changed', set())}
blockable = [
    f for f in verdict['claimed_but_unchanged']
    if not gitdelta.path_on_disk(root, f, cwd=cwd)
    and os.path.basename(f.rstrip('/')) not in observed_basenames
]
refined_false_done = bool(verdict['false_done'] and blockable)
```

A claimed-but-unchanged file is **dropped from the block set** if either:

1. **It exists on disk.** `gitdelta.path_on_disk(root, f, cwd=cwd)` resolves the path against
   the git toplevel, against the subagent's payload `cwd` (which may be a subdirectory of the
   repo), **and** as an absolute path. This catches gitignored writes, identical rewrites
   (content unchanged so no delta, but the file is genuinely there), prior work, and
   cwd-relative claims. `path_on_disk` is documented as intentionally biased toward `True`: a
   spurious `True` only ever *suppresses* a block.
2. **Some observed-changed file shares its basename.** The agent did change the file but
   reported a different path form (claimed `app.py` while `src/app.py` changed, or a bare
   basename from a subdirectory cwd).

`refined_false_done` additionally requires the blockable set to be **non-empty**. Both
refinements are strictly **subtractive** — they only ever *remove* a file from the block set,
never add one. The inline comment makes the contract explicit: *"a path-reporting imprecision
can never block real work."* This is why Attest can be aggressive about what counts as
"evidence of work" without risking a false block.

---

## 4. The two-tier conservative claim parser

[`../attest/claim.py`](../attest/claim.py) is conservative **by construction**: it would rather
miss a claim than invent one.

### Tier 1 — the `## Handoff` block (CAST-compatible)

`_parse_handoff` looks for a `## Handoff` block using `_HANDOFF_RE` and parses `key: value`
lines with `_parse_kv`. Both mirror CAST's upstream `cast_handoff_parser._HANDOFF_RE` and
`_parse_kv` exactly, so a CAST handoff block parses identically here and upstream. It reads
`status`, `files_changed`, and `blockers`. A status string only counts if it is in
`{DONE, DONE_WITH_CONCERNS, BLOCKED, NEEDS_CONTEXT}`; `files_changed: none` parses to `[]`.

### Tier 2 — anchored natural-language fallback

When there is no `## Handoff` block, `_parse_nl` runs two **start-of-line-anchored** regexes,
applied with `re.match` per line:

- **Status:** `_NL_STATUS_SOL_RE` matches `Status: <value>` only at start-of-line (after
  optional whitespace).
- **Files:** `_FILES_KEY_SOL_RE` matches an explicit files key at start-of-line —
  `files_changed`, `files changed`, `changed files`, `modified files`, or `files modified`.
  The value terminator is newline, `;`, or `|` **— but not `/`,** because file paths contain
  directory slashes (the regex captures `[^\n|;]*`).

### The BUG-4 story (why prose scraping was removed)

An earlier version of the parser scraped much more aggressively, and it cost a false block on
a real agent. The source comments preserve the history
([`../attest/claim.py`](../attest/claim.py), lines 50–69):

- **BUG-4** removed `_BACKTICK_PATH_RE` (backtick-wrapped tokens became ghost file paths),
  `_VERB_PATH_RE` (`"created foo.py"` fired on prose descriptions), and `_BARE_DONE_RE` (a
  bare `DONE` anywhere caused false blocks on honest prose).
- **BUG-4b** removed separator anchoring (`/ ; |`). `"The previous implementation; status:
  DONE was broken."` was being read as a `DONE`, and `"Status: DONE / files_changed: ghost.py"`
  yielded a phantom `['ghost.py']`.

The trigger was a **live capture in which the old parser false-blocked an honest agent** — an
agent that created nothing, explained why in prose, and happened to mention a path and a
command. That exact payload is now pinned as a regression guard
([`../fixtures/README.md`](../fixtures/README.md), `subagent_stop_false_done.json`): the
conservative parser **must** extract `files_changed = []` from prose that literally contains
`ghost.py`, the words `files_changed: ghost.py` and `status: DONE`, and a `mkdir` command.

### The critical rule, and the accepted trade-off

```text
CRITICAL RULE: a MISSING or unparseable claim returns status=None, source="none".
               It MUST NEVER be treated as a false DONE downstream.
```

`parse_claim` enforces this twice: empty/whitespace text returns `source='none'`, and a Tier 2
parse that found neither a status nor a files key is downgraded to `source='none'` before
returning (lines 264–293). `verdict.evaluate` and `hook.on_stop` both refuse to call a
`source='none'` claim a false DONE.

The deliberate cost: a single-line `Status: DONE / Files changed: a.txt` detects the status but
does **not** extract the file (the `/` is not a value terminator). To have files verified, use
the multi-line form or a `## Handoff` block. This is the safe direction to err — a missed
detection allows a stop; a false detection blocks honest work. The parser always chooses the
former.

---

## 5. Fail-open-on-doubt / fail-closed-on-proof

This is the philosophy that governs every branch in `enforce.decide` and `hook.on_stop`.

> A wrong block is **far** costlier than a missed one. A wrong block can wedge a subagent in an
> infinite continue-loop, or convince a developer the plugin is broken and get it uninstalled.
> A missed false-DONE is just the status quo without Attest. So: **allow on every doubt, block
> only on proof.**

`enforce.decide` returns `allow` with a machine-readable `reason_code` at the first sign of any
doubt, and reaches `BLOCK_FALSE_DONE` only when *every* guard has passed
([`../attest/enforce.py`](../attest/enforce.py), lines 120–136):

| `reason_code` | Allows because… |
|---------------|-----------------|
| `ALLOW_NOT_ENFORCING` | `ATTEST_ENFORCE != 1` — enforcement is **off by default**. |
| `ALLOW_NO_AGENT_ID` | no unique `agent_id` to attribute or rate-limit the block. |
| `ALLOW_NOT_FALSE_DONE` | not a refined false DONE (no contradicted, evidence-free claim). |
| `ALLOW_DELTA_UNRELIABLE` | git was unreadable — see §9. |
| `ALLOW_AMBIGUOUS` | the tree was already dirty at start; the delta cannot be attributed. |
| `ALLOW_RETRY_CAP` | this agent's `block_count >= max_retries`. |
| `ALLOW_SESSION_CEILING` | `session_blocks >= session_ceiling`. |
| `ALLOW_STOP_HOOK_ACTIVE` | `stop_hook_active` is set (subtractive fast-path, §6). |
| `BLOCK_FALSE_DONE` | **proof** — every guard above passed. |

`hook.on_stop` adds the runtime doubts the pure layer cannot see: no stored snapshot, a delta
computation exception, a `source='none'` claim, a claimed file present on disk or basename-matched,
and — crucially — a counter write that cannot be confirmed (§6). The hook **never raises out**:
`main()` wraps the handlers in a try/except that downgrades any internal error to a non-fatal
stderr note and returns 0. The full enumerated doubt list lives in the `on_stop` docstring.

---

## 6. Layered loop-safety — belt, suspenders, and a second belt

The catastrophic failure for a blocking `SubagentStop` hook is an **infinite loop**: block →
agent re-fires → block → … forever. Attest defends against this with four independent layers,
so that no single mechanism failing can produce an unbounded loop.

**1. Per-agent retry cap.** `state.py` keeps a `block_count` on the snapshots row. `decide`
allows once `block_count >= max_retries` (`ATTEST_MAX_RETRIES`, default **1**). Setting it to
`0` means *enforcement is on but never blocks* — a kill-switch.

**2. Session-wide backstop.** `state.py` keeps `session_blocks` keyed on `(session_id, repo)`
(`ATTEST_SESSION_BLOCK_CEILING`, default **10**). This bounds a runaway **even if `agent_id`
churns** — if the framework were to assign fresh agent ids on each re-fire, the per-agent cap
would reset, but the session counter would not.

**3. Durable-commit-before-block.** This is the load-bearing one.
[`../attest/hook.py`](../attest/hook.py) (lines 336–352) increments **both** counters and
confirms the writes **before** emitting the block:

```python
new_agent = state_mod.increment_block_count(key)
new_session = state_mod.increment_session_blocks(session_id, root) if new_agent is not None else None
if new_agent is None or new_session is None:
    report('... would block but counter persist failed — failing open (no block)')
else:
    _emit_block(reason)  # the ONLY stdout write; the final action
```

The reasoning: **an unrecorded block is what loops.** If Attest emitted a block but failed to
record that it had done so, the next stop would see `block_count == 0` and block again, forever.
So if either persist cannot be confirmed, Attest **fails open** and does not block. The session
counter is advanced **only after** the per-agent increment is confirmed (`if new_agent is not
None`), so a failed agent-write can never inflate the session counter.

**4. `stop_hook_active` as a subtractive-only fast-path.** The payload carries a
`stop_hook_active` flag that is `true` on a re-fire after a block. `decide` treats it as an
extra reason to **allow**, never to block (it is checked last, only to suppress). It is
explicitly **not** load-bearing for loop safety. The `enforce.py` docstring states why:

```text
stop_hook_active is a SUBTRACTIVE fast-path only: it can suppress a block,
never create one. It is unconfirmed for SubagentStop, so the persisted counters
(block_count, session_blocks) remain the authoritative loop guards.
```

At design time, neither `stop_hook_active`'s presence on `SubagentStop` nor the stability of
`agent_id` across a block→continue cycle was confirmed. The persisted counters are therefore
the authoritative guard, and `stop_hook_active` is treated as a best-effort optimisation that
can only ever make Attest *safer*. (The live capture later confirmed both behaviours — see
[`./VALIDATION.md`](./VALIDATION.md) — but the design does not depend on them.)

**Why INSERT-OR-IGNORE + UPDATE, never INSERT OR REPLACE.** `state.save_snapshot` uses
`INSERT OR IGNORE` followed by `UPDATE` (lines 139–155). `INSERT OR REPLACE` would *delete and
reinsert* the row, silently zeroing `block_count` — so a `SubagentStart` re-firing mid-retry
could reset the loop counter and defeat the cap. The two-step form preserves an existing
`block_count`; new rows get `0` from the column default. The DB also runs in WAL mode and ships
an **idempotent `ALTER TABLE ... ADD COLUMN block_count`** migration (lines 62–64) for
pre-Phase-2 databases — without it, a `SELECT block_count` on an old DB would raise, get
swallowed, and read back as `0` forever (the same "counter never increments" loop).

**On a block, state is kept.** `decide` returns `keep_state: True` only on a block, and
`hook.on_stop` clears the snapshot only when it did **not** block (lines 380–381). The retry
must re-verify against the **same baseline** snapshot, so the block keeps it; every other path
clears it.

---

## 7. The block delivery contract

When Attest blocks, it does **not** use exit code 2. It writes a JSON object to stdout and exits
0 ([`../attest/hook.py`](../attest/hook.py), `_emit_block`, lines 46–60):

```python
sys.stdout.write(json.dumps({'decision': 'block', 'reason': reason}))
```

Three requirements make this work, and each is a deliberate design constraint:

1. **Pure stdout.** Claude Code parses the hook's **entire stdout** as one JSON object. Any
   other byte on stdout voids the block. So `_emit_block` must be the *final* stdout write of
   the run, with nothing printed before it. In enforce mode, **all** human diagnostics are
   routed to **stderr** — `hook.on_stop` sets `out = sys.stderr if enforce else sys.stdout` and
   every `report(...)` call respects it (lines 180–188). The stop shim routes that stderr to
   `~/.claude/logs/attest-errors.log`. In detect mode nothing is ever blocked, so diagnostics
   stay on stdout (Phase-1b behaviour).
2. **Exit 0, signal via JSON only.** The shim always exits 0; the block travels purely in the
   stdout JSON, never the exit code. `_emit_block` even guards `BrokenPipeError` so a closed
   parent pipe never surfaces as a non-zero exit. This keeps the hook from ever *crashing* the
   pipeline — a non-zero exit could be interpreted unpredictably; a clean exit with a JSON
   payload is the JSON-decision envelope Claude Code honours (empirically verified for
   `SubagentStop` on the version tested — that event's block capability is itself officially
   undocumented; see point 3 below and [`./VALIDATION.md`](./VALIDATION.md)).
3. **Synchronous hook required.** The hook must be registered `async: false`. An async hook's
   stdout is not awaited, which would void the block. This is wired in
   [`../hooks/hooks.json`](../hooks/hooks.json) and the installer. The dependence on synchronous
   delivery — and the fact that `SubagentStop` blocking is officially undocumented on the
   version tested — is the headline risk documented in [`./VALIDATION.md`](./VALIDATION.md) and
   [`./LIMITATIONS.md`](./LIMITATIONS.md).

The escaping is also load-bearing: `json.dumps` escapes any quotes, backticks, or newlines in a
file path inside the reason string, so `build_block_reason`
([`../attest/enforce.py`](../attest/enforce.py)) can name the phantom files safely.

---

## 8. State and the optional CAST mirror

[`../attest/state.py`](../attest/state.py) is the only stateful component. It is a stdlib
`sqlite3` store at `ATTEST_STATE_DB` (default `~/.attest/state.db`), running in **WAL** mode,
with two tables: `snapshots` (per-agent snapshot + `block_count`) and `session_blocks`
(session backstop counter). Schema creation uses `CREATE TABLE IF NOT EXISTS` and the idempotent
`ALTER TABLE` migration described in §6, so the store is safe to open repeatedly and safe to
upgrade in place. Every accessor wraps its work in try/except and a `finally`-closed connection
so a transient sqlite error degrades to a safe default (`get_block_count` → `0`, `load_snapshot`
→ `None`, `increment_*` → `None` = fail open) rather than crashing the hook.

`mirror_to_cast_db` is a **best-effort, never-load-bearing** integration. If `CAST_DB_PATH`
(default `~/.claude/cast.db`) points at an existing file, each verdict is also written to an
`attestations` table (created if absent). Any failure — module not importable, file absent,
write error — is silently ignored. Nothing about a block or an allow depends on the mirror
succeeding; it exists purely so a CAST installation can observe Attest's verdicts. Note it is
called with `refined_false_done`, not the raw verdict, so the mirror records the same
decision the enforcement path acted on.

---

## 9. The reliability signal

The single subtlest correctness point in the system: `delta.reliable` is derived **only** from
whether each git snapshot errored, and is **never** inferred from `len(changed)`
([`../attest/gitdelta.py`](../attest/gitdelta.py), `delta`, lines 212–270).

The trap is that an **empty `changed` set is ambiguous on its own.** It means *either*:

- "the agent changed nothing" — which, with a `DONE` claim naming files, is exactly a false
  DONE we want to act on; **or**
- "git could not be read" — a non-git directory, a transient `git status` failure — in which
  case we have no baseline at all and must fail open.

Conflating these two would be a disaster in either direction. If an empty set were read as
"nothing changed," a git error would manufacture a false DONE and block honest work. If an empty
set were read as "unreadable," a genuine do-nothing false DONE would always slip through. So
`delta` computes `reliable` structurally:

```python
if '_error' in before:
    return {'changed': set(), 'ambiguous': False, 'reliable': False}
...
after = snapshot(repo_dir)
if '_error' in after:
    return {'changed': set(), 'ambiguous': before_has_changes, 'reliable': False}
...
return {'changed': changed, 'ambiguous': before_has_changes, 'reliable': True}
```

`reliable` is `True` **only when both** the start snapshot and the stop snapshot read git without
an `_error` sentinel. `enforce.decide` then refuses to block on `not reliable`
(`ALLOW_DELTA_UNRELIABLE`), and `hook.on_stop` reports an `UNVERIFIABLE` line rather than a
mismatch. This is the structural guarantee that a git error fails open. The companion signal,
`ambiguous`, is `True` when the start tree already had uncommitted changes — the delta cannot be
cleanly attributed to the agent under evaluation, so `decide` allows (`ALLOW_AMBIGUOUS`).

---

## See also

- [`./VALIDATION.md`](./VALIDATION.md) — the empirical proof: the mechanism test, the live
  capture against real Claude Code, the four pinned fixtures, and the honest account of what is
  and is not deterministically proven.
- [`./LIMITATIONS.md`](./LIMITATIONS.md) — the consequences of these design choices: the
  undocumented `SubagentStop` blocking dependency, the conservative-parser trade-offs, the
  ambiguous-tree blind spot, and the scope boundary.
- [`./INSTALL.md`](./INSTALL.md) — installation and configuration.
- [`../README.md`](../README.md) — project overview.
- [`../scripts/live-capture-test.sh`](../scripts/live-capture-test.sh) — rebuilds the live
  validation harness.
