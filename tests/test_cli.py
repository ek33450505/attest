"""
tests/test_cli.py — Unit tests for attest.cli (command-line interface).

All tests that need a real git repo use isolated temp directories.
Tests invoke cli.main() directly (not subprocess) for speed.

Covers:
  - attest --version
  - attest snapshot --repo <git-dir> (happy path + non-git error)
  - attest verify --claim-file <md> --before <json> --repo <dir> (happy path + errors)
  - Missing required args produce a non-zero exit via SystemExit
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from attest.cli import main


# ---------------------------------------------------------------------------
# Git fixture helpers (duplicated minimally from test_gitdelta for isolation)
# ---------------------------------------------------------------------------

def _git(args: list, cwd: str) -> None:
    subprocess.run(['git'] + args, cwd=cwd, capture_output=True, check=True)


def _init_repo(path: str) -> None:
    _git(['init', '-b', 'main'], path)
    _git(['config', 'user.email', 'test@attest.local'], path)
    _git(['config', 'user.name', 'Attest Test'], path)
    readme = os.path.join(path, 'README.md')
    with open(readme, 'w') as fh:
        fh.write('# test\n')
    _git(['add', 'README.md'], path)
    _git(['commit', '-m', 'init'], path)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVersion(unittest.TestCase):

    def test_version_flag_exits_zero_and_prints_version(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            with patch('sys.stdout', new_callable=StringIO) as mock_out:
                main(['--version'])
        self.assertEqual(cm.exception.code, 0)
        # argparse prints to stdout for --version
        out = mock_out.getvalue()
        self.assertIn('attest', out)

    def test_version_string_contains_semver(self) -> None:
        import re
        from attest import __version__
        self.assertRegex(__version__, r'^\d+\.\d+\.\d+')


class TestSnapshotCommand(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_snapshot_clean_repo_prints_empty_json(self) -> None:
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            rc = main(['snapshot', '--repo', self.repo])
        self.assertEqual(rc, 0)
        data = json.loads(mock_out.getvalue())
        self.assertEqual(data, {})

    def test_snapshot_with_new_file_includes_it(self) -> None:
        new_file = os.path.join(self.repo, 'new.py')
        _write(new_file, 'x = 1\n')
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            rc = main(['snapshot', '--repo', self.repo])
        self.assertEqual(rc, 0)
        data = json.loads(mock_out.getvalue())
        self.assertIn('new.py', data)

    def test_snapshot_non_git_dir_returns_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as non_git:
            with patch('sys.stderr', new_callable=StringIO) as mock_err:
                rc = main(['snapshot', '--repo', non_git])
        self.assertEqual(rc, 1)
        err_data = json.loads(mock_err.getvalue())
        self.assertIn('error', err_data)

    def test_snapshot_output_is_valid_json(self) -> None:
        _write(os.path.join(self.repo, 'a.py'), 'a\n')
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            main(['snapshot', '--repo', self.repo])
        # Should not raise.
        json.loads(mock_out.getvalue())


class TestVerifyCommand(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        _init_repo(self.repo)
        self._workdir = tempfile.TemporaryDirectory()
        self.workdir = self._workdir.name

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        self._workdir.cleanup()

    def _before_json(self, snap: dict) -> str:
        """Write a before-snapshot JSON to a temp file and return its path."""
        p = os.path.join(self.workdir, 'before.json')
        with open(p, 'w') as fh:
            json.dump(snap, fh)
        return p

    def _claim_file(self, text: str) -> str:
        """Write a claim markdown to a temp file and return its path."""
        p = os.path.join(self.workdir, 'claim.md')
        with open(p, 'w') as fh:
            fh.write(text)
        return p

    def test_verify_happy_path_prints_verdict(self) -> None:
        before = {}  # clean repo snapshot
        before_path = self._before_json(before)
        claim_text = (
            "## Handoff\n"
            "files_changed: new.py\n"
            "status: DONE\n"
            "blockers: none\n"
        )
        claim_path = self._claim_file(claim_text)
        _write(os.path.join(self.repo, 'new.py'), 'x = 1\n')
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            rc = main(['verify', '--claim-file', claim_path, '--before', before_path, '--repo', self.repo])
        self.assertEqual(rc, 0)
        verdict = json.loads(mock_out.getvalue())
        self.assertIn('false_done', verdict)
        self.assertIn('claimed_but_unchanged', verdict)
        self.assertIn('observed_but_unclaimed', verdict)
        self.assertIn('reason', verdict)

    def test_verify_false_done_detected(self) -> None:
        before = {}
        before_path = self._before_json(before)
        # Claim says ghost.py was changed, but we actually change something else.
        claim_text = (
            "## Handoff\n"
            "files_changed: ghost.py\n"
            "status: DONE\n"
            "blockers: none\n"
        )
        claim_path = self._claim_file(claim_text)
        _write(os.path.join(self.repo, 'real.py'), 'x = 1\n')
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            rc = main(['verify', '--claim-file', claim_path, '--before', before_path, '--repo', self.repo])
        self.assertEqual(rc, 0)  # Phase 1a is print-only, never blocks
        verdict = json.loads(mock_out.getvalue())
        self.assertTrue(verdict['false_done'])
        self.assertIn('ghost.py', verdict['claimed_but_unchanged'])

    def test_verify_missing_before_file_exits_1(self) -> None:
        claim_path = self._claim_file('Status: DONE\n')
        with patch('sys.stderr', new_callable=StringIO):
            rc = main([
                'verify',
                '--claim-file', claim_path,
                '--before', '/nonexistent/before.json',
                '--repo', self.repo,
            ])
        self.assertEqual(rc, 1)

    def test_verify_missing_claim_file_exits_1(self) -> None:
        before_path = self._before_json({})
        with patch('sys.stderr', new_callable=StringIO):
            rc = main([
                'verify',
                '--claim-file', '/nonexistent/claim.md',
                '--before', before_path,
                '--repo', self.repo,
            ])
        self.assertEqual(rc, 1)

    def test_verify_invalid_before_json_exits_1(self) -> None:
        bad_json = os.path.join(self.workdir, 'bad.json')
        _write(bad_json, 'not json{{')
        claim_path = self._claim_file('Status: DONE\n')
        with patch('sys.stderr', new_callable=StringIO):
            rc = main([
                'verify',
                '--claim-file', claim_path,
                '--before', bad_json,
                '--repo', self.repo,
            ])
        self.assertEqual(rc, 1)

    def test_verify_output_is_valid_json(self) -> None:
        before_path = self._before_json({})
        claim_path = self._claim_file('Status: DONE\n')
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            main(['verify', '--claim-file', claim_path, '--before', before_path, '--repo', self.repo])
        json.loads(mock_out.getvalue())  # must not raise

    def test_verify_sets_serialized_as_lists(self) -> None:
        """Verdict 'changed' sets are JSON arrays (lists), not Python sets."""
        before_path = self._before_json({})
        claim_path = self._claim_file('Status: DONE\nfiles_changed: a.py\n')
        _write(os.path.join(self.repo, 'a.py'), 'x\n')
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            main(['verify', '--claim-file', claim_path, '--before', before_path, '--repo', self.repo])
        data = json.loads(mock_out.getvalue())
        # These should be lists, not raise TypeError from JSON serialization
        self.assertIsInstance(data.get('claimed_but_unchanged', []), list)
        self.assertIsInstance(data.get('observed_but_unclaimed', []), list)


class TestVerifyUnreliableDelta(unittest.TestCase):
    """Fix: verify with a before-snapshot that carries _error → unreliable → exit 1."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workdir = self._tmpdir.name

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _before_json(self, snap: dict) -> str:
        p = os.path.join(self.workdir, 'before.json')
        with open(p, 'w') as fh:
            json.dump(snap, fh)
        return p

    def _claim_file(self, text: str) -> str:
        p = os.path.join(self.workdir, 'claim.md')
        with open(p, 'w') as fh:
            fh.write(text)
        return p

    def test_verify_non_git_repo_exits_1_with_error(self) -> None:
        """Fix: delta(reliable=False) on non-git dir → exit 1 + error message.

        Previously the NotAGitRepo except was unreachable; the reliable=False
        path was never handled, causing silent incorrect output.
        """
        with tempfile.TemporaryDirectory() as non_git:
            before_path = self._before_json({})
            claim_path = self._claim_file('Status: DONE\n')
            with patch('sys.stderr', new_callable=StringIO) as mock_err:
                rc = main([
                    'verify',
                    '--claim-file', claim_path,
                    '--before', before_path,
                    '--repo', non_git,
                ])
            self.assertEqual(rc, 1)
            err_text = mock_err.getvalue()
            self.assertTrue(err_text, 'Expected an error message on stderr')
            err_data = json.loads(err_text)
            self.assertIn('error', err_data)
            self.assertIn('unreliable', err_data['error'].lower())


class TestMissingArgs(unittest.TestCase):

    def test_no_subcommand_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            main([])
        self.assertNotEqual(cm.exception.code, 0)

    def test_snapshot_missing_repo_arg_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            main(['snapshot'])
        self.assertNotEqual(cm.exception.code, 0)

    def test_verify_missing_claim_file_arg_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            main(['verify', '--before', '/tmp/b.json', '--repo', '/tmp'])
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
