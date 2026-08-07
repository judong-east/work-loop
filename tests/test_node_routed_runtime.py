from __future__ import annotations

import unittest

from app.agents.contracts import AgentAccess, AgentRequest, AgentResult
from app.agents.runtime import NodeRoutedRuntime, ProfileRoutedRuntime


class LabelRuntime:
    """Records its label on every call so tests can assert which runtime ran."""

    def __init__(self, label: str):
        self.label = label
        self.invoked: list[AgentRequest] = []
        self.described: list[AgentRequest] = []
        self.cancelled: list[str] = []
        # Hold an event so cancel tests can verify the active runtime is reached.
        self._active_task_id: str | None = None

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.invoked.append(request)
        return AgentResult(
            succeeded=True,
            output={"label": self.label},
            session_id=f"session-{self.label}",
            runtime=self.label,
            runtime_version="1",
            model=self.label,
        )

    def describe(self, request: AgentRequest) -> dict:
        self.described.append(request)
        return {"runtime": self.label, "runtime_version": "1", "model": self.label, "config": {}}

    def cancel(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True

    def health_check(self) -> dict:
        return {"available": True, "runtime": self.label, "error": ""}


def request(role: str, node_id: str = "", task_id: str = "TASK-1") -> AgentRequest:
    return AgentRequest(
        task_id=task_id,
        role=role,
        instructions="",
        workspace=None,  # type: ignore[arg-type]  # not used by LabelRuntime
        access=AgentAccess.READ_ONLY,
        node_id=node_id,
    )


class NodeRoutedRuntimeTest(unittest.TestCase):
    def test_node_id_routes_to_node_runtime(self) -> None:
        planner = LabelRuntime("planner")
        executor = LabelRuntime("executor")
        ui_runtime = LabelRuntime("pi-kimi")
        routed = NodeRoutedRuntime(
            {"planner": planner, "executor": executor, "reviewer": planner},
            {"ui": ui_runtime},
        )

        result = routed.invoke(request(role="executor", node_id="ui"))

        self.assertEqual(ui_runtime.invoked and not executor.invoked, True)
        self.assertEqual(result.runtime, "pi-kimi")
        # A request without a node override falls back to the role runtime.
        routed.invoke(request(role="executor", node_id="backend"))
        self.assertEqual(len(executor.invoked), 1)
        self.assertEqual(executor.invoked[0].node_id, "backend")

    def test_empty_node_id_falls_back_to_role(self) -> None:
        planner = LabelRuntime("planner")
        executor = LabelRuntime("executor")
        routed = NodeRoutedRuntime(
            {"planner": planner, "executor": executor, "reviewer": planner},
            {"ui": LabelRuntime("pi-kimi")},
        )

        routed.invoke(request(role="reviewer", node_id=""))
        self.assertEqual(len(planner.invoked), 1)
        self.assertEqual(planner.invoked[0].role, "reviewer")

    def test_describe_routes_per_node(self) -> None:
        planner = LabelRuntime("planner")
        ui_runtime = LabelRuntime("pi-kimi")
        routed = NodeRoutedRuntime(
            {"planner": planner, "executor": planner, "reviewer": planner},
            {"ui": ui_runtime},
        )

        identity = routed.describe(request(role="executor", node_id="ui"))
        self.assertEqual(identity["runtime"], "pi-kimi")
        routed.describe(request(role="planner", node_id="planning"))
        self.assertEqual(len(planner.described), 1)

    def test_cancel_reaches_node_runtime_via_pending(self) -> None:
        planner = LabelRuntime("planner")
        ui_runtime = LabelRuntime("pi-kimi")
        routed = NodeRoutedRuntime(
            {"planner": planner, "executor": planner, "reviewer": planner},
            {"ui": ui_runtime},
        )

        # describe() registers the resolved runtime as pending for the task so
        # a concurrent cancel can reach it. Resolving it for the node target and
        # then cancelling must hit the node runtime, not the role fallback.
        routed.describe(request(role="executor", node_id="ui", task_id="TASK-X"))
        self.assertTrue(routed.cancel("TASK-X"))
        self.assertIn("TASK-X", ui_runtime.cancelled)
        self.assertEqual(planner.cancelled, [])

    def test_health_check_includes_node_entries(self) -> None:
        planner = LabelRuntime("planner")
        ui_runtime = LabelRuntime("pi-kimi")
        routed = NodeRoutedRuntime(
            {"planner": planner, "executor": planner, "reviewer": planner},
            {"ui": ui_runtime},
        )

        health = routed.health_check()
        self.assertIn("planner", health)
        self.assertIn("node:ui", health)
        self.assertEqual(health["node:ui"]["runtime"], "pi-kimi")

    def test_rejects_empty_role_runtimes(self) -> None:
        with self.assertRaises(ValueError):
            NodeRoutedRuntime({}, {"ui": LabelRuntime("pi-kimi")})

    def test_model_profile_routes_independently_of_role_or_node_id(self) -> None:
        fallback = LabelRuntime("fallback")
        premium = LabelRuntime("premium")
        routed = ProfileRoutedRuntime(
            {"planner": fallback, "executor": fallback, "reviewer": fallback},
            {"security-premium": premium},
        )
        selected = request(role="executor", node_id="authentication")
        selected.model_profile_id = "security-premium"

        result = routed.invoke(selected)

        self.assertEqual(result.runtime, "premium")
        self.assertEqual(len(premium.invoked), 1)
        self.assertEqual(fallback.invoked, [])


if __name__ == "__main__":
    unittest.main()
