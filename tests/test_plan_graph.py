from __future__ import annotations

import unittest

from app.agents.contracts import ExecutionPlan
from app.agents.plan_graph import ModelBinding, PlanGraph, PlanNode, PlanNodeAccess, PlanNodeKind


class PlanGraphTest(unittest.TestCase):
    def test_execution_plan_becomes_dependency_graph(self) -> None:
        plan = ExecutionPlan(
            requirement_understanding="Build a small feature",
            non_goals=[],
            files_and_symbols=["app/main.py"],
            steps=["Implement core", "Add UI"],
            constraints=[],
            acceptance_criteria=["Works"],
            required_tests=["tests"],
            risks=[],
            open_questions=[],
        )

        graph = PlanGraph.from_execution_plan(plan)

        self.assertEqual([node.node_id for node in graph.ready(set())], ["step-1"])
        self.assertEqual(
            [node.node_id for node in graph.ready({"step-1"})],
            ["step-2"],
        )
        self.assertEqual([node.kind for node in graph.nodes], [
            PlanNodeKind.IMPLEMENTATION,
            PlanNodeKind.IMPLEMENTATION,
        ])

    def test_enabled_node_cannot_depend_on_disabled_node(self) -> None:
        with self.assertRaisesRegex(ValueError, "depends on disabled"):
            PlanGraph.from_dict(
                {
                    "requirement_summary": "blocked",
                    "nodes": [
                        {"node_id": "a", "title": "A", "kind": "custom", "enabled": False},
                        {"node_id": "b", "title": "B", "kind": "custom", "depends_on": ["a"]},
                    ],
                }
            )

    def test_node_model_and_terminal_configuration_round_trips(self) -> None:
        graph = PlanGraph(
            requirement_summary="Configure a UI node",
            nodes=[
                PlanNode(
                    node_id="ui",
                    title="Implement UI",
                    kind=PlanNodeKind.IMPLEMENTATION,
                    access=PlanNodeAccess.WORKSPACE_WRITE,
                    model=ModelBinding(provider="kimi", model="kimi-k3", thinking="high"),
                    terminal={"kind": "local"},  # type: ignore[arg-type]
                )
            ],
        )
        # Keep this test focused on the serialized contract while accepting the
        # dataclass as the canonical construction surface below.
        graph = PlanGraph.from_dict(
            {
                "requirement_summary": "Configure a UI node",
                "nodes": [
                    {
                        "node_id": "ui",
                        "title": "Implement UI",
                        "kind": "implementation",
                        "access": "workspace_write",
                        "model": {"provider": "kimi", "model": "kimi-k3", "thinking": "high"},
                        "terminal": {"kind": "local", "worktree": "ui"},
                    }
                ],
            }
        )

        graph.validate()
        self.assertEqual(graph.nodes[0].model.model, "kimi-k3")
        self.assertEqual(graph.nodes[0].terminal.worktree, "ui")

    def test_capability_and_phase_models_round_trip(self) -> None:
        graph = PlanGraph.from_dict(
            {
                "requirement_summary": "Inspect and implement",
                "planning_model": {"profile_id": "planner", "runtime": "claude_code"},
                "review_model": {"profile_id": "reviewer", "runtime": "claude_code"},
                "nodes": [
                    {
                        "node_id": "research",
                        "title": "Research constraints",
                        "kind": "custom",
                        "capability": "research",
                        "access": "read_only",
                    }
                ],
            }
        )

        restored = PlanGraph.from_dict(graph.to_dict())

        self.assertEqual(restored.node("research").capability, "research")
        self.assertEqual(restored.planning_model.profile_id, "planner")
        self.assertEqual(restored.review_model.profile_id, "reviewer")

    def test_legacy_graph_without_phase_models_still_loads(self) -> None:
        graph = PlanGraph.from_dict(
            {
                "requirement_summary": "Legacy",
                "nodes": [{"node_id": "old", "title": "Old", "kind": "custom"}],
            }
        )

        self.assertEqual(graph.planning_model.profile_id, "")
        self.assertEqual(graph.review_model.profile_id, "")

    def test_cycles_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cycle"):
            PlanGraph.from_dict(
                {
                    "requirement_summary": "cycle",
                    "nodes": [
                        {"node_id": "a", "title": "A", "kind": "custom", "depends_on": ["b"]},
                        {"node_id": "b", "title": "B", "kind": "custom", "depends_on": ["a"]},
                    ],
                }
            )

    def test_long_step_title_is_truncated_but_full_instructions_kept(self) -> None:
        # A verbose model may write a step longer than the 160-char node-title
        # cap; from_execution_plan must keep the full text as instructions
        # (what the executor runs) while deriving a short, valid label.
        long_step = "在 app/agents/workflow.py 中定位 _PLANNER_OUTPUT_INSTRUCTION 常量，" * 6
        plan = ExecutionPlan(
            requirement_understanding="append a note",
            non_goals=[],
            files_and_symbols=["app/agents/workflow.py"],
            steps=[long_step],
            constraints=[],
            acceptance_criteria=["marker present"],
            required_tests=["tests"],
            risks=[],
            open_questions=[],
        )
        graph = PlanGraph.from_execution_plan(plan)
        impl = graph.node("step-1")
        self.assertLessEqual(len(impl.title), 160)
        self.assertTrue(impl.title)  # non-empty
        self.assertEqual(impl.instructions, long_step)  # full step preserved
        graph.validate()  # must not raise


if __name__ == "__main__":
    unittest.main()
