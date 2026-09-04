from __future__ import annotations

import unittest

from agentflow.adapters import AdapterRouter
from agentflow.contracts import DataSensitivity, InvocationRequest, ModelRef
from agentflow.fake_adapter import FakeAdapter


class AdapterRouterTests(unittest.TestCase):
    def test_routes_by_provider_without_fallback(self) -> None:
        request = InvocationRequest(
            call_id="call",
            request_key="key",
            run_id="run",
            task_id="task",
            role="implementation",
            model=ModelRef("fake", "model", "1", is_local=True),
            prompt="test",
            data_sensitivity=DataSensitivity.PUBLIC,
            read_only=False,
        )
        fake = FakeAdapter()
        router = AdapterRouter({"fake": fake})
        router.invoke(request)
        self.assertEqual([request], fake.invocations)
        with self.assertRaises(ValueError):
            router.adapter_for("missing")


if __name__ == "__main__":
    unittest.main()
