from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
from resource_budget_fixtures import budgeted_request, resolved_output_stub

from agentflow.adapters import InvocationIncompleteError, InvocationOutcomeUnknown, InvocationRequest
from agentflow.contracts import DataSensitivity, ModelRef
from agentflow.opencode_adapter import (
    OpenCodeAdapter,
    _merge_overlapping_output_bytes,
    parse_opencode_json,
    parse_opencode_partial_usage,
)


class _FakeProcess:
    pid = 41234

    def __init__(self, stdout: str, returncode: int, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return self.stdout, self.stderr


class _TimingOutProcess:
    pid = 41234

    def __init__(self, partial_output: str | bytes, remaining: str | bytes = "") -> None:
        self.partial_output = partial_output
        self.remaining = remaining
        self.returncode = -15
        self.timeout_args: list[int | None] = []
        self.communicate_calls = 0

    def communicate(self, timeout: int | None = None) -> tuple[str | bytes, str]:
        self.communicate_calls += 1
        self.timeout_args.append(timeout)
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(
                ("opencode", "run"), timeout or 0, output=self.partial_output
            )
        return self.remaining, ""


class _KillFailingProcess:
    pid = 41234

    def __init__(self, partial_output: bytes, drain_output: bytes) -> None:
        self.partial_output = partial_output
        self.drain_output = drain_output
        self.returncode = None
        self.timeout_args: list[int | None] = []
        self.communicate_calls = 0

    def communicate(self, timeout: int | None = None) -> tuple[bytes, str]:
        self.communicate_calls += 1
        self.timeout_args.append(timeout)
        raise subprocess.TimeoutExpired(
            ("opencode", "run"),
            timeout or 0,
            output=self.partial_output if self.communicate_calls == 1 else self.drain_output,
        )

    def kill(self) -> None:
        raise OSError("kill failed")


def _local_request(worktree: Path) -> InvocationRequest:
    return budgeted_request(
        call_id="call-1",
        request_key="request-1",
        run_id="run-1",
        task_id="task-1",
        role="implementation",
        model=ModelRef("lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True),
        prompt="implement the change",
        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
        read_only=False,
        metadata={"worktree": str(worktree), "allowed_files": ()},
    )


@mock.patch('agentflow.opencode_adapter._read_output_config', new=resolved_output_stub)
class OpenCodeAdapterTests(unittest.TestCase):
    def test_discovery_only_returns_loaded_llms(self) -> None:
        payload = [
            {
                "type": "llm",
                "modelKey": "qwen/model",
                "identifier": "qwen/model",
                "selectedVariant": "qwen/model@8bit",
                "status": "idle",
                "contextLength": 4096,
                "trainedForToolUse": True,
            },
            {"type": "embedding", "modelKey": "embed"},
        ]
        completed = subprocess.CompletedProcess(
            ("lms",), 0, stdout=json.dumps(payload), stderr=""
        )
        with mock.patch("subprocess.run", return_value=completed):
            records = OpenCodeAdapter(lms_command="lms").discover()
        self.assertEqual(1, len(records))
        self.assertEqual("lmstudio", records[0].ref.provider)
        self.assertEqual("qwen", records[0].ref.family)
        self.assertEqual(0, records[0].input_cost_per_million)

    def test_parser_collects_text_tokens_and_session(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session-1",
                        "part": {"type": "text", "text": "hello"},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "tokens": {"input": 10, "output": 4},
                            "cost": 0,
                        },
                    }
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=50)
        self.assertEqual("hello", result.output)
        self.assertEqual("session-1", result.provider_request_id)
        self.assertEqual(10, result.input_tokens)
        self.assertEqual(4, result.output_tokens)
        self.assertEqual(0, result.remote_cost)
        self.assertEqual(
            0,
            parse_opencode_json(events, duration_ms=50, is_local=True).remote_cost,
        )

    def test_parser_rejects_realistic_zero_exit_step_limit_stream(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(
                fixture.read_text(encoding="utf-8"),
                duration_ms=50,
                is_local=False,
            )
        result = raised.exception.result
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual("session-step-limit", result.provider_request_id)
        self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
        self.assertEqual(0.125, result.remote_cost)
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertIn("responding with text only", result.output)
        self.assertIn("## Summary of work accomplished so far", result.output)

    def test_parser_prefers_structured_step_limit_signal(self) -> None:
        event = json.dumps(
            {
                "type": "error",
                "error": {"code": "max_steps_reached"},
                "tokens": {"input": 3, "output": 1},
            }
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(event, duration_ms=1, is_local=True)
        self.assertEqual(
            "structured_event",
            raised.exception.result.raw_metadata["termination_source"],
        )

    def test_reviewer_json_may_quote_step_limit_in_explanation(self) -> None:
        review = json.dumps(
            {
                "approved": False,
                "findings": [
                    {
                        "severity": "P1",
                        "title": "Worker stopped early",
                        "explanation": (
                            "The maximum number of steps for this agent has been "
                            "reached."
                        ),
                    }
                ],
            }
        )
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": review}}
        )
        result = parse_opencode_json(event, duration_ms=1, is_local=False)
        self.assertEqual(review, result.output)

    def test_reviewer_json_may_quote_step_limit_in_title_or_remediation(self) -> None:
        marker = "The maximum number of steps for this agent has been reached."
        for field in ("title", "remediation"):
            with self.subTest(field=field):
                finding = {
                    "severity": "P2",
                    "title": "Quoted diagnostic",
                    "explanation": "The review documents a prior failure.",
                    "remediation": "Keep the diagnostic as evidence.",
                }
                finding[field] = marker
                review = json.dumps({"approved": True, "findings": [finding]})
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": review}}
                )
                self.assertEqual(
                    review,
                    parse_opencode_json(
                        event, duration_ms=1, is_local=False
                    ).output,
                )

    def test_implementation_text_may_quote_step_limit_without_terminating(self) -> None:
        output = (
            "The test log said: The maximum number of steps for this agent has "
            "been reached. The implementation handled that error correctly."
        )
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": output}}
        )
        self.assertEqual(
            output,
            parse_opencode_json(event, duration_ms=1, is_local=True).output,
        )

    def test_structured_step_limit_overrides_valid_reviewer_json(self) -> None:
        review = json.dumps({"approved": True, "findings": []})
        for reason in (
            "max_steps",
            "maximum_steps",
            "step_limit_reached",
            "steps_exhausted",
        ):
            with self.subTest(reason=reason):
                events = "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "text",
                                "part": {"type": "text", "text": review},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "step_finish",
                                "part": {"finishReason": reason},
                            }
                        ),
                    )
                )
                with self.assertRaises(InvocationIncompleteError) as raised:
                    parse_opencode_json(events, duration_ms=1, is_local=False)
                self.assertEqual(review, raised.exception.result.output)
                self.assertEqual(
                    "structured_event",
                    raised.exception.result.raw_metadata["termination_source"],
                )

    def test_parser_recognizes_documented_step_limit_text_variants(self) -> None:
        variants = (
            "CRITICAL — MAXIMUM STEPS REACHED",
            "critical - maximum steps reached",
            "The maximum number of steps for this agent has been reached.",
            (
                "CRITICAL — MAXIMUM STEPS REACHED\n\n"
                "The maximum number of steps for this agent has been reached.\n"
                "Tools are disabled until the next user input."
            ),
        )
        for marker in variants:
            with self.subTest(marker=marker):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": marker}}
                )
                with self.assertRaises(InvocationIncompleteError):
                    parse_opencode_json(event, duration_ms=1, is_local=True)

    def test_parser_recognizes_real_opencode_max_steps_message(self) -> None:
        output = (
            "CRITICAL — MAXIMUM STEPS REACHED\n\n"
            "Maximum steps for this agent have been reached.\n"
            "Tools are disabled until the next user input."
        )
        event = json.dumps({"type": "text", "part": {"type": "text", "text": output}})
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(event, duration_ms=1, is_local=True)
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)

    def test_parser_recognizes_step_limit_marker_after_prose_and_summary(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps_long.jsonl"
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(
                fixture.read_text(encoding="utf-8"),
                duration_ms=53,
                is_local=False,
            )
        result = raised.exception.result
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual("session-long-step-limit", result.provider_request_id)
        self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
        self.assertEqual(0.125, result.remote_cost)
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertIn("Maximum steps for this agent have been reached.", result.output)
        self.assertIn("## Summary of Work Done", result.output)
        self.assertIn("Remaining work", result.output)

    def test_parser_recognizes_step_limit_split_across_text_events(self) -> None:
        head = (
            "I inspected the repository and drafted an implementation plan.\n\n"
            "</think>\n\n"
            "Maximum steps for this agent have been reached."
        )
        tail = (
            "\n\n## Summary of Work Done\n"
            "- Inspected the existing implementation.\n"
            "- Drafted a partial implementation.\n"
        )
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "sess-split",
                        "part": {"type": "text", "text": head},
                    }
                ),
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "sess-split",
                        "part": {"type": "text", "text": tail},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "sessionID": "sess-split",
                        "part": {"tokens": {"input": 5, "output": 3}},
                    }
                ),
            )
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(events, duration_ms=1, is_local=True)
        result = raised.exception.result
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual("sess-split", result.provider_request_id)
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertIn("Summary of Work Done", result.output)

    def test_long_step_limit_zero_and_nonzero_exit_adapter_paths(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps_long.jsonl"
        stdout = fixture.read_text(encoding="utf-8")
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                with tempfile.TemporaryDirectory() as directory:
                    request = _local_request(Path(directory))
                    process = _FakeProcess(
                        stdout, returncode=returncode, stderr="non-zero exit"
                    )
                    adapter = OpenCodeAdapter(opencode_command="opencode-stub")
                    with mock.patch("subprocess.Popen", return_value=process):
                        with self.assertRaises(InvocationIncompleteError) as raised:
                            adapter.invoke(request)
                error = raised.exception
                self.assertEqual("step_limit_reached", error.failure_kind)
                result = error.result
                self.assertEqual("session-long-step-limit", result.provider_request_id)
                self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
                self.assertEqual(0.0, result.remote_cost)
                self.assertFalse(result.cost_unavailable)
                self.assertEqual(0.125, result.raw_metadata["reported_cost"])
                self.assertEqual("final_text", result.raw_metadata["termination_source"])

    def test_summary_without_independent_marker_line_is_not_termination(self) -> None:
        output = (
            "## Summary of Work Done\n"
            "- The implementation stopped because the maximum number of steps "
            "for this agent has been reached, so this run was cut short.\n"
            "- No independent termination line was emitted here.\n"
        )
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": output}}
        )
        self.assertEqual(
            output.strip(),
            parse_opencode_json(event, duration_ms=1, is_local=True).output,
        )

    def test_step_limit_marker_wrapped_in_markdown_or_quotes_is_not_termination(self) -> None:
        marker = "Maximum steps for this agent have been reached."
        wrapped = (
            f"## {marker}",
            f"**{marker}**",
            f"*{marker}*",
            f"`{marker}`",
            f'"{marker}"',
            f"'{marker}'",
            f"* {marker}",
        )
        for output in wrapped:
            with self.subTest(output=output):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": output}}
                )
                self.assertEqual(
                    output,
                    parse_opencode_json(event, duration_ms=1, is_local=True).output,
                )

    def test_step_limit_reference_in_code_quote_or_diff_is_not_a_termination(self) -> None:
        marker = "The maximum number of steps for this agent has been reached."
        outputs = (
            f"```text\nCRITICAL — MAXIMUM STEPS REACHED\n{marker}\n```",
            f"> {marker}",
            f"+CRITICAL — MAXIMUM STEPS REACHED\n+{marker}",
        )
        for output in outputs:
            with self.subTest(output=output):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": output}}
                )
                self.assertEqual(
                    output,
                    parse_opencode_json(
                        event, duration_ms=1, is_local=True
                    ).output,
                )

    def test_parser_recognizes_summary_remaining_step_limit_format(self) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "opencode_max_steps_summary_remaining.jsonl"
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(
                fixture.read_text(encoding="utf-8"),
                duration_ms=50,
                is_local=False,
            )
        result = raised.exception.result
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual("session-summary-remaining", result.provider_request_id)
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertEqual(
            "step_limit_with_work_remaining_summary",
            result.raw_metadata["matched_rule_id"],
        )
        self.assertIn(
            "Here is a summary of the work done and remaining tasks:",
            result.raw_metadata["matched_line"],
        )
        self.assertIn("## Summary of Work Done", result.output)
        self.assertIn("## Remaining Tasks", result.output)

    def test_parser_recognizes_summary_completed_step_limit_format(self) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "opencode_max_steps_summary_completed.jsonl"
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(
                fixture.read_text(encoding="utf-8"),
                duration_ms=50,
                is_local=False,
            )
        result = raised.exception.result
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual("session-summary-completed", result.provider_request_id)
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertEqual(
            "step_limit_with_work_completed_summary",
            result.raw_metadata["matched_rule_id"],
        )
        self.assertIn("## Summary of Work Completed", result.output)

    def test_summary_step_limit_format_with_crlf_is_recognized(self) -> None:
        text = (
            "Maximum steps for this agent have been reached. "
            "Here is a summary of the work done and remaining tasks:\r\n"
            "\r\n"
            "## Summary of Work Done\r\n"
            "- Inspected the implementation.\r\n"
        )
        event = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": text}}
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(event, duration_ms=1, is_local=True)
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual(
            "step_limit_with_work_remaining_summary",
            raised.exception.result.raw_metadata["matched_rule_id"],
        )

    def test_summary_step_limit_with_reason_stop_is_recognized(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "s",
                        "part": {
                            "type": "text",
                            "text": (
                                "Maximum steps for this agent have been reached. "
                                "Here's a summary of the work completed and remaining:"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "type": "step-finish",
                            "reason": "stop",
                            "tokens": {"input": 1, "output": 1},
                        },
                    }
                ),
            )
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual(
            "stop", raised.exception.result.raw_metadata["terminal_reason"]
        )

    def test_summary_step_limit_with_event_counts_not_32_is_recognized(self) -> None:
        events = "\n".join(
            (
                json.dumps({"type": "step_start", "part": {}}),
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "s",
                        "part": {
                            "type": "text",
                            "text": (
                                "Maximum steps for this agent have been reached. "
                                "Here is a summary of the work done and remaining tasks:"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "type": "step-finish",
                            "reason": "stop",
                            "tokens": {"input": 1, "output": 1},
                        },
                    }
                ),
            )
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(events, duration_ms=1, is_local=True)
        metadata = raised.exception.result.raw_metadata
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual(1, metadata["step_start_count"])
        self.assertEqual(1, metadata["step_finish_count"])

    def test_configured_step_limit_is_recorded_in_metadata(self) -> None:
        event = json.dumps(
            {
                "type": "text",
                "sessionID": "s",
                "part": {
                    "type": "text",
                    "text": "Maximum steps for this agent have been reached.",
                },
            }
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(
                event, duration_ms=1, is_local=True, configured_step_limit=32
            )
        self.assertEqual(
            32, raised.exception.result.raw_metadata["configured_step_limit"]
        )
        self.assertEqual(
            "3", raised.exception.result.raw_metadata["classifier_version"]
        )

    def test_summary_step_limit_marker_wrapped_in_markdown_is_not_termination(self) -> None:
        markers = (
            "Maximum steps for this agent have been reached. Here is a summary of the work done and remaining tasks:",
            "Maximum steps for this agent have been reached. Here's a summary of the work completed and remaining:",
        )
        wrapped = []
        for marker in markers:
            wrapped.extend(
                (
                    f"## {marker}",
                    f"**{marker}**",
                    f"*{marker}*",
                    f"`{marker}`",
                    f'"{marker}"',
                    f"'{marker}'",
                    f"* {marker}",
                )
            )
        for output in wrapped:
            with self.subTest(output=output):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": output}}
                )
                self.assertEqual(
                    output,
                    parse_opencode_json(
                        event, duration_ms=1, is_local=True
                    ).output,
                )

    def test_summary_step_limit_marker_in_code_quote_or_diff_is_not_termination(self) -> None:
        marker = (
            "Maximum steps for this agent have been reached. "
            "Here is a summary of the work done and remaining tasks:"
        )
        outputs = (
            f"```text\n{marker}\n```",
            f"> {marker}",
            f"+{marker}",
        )
        for output in outputs:
            with self.subTest(output=output):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": output}}
                )
                self.assertEqual(
                    output,
                    parse_opencode_json(
                        event, duration_ms=1, is_local=True
                    ).output,
                )

    def test_summary_step_limit_phrase_in_middle_of_sentence_is_not_termination(self) -> None:
        output = (
            "The worker logged the notice: Maximum steps for this agent have been "
            "reached. Here is a summary of the work done and remaining tasks: and "
            "then it kept going."
        )
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": output}}
        )
        self.assertEqual(
            output,
            parse_opencode_json(event, duration_ms=1, is_local=True).output,
        )

    def test_bare_marker_with_unrecognized_suffix_is_suspected(self) -> None:
        output = (
            "Maximum steps for this agent have been reached. "
            "Tools are disabled until the next user input.\n"
        )
        event = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": output}}
        )
        with self.assertRaises(InvocationIncompleteError) as raised:
            parse_opencode_json(event, duration_ms=1, is_local=True)
        self.assertEqual("suspected_step_limit", raised.exception.failure_kind)
        metadata = raised.exception.result.raw_metadata
        self.assertEqual("final_text", metadata["termination_source"])
        self.assertEqual(
            "unrecognized_step_limit_suffix", metadata["matched_rule_id"]
        )

    def test_any_legal_bare_marker_with_unapproved_suffix_is_suspected(self) -> None:
        suffix = "Unknown suffix"
        variants = (
            "The maximum number of steps for this agent has been reached. {suffix}",
            "the maximum number of steps for this agent has been reached. {suffix}",
            "Maximum steps for this agent have been reached. {suffix}",
            "The maximum steps for this agent has been reached. {suffix}",
            "max number of steps for this agent were reached. {suffix}",
            "maximum steps for this agent was reached. {suffix}",
            "CRITICAL — MAXIMUM STEPS REACHED. {suffix}",
            "critical - maximum steps reached. {suffix}",
            "CRITICAL — MAX STEPS REACHED. {suffix}",
        )
        for marker in variants:
            with self.subTest(marker=marker):
                text = marker.format(suffix=suffix)
                event = json.dumps(
                    {
                        "type": "text",
                        "sessionID": "s",
                        "part": {"type": "text", "text": text},
                    }
                )
                with self.assertRaises(InvocationIncompleteError) as raised:
                    parse_opencode_json(event, duration_ms=1, is_local=True)
                self.assertEqual("suspected_step_limit", raised.exception.failure_kind)
                metadata = raised.exception.result.raw_metadata
                self.assertEqual("final_text", metadata["termination_source"])
                self.assertEqual(
                    "unrecognized_step_limit_suffix", metadata["matched_rule_id"]
                )

    def test_marker_period_is_a_suffix_boundary_without_following_space(self) -> None:
        variants = (
            "Maximum steps for this agent have been reached.Unknown suffix",
            "The maximum number of steps for this agent has been reached.Unknown suffix",
            "CRITICAL — MAXIMUM STEPS REACHED.Unknown suffix",
        )
        for output in variants:
            with self.subTest(output=output):
                event = json.dumps(
                    {
                        "type": "text",
                        "sessionID": "s",
                        "part": {"type": "text", "text": output},
                    }
                )
                with self.assertRaises(InvocationIncompleteError) as raised:
                    parse_opencode_json(event, duration_ms=1, is_local=True)
                self.assertEqual("suspected_step_limit", raised.exception.failure_kind)

    def test_marker_immediately_followed_by_word_char_is_not_termination(self) -> None:
        outputs = (
            "Maximum steps for this agent have been reachedness is not a marker",
            "Maximum steps for this agent have been reached123 is not a marker",
            "Maximum steps for this agent have been reached_value is not a marker",
        )
        for output in outputs:
            with self.subTest(output=output):
                event = json.dumps(
                    {"type": "text", "part": {"type": "text", "text": output}}
                )
                self.assertEqual(
                    output,
                    parse_opencode_json(event, duration_ms=1, is_local=True).output,
                )

    def test_terminal_reason_reflects_last_defined_step_finish_reason(self) -> None:
        text = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "done"}}
        )
        first = json.dumps(
            {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool_use"}}
        )
        second = json.dumps(
            {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}}
        )
        events = "\n".join((text, first, second))
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual("stop", result.raw_metadata["terminal_reason"])

        no_reason_last = json.dumps(
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 1, "output": 1}},
            }
        )
        events_without_final_reason = "\n".join((text, first, no_reason_last))
        result = parse_opencode_json(
            events_without_final_reason, duration_ms=1, is_local=True
        )
        self.assertEqual("tool_use", result.raw_metadata["terminal_reason"])

    def test_event_count_without_marker_is_not_step_limit(self) -> None:
        events = "\n".join(
            (json.dumps({"type": "step_start", "part": {}}),) * 32
            + (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "s",
                        "part": {"type": "text", "text": "done"},
                    }
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual("done", result.output)
        self.assertEqual(32, result.raw_metadata["step_start_count"])

    def test_reason_stop_without_marker_is_not_step_limit(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "s",
                        "part": {"type": "text", "text": "implementation complete"},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {
                            "type": "step-finish",
                            "reason": "stop",
                            "tokens": {"input": 1, "output": 1},
                        },
                    }
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual("implementation complete", result.output)
        self.assertEqual("stop", result.raw_metadata["terminal_reason"])

    def test_marker_not_fabricated_across_unrelated_text_events(self) -> None:
        head = json.dumps(
            {
                "type": "text",
                "sessionID": "s",
                "part": {"type": "text", "text": "Maximum steps for this agent "},
            }
        )
        tail = json.dumps(
            {
                "type": "text",
                "sessionID": "s",
                "part": {"type": "text", "text": "have been reached."},
            }
        )
        events = f"{head}\n{tail}"
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual(
            "Maximum steps for this agent have been reached.", result.output
        )

    def test_read_only_permissions_deny_edits_and_external_access(self) -> None:
        permission = OpenCodeAdapter._permission_config(read_only=True)["permission"]
        self.assertEqual("deny", permission["edit"])
        self.assertEqual("deny", permission["bash"])
        self.assertEqual("deny", permission["external_directory"])
        self.assertEqual(["lmstudio"], OpenCodeAdapter._permission_config(read_only=True)["enabled_providers"])
        self.assertEqual(
            permission,
            OpenCodeAdapter._permission_config(read_only=True)["agent"]["agentflow-sandbox"]["permission"],
        )

    def test_cancel_stops_local_process_group(self) -> None:
        process = subprocess.Popen(
            (sys.executable, "-c", "import time; time.sleep(60)"),
            start_new_session=True,
        )
        try:
            self.assertTrue(
                OpenCodeAdapter().cancel(f"local-process-group:{process.pid}")
            )
            process.wait(timeout=2)
            self.assertNotEqual(0, process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_nonzero_exit_step_limit_is_incomplete_not_runtime_error(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            process = _FakeProcess(
                fixture.read_text(encoding="utf-8"), returncode=1, stderr="non-zero exit"
            )
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationIncompleteError) as raised:
                    adapter.invoke(request)
        error = raised.exception
        self.assertEqual("step_limit_reached", error.failure_kind)
        result = error.result
        self.assertEqual("session-step-limit", result.provider_request_id)
        self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)
        self.assertEqual(0.125, result.raw_metadata["reported_cost"])
        self.assertEqual("final_text", result.raw_metadata["termination_source"])
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_nonzero_exit_without_step_limit_signal_is_runtime_error(self) -> None:
        stdout = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "partial"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            process = _FakeProcess(stdout, returncode=1, stderr="boom")
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(RuntimeError) as raised:
                    adapter.invoke(request)
        self.assertNotIsInstance(raised.exception, InvocationIncompleteError)
        self.assertIn("boom", str(raised.exception))

    def test_signal_terminated_local_process_is_unknown_not_known_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            process = _FakeProcess("", returncode=-15)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown):
                    adapter.invoke(request)

    def test_implementation_max_steps_is_read_from_request_metadata(self) -> None:
        stdout = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "done"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = budgeted_request(
                call_id="call-1",
                request_key="request-1",
                run_id="run-1",
                task_id="task-1",
                role="implementation",
                model=ModelRef("lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True),
                prompt="implement the change",
                data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
                read_only=False,
                metadata={
                    "worktree": str(directory),
                    "allowed_files": (),
                    "implementation_max_steps": 16,
                },
            )
            captured: dict[str, object] = {}

            def popen(command, **kwargs):
                captured["env"] = kwargs["env"]
                return _FakeProcess(stdout, returncode=0)

            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", side_effect=popen):
                adapter.invoke(request)
        config = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(16, config["agent"]["agentflow-sandbox"]["steps"])

    def test_continuation_session_adds_session_flag(self) -> None:
        stdout = json.dumps(
            {"type": "text", "sessionID": "session-abc-123", "part": {"type": "text", "text": "done"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            request = replace(request, metadata={
                **request.metadata,
                "continuation_session_id": "session-abc-123",
            })
            captured: dict[str, object] = {}

            def popen(command, **kwargs):
                captured["command"] = command
                return _FakeProcess(stdout, returncode=0)

            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", side_effect=popen):
                adapter.invoke(request)
        command = captured["command"]
        self.assertIn("--session", command)
        self.assertEqual("session-abc-123", command[command.index("--session") + 1])

    def test_continuation_session_omitted_when_not_requested(self) -> None:
        stdout = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "done"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            captured: dict[str, object] = {}

            def popen(command, **kwargs):
                captured["command"] = command
                return _FakeProcess(stdout, returncode=0)

            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", side_effect=popen):
                adapter.invoke(request)
        self.assertNotIn("--session", captured["command"])

    def test_continuation_rejects_mismatched_session(self) -> None:
        stdout = json.dumps(
            {"type": "text", "sessionID": "different-session", "part": {"type": "text", "text": "done"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            request = replace(request, metadata={
                **request.metadata,
                "continuation_session_id": "session-abc-123",
            })
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch(
                "subprocess.Popen", return_value=_FakeProcess(stdout, returncode=0)
            ):
                with self.assertRaises(InvocationIncompleteError) as raised:
                    adapter.invoke(request)
        self.assertEqual("session_mismatch", raised.exception.failure_kind)
        self.assertEqual("different-session", raised.exception.result.provider_request_id)

    def test_continuation_step_limit_mismatched_session_is_protocol_error(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            request = _local_request(Path(directory))
            request = replace(request, metadata={
                **request.metadata,
                "continuation_session_id": "expected-session",
            })
            process = _FakeProcess(
                fixture.read_text(encoding="utf-8"), returncode=1, stderr="non-zero exit"
            )
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationIncompleteError) as raised:
                    adapter.invoke(request)
        self.assertEqual("session_mismatch", raised.exception.failure_kind)

    def test_invalid_continuation_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in ("", "   ", "bad session"):
                with self.subTest(value=value):
                    request = _local_request(Path(directory))
                    request = replace(request, metadata={
                        **request.metadata,
                        "continuation_session_id": value,
                    })
                    adapter = OpenCodeAdapter(opencode_command="opencode-stub")
                    with mock.patch("subprocess.Popen") as popen:
                        with self.assertRaises(ValueError):
                            adapter.invoke(request)
                    popen.assert_not_called()

    def test_invalid_implementation_max_steps_in_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in (0, -1, 33, True, 8.5, "16"):
                with self.subTest(value=value):
                    request = budgeted_request(
                        call_id="call-1",
                        request_key="request-1",
                        run_id="run-1",
                        task_id="task-1",
                        role="implementation",
                        model=ModelRef("lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True),
                        prompt="implement the change",
                        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
                        read_only=False,
                        metadata={
                            "worktree": str(directory),
                            "allowed_files": (),
                            "implementation_max_steps": value,
                        },
                    )
                    adapter = OpenCodeAdapter(opencode_command="opencode-stub")
                    with mock.patch("subprocess.Popen") as popen:
                        with self.assertRaises(ValueError):
                            adapter.invoke(request)
                    popen.assert_not_called()

    def _timeout_request(
        self, directory: str, metadata: dict[str, object]
    ) -> InvocationRequest:
        return budgeted_request(
            call_id="call-1",
            request_key="request-1",
            run_id="run-1",
            task_id="task-1",
            role="implementation",
            model=ModelRef("lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True),
            prompt="implement the change",
            data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
            read_only=False,
            metadata={"worktree": directory, "allowed_files": (), **metadata},
        )

    def test_implementation_timeout_is_read_from_request_metadata(self) -> None:
        stdout = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "done"}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(
                directory, {"implementation_timeout_seconds": 120}
            )
            process = _FakeProcess(stdout, returncode=0)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process) as popen:
                adapter.invoke(request)
            popen.assert_called_once()

    def test_default_timeout_is_used_when_not_specified(self) -> None:
        partial = json.dumps(
            {"type": "step_finish", "part": {"tokens": {"input": 3, "output": 1}}}
        )
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(directory, {})
            process = _TimingOutProcess(partial)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown):
                    adapter.invoke(request)
        self.assertEqual(900, process.timeout_args[0])

    def test_invalid_implementation_timeout_in_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in (0, -1, 59, 14401, True, 900.0, 60.5, "900"):
                with self.subTest(value=value):
                    request = self._timeout_request(
                        directory, {"implementation_timeout_seconds": value}
                    )
                    adapter = OpenCodeAdapter(opencode_command="opencode-stub")
                    with mock.patch("subprocess.Popen") as popen:
                        with self.assertRaises(ValueError):
                            adapter.invoke(request)
                    popen.assert_not_called()

    def test_timeout_raises_unknown_with_partial_usage(self) -> None:
        events = (
            json.dumps({"type": "step_start", "sessionID": "sess-9", "part": {}}),
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 17, "output": 9}}}
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"tokens": {"input": 23, "output": 6}},
                }
            ),
        )
        partial_bytes = ("\n".join(events) + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(
                directory, {"implementation_timeout_seconds": 120}
            )
            process = _TimingOutProcess(partial_bytes)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown) as raised:
                    adapter.invoke(request)
        error = raised.exception
        self.assertEqual("local-process-group:41234", error.provider_request_id)
        self.assertEqual([120, 5.0], process.timeout_args)
        result = error.result
        self.assertIsNotNone(result)
        self.assertEqual("", result.output)
        self.assertEqual("sess-9", result.provider_request_id)
        self.assertEqual((40, 15), (result.input_tokens, result.output_tokens))
        self.assertEqual(2, result.raw_metadata["completed_step_count"])
        self.assertEqual("timeout", result.raw_metadata["termination_reason"])
        self.assertEqual(120, result.raw_metadata["timeout_seconds"])
        self.assertEqual("opencode_json_events", result.raw_metadata["token_source"])
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_drain_never_waits_without_timeout_when_kill_fails(self) -> None:
        events = (
            json.dumps({"type": "step_start", "sessionID": "sess-9", "part": {}}),
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 17, "output": 9}}}
            ),
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 23, "output": 6}}}
            ),
        )
        partial_bytes = ("\n".join(events) + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(
                directory, {"implementation_timeout_seconds": 120}
            )
            process = _KillFailingProcess(partial_bytes, b"")
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown) as raised:
                    adapter.invoke(request)
        error = raised.exception
        self.assertEqual("timeout", error.termination_reason)
        result = error.result
        self.assertIsNotNone(result)
        self.assertEqual(3, process.communicate_calls)
        for timeout_arg in process.timeout_args:
            self.assertIsNotNone(timeout_arg)
        self.assertEqual((40, 15), (result.input_tokens, result.output_tokens))
        self.assertEqual("sess-9", result.provider_request_id)
        self.assertTrue(result.raw_metadata["cleanup_incomplete"])
        self.assertEqual("timeout", result.raw_metadata["termination_reason"])

    def test_timeout_identical_step_finish_events_are_counted_twice(self) -> None:
        step = json.dumps(
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 17, "output": 9, "reasoning": 2}},
            }
        )
        partial = (step + "\n" + step + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(directory, {})
            process = _TimingOutProcess(partial)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown) as raised:
                    adapter.invoke(request)
        result = raised.exception.result
        self.assertEqual(34, result.input_tokens)
        self.assertEqual(18, result.output_tokens)
        self.assertEqual(4, result.raw_metadata["reasoning_tokens"])
        self.assertEqual(2, result.raw_metadata["completed_step_count"])

    def test_timeout_overlapping_output_is_not_double_counted(self) -> None:
        first_event = json.dumps(
            {"type": "step_finish", "part": {"tokens": {"input": 17, "output": 9}}}
        )
        second_event = json.dumps(
            {"type": "step_finish", "part": {"tokens": {"input": 23, "output": 6}}}
        )
        first_bytes = (first_event + "\n").encode("utf-8")
        remaining = first_event + "\n" + second_event + "\n"
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(directory, {})
            process = _TimingOutProcess(first_bytes, remaining=remaining)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown) as raised:
                    adapter.invoke(request)
        result = raised.exception.result
        self.assertEqual((40, 15), (result.input_tokens, result.output_tokens))
        self.assertEqual(2, result.raw_metadata["completed_step_count"])

    def test_completed_step_count_only_counts_step_finish(self) -> None:
        events = (
            json.dumps({"type": "step_start", "part": {}}),
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 1, "output": 1}}}
            ),
            json.dumps(
                {"type": "step_finish", "part": {"tokens": {"input": 2, "output": 2}}}
            ),
        )
        data = ("\n".join(events) + "\n").encode("utf-8")
        result = parse_opencode_partial_usage(data, duration_ms=1, timeout_seconds=60)
        self.assertEqual(2, result.raw_metadata["completed_step_count"])
        self.assertEqual(3, result.input_tokens)
        self.assertEqual(3, result.output_tokens)

    def test_merge_bytes_removes_partial_overlap(self) -> None:
        first = b'{"a":1}\n{"b":2}\n'
        second = b'{"b":2}\n{"c":3}\n'
        merged = _merge_overlapping_output_bytes(first, second)
        self.assertEqual(b'{"a":1}\n{"b":2}\n{"c":3}\n', merged)

    def test_merge_bytes_handles_utf8_multibyte_split(self) -> None:
        line = '{"text":"café"}\n'.encode("utf-8")
        first = line[: line.index(b"\xc3") + 1]
        self.assertTrue(first.endswith(b"\xc3"))
        merged = _merge_overlapping_output_bytes(first, line)
        self.assertEqual(line, merged)
        self.assertEqual('{"text":"café"}', merged.decode("utf-8").strip())

    def test_parser_sums_tokens_across_steps(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {"type": "text", "part": {"type": "text", "text": "hello"}}
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"tokens": {"input": 100, "output": 10, "reasoning": 20}},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"tokens": {"input": 200, "output": 15, "reasoning": 5}},
                    }
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual(300, result.input_tokens)
        self.assertEqual(25, result.output_tokens)
        self.assertEqual(25, result.raw_metadata["reasoning_tokens"])

    def test_timeout_without_usage_marks_unavailable(self) -> None:
        partial = "this is not json\n"
        with tempfile.TemporaryDirectory() as directory:
            request = self._timeout_request(directory, {})
            process = _TimingOutProcess(partial)
            adapter = OpenCodeAdapter(opencode_command="opencode-stub")
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(InvocationOutcomeUnknown) as raised:
                    adapter.invoke(request)
        result = raised.exception.result
        self.assertEqual(0, result.input_tokens)
        self.assertTrue(result.raw_metadata["usage_unavailable"])
        self.assertEqual("unavailable", result.raw_metadata["token_source"])


if __name__ == "__main__":
    unittest.main()
