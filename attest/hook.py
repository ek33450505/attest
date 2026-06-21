#!/usr/bin/env python3
"""
hook.py — Orchestrator for SubagentStart / SubagentStop hook events.

Public API (called from the bash shims):
  on_start(payload_dict: dict) -> None
  on_stop(payload_dict: dict) -> None
  main() -> int   # CLI entry: python3 -m attest.hook start|stop

Mode: detect-and-print by default. In enforce mode (ATTEST_ENFORCE=1) a proven
false DONE is blocked by writing a {"decision":"block"} JSON object to stdout.
Either way the hook process always exits 0 — the block travels via stdout, never
the exit code (see on_stop / enforce.decide).

ATTEST_CAPTURE=1 env var: when set, dumps the raw payload JSON and a copy of
the transcript into fixtures/captured/ (relative to the attest repo root or cwd).

Report format:
  attest: CLAIMED [a.py, b.py] OBSERVED [a.py] -> MISMATCH: b.py claimed-but-unchanged (would block in enforce mode)
  attest: CLAIMED [a.py] OBSERVED [a.py, c.py] -> SCOPE_CREEP: c.py observed-but-unclaimed
  attest: CLAIMED [a.py] OBSERVED [a.py] -> OK
  attest: no snapshot found for <key> (start event may have been missed)
  attest: claim source=none — cannot verify (never treating as false DONE)
"""
import json
import os
import shutil
import sys
import time
from typing import Optional

from attest import gitdelta
from attest import claim as claim_mod
from attest import verdict as verdict_mod
from attest import state as state_mod
from attest import transcript as transcript_mod
from attest import enforce as enforce_mod


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_report(label: str, message: str) -> None:
    """Print a structured attest report line to stdout."""
    print(f'attest: {label}: {message}', flush=True)


def _emit_block(reason: str) -> None:
    """Emit the SubagentStop block decision as the SOLE stdout content.

    Claude Code parses the hook's entire stdout as one JSON object for the
    decision; any other byte on stdout voids the block. This must therefore be
    the FINAL stdout write of the run, with nothing printed to stdout before it
    (enforce-mode diagnostics go to stderr). Guarded against BrokenPipeError so a
    closed parent pipe never surfaces as a non-zero exit.
    """
    try:
        sys.stdout.write(json.dumps({'decision': 'block', 'reason': reason}))
        sys.stdout.write('\n')
        sys.stdout.flush()
    except BrokenPipeError:
        pass


def _capture_if_requested(
    event: str,
    payload_dict: dict,
    transcript_path: str,
    *,
    raw: Optional[str] = None,
) -> None:
    """Dump payload + transcript to fixtures/captured/ when ATTEST_CAPTURE=1.

    Args:
        event: 'start' or 'stop'.
        payload_dict: normalized payload (from parse_payload).
        transcript_path: path to copy for the transcript side-file.
        raw: optional raw stdin string. When provided and ATTEST_CAPTURE=1,
             also writes a '{event}-raw-{agent_id16}-{ts}.json' file
             containing the verbatim stdin so callers can verify raw field names.
    """
    if os.environ.get('ATTEST_CAPTURE', '').strip() != '1':
        return

    # Allow tests/CI to redirect capture writes without touching the real fixtures dir.
    capture_dir_override = os.environ.get('ATTEST_CAPTURE_DIR', '').strip()
    if capture_dir_override:
        captured_dir = capture_dir_override
    else:
        # Locate repo root: walk up from __file__ looking for bin/ or attest/
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(here)  # attest/<pkg> → attest/ (repo root)
        captured_dir = os.path.join(repo_root, 'fixtures', 'captured')

    try:
        os.makedirs(captured_dir, exist_ok=True)
    except OSError:
        return

    timestamp = str(int(time.time()))
    agent_id = payload_dict.get('agent_id', 'unknown')[:16]

    # Write normalized payload
    payload_file = os.path.join(captured_dir, f'{event}-{agent_id}-{timestamp}.json')
    try:
        with open(payload_file, 'w', encoding='utf-8') as fh:
            json.dump(payload_dict, fh, indent=2)
    except OSError:
        pass

    # Write raw stdin verbatim so field-name verification is possible.
    if raw is not None:
        raw_file = os.path.join(captured_dir, f'{event}-raw-{agent_id}-{timestamp}.json')
        try:
            with open(raw_file, 'w', encoding='utf-8') as fh:
                fh.write(raw)
        except OSError:
            pass

    # Copy transcript if accessible
    if transcript_path and os.path.isfile(transcript_path):
        ts_dest = os.path.join(captured_dir, f'transcript-{agent_id}-{timestamp}.jsonl')
        try:
            shutil.copy2(transcript_path, ts_dest)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public hook handlers
# ---------------------------------------------------------------------------

def on_start(payload_dict: dict, *, raw: Optional[str] = None) -> None:
    """Handle SubagentStart: snapshot the git tree and persist it.

    Args:
        payload_dict: output of hookio.parse_payload().
        raw: optional verbatim stdin string forwarded to the capture helper.
    """
    key = state_mod.agent_key(payload_dict)
    cwd = payload_dict.get('cwd', '') or os.getcwd()
    session_id = payload_dict.get('session_id', '')

    snap = gitdelta.snapshot(cwd)

    if '_error' in snap:
        # Non-git dir or git not installed — note it and continue.
        print(f'attest: start: snapshot failed for {key}: {snap["_error"]} (non-fatal)', flush=True)
        # Still store the error snapshot so on_stop knows start was attempted.

    ok = state_mod.save_snapshot(
        key,
        snap,
        repo=cwd,
        session_id=session_id,
        meta={
            'agent_type': payload_dict.get('agent_type', ''),
            'agent_id': payload_dict.get('agent_id', ''),
        },
    )
    if not ok:
        print(f'attest: start: state save failed for {key} (non-fatal)', flush=True)
    else:
        print(f'attest: start: snapshot stored for {key}', flush=True)

    _capture_if_requested('start', payload_dict, payload_dict.get('transcript_path', ''), raw=raw)


def on_stop(payload_dict: dict, *, raw: Optional[str] = None) -> None:
    """Handle SubagentStop: report in detect mode, or block a proven false DONE.

    Fail-open contract: this NEVER raises out, and emits a block decision on stdout
    ONLY when every precondition in ``enforce.decide()`` holds AND both persisted
    counters durably increment. Any doubt — non-git, dirty tree, gitignored/on-disk
    file, missing claim, missing snapshot, absent agent_id, failed counter write, or
    any exception — allows the stop (no JSON on stdout).

    Args:
        payload_dict: output of hookio.parse_payload().
        raw: optional verbatim stdin string forwarded to the capture helper.
    """
    enforce = enforce_mod.enforcement_enabled()
    out = sys.stderr if enforce else sys.stdout

    def report(msg: str) -> None:
        # Human-readable diagnostics. In ENFORCE mode they go to stderr so stdout is
        # reserved exclusively for the single JSON decision object (pure-stdout
        # contract). In detect mode they go to stdout (Phase-1b behaviour preserved).
        print(msg, file=out, flush=True)

    key = state_mod.agent_key(payload_dict)
    cwd = payload_dict.get('cwd', '') or os.getcwd()
    session_id = payload_dict.get('session_id', '')
    transcript_path = payload_dict.get('transcript_path', '')

    _capture_if_requested('stop', payload_dict, transcript_path, raw=raw)

    # Load the start snapshot
    snap = state_mod.load_snapshot(key)
    if snap is None:
        report(
            f'attest: stop: no snapshot found for {key} '
            f'(start event may have been missed) — skipping verification'
        )
        return

    # Compute the observed delta since start
    try:
        observed = gitdelta.delta(snap, cwd)
    except Exception as exc:  # noqa: BLE001
        report(f'attest: stop: delta computation failed for {key}: {exc} (non-fatal)')
        state_mod.clear(key)
        return

    # Get claim text: payload fast-path first, then transcript.
    # For SubagentStop, prefer the subagent's own transcript (agent_transcript_path) over
    # the parent session transcript (transcript_path) — the subagent's jsonl contains the
    # actual completion message; the parent jsonl is the orchestrating session's file.
    claim_text: str = payload_dict.get('payload_text', '').strip()
    claim_source_label = 'payload'
    if not claim_text:
        best_transcript = (
            payload_dict.get('agent_transcript_path') or
            payload_dict.get('transcript_path') or
            ''
        )
        if best_transcript:
            claim_text = transcript_mod.last_assistant_text(best_transcript)
            claim_source_label = 'transcript'

    parsed = claim_mod.parse_claim(claim_text)

    # CRITICAL RULE: source="none" → cannot verify → never a false DONE.
    if parsed['source'] == 'none':
        report(
            f'attest: stop: {key}: claim source=none — cannot verify '
            f'(never treating as false DONE)'
        )
        state_mod.clear(key)
        return

    # Resolve the git toplevel so claimed/observed paths normalize against the same
    # root (handles /tmp vs /private/tmp); fall back to cwd if unresolved.
    root = gitdelta.repo_root(cwd) or cwd

    verdict = verdict_mod.evaluate(parsed, observed, repo_root=root)

    claimed_files = parsed.get('files_changed', [])
    observed_files = sorted(observed.get('changed', set()))
    claimed_str = ', '.join(claimed_files) if claimed_files else '(none)'
    observed_str = ', '.join(observed_files) if observed_files else '(none)'
    status_str = parsed.get('status') or '?'
    reliable = bool(observed.get('reliable', False))

    # Blockable set: claimed-but-unchanged files that show NO evidence of work.
    # A claimed file is dropped (not phantom) if EITHER:
    #   (a) it exists on disk — gitignored write, identical rewrite, prior work,
    #       or a cwd-relative claim that resolves under the subagent's payload cwd; OR
    #   (b) some observed-changed file shares its basename — the agent did change the
    #       file but reported a different path form (e.g. claimed "app.py" while
    #       "src/app.py" changed, or a bare basename from a subdirectory cwd).
    # Both are strictly fail-open: they only ever REMOVE a file from the block set,
    # never add one, so a path-reporting imprecision can never block real work.
    observed_basenames = {
        os.path.basename(p.rstrip('/')) for p in observed.get('changed', set())
    }
    blockable = [
        f for f in verdict['claimed_but_unchanged']
        if not gitdelta.path_on_disk(root, f, cwd=cwd)
        and os.path.basename(f.rstrip('/')) not in observed_basenames
    ]
    refined_false_done = bool(verdict['false_done'] and blockable)

    # ---- Human-readable report (both modes) ----
    if not reliable:
        report(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> UNVERIFIABLE: git delta unreliable (non-git or git error) '
            f'[source={claim_source_label}]'
        )
    elif refined_false_done:
        mismatched = ', '.join(blockable)
        suffix = '' if enforce else ' (would block in enforce mode)'
        report(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> MISMATCH: {mismatched} claimed-but-unchanged{suffix} '
            f'[source={claim_source_label}]'
        )
    elif verdict['claimed_but_unchanged']:
        # Some claimed files missing from the delta, but each exists on disk (or
        # status != DONE) — report without calling it a false DONE.
        mismatched = ', '.join(verdict['claimed_but_unchanged'])
        report(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> WARN: {mismatched} claimed but not in delta (status={status_str}) '
            f'[source={claim_source_label}]'
        )
    elif verdict['observed_but_unclaimed']:
        extras = ', '.join(verdict['observed_but_unclaimed'])
        report(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> SCOPE_CREEP: {extras} observed-but-unclaimed '
            f'[source={claim_source_label}]'
        )
    else:
        report(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] -> OK '
            f'[source={claim_source_label}]'
        )

    if observed.get('ambiguous'):
        report(
            f'attest: stop: {key}: WARNING: delta is ambiguous '
            f'(pre-existing uncommitted changes at start)'
        )

    # ---- Enforcement decision ----
    agent_id_present = bool((payload_dict.get('agent_id') or '').strip())
    decision = enforce_mod.decide(
        enforce=enforce,
        false_done=refined_false_done,
        reliable=reliable,
        ambiguous=bool(observed.get('ambiguous', False)),
        agent_id_present=agent_id_present,
        stop_hook_active=bool(payload_dict.get('stop_hook_active', False)),
        block_count=state_mod.get_block_count(key) if enforce else 0,
        max_retries=enforce_mod.max_retries(),
        session_blocks=state_mod.get_session_blocks(session_id, root) if enforce else 0,
        session_ceiling=enforce_mod.session_ceiling(),
    )

    blocked = False
    if decision['action'] == 'block':
        # Durably record BOTH counters BEFORE emitting. If either commit cannot be
        # confirmed, fail OPEN (no block) — an unrecorded block is what loops. The
        # session backstop is only advanced once the per-agent increment is confirmed,
        # so a failed agent-write never inflates the session counter.
        new_agent = state_mod.increment_block_count(key)
        new_session = state_mod.increment_session_blocks(session_id, root) if new_agent is not None else None
        if new_agent is None or new_session is None:
            report(
                f'attest: stop: {key}: would block but counter persist failed '
                f'— failing open (no block)'
            )
        else:
            reason = enforce_mod.build_block_reason(blockable, status_str)
            _emit_block(reason)  # the ONLY stdout write; the final action
            blocked = True
            report(f'attest: stop: {key}: BLOCKED false DONE: {", ".join(blockable)}')
            # KEEP state: the retry must re-verify against the same baseline.

    # Observability: if we proved a false DONE but a gate (retry cap, session
    # ceiling, stop_hook_active, ...) suppressed the block, say WHY — so an operator
    # reading the log never mistakes a suppressed detection for a missed one.
    if enforce and refined_false_done and not blocked:
        report(
            f'attest: stop: {key}: detected false DONE but NOT blocking '
            f'(decision={decision["reason_code"]})'
        )

    # Optional CAST mirror (best-effort)
    state_mod.mirror_to_cast_db({
        'agent_key': key,
        'false_done': refined_false_done,
        'enforced': enforce,
        'blocked': blocked,
        'reason_code': decision['reason_code'],
        'agent_type': payload_dict.get('agent_type', ''),
        'session_id': session_id,
        'status': status_str,
        'reason': verdict['reason'],
        'claimed': claimed_files,
        'observed': observed_files,
    })

    # Clear unless we actually blocked (a block keeps state for the retry).
    if not blocked:
        state_mod.clear(key)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    """Entry point: python3 -m attest.hook start|stop

    Reads the raw JSON payload from stdin and dispatches to on_start or on_stop.
    Always returns 0; in enforce mode the block decision is emitted on stdout
    (a {"decision":"block"} object), never via the exit code.
    """
    from attest.hookio import parse_payload

    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] not in ('start', 'stop'):
        print('Usage: python3 -m attest.hook start|stop', file=sys.stderr)
        return 0  # Never fail the hook pipeline

    event = argv[0]

    # Read stdin once
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ''

    payload = parse_payload(raw)

    try:
        if event == 'start':
            on_start(payload, raw=raw)
        else:
            on_stop(payload, raw=raw)
    except Exception as exc:  # noqa: BLE001
        # Fail-open: never let an internal error surface as a non-zero exit
        print(f'attest: internal error in {event} handler: {exc} (non-fatal)', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
