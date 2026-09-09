"""Synthetic streams: no real provider calls or historical DB changes."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agentflow.adapters import InvocationIncompleteError
from agentflow.authorization import issue_authorization
from agentflow.fake_adapter import FakeAdapter
from agentflow.opencode_adapter import OpenCodeAdapter, parse_opencode_json
from agentflow.runner import Runner
from agentflow.states import RunState
from resource_budget_fixtures import resolved_output_stub
from test_opencode_adapter import _local_request, _FakeProcess
import test_runner as fixtures


def stream(reason='length', text='', tokens=None):
    events = []
    if text:
        events.append({'type': 'text', 'sessionID': 'length-fixture',
                       'part': {'type': 'text', 'text': text}})
    events.append({'type': 'step_finish', 'sessionID': 'length-fixture',
                   'part': {'reason': reason, 'cost': 0.25, 'tokens': tokens if tokens is not None
                            else {'input': 19802, 'output': 0, 'reasoning': 7999}}})
    return '\n'.join(json.dumps(event) for event in events)


class OutputTerminationTests(unittest.TestCase):
    def test_length_never_succeeds_even_with_valid_approval(self):
        for text in ('', 'partial', '{"approved":true,"findings":[]}'):
            with self.subTest(text=text):
                with self.assertRaises(InvocationIncompleteError) as caught:
                    parse_opencode_json(stream(text=text), duration_ms=12, is_local=True)
                error = caught.exception
                self.assertEqual('output_limit_reached', error.failure_kind)
                self.assertEqual(19802, error.result.input_tokens)
                self.assertEqual(7999, error.result.raw_metadata['reasoning_tokens'])
                self.assertEqual(text, error.result.output)

    def test_no_text_protocol_failure_carries_usage(self):
        with self.assertRaises(RuntimeError) as caught:
            parse_opencode_json(stream('stop'), duration_ms=12, is_local=True)
        self.assertTrue(hasattr(caught.exception, 'result'))
        result = caught.exception.result
        self.assertEqual(19802, result.input_tokens)
        self.assertEqual('no_text', result.raw_metadata['termination_reason'])

    def test_missing_tokens_differ_from_explicit_zero_on_failure(self):
        for tokens, unavailable in (({}, True), ({'input': 0, 'output': 0}, False)):
            with self.subTest(tokens=tokens), self.assertRaises(RuntimeError) as caught:
                parse_opencode_json(stream(tokens=tokens), duration_ms=1)
            self.assertTrue(hasattr(caught.exception, 'result'))
            self.assertEqual(unavailable, caught.exception.result.raw_metadata['usage_unavailable'])

    def test_final_stop_after_intermediate_length_can_succeed(self):
        result = parse_opencode_json(stream() + '\n' + stream('stop', 'done'), duration_ms=1)
        self.assertEqual('done', result.output)
        self.assertEqual('stop', result.raw_metadata['terminal_reason'])

    @mock.patch('agentflow.opencode_adapter._read_output_config', new=resolved_output_stub)
    def test_local_exit_paths_preserve_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            for code, reason, text in ((0, 'length', ''), (1, 'length', ''),
                                       (0, 'stop', ''), (1, 'stop', 'partial')):
                with self.subTest(code=code, reason=reason, text=text):
                    with mock.patch('subprocess.Popen', return_value=_FakeProcess(stream(reason, text), code)):
                        with self.assertRaises(RuntimeError) as caught:
                            OpenCodeAdapter().invoke(_local_request(Path(directory)))
                    self.assertTrue(hasattr(caught.exception, 'result'))
                    self.assertEqual(19802, caught.exception.result.input_tokens)
                    self.assertEqual(0, caught.exception.result.remote_cost)


class OutputTerminationRunnerTests(unittest.TestCase):
    setUp = fixtures.RunnerTests.setUp
    tearDown = fixtures.RunnerTests.tearDown

    def test_length_pause_preserves_audit_and_cannot_resume(self):
        self._check_failed_call('length', 'review')

    def test_no_text_pause_preserves_audit_and_cannot_resume(self):
        self._check_failed_call('stop', 'review')

    def test_implementation_length_does_not_continue_or_fallback(self):
        self._check_failed_call('length', 'implementation')

    def test_implementation_no_text_does_not_retry_or_fallback(self):
        self._check_failed_call('stop', 'implementation')

    def _check_failed_call(self, reason, role):
        task = fixtures.make_task('output-' + reason)
        task = replace(task, max_retry_count=2, implementation_max_continuations=2,
                       fallback_model=replace(task.review_model, model_id='fallback'))
        plan = replace(fixtures.make_plan(tasks=(task,)), plan_id='plan-' + reason)
        auth = issue_authorization(plan)

        def respond(request):
            if request.role == role:
                return parse_opencode_json(stream(reason), duration_ms=12, is_local=True)
            return fixtures.approved_response(request)

        adapter = FakeAdapter(responder=respond)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, auth)
        self.assertEqual(RunState.PAUSED, result.state)
        calls = self.database.fetch_one('SELECT count(*) n FROM model_calls')['n']
        self.assertEqual(1 if role == 'implementation' else 2, calls)
        with self.assertRaises(ValueError):
            runner.resume(result.run_id, plan, auth)
        self.assertEqual(calls, self.database.fetch_one('SELECT count(*) n FROM model_calls')['n'])
        row = self.database.fetch_one('SELECT * FROM model_calls ORDER BY rowid DESC LIMIT 1')
        self.assertEqual('failed', row['state'])
        self.assertEqual(19802, row['input_tokens'])
        self.assertEqual(0, row['remote_cost'])
        self.assertEqual(7999, json.loads(row['raw_metadata_json'])['reasoning_tokens'])
        self.assertEqual(16000, json.loads(row['request_scope_json'])['resource_budgets']['effective_max_output_tokens'])
        checkpoint = json.loads(self.database.run_snapshot(result.run_id)['run']['checkpoint_json'])
        self.assertEqual(role + '_output_limit_reached' if reason == 'length' else 'model_output_invalid', checkpoint['reason'])
        self.assertEqual(0, self.database.fetch_one('SELECT count(*) n FROM reviews')['n'])
