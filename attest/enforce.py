#!/usr/bin/env python3
"""
enforce.py — Pure enforcement-policy layer for Attest (Phase 2).

Separates three concerns that Phase 1 conflated:
  - SEMANTICS  (``verdict.evaluate``): "is this a proven false DONE?"
  - POLICY     (this module): "given the verdict + config + loop state, BLOCK or allow?"
  - I/O        (``hook.on_stop``): load state, run git, emit the decision.

Every function here is pure and total — no git, no sqlite, no stdout, no env
mutation — so the entire block/allow decision is a unit-testable truth-table.

Design invariants (see plan: fail-closed-on-proof / fail-open-on-doubt):
  - ``decide`` returns ``allow`` for every doubt; it returns ``block`` only when
    EVERY precondition is satisfied. The caller adds two more runtime gates that
    cannot live here (they require I/O): a durably-committed counter increment and
    pure-JSON stdout emission.
  - ``stop_hook_active`` is a SUBTRACTIVE fast-path only: it can suppress a block,
    never create one. It is unconfirmed for SubagentStop, so the persisted counters
    (block_count, session_blocks) remain the authoritative loop guards.

Public API:
  enforcement_enabled(env) -> bool
  max_retries(env) -> int
  session_ceiling(env) -> int
  decide(**kwargs) -> dict
  build_block_reason(blockable_files, status) -> str
"""
import os
from typing import Mapping, Optional, Sequence

_DEFAULT_MAX_RETRIES = 1
_DEFAULT_SESSION_CEILING = 10
_MAX_FILES_IN_REASON = 20


# ---------------------------------------------------------------------------
# Config readers
# ---------------------------------------------------------------------------

def enforcement_enabled(env: Optional[Mapping] = None) -> bool:
    """True only when ATTEST_ENFORCE == '1' (strict, mirrors ATTEST_CAPTURE idiom).

    Everything else — unset, '', '0', 'true', 'yes' — is OFF. Enforcement is
    opt-in and must be unambiguous to turn on.
    """
    if env is None:
        env = os.environ
    return env.get('ATTEST_ENFORCE', '').strip() == '1'


def _parse_nonneg_int(raw, default: int) -> int:
    """Parse a non-negative int; missing/blank/invalid -> default; negative -> 0."""
    if raw is None:
        return default
    try:
        n = int(str(raw).strip())
    except (ValueError, TypeError):
        return default
    return n if n >= 0 else 0


def max_retries(env: Optional[Mapping] = None) -> int:
    """Per-agent block cap (default 1). 0 = enforcement on but never blocks (kill-switch)."""
    if env is None:
        env = os.environ
    return _parse_nonneg_int(env.get('ATTEST_MAX_RETRIES'), _DEFAULT_MAX_RETRIES)


def session_ceiling(env: Optional[Mapping] = None) -> int:
    """Session-scoped backstop ceiling (default 10) — bounds a runaway even if agent_id churns."""
    if env is None:
        env = os.environ
    return _parse_nonneg_int(env.get('ATTEST_SESSION_BLOCK_CEILING'), _DEFAULT_SESSION_CEILING)


# ---------------------------------------------------------------------------
# Pure decision
# ---------------------------------------------------------------------------

def decide(
    *,
    enforce: bool,
    false_done: bool,
    reliable: bool,
    ambiguous: bool,
    agent_id_present: bool,
    stop_hook_active: bool,
    block_count: int,
    max_retries: int,
    session_blocks: int,
    session_ceiling: int,
) -> dict:
    """Pure block/allow decision implementing the Phase-2 truth-table.

    Args:
        enforce:           ATTEST_ENFORCE is on.
        false_done:        REFINED proof — status==DONE AND claim present AND the
                           blockable set (delta-absent ∧ disk-absent) is non-empty.
        reliable:          delta.reliable — both snapshots read git without error.
        ambiguous:         delta.ambiguous — tree was dirty at the agent's start.
        agent_id_present:  a unique agent_id is available (required to block).
        stop_hook_active:  payload flag (subtractive fast-path only).
        block_count:       persisted per-agent block count BEFORE this stop.
        max_retries:       per-agent cap.
        session_blocks:    persisted session-scoped block count BEFORE this stop.
        session_ceiling:   session backstop ceiling.

    Returns:
        {
          'action':      'block' | 'allow',
          'increment':   bool,   # True only on block
          'keep_state':  bool,   # True only on block (retry re-verifies same baseline)
          'reason_code': str,    # machine tag for diagnostics / cast mirror
        }
    """
    def allow(code: str) -> dict:
        return {'action': 'allow', 'increment': False, 'keep_state': False, 'reason_code': code}

    if not enforce:
        return allow('ALLOW_NOT_ENFORCING')
    if not agent_id_present:
        return allow('ALLOW_NO_AGENT_ID')
    if not false_done:
        return allow('ALLOW_NOT_FALSE_DONE')
    if not reliable:
        return allow('ALLOW_DELTA_UNRELIABLE')
    if ambiguous:
        return allow('ALLOW_AMBIGUOUS')
    if block_count >= max_retries:
        return allow('ALLOW_RETRY_CAP')
    if session_blocks >= session_ceiling:
        return allow('ALLOW_SESSION_CEILING')
    if stop_hook_active:
        return allow('ALLOW_STOP_HOOK_ACTIVE')
    return {'action': 'block', 'increment': True, 'keep_state': True, 'reason_code': 'BLOCK_FALSE_DONE'}


# ---------------------------------------------------------------------------
# Block reason (delivered to the subagent as the instruction to continue/fix)
# ---------------------------------------------------------------------------

def build_block_reason(blockable_files: Sequence, status: str = 'DONE') -> str:
    """Build the actionable block reason, naming the phantom file(s).

    Returns a plain string. The JSON envelope is built by the caller with
    ``json.dumps`` so any quotes/backticks/newlines in paths are escaped.
    """
    files = [str(f) for f in (blockable_files or []) if str(f).strip()]
    if not files:
        # Defensive — decide() never blocks with an empty blockable set.
        return (
            'Attest blocked this completion: a file you reported as changed was not found '
            'in the git working tree. Make the change, or correct your handoff, then finish.'
        )
    shown = files[:_MAX_FILES_IN_REASON]
    extra = len(files) - len(shown)
    listed = ', '.join(shown) + (f' (+{extra} more)' if extra > 0 else '')
    n = len(files)
    noun = 'file' if n == 1 else 'files'
    pronoun = 'it' if n == 1 else 'they'
    verb = 'is' if n == 1 else 'are'
    return (
        f'Attest blocked this `Status: {status}`: you reported the following {noun} as changed, '
        f'but {pronoun} {verb} absent from the git working tree AND not present on disk since this '
        f'subagent started — so the change never landed: {listed}. '
        f'Either actually make the edit(s), or correct your handoff (`files_changed`/`status`) to '
        f'match what you truly changed, then finish again. '
        f'(Attest verifies completion claims against the real git delta; it acts only on a '
        f'contradicted claim, never a missing one.)'
    )
