#!/usr/bin/env python3
"""
hook.py — Orchestrator for SubagentStart / SubagentStop hook events.

Public API (called from the bash shims):
  on_start(payload_dict: dict) -> None
  on_stop(payload_dict: dict) -> None
  main() -> int   # CLI entry: python3 -m attest.hook start|stop

Mode: DETECT-AND-PRINT only (Phase 1b). Hooks always exit 0 and never block.

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_report(label: str, message: str) -> None:
    """Print a structured attest report line to stdout."""
    print(f'attest: {label}: {message}', flush=True)


def _capture_if_requested(
    event: str,
    payload_dict: dict,
    transcript_path: str,
) -> None:
    """Dump payload + transcript to fixtures/captured/ when ATTEST_CAPTURE=1."""
    if os.environ.get('ATTEST_CAPTURE', '').strip() != '1':
        return

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

    # Write payload
    payload_file = os.path.join(captured_dir, f'{event}-{agent_id}-{timestamp}.json')
    try:
        with open(payload_file, 'w', encoding='utf-8') as fh:
            json.dump(payload_dict, fh, indent=2)
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

def on_start(payload_dict: dict) -> None:
    """Handle SubagentStart: snapshot the git tree and persist it.

    Args:
        payload_dict: output of hookio.parse_payload().
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

    _capture_if_requested('start', payload_dict, payload_dict.get('transcript_path', ''))


def on_stop(payload_dict: dict) -> None:
    """Handle SubagentStop: compare claim against observed delta and print report.

    Always exits without raising. Prints to stdout.

    Args:
        payload_dict: output of hookio.parse_payload().
    """
    key = state_mod.agent_key(payload_dict)
    cwd = payload_dict.get('cwd', '') or os.getcwd()
    transcript_path = payload_dict.get('transcript_path', '')

    _capture_if_requested('stop', payload_dict, transcript_path)

    # Load the start snapshot
    snap = state_mod.load_snapshot(key)
    if snap is None:
        print(
            f'attest: stop: no snapshot found for {key} '
            f'(start event may have been missed) — skipping verification',
            flush=True,
        )
        return

    # Compute the observed delta since start
    try:
        observed = gitdelta.delta(snap, cwd)
    except Exception as exc:  # noqa: BLE001
        print(f'attest: stop: delta computation failed for {key}: {exc} (non-fatal)', flush=True)
        state_mod.clear(key)
        return

    # Get claim text: try payload fast-path first, then transcript
    claim_text: str = payload_dict.get('payload_text', '').strip()
    claim_source_label = 'payload'
    if not claim_text and transcript_path:
        claim_text = transcript_mod.last_assistant_text(transcript_path)
        claim_source_label = 'transcript'

    # Parse the claim
    parsed = claim_mod.parse_claim(claim_text)

    # Enforce CRITICAL RULE: source="none" → cannot verify → never false DONE
    if parsed['source'] == 'none':
        print(
            f'attest: stop: {key}: claim source=none — cannot verify (never treating as false DONE)',
            flush=True,
        )
        state_mod.clear(key)
        return

    # Evaluate verdict
    verdict = verdict_mod.evaluate(parsed, observed, repo_root=cwd)

    # Build human report
    claimed_files = parsed.get('files_changed', [])
    observed_files = sorted(observed.get('changed', set()))

    claimed_str = ', '.join(claimed_files) if claimed_files else '(none)'
    observed_str = ', '.join(observed_files) if observed_files else '(none)'
    status_str = parsed.get('status') or '?'

    if verdict['false_done']:
        mismatched = ', '.join(verdict['claimed_but_unchanged'])
        print(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> MISMATCH: {mismatched} claimed-but-unchanged '
            f'(would block in enforce mode) [source={claim_source_label}]',
            flush=True,
        )
    elif verdict['claimed_but_unchanged']:
        # Non-DONE status with unmatched files — warn but don't call it false DONE
        mismatched = ', '.join(verdict['claimed_but_unchanged'])
        print(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> WARN: {mismatched} claimed but not in delta (status={status_str}) '
            f'[source={claim_source_label}]',
            flush=True,
        )
    elif verdict['observed_but_unclaimed']:
        extras = ', '.join(verdict['observed_but_unclaimed'])
        print(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> SCOPE_CREEP: {extras} observed-but-unclaimed '
            f'[source={claim_source_label}]',
            flush=True,
        )
    else:
        print(
            f'attest: stop: {key}: '
            f'CLAIMED [{claimed_str}] OBSERVED [{observed_str}] '
            f'-> OK [source={claim_source_label}]',
            flush=True,
        )

    if verdict['ambiguous']:
        print(
            f'attest: stop: {key}: WARNING: delta is ambiguous (pre-existing uncommitted changes at start)',
            flush=True,
        )

    # Optional CAST mirror (best-effort)
    state_mod.mirror_to_cast_db({
        'agent_key': key,
        'false_done': verdict['false_done'],
        'agent_type': payload_dict.get('agent_type', ''),
        'session_id': payload_dict.get('session_id', ''),
        'status': status_str,
        'reason': verdict['reason'],
        'claimed': claimed_files,
        'observed': observed_files,
    })

    # Clear the snapshot (the agent run is over)
    state_mod.clear(key)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    """Entry point: python3 -m attest.hook start|stop

    Reads the raw JSON payload from stdin and dispatches to on_start or on_stop.
    Always returns 0 (detect-and-print mode — never blocks).
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
            on_start(payload)
        else:
            on_stop(payload)
    except Exception as exc:  # noqa: BLE001
        # Fail-open: never let an internal error surface as a non-zero exit
        print(f'attest: internal error in {event} handler: {exc} (non-fatal)', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
