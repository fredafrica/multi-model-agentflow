from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest import mock

from agentflow.opencode_adapter import OpenCodeAdapter, parse_opencode_json


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


if __name__ == "__main__":
    unittest.main()
