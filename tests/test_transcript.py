"""
tests/test_transcript.py — Unit tests for attest.transcript.last_assistant_text()

Covers:
  - Happy path: extracts text from last assistant message in fixture JSONL
  - Multiple assistant turns: returns only the LAST one
  - Non-text blocks (thinking, tool_use, tool_result) are dropped
  - Missing file returns ""
  - Empty string path returns ""
  - Malformed lines are skipped gracefully
  - Mixed types in content block: only "text" extracted
  - Final message with only thinking → returns "" (no text blocks)
"""
import json
import os
import tempfile
import unittest

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    '..',
    'fixtures',
    'transcript_sample.jsonl',
)


class TestLastAssistantText(unittest.TestCase):
    def setUp(self) -> None:
        from attest.transcript import last_assistant_text
        self.last_assistant_text = last_assistant_text

    def test_fixture_returns_handoff_block(self) -> None:
        """The fixture's final assistant message contains a ## Handoff block."""
        text = self.last_assistant_text(FIXTURE_PATH)
        self.assertIn('## Handoff', text)
        self.assertIn('files_changed', text)
        self.assertIn('status: DONE', text)

    def test_fixture_does_not_return_thinking(self) -> None:
        """thinking blocks must be dropped."""
        text = self.last_assistant_text(FIXTURE_PATH)
        # The fixture has thinking blocks but no literal "thinking" key in text blocks
        self.assertNotIn('synthetic-thinking-sig', text)

    def test_fixture_does_not_return_tool_use(self) -> None:
        """tool_use blocks must be dropped."""
        text = self.last_assistant_text(FIXTURE_PATH)
        # tool_use blocks have "name" fields like "Write", "Read" — not expected in text
        # The text blocks in the fixture don't mention these by name, but verify no tool data
        self.assertNotIn('toolu_000', text)

    def test_missing_file_returns_empty(self) -> None:
        text = self.last_assistant_text('/no/such/file.jsonl')
        self.assertEqual(text, '')

    def test_empty_path_returns_empty(self) -> None:
        text = self.last_assistant_text('')
        self.assertEqual(text, '')

    def test_custom_transcript_last_text_wins(self) -> None:
        """When multiple assistant turns have text blocks, only the last counts."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as fh:
            # First assistant text turn
            fh.write(json.dumps({
                'type': 'assistant',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'FIRST TURN TEXT'}],
                },
            }) + '\n')
            # A user turn
            fh.write(json.dumps({
                'type': 'user',
                'message': {'role': 'user', 'content': 'ok'},
            }) + '\n')
            # Second (final) assistant text turn
            fh.write(json.dumps({
                'type': 'assistant',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'FINAL TURN TEXT'}],
                },
            }) + '\n')
            tmp_path = fh.name

        try:
            text = self.last_assistant_text(tmp_path)
            self.assertEqual(text, 'FINAL TURN TEXT')
            self.assertNotIn('FIRST TURN TEXT', text)
        finally:
            os.unlink(tmp_path)

    def test_only_thinking_blocks_returns_empty(self) -> None:
        """If the last assistant turn has only thinking, text must be ""."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as fh:
            fh.write(json.dumps({
                'type': 'assistant',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'thinking', 'thinking': 'secret thoughts'}],
                },
            }) + '\n')
            tmp_path = fh.name

        try:
            text = self.last_assistant_text(tmp_path)
            self.assertEqual(text, '')
        finally:
            os.unlink(tmp_path)

    def test_malformed_lines_skipped(self) -> None:
        """Malformed JSON lines don't break parsing; valid lines still parsed."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as fh:
            fh.write('{bad json line}\n')
            fh.write('\n')
            fh.write(json.dumps({
                'type': 'assistant',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'good line'}],
                },
            }) + '\n')
            tmp_path = fh.name

        try:
            text = self.last_assistant_text(tmp_path)
            self.assertEqual(text, 'good line')
        finally:
            os.unlink(tmp_path)

    def test_empty_file_returns_empty(self) -> None:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as fh:
            tmp_path = fh.name
        try:
            text = self.last_assistant_text(tmp_path)
            self.assertEqual(text, '')
        finally:
            os.unlink(tmp_path)

    def test_non_assistant_lines_ignored(self) -> None:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as fh:
            fh.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}) + '\n')
            fh.write(json.dumps({'type': 'attachment', 'data': 'something'}) + '\n')
            tmp_path = fh.name
        try:
            text = self.last_assistant_text(tmp_path)
            self.assertEqual(text, '')
        finally:
            os.unlink(tmp_path)

    def test_multiple_text_blocks_concatenated(self) -> None:
        """Multiple text blocks in one turn are joined with double newlines."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as fh:
            fh.write(json.dumps({
                'type': 'assistant',
                'message': {
                    'role': 'assistant',
                    'content': [
                        {'type': 'text', 'text': 'Block A'},
                        {'type': 'thinking', 'thinking': 'skip this'},
                        {'type': 'text', 'text': 'Block B'},
                    ],
                },
            }) + '\n')
            tmp_path = fh.name
        try:
            text = self.last_assistant_text(tmp_path)
            self.assertIn('Block A', text)
            self.assertIn('Block B', text)
            self.assertNotIn('skip this', text)
        finally:
            os.unlink(tmp_path)
