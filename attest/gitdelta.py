#!/usr/bin/env python3
"""
gitdelta.py — Git working-tree snapshot and delta computation.

All git operations are READ-ONLY. This module never mutates a repository.

Public API:
  snapshot(repo_dir: str) -> dict
      Returns {relpath: sha256_hex} for every path that differs from HEAD.
      Deleted files map to the _DELETED_SENTINEL constant.
      On error (non-git dir, git not installed, etc.) returns {"_error": "..."}.

  delta(before: dict, repo_dir: str) -> dict
      Returns {"changed": set[str], "ambiguous": bool}.
      "changed" is the set of repo-relative paths whose content-hash differs
      between the before snapshot and the current working tree.
      "ambiguous" is True when before had pre-existing uncommitted changes that
      cannot be cleanly attributed to the agent under evaluation.
"""
import hashlib
import os
import subprocess
from typing import Optional

# Sentinel stored for deleted files in a snapshot.
_DELETED_SENTINEL = '\x00DELETED\x00'


class NotAGitRepo(Exception):
    """Raised when a directory is not inside a git repository."""


# ---------------------------------------------------------------------------
# Low-level git helpers
# ---------------------------------------------------------------------------

def _run_git(args: list, cwd: str) -> tuple:
    """Run a git subcommand. Returns (stdout_bytes, stderr_bytes, returncode)."""
    result = subprocess.run(
        ['git'] + args,
        cwd=cwd,
        capture_output=True,
    )
    return result.stdout, result.stderr, result.returncode


def _get_repo_root(repo_dir: str) -> str:
    """Return the absolute git repo root for repo_dir.

    Raises:
        NotAGitRepo: if repo_dir is not inside a git repository.
    """
    stdout, _stderr, rc = _run_git(['rev-parse', '--show-toplevel'], cwd=repo_dir)
    if rc != 0:
        raise NotAGitRepo(f'Not a git repository: {repo_dir}')
    return stdout.decode('utf-8', errors='replace').strip()


def _sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of the file at path.

    Returns _DELETED_SENTINEL if the file cannot be read (deleted, permission denied, etc.).
    """
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except (IOError, OSError):
        return _DELETED_SENTINEL
    return h.hexdigest()


def _parse_porcelain_v1_z(output: bytes) -> list:
    """Parse ``git status --porcelain=v1 -z`` output.

    With ``-z``, entries are NUL-terminated instead of newline-terminated.
    For renames and copies, the format is:
        XY SP NEW_PATH NUL OLD_PATH NUL
    so the OLD_PATH is consumed as a second token.

    Returns a list of repo-relative path strings (new path for renames).
    """
    paths: list = []
    tokens = output.split(b'\x00')
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # Each entry is at least XY + space + one filename char = 4 bytes.
        if len(token) < 4:
            i += 1
            continue
        xy = token[:2].decode('ascii', errors='replace')
        path = token[3:].decode('utf-8', errors='replace')
        x_status = xy[0]

        if x_status in ('R', 'C'):
            # Rename/copy: next NUL-delimited token is the old path; skip it.
            i += 2
        else:
            i += 1

        if path:
            paths.append(path)

    return paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snapshot(repo_dir: str) -> dict:
    """Snapshot the git working-tree state.

    Captures all files that differ from HEAD (modified, added, untracked,
    deleted) and hashes their current content with SHA-256.

    Args:
        repo_dir: path to or inside a git repository.

    Returns:
        A dict mapping repo-relative paths to SHA-256 hex digests.
        Deleted files map to _DELETED_SENTINEL.
        On any error, returns ``{"_error": "<message>"}`` instead of raising.
    """
    try:
        root = _get_repo_root(repo_dir)
    except NotAGitRepo as exc:
        return {'_error': str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {'_error': f'Failed to resolve repo root: {exc}'}

    try:
        stdout, stderr, rc = _run_git(['status', '--porcelain=v1', '-z'], cwd=root)
        if rc != 0:
            err = stderr.decode('utf-8', errors='replace').strip()
            return {'_error': f'git status failed (rc={rc}): {err}'}

        paths = _parse_porcelain_v1_z(stdout)

        result: dict = {}
        for relpath in paths:
            abs_path = os.path.join(root, relpath)

            # git status reports untracked directories as "dirname/" (with trailing
            # slash).  Walk them to include each individual file.
            if relpath.endswith('/') and os.path.isdir(abs_path):
                for dirpath, _dirs, filenames in os.walk(abs_path):
                    for fname in filenames:
                        file_abs = os.path.join(dirpath, fname)
                        file_rel = os.path.relpath(file_abs, root)
                        result[file_rel] = _sha256_file(file_abs)
            elif os.path.isfile(abs_path):
                result[relpath] = _sha256_file(abs_path)
            else:
                result[relpath] = _DELETED_SENTINEL

        return result

    except Exception as exc:  # noqa: BLE001
        return {'_error': f'Snapshot failed: {exc}'}


def delta(before: dict, repo_dir: str) -> dict:
    """Compute what changed between the before snapshot and the current working tree.

    Args:
        before: snapshot dict from a prior call to ``snapshot()``.
        repo_dir: path to the git repository.

    Returns:
        {
            "changed":   set[str],  # repo-relative paths whose hash differs from before
            "ambiguous": bool,      # True when before had pre-existing uncommitted changes
        }

    Ambiguity: if ``before`` is non-empty and has no ``_error`` key, it means the repo
    had uncommitted changes at snapshot time. Changes from other agents or the user
    cannot be cleanly separated, so ``ambiguous=True`` is set honestly rather than
    asserting false precision (v1 sequential-case behaviour per spec).
    """
    # Ambiguity: before had pre-existing uncommitted changes.
    before_has_changes = bool(
        before
        and '_error' not in before
        and len(before) > 0
    )

    after = snapshot(repo_dir)

    if '_error' in after:
        # Cannot determine what changed; return empty set with ambiguity noted.
        return {'changed': set(), 'ambiguous': before_has_changes}

    changed: set = set()

    # Files in after that are new or have a different hash than in before.
    for path, after_hash in after.items():
        if after_hash != before.get(path):
            changed.add(path)

    # Files that were in before but have disappeared from after entirely
    # (e.g., deleted and then staged/committed during the agent run).
    for path in before:
        if path != '_error' and path not in after:
            changed.add(path)

    return {'changed': changed, 'ambiguous': before_has_changes}
