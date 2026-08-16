from __future__ import annotations

import unittest

from app.agents.contracts import AgentAccess, AgentRequest, AgentResult
from app.agents.runtime import ProfileRoutedRuntime, RoleRoutedRuntime


class LabelRuntime:
    """Records its label on every call so tests can assert which runtime ran."""

    def __init__(self, label: str):
        self.label = label
        self.invoked: list[AgentRequest] = []
        self.described: list[AgentRequest] = []
        self.cancelled: list[str] = []

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


def request(role: str, task_id: str = "TASK-1") -> AgentRequest:
    return AgentRequest(
        task_id=task_id,
        role=role,
        instructions="",
        workspace=None,  # type: ignore[arg-type]  # not used by LabelRuntime
        access=AgentAccess.READ_ONLY,
    )


class ProfileRoutedRuntimeTest(unittest.TestCase):
    def test_model_profile_routes_independently_of_role(self) -> None:
        fallback = LabelRuntime("fallback")
        premium = LabelRuntime("premium")
        routed = ProfileRoutedRuntime(
            {"planner": fallback, "executor": fallback, "reviewer": fallback},
            {"security-premium": premium},
        )
        selected = request(role="executor")
        selected.model_profile_id = "security-premium"

        result = routed.invoke(selected)

        self.assertEqual(result.runtime, "premium")
        self.assertEqual(len(premium.invoked), 1)
        self.assertEqual(fallback.invoked, [])

    def test_missing_profile_falls_back_to_role(self) -> None:
        fallback = LabelRuntime("fallback")
        routed = ProfileRoutedRuntime(
            {"planner": fallback, "executor": fallback, "reviewer": fallback},
            {"security-premium": LabelRuntime("premium")},
        )

        result = routed.invoke(request(role="reviewer"))

        self.assertEqual(result.runtime, "fallback")
        self.assertEqual(routed.describe(request(role="reviewer"))["runtime"], "fallback")

    def test_unknown_profile_is_rejected(self) -> None:
        routed = ProfileRoutedRuntime(
            {"planner": LabelRuntime("fallback")},
            {"security-premium": LabelRuntime("premium")},
        )
        selected = request(role="planner")
        selected.model_profile_id = "missing"
        with self.assertRaises(ValueError):
            routed.invoke(selected)

    def test_cancel_reaches_profile_runtime_via_pending(self) -> None:
        fallback = LabelRuntime("fallback")
        premium = LabelRuntime("premium")
        routed = ProfileRoutedRuntime(
            {"planner": fallback, "executor": fallback, "reviewer": fallback},
            {"security-premium": premium},
        )

        # describe() registers the resolved runtime as pending for the task so
        # a concurrent cancel can reach it; cancelling must hit the profile
        # runtime, not the role fallback.
        selected = request(role="executor", task_id="TASK-X")
        selected.model_profile_id = "security-premium"
        routed.describe(selected)
        self.assertTrue(routed.cancel("TASK-X"))
        self.assertIn("TASK-X", premium.cancelled)
        self.assertEqual(fallback.cancelled, [])

    def test_health_check_includes_profile_entries(self) -> None:
        routed = ProfileRoutedRuntime(
            {"planner": LabelRuntime("fallback")},
            {"security-premium": LabelRuntime("premium")},
        )

        health = routed.health_check()
        self.assertIn("planner", health)
        self.assertIn("profile:security-premium", health)
        self.assertEqual(health["profile:security-premium"]["runtime"], "premium")

    def test_rejects_empty_runtimes(self) -> None:
        with self.assertRaises(ValueError):
            RoleRoutedRuntime({})
        with self.assertRaises(ValueError):
            ProfileRoutedRuntime({"planner": LabelRuntime("fallback")}, {})


if __name__ == "__main__":
    unittest.main()
