"""
tests/test_gitdelta.py — Unit tests for attest.gitdelta (snapshot + delta).

All tests use real git repos in tempfile.TemporaryDirectory() — they NEVER
touch $HOME or any project directory. Each test gets an isolated temp repo
created in setUp() and torn down in tearDown().

Covers:
  - snapshot of a clean repo (no uncommitted changes) → empty dict
  - snapshot with a modified tracked file
  - snapshot with an untracked new file
  - snapshot with a deleted tracked file
  - snapshot of a non-git directory → returns {"_error": ...}
  - delta: changed = paths that differ between before and current
  - delta: ambiguous=True when before had pre-existing uncommitted changes
  - delta: ambiguous=False when before was empty (clean tree)
  - NotAGitRepo is raised by _get_repo_root (tested indirectly via snapshot error key)
"""
import os
import subprocess
import tempfile
import unittest

from attest.gitdelta import (
    snapshot, delta, NotAGitRepo, _DELETED_SENTINEL, repo_root, path_on_disk,
    _sha256_file, _MAX_HASH_BYTES,
)


# ---------------------------------------------------------------------------
# Git fixture helpers
# ---------------------------------------------------------------------------

def _git(args: list, cwd: str) -> subprocess.CompletedProcess:
    """Run a git command in cwd. Raises CalledProcessError on failure."""
    return subprocess.run(
        ['git'] + args,
        cwd=cwd,
        capture_output=True,
        check=True,
    )


def _init_repo(path: str) -> None:
    """Create a minimal git repo with an initial commit."""
    _git(['init', '-b', 'main'], path)
    _git(['config', 'user.email', 'test@attest.local'], path)
    _git(['config', 'user.name', 'Attest Test'], path)
    # Create an initial commit so HEAD exists.
    readme = os.path.join(path, 'README.md')
    with open(readme, 'w') as fh:
        fh.write('# test repo\n')
    _git(['add', 'README.md'], path)
    _git(['commit', '-m', 'initial commit'], path)


def _write_file(repo: str, relpath: str, content: str) -> None:
    """Write a file inside the repo."""
    full = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w') as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSnapshot(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_clean_repo_returns_empty(self) -> None:
        """A repo with no uncommitted changes produces an empty snapshot."""
        snap = snapshot(self.repo)
        self.assertNotIn('_error', snap)
        self.assertEqual(snap, {})

    def test_modified_tracked_file(self) -> None:
        """Modifying a tracked file includes it in the snapshot."""
        _write_file(self.repo, 'README.md', '# modified\n')
        snap = snapshot(self.repo)
        self.assertNotIn('_error', snap)
        self.assertIn('README.md', snap)
        # Hash should be a 64-char SHA-256 hex string.
        self.assertRegex(snap['README.md'], r'^[0-9a-f]{64}$')

    def test_new_untracked_file(self) -> None:
        """An untracked new file is included in the snapshot."""
        _write_file(self.repo, 'src/new.py', 'print("hello")\n')
        snap = snapshot(self.repo)
        self.assertNotIn('_error', snap)
        self.assertIn('src/new.py', snap)

    def test_deleted_tracked_file(self) -> None:
        """Deleting a tracked file stores the sentinel."""
        # First commit a tracked file.
        _write_file(self.repo, 'will_delete.txt', 'content\n')
        _git(['add', 'will_delete.txt'], self.repo)
        _git(['commit', '-m', 'add file'], self.repo)
        # Now delete it from the working tree.
        os.remove(os.path.join(self.repo, 'will_delete.txt'))
        snap = snapshot(self.repo)
        self.assertNotIn('_error', snap)
        self.assertIn('will_delete.txt', snap)
        self.assertEqual(snap['will_delete.txt'], _DELETED_SENTINEL)

    def test_non_git_directory_returns_error(self) -> None:
        """A non-git directory returns {'_error': ...} instead of raising."""
        with tempfile.TemporaryDirectory() as non_git:
            snap = snapshot(non_git)
        self.assertIn('_error', snap)

    def test_snapshot_values_are_strings(self) -> None:
        """All snapshot values are strings (sha256 hex or sentinel)."""
        _write_file(self.repo, 'file.py', 'x = 1\n')
        snap = snapshot(self.repo)
        for value in snap.values():
            self.assertIsInstance(value, str)

    def test_snapshot_keys_are_repo_relative(self) -> None:
        """Snapshot keys are repo-relative (no leading slash or repo prefix)."""
        _write_file(self.repo, 'subdir/file.py', 'x = 1\n')
        snap = snapshot(self.repo)
        for key in snap:
            self.assertFalse(key.startswith('/'), f'Key is absolute: {key!r}')
            self.assertFalse(
                key.startswith(self.repo),
                f'Key contains repo prefix: {key!r}',
            )

    def test_snapshot_consistent_hash_for_same_content(self) -> None:
        """Two snapshots of the same file content produce the same hash."""
        _write_file(self.repo, 'stable.py', 'x = 42\n')
        snap1 = snapshot(self.repo)
        snap2 = snapshot(self.repo)
        self.assertEqual(snap1.get('stable.py'), snap2.get('stable.py'))

    def test_snapshot_different_hash_after_edit(self) -> None:
        """Editing a file changes its hash in the snapshot."""
        _write_file(self.repo, 'README.md', 'version 1\n')
        snap1 = snapshot(self.repo)
        _write_file(self.repo, 'README.md', 'version 2\n')
        snap2 = snapshot(self.repo)
        self.assertNotEqual(snap1.get('README.md'), snap2.get('README.md'))


class TestDelta(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_delta_empty_when_nothing_changed(self) -> None:
        """No changes between before and current → empty changed set."""
        before = snapshot(self.repo)  # clean repo → {}
        result = delta(before, self.repo)
        self.assertEqual(result['changed'], set())
        self.assertFalse(result['ambiguous'])

    def test_delta_detects_new_file(self) -> None:
        """A new file created after the before-snapshot appears in changed."""
        before = snapshot(self.repo)
        _write_file(self.repo, 'new.py', 'x = 1\n')
        result = delta(before, self.repo)
        self.assertIn('new.py', result['changed'])

    def test_delta_detects_modified_file(self) -> None:
        """Modifying a tracked file after the snapshot → it appears in changed."""
        before = snapshot(self.repo)
        _write_file(self.repo, 'README.md', '# changed\n')
        result = delta(before, self.repo)
        self.assertIn('README.md', result['changed'])

    def test_delta_detects_deleted_file(self) -> None:
        """Deleting a tracked file after the snapshot → it appears in changed."""
        _write_file(self.repo, 'bye.txt', 'content\n')
        _git(['add', 'bye.txt'], self.repo)
        _git(['commit', '-m', 'add bye'], self.repo)
        before = snapshot(self.repo)  # clean → {}
        os.remove(os.path.join(self.repo, 'bye.txt'))
        result = delta(before, self.repo)
        self.assertIn('bye.txt', result['changed'])

    def test_delta_file_unchanged_since_snapshot_not_in_changed(self) -> None:
        """A file present in before-snapshot with same hash → not in changed."""
        # Create a file, snapshot with it already present, don't touch it.
        _write_file(self.repo, 'stable.py', 'x = 1\n')
        before = snapshot(self.repo)  # stable.py is uncommitted, in before
        result = delta(before, self.repo)
        # stable.py hash is same before and after → not in changed
        self.assertNotIn('stable.py', result['changed'])

    def test_delta_ambiguous_when_before_nonempty(self) -> None:
        """Ambiguity is set when before-snapshot had pre-existing changes."""
        # Create an uncommitted file before the snapshot.
        _write_file(self.repo, 'preexisting.py', 'x = 1\n')
        before = snapshot(self.repo)  # before is non-empty
        self.assertTrue(before)  # sanity: before is not empty
        result = delta(before, self.repo)
        self.assertTrue(result['ambiguous'])

    def test_delta_not_ambiguous_when_before_empty(self) -> None:
        """No ambiguity when before-snapshot was empty (clean tree)."""
        before = snapshot(self.repo)  # clean → {}
        self.assertEqual(before, {})
        _write_file(self.repo, 'new.py', 'x = 1\n')
        result = delta(before, self.repo)
        self.assertFalse(result['ambiguous'])

    def test_delta_changed_is_a_set(self) -> None:
        """delta() returns 'changed' as a set, not a list."""
        before = snapshot(self.repo)
        result = delta(before, self.repo)
        self.assertIsInstance(result['changed'], set)

    def test_delta_multiple_files(self) -> None:
        """Multiple files changed since before all appear in changed."""
        before = snapshot(self.repo)
        _write_file(self.repo, 'a.py', 'a\n')
        _write_file(self.repo, 'b.py', 'b\n')
        result = delta(before, self.repo)
        self.assertIn('a.py', result['changed'])
        self.assertIn('b.py', result['changed'])


class TestDeltaReliability(unittest.TestCase):
    """The explicit reliable flag — the Phase-2 landmine guard."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_clean_repo_delta_is_reliable(self) -> None:
        before = snapshot(self.repo)
        result = delta(before, self.repo)
        self.assertTrue(result['reliable'])

    def test_changed_repo_delta_is_reliable(self) -> None:
        before = snapshot(self.repo)
        _write_file(self.repo, 'new.py', 'x = 1\n')
        result = delta(before, self.repo)
        self.assertTrue(result['reliable'])
        self.assertIn('new.py', result['changed'])

    def test_after_error_is_unreliable(self) -> None:
        """A valid before-snapshot but a non-git dir at stop time → unreliable, empty."""
        before = snapshot(self.repo)  # clean, reliable baseline
        with tempfile.TemporaryDirectory() as non_git:
            result = delta(before, non_git)
        self.assertFalse(result['reliable'])
        self.assertEqual(result['changed'], set())

    def test_before_error_is_unreliable(self) -> None:
        """A before-snapshot carrying _error (start failed) → unreliable, empty."""
        before = {'_error': 'Not a git repository: /nope'}
        result = delta(before, self.repo)
        self.assertFalse(result['reliable'])
        self.assertEqual(result['changed'], set())

    def test_unreliable_never_inferred_from_emptiness(self) -> None:
        """An empty-but-reliable delta (agent changed nothing) stays reliable=True."""
        before = snapshot(self.repo)  # clean
        result = delta(before, self.repo)  # nothing changed
        self.assertEqual(result['changed'], set())
        self.assertTrue(result['reliable'])  # empty != unreliable


class TestRepoRootAndPathOnDisk(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_repo_root_resolves_toplevel(self) -> None:
        root = repo_root(self.repo)
        self.assertIsNotNone(root)
        # The resolved root points at the same repo (realpath-equal).
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.repo))

    def test_repo_root_none_for_non_git(self) -> None:
        with tempfile.TemporaryDirectory() as non_git:
            self.assertIsNone(repo_root(non_git))

    def test_path_on_disk_true_for_existing_relative(self) -> None:
        _write_file(self.repo, 'src/real.py', 'x = 1\n')
        root = repo_root(self.repo) or self.repo
        self.assertTrue(path_on_disk(root, 'src/real.py'))

    def test_path_on_disk_false_for_phantom(self) -> None:
        root = repo_root(self.repo) or self.repo
        self.assertFalse(path_on_disk(root, 'src/ghost.py'))

    def test_path_on_disk_true_for_existing_absolute(self) -> None:
        _write_file(self.repo, 'abs.py', 'x = 1\n')
        abs_path = os.path.join(self.repo, 'abs.py')
        self.assertTrue(path_on_disk(self.repo, abs_path))

    def test_path_on_disk_empty_is_false(self) -> None:
        self.assertFalse(path_on_disk(self.repo, ''))

    def test_path_on_disk_resolves_against_payload_cwd(self) -> None:
        """A cwd-relative claim resolves under the subagent's payload cwd subdir."""
        sub = os.path.join(self.repo, 'sub')
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, 'foo.py'), 'w') as fh:
            fh.write('x = 1\n')
        root = repo_root(self.repo) or self.repo
        # Not found relative to the repo root...
        self.assertFalse(path_on_disk(root, 'foo.py'))
        # ...but found when the payload cwd (the subdir) is supplied.
        self.assertTrue(path_on_disk(root, 'foo.py', cwd=sub))


class TestNotAGitRepo(unittest.TestCase):
    """_get_repo_root raises NotAGitRepo for non-git dirs; snapshot surfaces as _error."""

    def test_not_a_git_repo_exception_class(self) -> None:
        """NotAGitRepo is importable and is an Exception subclass."""
        self.assertTrue(issubclass(NotAGitRepo, Exception))

    def test_snapshot_non_git_returns_error_key(self) -> None:
        with tempfile.TemporaryDirectory() as non_git:
            snap = snapshot(non_git)
        self.assertIn('_error', snap)
        self.assertIsInstance(snap['_error'], str)

    def test_snapshot_error_key_message_mentions_git(self) -> None:
        with tempfile.TemporaryDirectory() as non_git:
            snap = snapshot(non_git)
        # Error message should indicate git/repo nature.
        self.assertTrue(
            'git' in snap['_error'].lower() or 'repo' in snap['_error'].lower(),
            f"Unexpected error message: {snap['_error']!r}",
        )


class TestSha256FileHashCap(unittest.TestCase):
    """2026-06-22 audit fix: large files return a meta fingerprint, not a content hash."""

    # Use an env-override threshold of 100 bytes so tests stay fast.
    _SMALL_THRESHOLD = '100'

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = self._tmpdir.name
        os.environ['ATTEST_MAX_HASH_BYTES'] = self._SMALL_THRESHOLD

    def tearDown(self) -> None:
        del os.environ['ATTEST_MAX_HASH_BYTES']
        self._tmpdir.cleanup()

    def _write(self, name: str, content: bytes) -> str:
        p = os.path.join(self.dir, name)
        with open(p, 'wb') as fh:
            fh.write(content)
        return p

    def test_large_file_yields_meta_prefix(self) -> None:
        """A file exceeding the threshold returns a ``meta:``-prefixed fingerprint."""
        p = self._write('big.bin', b'x' * 200)  # 200 > 100 byte threshold
        result = _sha256_file(p)
        self.assertTrue(result.startswith('meta:'), f'Expected meta: prefix, got {result!r}')

    def test_large_file_fingerprint_not_hex64(self) -> None:
        """The meta fingerprint is not a 64-char hex string (no collision with sha256)."""
        p = self._write('big2.bin', b'y' * 200)
        result = _sha256_file(p)
        # sha256 hex is exactly 64 lowercase hex chars — meta prefix breaks this.
        self.assertNotRegex(result, r'^[0-9a-f]{64}$')

    def test_large_file_content_not_read(self) -> None:
        """Content of a large file is NOT read (builtin open is never called for it)."""
        from unittest.mock import patch
        p = self._write('big3.bin', b'z' * 200)
        with patch('builtins.open') as mock_open:
            _sha256_file(p)
        mock_open.assert_not_called()

    def test_large_file_fingerprint_changes_on_content_write(self) -> None:
        """Appending bytes to a large file changes its fingerprint (size changes)."""
        p = self._write('large_mut.bin', b'a' * 200)
        fp1 = _sha256_file(p)
        with open(p, 'ab') as fh:
            fh.write(b'b' * 50)  # size now 250 — still > 100 threshold
        fp2 = _sha256_file(p)
        self.assertNotEqual(fp1, fp2)

    def test_large_file_fingerprint_changes_on_mtime_touch(self) -> None:
        """Touching a large file's mtime changes its fingerprint (mtime_ns changes)."""
        p = self._write('large_touch.bin', b'c' * 200)
        fp1 = _sha256_file(p)
        # Advance mtime by 2 seconds to guarantee st_mtime_ns differs.
        current = os.stat(p)
        os.utime(p, (current.st_atime + 2, current.st_mtime + 2))
        fp2 = _sha256_file(p)
        self.assertNotEqual(fp1, fp2)

    def test_small_file_yields_64_char_hex(self) -> None:
        """A file at or below the threshold yields a real SHA-256 hex digest."""
        p = self._write('small.py', b'x = 1\n')  # 6 bytes < 100 byte threshold
        result = _sha256_file(p)
        self.assertRegex(result, r'^[0-9a-f]{64}$')

    def test_missing_file_returns_deleted_sentinel(self) -> None:
        """A path that does not exist returns _DELETED_SENTINEL (unchanged behaviour)."""
        result = _sha256_file(os.path.join(self.dir, 'ghost.py'))
        self.assertEqual(result, _DELETED_SENTINEL)

    def test_default_threshold_is_10mb(self) -> None:
        """The module-level constant equals 10 MB."""
        self.assertEqual(_MAX_HASH_BYTES, 10 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
