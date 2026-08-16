from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from app.agents.composition import ExecutionComposer, ModelCatalog, ModelOption
from app.agents.context_ledger import MAX_CONTEXT_CHARS, ContextLedger, ContextPack
from app.agents.contracts import AgentAccess, AgentTask, AgentTaskStatus, ExecutionPlan, TaskBudget
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.plan_graph import PlanGraph, PlanNode, PlanNodeAccess, PlanNodeKind
from app.agents.task_budget import usage_tokens
from app.agents.workflow import AgentWorkflow
from app.memory.experience_store import ExperienceStore
from tests.git_support import create_repository
from tests.test_agent_workflow import PassingValidator, execution_plan, passing_review


def plan_with_specialized_steps() -> ExecutionPlan:
    return ExecutionPlan(
        requirement_understanding="Build a secure account settings experience",
        non_goals=[],
        files_and_symbols=["app/web/settings.tsx", "app/auth/service.py"],
        steps=[
            "Implement the React account settings interface",
            "Harden authentication token rotation against concurrent requests",
        ],
        constraints=["Preserve existing API compatibility"],
        acceptance_criteria=[
            "Users can update account settings",
            "Concurrent token rotation remains safe",
        ],
        required_tests=["unit"],
        risks=["Authentication regression"],
        open_questions=[],
    )


class ExecutionComposerTest(unittest.TestCase):
    def test_high_risk_capability_requires_quality_four(self) -> None:
        catalog = ModelCatalog(
            [
                ModelOption(
                    profile_id="planner",
                    label="Planner and reviewer",
                    runtime="claude_code",
                    model="read-model",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["planning", "review", "general"],
                    quality=4,
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                ),
                ModelOption(
                    profile_id="low-security",
                    label="Low security",
                    runtime="pi_rpc",
                    model="write-model",
                    access=AgentAccess.WORKSPACE_WRITE,
                    capabilities=["implementation", "security"],
                    quality=3,
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                ),
            ]
        )
        plan = plan_with_specialized_steps()
        plan.steps = ["Harden authentication token rotation security"]

        with self.assertRaisesRegex(ValueError, "quality 4 for security"):
            ExecutionComposer(catalog).compose(plan)

    def test_composes_plan_with_capability_and_price_aware_model_selection(self) -> None:
        catalog = ModelCatalog(
            [
                ModelOption(
                    profile_id="planner",
                    label="Planning model",
                    runtime="claude_code",
                    model="planner-model",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["planning", "architecture"],
                    quality=4,
                    input_cost_per_million=3.0,
                    output_cost_per_million=15.0,
                ),
                ModelOption(
                    profile_id="frontend-efficient",
                    label="Frontend specialist",
                    runtime="pi_rpc",
                    provider="example",
                    model="frontend-model",
                    access=AgentAccess.WORKSPACE_WRITE,
                    capabilities=["implementation", "frontend"],
                    quality=4,
                    input_cost_per_million=0.5,
                    output_cost_per_million=2.0,
                ),
                ModelOption(
                    profile_id="security-premium",
                    label="Security specialist",
                    runtime="pi_rpc",
                    provider="example",
                    model="security-model",
                    access=AgentAccess.WORKSPACE_WRITE,
                    capabilities=["implementation", "security", "backend"],
                    quality=5,
                    input_cost_per_million=8.0,
                    output_cost_per_million=24.0,
                ),
                ModelOption(
                    profile_id="reviewer",
                    label="Review model",
                    runtime="claude_code",
                    model="review-model",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["review", "security"],
                    quality=5,
                    input_cost_per_million=5.0,
                    output_cost_per_million=25.0,
                ),
            ]
        )

        graph = ExecutionComposer(catalog).compose(plan_with_specialized_steps())

        self.assertEqual(
            [node.kind.value for node in graph.nodes],
            ["implementation", "implementation"],
        )
        self.assertEqual(graph.planning_model.profile_id, "planner")
        self.assertEqual(graph.review_model.profile_id, "reviewer")
        self.assertEqual(graph.node("step-1").capability, "frontend")
        self.assertEqual(graph.node("step-2").capability, "security")
        self.assertEqual(graph.node("step-1").model.profile_id, "frontend-efficient")
        self.assertEqual(graph.node("step-2").model.profile_id, "security-premium")
        self.assertGreater(graph.node("step-1").model.estimated_cost_usd, 0)
        self.assertIn("frontend", graph.node("step-1").model.selection_reason)
        self.assertIn("security", graph.node("step-2").model.selection_reason)

    def test_catalog_round_trips_operator_prices_and_capabilities(self) -> None:
        original = ModelCatalog(
            [
                ModelOption(
                    profile_id="cheap",
                    label="Cheap",
                    runtime="pi_rpc",
                    provider="local",
                    model="small",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["planning", "review"],
                    quality=3,
                    input_cost_per_million=0.2,
                    output_cost_per_million=0.8,
                    context_window=128000,
                )
            ]
        )

        restored = ModelCatalog.from_dict(original.to_dict()).get("cheap")

        self.assertEqual(restored.capabilities, ["planning", "review"])
        self.assertEqual(restored.input_cost_per_million, 0.2)
        self.assertEqual(restored.context_window, 128000)


class ComposedWorkflowBehaviorTest(unittest.TestCase):
    def test_graph_records_the_model_that_actually_generated_the_plan(self) -> None:
        catalog = ModelCatalog(
            [
                ModelOption(
                    profile_id="actual-planner",
                    label="Actual planner",
                    runtime="claude_code",
                    model="actual",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["planning"],
                    quality=4,
                    input_cost_per_million=10,
                    output_cost_per_million=10,
                ),
                ModelOption(
                    profile_id="cheap-planner",
                    label="Cheap planner",
                    runtime="claude_code",
                    model="cheap",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["planning"],
                    quality=4,
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                ),
                ModelOption(
                    profile_id="executor",
                    label="Executor",
                    runtime="pi_rpc",
                    model="write",
                    access=AgentAccess.WORKSPACE_WRITE,
                    capabilities=["implementation"],
                    quality=4,
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                ),
                ModelOption(
                    profile_id="reviewer",
                    label="Reviewer",
                    runtime="claude_code",
                    model="review",
                    access=AgentAccess.READ_ONLY,
                    capabilities=["review"],
                    quality=4,
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                ),
            ]
        )

        class ActualPlannerComposer(ExecutionComposer):
            def select_binding(self, capability, access, text):
                if capability == "planning":
                    option = self.catalog.get("actual-planner")
                    return self._binding(option, capability, text)
                return super().select_binding(capability, access, text)

        runtime = ScriptedFakeRuntime(
            {"planner": [FakeAgentStep(output=execution_plan(), session_id="planning-session")]}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workflow = AgentWorkflow(
                root,
                runtime,
                validator=PassingValidator(),
                composer=ActualPlannerComposer(catalog),
            )
            project = workflow.register_project("project", repository, "main")
            task = workflow.create_task("task", "write result", project.project_id)

            analyzed = workflow.analyze(task.task_id)

        self.assertEqual(runtime.requests[0].model_profile_id, "actual-planner")
        self.assertEqual(analyzed.plan_graph["planning_model"]["profile_id"], "actual-planner")

    def test_plan_graph_cannot_overwrite_an_active_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow = AgentWorkflow(Path(tmp), ScriptedFakeRuntime({}))
            task = AgentTask(
                title="active",
                requirement="analyzing",
                status=AgentTaskStatus.ANALYZING,
            )
            workflow.store.save(task)
            graph = PlanGraph(
                requirement_summary="active",
                nodes=[
                    PlanNode(
                        node_id="research",
                        title="Research",
                        kind=PlanNodeKind.CUSTOM,
                        capability="research",
                        access=PlanNodeAccess.READ_ONLY,
                    )
                ],
            )

            with self.assertRaisesRegex(ValueError, "waiting for plan approval"):
                workflow.save_plan_graph(task.task_id, graph)

    def test_rerun_clears_each_execution_node_session(self) -> None:
        graph = PlanGraph(
            requirement_summary="run nodes",
            nodes=[
                PlanNode(
                    node_id="research",
                    title="Research",
                    kind=PlanNodeKind.CUSTOM,
                    capability="research",
                    access=PlanNodeAccess.READ_ONLY,
                ),
                PlanNode(
                    node_id="write",
                    title="Write",
                    kind=PlanNodeKind.IMPLEMENTATION,
                    depends_on=["research"],
                    access=PlanNodeAccess.WORKSPACE_WRITE,
                ),
            ],
        )
        task = AgentTask(
            title="task",
            requirement="run nodes",
            plan_graph=graph.to_dict(),
            sessions={
                "node:research": "research-session",
                "node:write": "write-session",
                "executor": "legacy-session",
            },
            node_runs={
                "research": {"session_id": "research-session"},
                "write": {"session_id": "write-session"},
            },
        )

        AgentWorkflow._clear_phase_sessions(task, AgentTaskStatus.EXECUTING)

        self.assertEqual(task.sessions, {})
        self.assertEqual(task.node_runs["research"]["session_id"], "")
        self.assertEqual(task.node_runs["write"]["session_id"], "")

    def test_codex_included_cache_tokens_are_not_counted_twice(self) -> None:
        self.assertEqual(
            usage_tokens(
                {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10}
            ),
            (100, 10, 40, 60),
        )

    def test_new_task_uses_node_sessions_and_bounded_reviewer_evidence(self) -> None:
        sentinel = "FULL-DIFF-CONTENT-MUST-NOT-BE-COPIED-" * 50
        runtime = ScriptedFakeRuntime(
            {
                "planner": [
                    FakeAgentStep(
                        output=execution_plan(),
                        session_id="planning-session",
                        usage={"input_tokens": 100, "output_tokens": 40},
                    )
                ],
                "executor": [
                    FakeAgentStep(
                        output={
                            "completed_steps": ["write result"],
                            "modified_files": ["result.txt"],
                            "tests": [],
                            "deviations": [],
                            "remaining_risks": [],
                            "next_steps": [],
                        },
                        writes={"result.txt": sentinel},
                        session_id="implementation-session",
                        usage={"prompt_tokens": 200, "completion_tokens": 80},
                    )
                ],
                "reviewer": [
                    FakeAgentStep(
                        output=passing_review("pass"),
                        session_id="review-session",
                        usage={
                            "input_tokens": 120,
                            "output_tokens": 30,
                            "cache_read_input_tokens": 20,
                        },
                    )
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workflow = AgentWorkflow(root, runtime, validator=PassingValidator())
            project = workflow.register_project("project", repository, "main")
            task = workflow.create_task("task", "write result", project.project_id)

            workflow.analyze(task.task_id)
            completed = workflow.approve_plan(task.task_id)

        executor_request = next(item for item in runtime.requests if item.role == "executor")
        reviewer_request = next(item for item in runtime.requests if item.role == "reviewer")
        self.assertTrue(completed.graph_execution)
        self.assertEqual(completed.plan_graph["status"], "approved")
        self.assertTrue(completed.plan_graph["approved_at"])
        self.assertEqual(executor_request.session_key, "node:step-1")
        self.assertEqual(reviewer_request.session_key, "node:review")
        self.assertEqual(completed.sessions["node:step-1"], "implementation-session")
        self.assertNotIn(sentinel, reviewer_request.instructions)
        self.assertIn("result.txt", reviewer_request.instructions)
        self.assertEqual(completed.budget.consumed_input_tokens, 440)
        self.assertEqual(completed.budget.consumed_output_tokens, 150)
        self.assertEqual(completed.budget.consumed_cached_input_tokens, 20)

    def test_token_budget_stops_before_another_model_node_runs(self) -> None:
        runtime = ScriptedFakeRuntime(
            {
                "planner": [
                    FakeAgentStep(
                        output=execution_plan(),
                        session_id="planning-session",
                        usage={"input_tokens": 90, "output_tokens": 20},
                    )
                ]
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workflow = AgentWorkflow(root, runtime, validator=PassingValidator())
            project = workflow.register_project("project", repository, "main")
            task = workflow.create_task(
                "task",
                "write result",
                project.project_id,
                budget=TaskBudget(max_total_tokens=100),
            )

            paused = workflow.analyze(task.task_id)

        self.assertEqual(paused.status, AgentTaskStatus.PAUSED)
        self.assertEqual(paused.pause_reason, "token_budget_exhausted")
        self.assertEqual([item.role for item in runtime.requests], ["planner"])

    def test_planner_receives_only_bounded_relevant_approved_experience(self) -> None:
        runtime = ScriptedFakeRuntime(
            {"planner": [FakeAgentStep(output=execution_plan(), session_id="planner")]}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ExperienceStore(root / "memory")
            memory.add_manual("Authentication token rotation must use compare-and-swap")
            memory.add_manual("CSS card spacing should use eight pixels")
            repository = create_repository(root)
            workflow = AgentWorkflow(
                root,
                runtime,
                validator=PassingValidator(),
                experience_store=memory,
            )
            project = workflow.register_project("project", repository, "main")
            task = workflow.create_task(
                "authentication hardening",
                "Make authentication token rotation concurrency safe",
                project.project_id,
            )

            workflow.analyze(task.task_id)

        instructions = runtime.requests[0].instructions
        self.assertIn("compare-and-swap", instructions)
        self.assertNotIn("CSS card spacing", instructions)


class ContextLedgerBoundsTest(unittest.TestCase):
    def test_merge_is_deduplicated_and_strictly_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ContextLedger(Path(tmp))
            packs = [
                ContextPack(
                    task_id="TASK-1",
                    node_id=f"node-{index}",
                    summary="summary " + ("x" * 4000),
                    facts=["shared fact", *[f"fact-{index}-{n}-" + "x" * 900 for n in range(20)]],
                    constraints=["shared constraint", *["c" * 900 for _ in range(20)]],
                    artifacts=[f"artifacts/node-{index}/{n}.json" for n in range(30)],
                )
                for index in range(8)
            ]

            merged = ledger.merge("TASK-1", "fan-in", packs, "final " + "z" * 5000)
            serialized_chars = len(merged.summary) + sum(
                len(item)
                for field in (
                    merged.facts,
                    merged.decisions,
                    merged.constraints,
                    merged.inputs,
                    merged.artifacts,
                    merged.open_questions,
                    merged.source_sessions,
                )
                for item in field
            )

            self.assertLessEqual(serialized_chars, MAX_CONTEXT_CHARS)
            persisted = (
                Path(tmp)
                / "TASK-1"
                / f"artifacts/context/{merged.node_id}/{merged.version}.json"
            ).read_text("utf-8")
            self.assertLessEqual(len(persisted), MAX_CONTEXT_CHARS)
            self.assertEqual(merged.facts.count("shared fact"), 1)
            self.assertEqual(merged.constraints.count("shared constraint"), 1)


if __name__ == "__main__":
    unittest.main()
