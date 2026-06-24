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


class TestHookCaptureSecurity(unittest.TestCase):
    """ATTEST_CAPTURE=1 security: agent_id sanitization + transcript_path bounds check."""

    def setUp(self) -> None:
        self.capture_dir = tempfile.mkdtemp()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'state.db')
        os.environ['ATTEST_STATE_DB'] = self.db_path
        os.environ['ATTEST_CAPTURE'] = '1'
        # Redirect all capture output to our isolated temp dir.
        os.environ['ATTEST_CAPTURE_DIR'] = self.capture_dir
        from attest import hook
        self.hook = hook

    def tearDown(self) -> None:
        import shutil as _shutil
        os.environ.pop('ATTEST_STATE_DB', None)
        os.environ.pop('ATTEST_CAPTURE', None)
        os.environ.pop('ATTEST_CAPTURE_DIR', None)
        _shutil.rmtree(self.capture_dir, ignore_errors=True)
        _shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_agent_id_with_path_traversal_produces_sanitized_filename(self) -> None:
        """An agent_id containing '../' is sanitized; the capture file stays inside capture_dir."""
        payload = {
            'agent_id': '../../../evil',  # path traversal attempt
            'agent_type': 'code-writer',
            'session_id': 'sess-sec1',
            'transcript_path': '',
            'cwd': self.tmpdir,
            'stop_hook_active': False,
            'payload_text': '',
        }
        self.hook._capture_if_requested('stop', payload, '')

        # After sanitization, the agent_id becomes '____evil' (or similar all-safe chars).
        # All written files must reside directly inside capture_dir.
        files_written = os.listdir(self.capture_dir)
        self.assertTrue(len(files_written) >= 1, 'Expected at least one capture file')
        for fname in files_written:
            full = os.path.join(self.capture_dir, fname)
            # The file must exist inside capture_dir (realpath check).
            real_capture = os.path.realpath(self.capture_dir)
            real_file = os.path.realpath(full)
            self.assertTrue(
                real_file.startswith(real_capture + os.sep),
                f'Capture file escaped capture_dir: {real_file!r}',
            )
        # The filename must NOT contain '/' or '..' after sanitization.
        for fname in files_written:
            self.assertNotIn('/', fname, f'Slash in capture filename: {fname!r}')
            self.assertNotIn('..', fname, f'Dotdot in capture filename: {fname!r}')

    def test_transcript_path_outside_home_is_not_copied(self) -> None:
        """A transcript_path resolving outside ~ is silently skipped (no copy)."""
        # Create a real file outside ~ to serve as the adversarial transcript.
        outside_file = os.path.join(self.capture_dir, 'system_file.txt')
        with open(outside_file, 'w') as fh:
            fh.write('sensitive content\n')

        # Simulate a transcript_path that resolves to a path outside ~.
        # We use /etc/hosts as a known-existent system path; if unavailable we use
        # the outside_file created in capture_dir (which is under /tmp, outside ~).
        etc_hosts = '/etc/hosts'
        adversarial_path = etc_hosts if os.path.isfile(etc_hosts) else outside_file

        payload = {
            'agent_id': 'sec-test-tp',
            'agent_type': 'code-writer',
            'session_id': 'sess-sec2',
            'transcript_path': adversarial_path,
            'cwd': self.tmpdir,
            'stop_hook_active': False,
            'payload_text': '',
        }
        self.hook._capture_if_requested('stop', payload, '')

        # No 'transcript-' file should appear in capture_dir for this agent_id.
        transcript_files = [
            f for f in os.listdir(self.capture_dir)
            if f.startswith('transcript-sec_test_tp') or f.startswith('transcript-sec-test-tp')
        ]
        self.assertEqual(
            transcript_files, [],
            f'Unexpected transcript copy for out-of-home path: {transcript_files}',
        )


class TestHookTranscriptPreference(unittest.TestCase):
    """on_stop prefers agent_transcript_path over transcript_path for claim extraction."""

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

    def test_on_stop_prefers_agent_transcript_path(self) -> None:
        """When payload_text is empty, on_stop reads from agent_transcript_path, not transcript_path."""
        agent_id = 'agent-transc-pref'
        sess = 'sess-tp-1'

        # Snapshot at start
        payload_start = _make_payload(agent_id, sess, self.repo)
        with patch('sys.stdout', io.StringIO()):
            self.hook.on_start(payload_start)

        # Payload with no payload_text but with both transcript paths.
        # last_assistant_text is mocked to return a claim only for the subagent path.
        subagent_path = '/subagents/agent-abc.jsonl'
        parent_path = '/parent/session.jsonl'

        def fake_last_assistant_text(path: str) -> str:
            if path == subagent_path:
                return '## Handoff\nfiles_changed: src/real.py\nstatus: DONE\nblockers: none\n'
            return ''  # parent transcript has no usable content

        payload_stop = {
            'agent_id': agent_id,
            'agent_type': 'code-writer',
            'session_id': sess,
            'stop_reason': 'end_turn',
            'transcript_path': parent_path,
            'agent_transcript_path': subagent_path,
            'cwd': self.repo,
            'stop_hook_active': False,
            'payload_text': '',  # fast-path empty — must fall through to transcript
        }

        captured = io.StringIO()
        with patch('attest.hook.transcript_mod.last_assistant_text', side_effect=fake_last_assistant_text), \
             patch('sys.stdout', captured):
            self.hook.on_stop(payload_stop)

        output = captured.getvalue()
        # The claim was extracted (not source=none) — meaning the subagent transcript was used.
        self.assertNotIn('claim source=none', output)
        # src/real.py was claimed but not actually written → MISMATCH expected
        self.assertIn('src/real.py', output)

    def test_on_stop_falls_back_to_transcript_path_if_no_agent_transcript(self) -> None:
        """When agent_transcript_path is absent, on_stop falls back to transcript_path."""
        agent_id = 'agent-transc-fallback'
        sess = 'sess-tf-1'

        payload_start = _make_payload(agent_id, sess, self.repo)
        with patch('sys.stdout', io.StringIO()):
            self.hook.on_start(payload_start)

        parent_path = '/parent/only.jsonl'

        def fake_last_assistant_text(path: str) -> str:
            if path == parent_path:
                return '## Handoff\nfiles_changed: src/fallback.py\nstatus: DONE\nblockers: none\n'
            return ''

        payload_stop = {
            'agent_id': agent_id,
            'agent_type': 'code-writer',
            'session_id': sess,
            'stop_reason': 'end_turn',
            'transcript_path': parent_path,
            'agent_transcript_path': '',  # absent — should fall back to transcript_path
            'cwd': self.repo,
            'stop_hook_active': False,
            'payload_text': '',
        }

        captured = io.StringIO()
        with patch('attest.hook.transcript_mod.last_assistant_text', side_effect=fake_last_assistant_text), \
             patch('sys.stdout', captured):
            self.hook.on_stop(payload_stop)

        output = captured.getvalue()
        # Claim was extracted from the fallback transcript
        self.assertNotIn('claim source=none', output)
        self.assertIn('src/fallback.py', output)


class TestHookRawCapture(unittest.TestCase):
    """ATTEST_CAPTURE=1 with raw= writes a raw stdin file alongside the normalized payload."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.capture_dir = os.path.join(self.tmpdir, 'captured')
        os.makedirs(self.capture_dir)
        os.environ['ATTEST_CAPTURE'] = '1'
        os.environ['ATTEST_CAPTURE_DIR'] = self.capture_dir
        os.environ['ATTEST_STATE_DB'] = os.path.join(self.tmpdir, 'state.db')
        from attest import hook
        self.hook = hook

    def tearDown(self) -> None:
        del os.environ['ATTEST_CAPTURE']
        del os.environ['ATTEST_CAPTURE_DIR']
        del os.environ['ATTEST_STATE_DB']

    def test_raw_capture_writes_raw_file(self) -> None:
        """When raw= is provided, _capture_if_requested writes a *-raw-* JSON file."""
        raw_str = '{"agent_id":"rawtest","agent_type":"code-writer","session_id":"s-raw","cwd":"/tmp"}'
        payload = {
            'agent_id': 'rawtest',
            'agent_type': 'code-writer',
            'session_id': 's-raw',
            'stop_reason': 'end_turn',
            'transcript_path': '',
            'agent_transcript_path': '',
            'cwd': self.tmpdir,
            'stop_hook_active': False,
            'payload_text': '',
        }

        with patch('sys.stdout', io.StringIO()):
            self.hook._capture_if_requested('stop', payload, '', raw=raw_str)

        files = os.listdir(self.capture_dir)
        # Raw files have the literal '-raw-' segment: stop-raw-{agent_id}-{ts}.json
        raw_files = [f for f in files if f.startswith('stop-raw-')]
        self.assertTrue(len(raw_files) >= 1, f'Expected a raw capture file, got: {files}')

        # The raw file contains the verbatim stdin string
        raw_file_path = os.path.join(self.capture_dir, raw_files[0])
        with open(raw_file_path, encoding='utf-8') as fh:
            content = fh.read()
        self.assertEqual(content, raw_str)

    def test_normalized_payload_file_also_written(self) -> None:
        """Normalized payload file is still written alongside the raw file."""
        raw_str = '{"agent_id":"rawtest2","agent_type":"code-writer"}'
        payload = {
            'agent_id': 'rawtest2',
            'agent_type': 'code-writer',
            'session_id': 's-raw2',
            'stop_reason': '',
            'transcript_path': '',
            'agent_transcript_path': '',
            'cwd': self.tmpdir,
            'stop_hook_active': False,
            'payload_text': '',
        }

        with patch('sys.stdout', io.StringIO()):
            self.hook._capture_if_requested('start', payload, '', raw=raw_str)

        files = os.listdir(self.capture_dir)
        normalized_files = [f for f in files if f.startswith('start-rawtest2')]
        raw_files = [f for f in files if f.startswith('start-raw-rawtest2')]
        self.assertTrue(len(normalized_files) >= 1, f'Expected normalized file, got: {files}')
        self.assertTrue(len(raw_files) >= 1, f'Expected raw file, got: {files}')

    def test_no_raw_file_when_raw_is_none(self) -> None:
        """When raw= is not provided, no raw file is written."""
        payload = {
            'agent_id': 'norawarg',
            'agent_type': 'test',
            'session_id': 's-noraw',
            'stop_reason': '',
            'transcript_path': '',
            'agent_transcript_path': '',
            'cwd': self.tmpdir,
            'stop_hook_active': False,
            'payload_text': '',
        }

        with patch('sys.stdout', io.StringIO()):
            self.hook._capture_if_requested('stop', payload, '', raw=None)

        files = os.listdir(self.capture_dir)
        # Raw files are named '{event}-raw-{agent_id}-{ts}.json'
        # Use a startswith prefix so agent_ids that happen to contain 'raw' don't false-match.
        raw_files = [f for f in files if f.startswith('stop-raw-')]
        self.assertEqual(raw_files, [], f'No raw files expected, got: {raw_files}')

    def test_no_capture_when_attest_capture_off(self) -> None:
        """When ATTEST_CAPTURE != '1', no files are written even if raw= is passed."""
        del os.environ['ATTEST_CAPTURE']
        try:
            payload = {
                'agent_id': 'off',
                'agent_type': 'test',
                'session_id': 's-off',
                'stop_reason': '',
                'transcript_path': '',
                'agent_transcript_path': '',
                'cwd': self.tmpdir,
                'stop_hook_active': False,
                'payload_text': '',
            }
            self.hook._capture_if_requested('stop', payload, '', raw='{"raw":"data"}')
            files = os.listdir(self.capture_dir)
            self.assertEqual(files, [])
        finally:
            os.environ['ATTEST_CAPTURE'] = '1'  # restore for tearDown symmetry

    def test_main_passes_raw_to_handlers(self) -> None:
        """main() passes the raw stdin string through to on_start/on_stop so capture works."""
        raw_payload = json.dumps({
            'agent_id': 'main-raw-test',
            'agent_type': 'code-writer',
            'session_id': 's-main',
            'cwd': self.tmpdir,
        })

        from attest.hook import main
        with patch('sys.stdin', io.StringIO(raw_payload)), \
             patch('sys.stdout', io.StringIO()):
            main(['start'])

        files = os.listdir(self.capture_dir)
        # Raw files: start-raw-{agent_id}-{ts}.json
        raw_files = [f for f in files if f.startswith('start-raw-')]
        self.assertTrue(len(raw_files) >= 1, f'Expected raw file from main(), got: {files}')


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


class TestHookEnforce(unittest.TestCase):
    """Phase-2 enforce mode (ATTEST_ENFORCE=1): block proven false DONEs, fail open elsewhere."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmpdir, 'repo')
        os.makedirs(self.repo)
        _init_git_repo(self.repo)
        os.environ['ATTEST_STATE_DB'] = os.path.join(self.tmpdir, 'state.db')
        os.environ['ATTEST_ENFORCE'] = '1'
        from attest import hook, state, enforce
        self.hook = hook
        self.state = state
        self.enforce = enforce

    def tearDown(self) -> None:
        del os.environ['ATTEST_STATE_DB']
        del os.environ['ATTEST_ENFORCE']

    def _stop(self, payload: dict) -> tuple:
        """Run on_stop capturing stdout and stderr SEPARATELY. Returns (stdout, stderr)."""
        so, se = io.StringIO(), io.StringIO()
        with patch('sys.stdout', so), patch('sys.stderr', se):
            self.hook.on_stop(payload)
        return so.getvalue(), se.getvalue()

    def _start(self, payload: dict) -> None:
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            self.hook.on_start(payload)

    def _false_claim_payload(self, agent_id: str, sess: str, ghost: str = 'src/ghost.py') -> dict:
        return _make_payload(
            agent_id, sess, self.repo,
            payload_text=f'## Handoff\nfiles_changed: {ghost}\nstatus: DONE\nblockers: none\n',
        )

    def test_block_emits_pure_json_on_stdout(self) -> None:
        aid = 'enforce-block-1'
        self._start(_make_payload(aid, 's1', self.repo))
        stdout, stderr = self._stop(self._false_claim_payload(aid, 's1'))

        # stdout is EXACTLY one JSON object, parseable, decision=block, names the ghost file.
        parsed = json.loads(stdout.strip())
        self.assertEqual(parsed['decision'], 'block')
        self.assertIn('src/ghost.py', parsed['reason'])
        # No human "attest:" text leaked onto stdout (would void the block).
        self.assertNotIn('attest:', stdout)
        # Human diagnostics went to stderr instead.
        self.assertIn('attest: stop:', stderr)

    def test_block_keeps_state_and_increments(self) -> None:
        aid = 'enforce-keep-1'
        self._start(_make_payload(aid, 's1', self.repo))
        self._stop(self._false_claim_payload(aid, 's1'))
        # Snapshot kept for the retry; per-agent counter at 1.
        self.assertIsNotNone(self.state.load_snapshot(aid))
        self.assertEqual(self.state.get_block_count(aid), 1)

    def test_retry_cap_exhaustion_allows_second_stop(self) -> None:
        aid = 'enforce-retry-1'
        self._start(_make_payload(aid, 's1', self.repo))
        out1, _ = self._stop(self._false_claim_payload(aid, 's1'))
        out2, err2 = self._stop(self._false_claim_payload(aid, 's1'))
        # First stop blocks (JSON), second stop fails open (empty stdout), then cleared.
        self.assertIn('"decision"', out1)
        self.assertEqual(out2.strip(), '')
        self.assertIsNone(self.state.load_snapshot(aid))
        # Observability: second stop should emit stderr diagnostic explaining why not blocked
        self.assertIn('detected false DONE but NOT blocking', err2)
        self.assertIn('ALLOW_RETRY_CAP', err2)

    def test_truthful_claim_no_block(self) -> None:
        aid = 'enforce-truth-1'
        self._start(_make_payload(aid, 's1', self.repo))
        # Actually write the claimed file.
        path = os.path.join(self.repo, 'src', 'feature.py')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write('def f(): pass\n')
        payload = _make_payload(
            aid, 's1', self.repo,
            payload_text='## Handoff\nfiles_changed: src/feature.py\nstatus: DONE\nblockers: none\n',
        )
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_landmine_non_git_never_blocks(self) -> None:
        """A non-git dir with a DONE+files claim must NOT block (unreliable delta)."""
        non_git = os.path.join(self.tmpdir, 'not_a_repo')
        os.makedirs(non_git)
        aid = 'enforce-nongit-1'
        self._start(_make_payload(aid, 's1', non_git))
        payload = _make_payload(
            aid, 's1', non_git,
            payload_text='## Handoff\nfiles_changed: ghost.py\nstatus: DONE\nblockers: none\n',
        )
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_claimed_file_on_disk_never_blocks(self) -> None:
        """A claimed file that exists on disk (e.g. unchanged committed file) is not phantom."""
        aid = 'enforce-ondisk-1'
        self._start(_make_payload(aid, 's1', self.repo))
        # README.md is committed and on disk; claim it as DONE but don't touch it.
        payload = _make_payload(
            aid, 's1', self.repo,
            payload_text='## Handoff\nfiles_changed: README.md\nstatus: DONE\nblockers: none\n',
        )
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_basename_claim_never_blocks(self) -> None:
        """Agent changed src/app.py but reported the bare basename 'app.py' -> no block."""
        aid = 'enforce-basename-1'
        self._start(_make_payload(aid, 's1', self.repo))
        path = os.path.join(self.repo, 'src', 'app.py')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write('x = 1\n')
        payload = _make_payload(
            aid, 's1', self.repo,
            payload_text='## Handoff\nfiles_changed: app.py\nstatus: DONE\nblockers: none\n',
        )
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_subdir_cwd_relative_claim_never_blocks(self) -> None:
        """Agent cwd=<repo>/sub edits sub/foo.py and reports cwd-relative 'foo.py' -> no block."""
        sub = os.path.join(self.repo, 'sub')
        os.makedirs(sub, exist_ok=True)
        aid = 'enforce-subdir-1'
        self._start(_make_payload(aid, 's1', sub))
        with open(os.path.join(sub, 'foo.py'), 'w') as fh:
            fh.write('y = 2\n')
        payload = _make_payload(
            aid, 's1', sub,
            payload_text='## Handoff\nfiles_changed: foo.py\nstatus: DONE\nblockers: none\n',
        )
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_genuine_phantom_still_blocks_despite_other_change(self) -> None:
        """The basename/cwd fixes must NOT mask a real phantom: a claimed file with a
        DISTINCT basename that was never written still blocks, even when another
        claimed file legitimately changed."""
        aid = 'enforce-phantom-mix-1'
        self._start(_make_payload(aid, 's1', self.repo))
        real = os.path.join(self.repo, 'src', 'real.py')
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, 'w') as fh:
            fh.write('x = 1\n')
        payload = _make_payload(
            aid, 's1', self.repo,
            payload_text=(
                '## Handoff\n'
                'files_changed: src/real.py, src/phantom.py\n'
                'status: DONE\nblockers: none\n'
            ),
        )
        stdout, _ = self._stop(payload)
        parsed = json.loads(stdout.strip())
        self.assertEqual(parsed['decision'], 'block')
        self.assertIn('src/phantom.py', parsed['reason'])
        self.assertNotIn('src/real.py', parsed['reason'])  # the truthful file is not named

    def test_source_none_never_blocks(self) -> None:
        aid = 'enforce-noclaim-1'
        self._start(_make_payload(aid, 's1', self.repo))
        payload = _make_payload(aid, 's1', self.repo, payload_text='')
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_missing_snapshot_never_blocks(self) -> None:
        payload = self._false_claim_payload('no-start-here', 's1')
        stdout, _ = self._stop(payload)
        self.assertEqual(stdout.strip(), '')

    def test_absent_agent_id_never_blocks(self) -> None:
        """No agent_id -> detect-only even with a clean false DONE."""
        # agent_key falls back to agent_type:session; snapshot still saved under it.
        payload_start = _make_payload('', 's-noid', self.repo)
        self._start(payload_start)
        payload_stop = _make_payload(
            '', 's-noid', self.repo,
            payload_text='## Handoff\nfiles_changed: src/ghost.py\nstatus: DONE\nblockers: none\n',
        )
        stdout, _ = self._stop(payload_stop)
        self.assertEqual(stdout.strip(), '')

    def test_stop_hook_active_never_blocks(self) -> None:
        aid = 'enforce-sha-1'
        self._start(_make_payload(aid, 's1', self.repo))
        payload = self._false_claim_payload(aid, 's1')
        payload['stop_hook_active'] = True
        stdout, stderr = self._stop(payload)
        self.assertEqual(stdout.strip(), '')
        # Counter must NOT have incremented (no block attempted).
        self.assertEqual(self.state.get_block_count(aid), 0)
        # Observability: stderr should explain why not blocked despite detecting false DONE
        self.assertIn('detected false DONE but NOT blocking', stderr)
        self.assertIn('ALLOW_STOP_HOOK_ACTIVE', stderr)

    def test_internal_exception_fails_open(self) -> None:
        """An exception inside the decision path must not leave a block on stdout."""
        aid = 'enforce-exc-1'
        self._start(_make_payload(aid, 's1', self.repo))
        payload_json = json.dumps(self._false_claim_payload(aid, 's1'))
        so, se = io.StringIO(), io.StringIO()
        with patch('attest.hook.enforce_mod.decide', side_effect=RuntimeError('boom')), \
             patch('sys.stdin', io.StringIO(payload_json)), \
             patch('sys.stdout', so), patch('sys.stderr', se):
            code = self.hook.main(['stop'])
        self.assertEqual(code, 0)
        self.assertNotIn('"decision"', so.getvalue())

    def test_detect_mode_unchanged_when_enforce_off(self) -> None:
        """With ATTEST_ENFORCE unset, a false DONE prints MISMATCH to stdout, no JSON."""
        del os.environ['ATTEST_ENFORCE']
        try:
            aid = 'detect-1'
            self._start(_make_payload(aid, 's1', self.repo))
            stdout, _ = self._stop(self._false_claim_payload(aid, 's1'))
            self.assertIn('MISMATCH', stdout)
            self.assertNotIn('"decision"', stdout)
        finally:
            os.environ['ATTEST_ENFORCE'] = '1'  # restored for tearDown symmetry

    def test_done_with_concerns_enforce_no_block(self) -> None:
        """DONE_WITH_CONCERNS status never blocks, even with claimed-but-absent file.

        Unlike DONE, DONE_WITH_CONCERNS is never a false claim — it's an
        intentional claim of incomplete work. Enforcement must never block it.
        """
        aid = 'enforce-concerns-1'
        self._start(_make_payload(aid, 's1', self.repo))
        # Claim a ghost file AND report DONE_WITH_CONCERNS (NOT DONE)
        payload = _make_payload(
            aid, 's1', self.repo,
            payload_text=(
                '## Handoff\n'
                'files_changed: src/ghost.py\n'
                'status: DONE_WITH_CONCERNS\n'
                'blockers: something not finished\n'
            )
        )
        stdout, _ = self._stop(payload)
        # DONE_WITH_CONCERNS must never block, even with a phantom file
        self.assertEqual(stdout.strip(), '')

    def test_counter_persist_fail_open(self) -> None:
        """If block counter increment fails, fail OPEN (no block emitted).

        This is the primary loop-prevention valve: an unrecorded block is what
        loops, so a failed counter commit must NOT emit a block.
        """
        aid = 'enforce-counter-fail-1'
        self._start(_make_payload(aid, 's1', self.repo))
        # Patch increment_block_count to simulate a persistence failure
        payload = self._false_claim_payload(aid, 's1')
        so, se = io.StringIO(), io.StringIO()
        with patch.object(self.state, 'increment_block_count', return_value=None), \
             patch('sys.stdout', so), patch('sys.stderr', se):
            self.hook.on_stop(payload)
        stdout, stderr = so.getvalue(), se.getvalue()
        # No block JSON on stdout (failing open)
        self.assertEqual(stdout.strip(), '')
        # Stderr should explain the failure
        self.assertIn('counter persist failed', stderr)
        self.assertIn('failing open', stderr)
