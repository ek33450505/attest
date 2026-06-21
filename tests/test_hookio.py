"""
tests/test_hookio.py — Unit tests for attest.hookio.parse_payload()

Covers:
  - Happy path: all fields present with agent_response.content
  - agent_type field precedence (primary vs legacy fallbacks)
  - payload_text extraction: agent_response > last_assistant_message > output > body
  - transcript_path field normalization
  - stop_hook_active boolean coercion
  - Empty/missing input → safe defaults
  - Invalid JSON → safe defaults
  - Non-dict JSON → safe defaults
"""
import json
import unittest


class TestParsePayload(unittest.TestCase):
    def setUp(self) -> None:
        from attest.hookio import parse_payload
        self.parse_payload = parse_payload

    def _make_payload(self, **kwargs) -> str:
        return json.dumps(kwargs)

    def test_happy_path_all_fields(self) -> None:
        raw = json.dumps({
            'agent_type': 'code-writer',
            'agent_id': 'abc123',
            'session_id': 'sess-XYZ',
            'stop_reason': 'end_turn',
            'transcript_path': '/tmp/agent.jsonl',
            'cwd': '/tmp/repo',
            'stop_hook_active': False,
            'agent_response': {
                'content': [
                    {'type': 'text', 'text': 'Status: DONE\nFiles: a.py'},
                ]
            },
        })
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_id'], 'abc123')
        self.assertEqual(result['agent_type'], 'code-writer')
        self.assertEqual(result['session_id'], 'sess-XYZ')
        self.assertEqual(result['stop_reason'], 'end_turn')
        self.assertEqual(result['transcript_path'], '/tmp/agent.jsonl')
        self.assertEqual(result['cwd'], '/tmp/repo')
        self.assertFalse(result['stop_hook_active'])
        self.assertIn('Status: DONE', result['payload_text'])

    def test_agent_type_primary_over_agent_name(self) -> None:
        raw = json.dumps({
            'agent_type': 'code-writer',
            'agent_name': 'old-name',
            'subagent_name': 'older-name',
        })
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_type'], 'code-writer')

    def test_agent_name_fallback_when_no_agent_type(self) -> None:
        raw = json.dumps({'agent_name': 'fallback-agent'})
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_type'], 'fallback-agent')

    def test_subagent_name_fallback(self) -> None:
        raw = json.dumps({'subagent_name': 'legacy-agent'})
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_type'], 'legacy-agent')

    def test_agent_id_from_subagent_id(self) -> None:
        raw = json.dumps({'subagent_id': 'sid-001', 'agent_type': 'test'})
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_id'], 'sid-001')

    def test_agent_id_prefers_agent_id_over_subagent_id(self) -> None:
        raw = json.dumps({'agent_id': 'main-id', 'subagent_id': 'sub-id', 'agent_type': 'test'})
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_id'], 'main-id')

    def test_payload_text_from_agent_response_content(self) -> None:
        raw = json.dumps({
            'agent_type': 'test',
            'agent_response': {
                'content': [
                    {'type': 'thinking', 'thinking': 'secret thought'},
                    {'type': 'text', 'text': 'Hello world'},
                    {'type': 'text', 'text': 'Second line'},
                ]
            },
            'last_assistant_message': 'fallback',
        })
        result = self.parse_payload(raw)
        # Should extract text blocks (not thinking), joined with newline
        self.assertIn('Hello world', result['payload_text'])
        self.assertIn('Second line', result['payload_text'])
        self.assertNotIn('secret thought', result['payload_text'])

    def test_payload_text_fallback_last_assistant_message(self) -> None:
        raw = json.dumps({
            'agent_type': 'test',
            'last_assistant_message': 'I am done with the task.',
        })
        result = self.parse_payload(raw)
        self.assertEqual(result['payload_text'], 'I am done with the task.')

    def test_payload_text_fallback_output(self) -> None:
        raw = json.dumps({'agent_type': 'test', 'output': 'output text'})
        result = self.parse_payload(raw)
        self.assertEqual(result['payload_text'], 'output text')

    def test_payload_text_fallback_body(self) -> None:
        raw = json.dumps({'agent_type': 'test', 'body': 'body text'})
        result = self.parse_payload(raw)
        self.assertEqual(result['payload_text'], 'body text')

    def test_payload_text_empty_when_none_present(self) -> None:
        raw = json.dumps({'agent_type': 'test'})
        result = self.parse_payload(raw)
        self.assertEqual(result['payload_text'], '')

    def test_transcript_path_agent_transcript_path_alias(self) -> None:
        raw = json.dumps({'agent_type': 'test', 'agent_transcript_path': '/path/to/t.jsonl'})
        result = self.parse_payload(raw)
        self.assertEqual(result['transcript_path'], '/path/to/t.jsonl')

    def test_transcript_path_prefers_transcript_path(self) -> None:
        raw = json.dumps({
            'agent_type': 'test',
            'transcript_path': '/primary.jsonl',
            'agent_transcript_path': '/secondary.jsonl',
        })
        result = self.parse_payload(raw)
        self.assertEqual(result['transcript_path'], '/primary.jsonl')

    def test_stop_hook_active_true(self) -> None:
        raw = json.dumps({'agent_type': 'test', 'stop_hook_active': True})
        result = self.parse_payload(raw)
        self.assertTrue(result['stop_hook_active'])

    def test_stop_hook_active_false(self) -> None:
        raw = json.dumps({'agent_type': 'test', 'stop_hook_active': False})
        result = self.parse_payload(raw)
        self.assertFalse(result['stop_hook_active'])

    def test_stop_hook_active_absent_defaults_false(self) -> None:
        raw = json.dumps({'agent_type': 'test'})
        result = self.parse_payload(raw)
        self.assertFalse(result['stop_hook_active'])

    def test_empty_string_returns_defaults(self) -> None:
        result = self.parse_payload('')
        self.assertEqual(result['agent_type'], 'unknown')
        self.assertEqual(result['agent_id'], '')
        self.assertEqual(result['payload_text'], '')

    def test_none_returns_defaults(self) -> None:
        # None is not a valid str but should not crash
        try:
            result = self.parse_payload(None)  # type: ignore[arg-type]
            self.assertEqual(result['agent_type'], 'unknown')
        except (TypeError, AttributeError):
            # Acceptable — parse_payload signature is str
            pass

    def test_invalid_json_returns_defaults(self) -> None:
        result = self.parse_payload('{not valid json}')
        self.assertEqual(result['agent_type'], 'unknown')
        self.assertEqual(result['payload_text'], '')

    def test_json_array_returns_defaults(self) -> None:
        result = self.parse_payload('[1, 2, 3]')
        self.assertEqual(result['agent_type'], 'unknown')

    def test_default_agent_type_unknown(self) -> None:
        raw = json.dumps({'session_id': 'sess-1'})
        result = self.parse_payload(raw)
        self.assertEqual(result['agent_type'], 'unknown')

    def test_all_keys_present_in_result(self) -> None:
        result = self.parse_payload('{}')
        expected_keys = {
            'agent_id', 'agent_type', 'session_id', 'stop_reason',
            'transcript_path', 'cwd', 'stop_hook_active', 'payload_text',
        }
        self.assertEqual(set(result.keys()), expected_keys)
