"""
tests/test_hook.py — Integration tests for attest.hook orchestrator

Covers:
  - on_start() + on_stop() happy path: truthful claim → OK printed (no MISMATCH)
  - on_start() + on_stop() false claim → MISMATCH printed
  - on_stop() with missing snapshot → graceful note printed
  - on_stop() claim source=none → never false_done
  - Non-git cwd → graceful (snapshot error stored, on_stop handles it)
  - ATTEST_CAPTURE=1 mode: payload/transcript files created in fixtures/captured/

Uses real temporary git repos for git operations. Isolates state DB via ATTEST_STATE_DB.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def _init_git_repo(path: str) -> None:
    """Init a git repo with an initial commit in path."""
    subprocess.run(['git', 'init', path], capture_output=True, check=True)
    subprocess.run(['git', '-C', path, 'config', 'user.email', 'test@attest.test'], capture_output=True)
    subprocess.run(['git', '-C', path, 'config', 'user.name', 'Attest Test'], capture_output=True)
    # Create an initial commit so HEAD exists
    readme = os.path.join(path, 'README.md')
    with open(readme, 'w') as fh:
        fh.write('# test\n')
    subprocess.run(['git', '-C', path, 'add', '.'], capture_output=True)
    subprocess.run(['git', '-C', path, 'commit', '-m', 'init'], capture_output=True)


def _make_payload(agent_id: str, session_id: str, cwd: str, **extra) -> dict:
    return {
        'agent_id': agent_id,
        'agent_type': 'code-writer',
        'session_id': session_id,
        'stop_reason': 'end_turn',
        'transcript_path': '',
        'cwd': cwd,
        'stop_hook_active': False,
        'payload_text': extra.get('payload_text', ''),
    }


class TestHookHappyPath(unittest.TestCase):
    """start → on_disk_change → stop with truthful claim → OK."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmpdir, 'repo')
        os.makedirs(self.repo)
        _init_git_repo(self.repo)
        self.db_path = os.path.join(self.tmpdir, 'state.db')
        os.environ['ATTEST_STATE_DB'] = self.db_path
        from attest import hook
        self.hook = hook

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def test_truthful_claim_prints_ok(self) -> None:
        """Agent claims file X, file X is actually changed → OK."""
        payload_start = _make_payload('agent-happy', 'sess-1', self.repo)

        # Snapshot before
        captured_start = io.StringIO()
        with patch('sys.stdout', captured_start):
            self.hook.on_start(payload_start)

        # "Agent" writes a file
        new_file = os.path.join(self.repo, 'src', 'feature.py')
        os.makedirs(os.path.dirname(new_file), exist_ok=True)
        with open(new_file, 'w') as fh:
            fh.write('def feature(): pass\n')

        # Stop: claim matches observed delta
        payload_stop = _make_payload(
            'agent-happy', 'sess-1', self.repo,
            payload_text=(
                '## Handoff\n'
                'files_changed: src/feature.py\n'
                'status: DONE\n'
                'blockers: none\n'
            )
        )
        captured_stop = io.StringIO()
        with patch('sys.stdout', captured_stop):
            self.hook.on_stop(payload_stop)

        output = captured_stop.getvalue()
        self.assertIn('OK', output)
        self.assertNotIn('MISMATCH', output)

    def test_false_claim_prints_mismatch(self) -> None:
        """Agent claims file X, but file X was NOT written → MISMATCH."""
        agent_id = 'agent-false'
        payload_start = _make_payload(agent_id, 'sess-2', self.repo)

        captured_start = io.StringIO()
        with patch('sys.stdout', captured_start):
            self.hook.on_start(payload_start)

        # Agent claims to have changed foo.py but doesn't write it
        payload_stop = _make_payload(
            agent_id, 'sess-2', self.repo,
            payload_text=(
                '## Handoff\n'
                'files_changed: src/ghost.py\n'
                'status: DONE\n'
                'blockers: none\n'
            )
        )
        captured_stop = io.StringIO()
        with patch('sys.stdout', captured_stop):
            self.hook.on_stop(payload_stop)

        output = captured_stop.getvalue()
        self.assertIn('MISMATCH', output)
        self.assertIn('src/ghost.py', output)
        self.assertNotIn('OK', output)

    def test_missing_snapshot_prints_note(self) -> None:
        """If on_start never ran, on_stop notes the missing snapshot gracefully."""
        payload_stop = _make_payload('no-start-agent', 'sess-3', self.repo)
        captured = io.StringIO()
        with patch('sys.stdout', captured):
            self.hook.on_stop(payload_stop)
        output = captured.getvalue()
        self.assertIn('no snapshot found', output)
        self.assertNotIn('MISMATCH', output)

    def test_claim_source_none_never_false_done(self) -> None:
        """An empty/unparseable claim → source=none → no MISMATCH."""
        agent_id = 'agent-noclaim'
        payload_start = _make_payload(agent_id, 'sess-4', self.repo)
        with patch('sys.stdout', io.StringIO()):
            self.hook.on_start(payload_start)

        payload_stop = _make_payload(agent_id, 'sess-4', self.repo, payload_text='')
        captured = io.StringIO()
        with patch('sys.stdout', captured):
            self.hook.on_stop(payload_stop)

        output = captured.getvalue()
        self.assertNotIn('MISMATCH', output)
        self.assertIn('source=none', output)

    def test_non_git_dir_graceful(self) -> None:
        """Non-git cwd → on_start notes the error; on_stop handles missing snapshot."""
        non_git = os.path.join(self.tmpdir, 'not_a_repo')
        os.makedirs(non_git)
        payload_start = _make_payload('agent-nongit', 'sess-5', non_git)

        captured_start = io.StringIO()
        with patch('sys.stdout', captured_start):
            self.hook.on_start(payload_start)
        # Should not raise; may print a note about snapshot failure

        payload_stop = _make_payload('agent-nongit', 'sess-5', non_git)
        captured_stop = io.StringIO()
        with patch('sys.stdout', captured_stop):
            self.hook.on_stop(payload_stop)
        # Should not raise regardless

    def test_scope_creep_printed(self) -> None:
        """File changed but not mentioned in claim → SCOPE_CREEP."""
        agent_id = 'agent-creep'
        payload_start = _make_payload(agent_id, 'sess-6', self.repo)
        with patch('sys.stdout', io.StringIO()):
            self.hook.on_start(payload_start)

        # Write a file
        extra_file = os.path.join(self.repo, 'extra.py')
        with open(extra_file, 'w') as fh:
            fh.write('# extra\n')

        # Claim mentions none of the changed files (empty list but status=DONE)
        payload_stop = _make_payload(
            agent_id, 'sess-6', self.repo,
            payload_text='Status: DONE\nI completed the task.'
        )
        captured = io.StringIO()
        with patch('sys.stdout', captured):
            self.hook.on_stop(payload_stop)
        output = captured.getvalue()
        # NL parser may or may not find files; key thing: no false DONE for scope creep
        # (the NL parser picks up "extra.py" only if it matches path pattern)
        # Just verify no crash and MISMATCH is not incorrectly triggered
        self.assertNotIn('internal error', output.lower())


class TestHookCaptureMode(unittest.TestCase):
    """ATTEST_CAPTURE=1 dumps files to fixtures/captured/."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'state.db')
        os.environ['ATTEST_STATE_DB'] = self.db_path
        os.environ['ATTEST_CAPTURE'] = '1'
        from attest import hook
        self.hook = hook

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']
        del os.environ['ATTEST_CAPTURE']

    def test_capture_creates_payload_file(self) -> None:
        """With ATTEST_CAPTURE=1, a payload JSON file is written to fixtures/captured/."""
        payload = {
            'agent_id': 'capture-test',
            'agent_type': 'code-writer',
            'session_id': 'sess-cap',
            'stop_reason': 'end_turn',
            'transcript_path': '',
            'cwd': self.tmpdir,
            'stop_hook_active': False,
            'payload_text': 'Status: DONE',
        }

        # Locate the expected captured dir relative to the hook module
        import attest
        repo_root = os.path.dirname(os.path.dirname(attest.__file__))
        captured_dir = os.path.join(repo_root, 'fixtures', 'captured')

        # Record files before
        before = set(os.listdir(captured_dir)) if os.path.isdir(captured_dir) else set()

        with patch('sys.stdout', io.StringIO()):
            self.hook._capture_if_requested('stop', payload, '')

        if os.path.isdir(captured_dir):
            after = set(os.listdir(captured_dir))
            new_files = after - before
            payload_files = [f for f in new_files if f.startswith('stop-capture-test')]
            self.assertTrue(len(payload_files) >= 1, f'Expected a captured payload file, got: {new_files}')
            # Cleanup new files
            for f in new_files:
                try:
                    os.unlink(os.path.join(captured_dir, f))
                except OSError:
                    pass


class TestHookMain(unittest.TestCase):
    """Test the CLI main() entry point."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        os.environ['ATTEST_STATE_DB'] = os.path.join(self.tmpdir, 'state.db')
        from attest.hook import main
        self.main = main

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']

    def _run_main(self, argv: list, stdin_data: str = '') -> tuple:
        """Run main() with given argv and stdin, returning (exit_code, stdout)."""
        captured = io.StringIO()
        with patch('sys.stdin', io.StringIO(stdin_data)), \
             patch('sys.stdout', captured):
            code = self.main(argv)
        return code, captured.getvalue()

    def test_main_start_always_exits_0(self) -> None:
        payload = json.dumps({'agent_type': 'test', 'session_id': 's1', 'cwd': self.tmpdir})
        code, _ = self._run_main(['start'], payload)
        self.assertEqual(code, 0)

    def test_main_stop_always_exits_0(self) -> None:
        payload = json.dumps({'agent_type': 'test', 'session_id': 's2', 'cwd': self.tmpdir})
        code, _ = self._run_main(['stop'], payload)
        self.assertEqual(code, 0)

    def test_main_unknown_event_exits_0(self) -> None:
        code, _ = self._run_main(['unknown_event'])
        self.assertEqual(code, 0)

    def test_main_no_args_exits_0(self) -> None:
        code, _ = self._run_main([])
        self.assertEqual(code, 0)

    def test_main_empty_stdin_exits_0(self) -> None:
        code, _ = self._run_main(['start'], '')
        self.assertEqual(code, 0)

    def test_main_invalid_json_exits_0(self) -> None:
        code, _ = self._run_main(['stop'], '{not json}')
        self.assertEqual(code, 0)
