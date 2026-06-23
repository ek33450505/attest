#!/usr/bin/env python3
"""
cli.py — Thin command-line interface for attest.

Commands:
  attest snapshot --repo <dir>
      Prints a JSON snapshot of the current git working-tree state to stdout.
      Exit 0 on success, 1 on error.

  attest verify --claim-file <md> --before <snapshot.json> --repo <dir>
      Reads a claim markdown file and a before-snapshot JSON, computes the
      observed delta, and prints the verdict as JSON.
      Exit 0 always (print-only in Phase 1a — no blocking yet).

  attest --version
      Prints the version string.
"""
import argparse
import json
import sys
from typing import Optional

from attest import __version__
from attest.claim import parse_claim
from attest.gitdelta import snapshot, delta
from attest.verdict import evaluate


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Handle: attest snapshot --repo <dir>"""
    snap = snapshot(args.repo)
    if '_error' in snap:
        _print_error(snap['_error'])
        return 1
    # JSON-serialize the snapshot (values are plain strings, keys are paths).
    print(json.dumps(snap, indent=2, sort_keys=True))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Handle: attest verify --claim-file <md> --before <snapshot.json> --repo <dir>"""
    # Load the before-snapshot JSON.
    try:
        with open(args.before, 'r', encoding='utf-8') as fh:
            before: dict = json.load(fh)
    except FileNotFoundError:
        _print_error(f'Before-snapshot file not found: {args.before}')
        return 1
    except json.JSONDecodeError as exc:
        _print_error(f'Before-snapshot is not valid JSON: {exc}')
        return 1
    except OSError as exc:
        _print_error(f'Cannot read before-snapshot: {exc}')
        return 1

    # Load the claim markdown.
    try:
        with open(args.claim_file, 'r', encoding='utf-8') as fh:
            claim_text: str = fh.read()
    except FileNotFoundError:
        _print_error(f'Claim file not found: {args.claim_file}')
        return 1
    except OSError as exc:
        _print_error(f'Cannot read claim file: {exc}')
        return 1

    # Compute the observed delta.
    try:
        observed = delta(before, args.repo)
    except Exception as exc:  # noqa: BLE001
        _print_error(f'Failed to compute delta: {exc}')
        return 1

    if not observed.get('reliable', True):
        _print_error(
            f'Delta unreliable for repo {args.repo!r}: '
            'not a git repository or git error occurred — cannot verify claim'
        )
        return 1

    # Parse the claim and evaluate the verdict.
    claim = parse_claim(claim_text)
    verdict = evaluate(claim, observed, repo_root=args.repo)

    # Serialize: sets are not JSON-serializable — convert to sorted lists.
    verdict_serializable = dict(verdict)
    for key, value in verdict_serializable.items():
        if isinstance(value, set):
            verdict_serializable[key] = sorted(value)

    print(json.dumps(verdict_serializable, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------

def _print_error(msg: str) -> None:
    """Print a JSON-formatted error object to stderr."""
    print(json.dumps({'error': msg}), file=sys.stderr)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='attest',
        description=(
            'Verify subagent DONE claims against the real git working-tree delta. '
            'Grade the act, not the output.'
        ),
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'attest {__version__}',
    )

    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND')
    subparsers.required = True

    # snapshot
    snap = subparsers.add_parser(
        'snapshot',
        help='Capture a JSON snapshot of the current git working-tree state.',
    )
    snap.add_argument('--repo', required=True, metavar='DIR', help='Path to the git repository.')

    # verify
    verify = subparsers.add_parser(
        'verify',
        help='Verify a claim file against a before-snapshot and the current repo state.',
    )
    verify.add_argument(
        '--claim-file',
        required=True,
        metavar='MD',
        help='Markdown file containing the agent completion claim.',
    )
    verify.add_argument(
        '--before',
        required=True,
        metavar='JSON',
        help='JSON snapshot file produced by "attest snapshot" (taken before the agent ran).',
    )
    verify.add_argument(
        '--repo',
        required=True,
        metavar='DIR',
        help='Path to the git repository.',
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    """Parse arguments and dispatch to the appropriate subcommand.

    Returns an integer exit code (0 = success, 1 = error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == 'snapshot':
        return _cmd_snapshot(args)
    if args.command == 'verify':
        return _cmd_verify(args)

    # Should be unreachable (argparse enforces subcommand requirement).
    parser.print_help(sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
