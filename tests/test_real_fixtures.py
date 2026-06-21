#!/usr/bin/env python3
"""
test_real_fixtures.py — Integration tests over REAL captured Claude Code payloads.

These fixtures are sanitized real SubagentStart/SubagentStop payloads captured from
Claude Code v2.1.170 during the P1 live-capture ship-gate (2026-06-21). They pin the
parser + hook normalization to ground truth, not synthetic assumptions. If Claude Code
changes its payload schema, these break — which is the point.

Fixtures:
  subagent_start_payload.json     — real SubagentStart (snapshot boundary)
  subagent_stop_payload.json      — real SubagentStop, true DONE (hello.py landed)
  subagent_stop_false_done.json   — real SubagentStop, HONEST refusal in prose (BUG-4 regression)
  subagent_stop_refire.json       — real post-block re-fire (stop_hook_active=True)
"""
import json
import os
import unittest

from attest import hookio
from attest import claim as claim_mod

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'fixtures')


def _load(name):
    with open(os.path.join(FIX, name), encoding='utf-8') as fh:
        return json.load(fh)


def _raw(name):
    with open(os.path.join(FIX, name), encoding='utf-8') as fh:
        return fh.read()


class TestRealStartPayload(unittest.TestCase):
    """SubagentStart carries identity + the parent transcript, no subagent transcript yet."""

    def setUp(self):
        self.norm = hookio.parse_payload(_raw('subagent_start_payload.json'))

    def test_agent_id_present(self):
        self.assertTrue(self.norm['agent_id'], 'real Task subagents must carry agent_id')

    def test_agent_type(self):
        self.assertEqual(self.norm['agent_type'], 'general-purpose')

    def test_parent_transcript_present(self):
        self.assertTrue(self.norm['transcript_path'])

    def test_no_subagent_transcript_at_start(self):
        # agent_transcript_path only appears at SubagentStop in the captured schema.
        self.assertEqual(self.norm['agent_transcript_path'], '')


class TestRealHappyStop(unittest.TestCase):
    """True DONE: a clean ## Handoff claim whose file actually landed."""

    def setUp(self):
        self.norm = hookio.parse_payload(_raw('subagent_stop_payload.json'))

    def test_identity_and_flags(self):
        self.assertTrue(self.norm['agent_id'])
        self.assertFalse(self.norm['stop_hook_active'])

    def test_subagent_transcript_present(self):
        # The real stop payload distinguishes the subagent's own jsonl.
        self.assertIn('subagents', self.norm['agent_transcript_path'])

    def test_claim_text_in_payload(self):
        # last_assistant_message is the payload fast-path for the claim.
        self.assertIn('Handoff', self.norm['payload_text'])

    def test_claim_parses_to_real_file(self):
        parsed = claim_mod.parse_claim(self.norm['payload_text'])
        self.assertEqual(parsed['source'], 'handoff')
        self.assertEqual(parsed['status'], 'DONE')
        self.assertEqual(parsed['files_changed'], ['hello.py'])


class TestRealFalseDoneRegression(unittest.TestCase):
    """BUG-4 regression: an HONEST subagent that did nothing and explained why in prose
    must NEVER yield a blockable file claim — files_changed MUST be empty."""

    def setUp(self):
        self.norm = hookio.parse_payload(_raw('subagent_stop_false_done.json'))
        self.parsed = claim_mod.parse_claim(self.norm['payload_text'])

    def test_stop_hook_active_false_on_first_stop(self):
        self.assertFalse(self.norm['stop_hook_active'])

    def test_no_files_extracted_from_prose(self):
        # The load-bearing safety assertion: prose mentioning paths/commands/"DONE"
        # must produce zero claimed files, so it can never trigger a false block.
        self.assertEqual(self.parsed['files_changed'], [])

    def test_not_a_blockable_done(self):
        # Either source is none, or there are simply no files to contradict.
        self.assertEqual(self.parsed['files_changed'], [])


class TestRealRefire(unittest.TestCase):
    """Post-block re-fire of the SAME agent_id: stop_hook_active flips True and the
    subagent corrected its claim to BLOCKED/none (loop terminates safely)."""

    def setUp(self):
        self.norm = hookio.parse_payload(_raw('subagent_stop_refire.json'))
        self.parsed = claim_mod.parse_claim(self.norm['payload_text'])

    def test_stop_hook_active_true_on_refire(self):
        self.assertTrue(self.norm['stop_hook_active'])

    def test_same_agent_id_as_first_stop(self):
        first = hookio.parse_payload(_raw('subagent_stop_false_done.json'))
        self.assertEqual(self.norm['agent_id'], first['agent_id'])

    def test_corrected_claim_not_false_done(self):
        # Corrected handoff: status BLOCKED, no files claimed.
        self.assertEqual(self.parsed['status'], 'BLOCKED')
        self.assertEqual(self.parsed['files_changed'], [])


if __name__ == '__main__':
    unittest.main()
