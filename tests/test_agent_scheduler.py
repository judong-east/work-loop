from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.agents.contracts import (
    AgentResult,
    AgentTaskStatus,
    ExecutionPlan,
    TaskBudget,
    ValidationResult,
)
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.runtime import AgentRuntime
from app.agents.scheduler import PersistentAgentScheduler, QueueEntryStatus
from app.agents.workflow import AgentWorkflow
from app.agents.workflow_config import workflow_from_dict
from tests.git_support import create_repository


def plan() -> dict:
    return {
        "requirement_understanding": "Create result.txt",
        "non_goals": [],
        "files_and_symbols": ["result.txt"],
        "steps": ["Write result.txt"],
        "constraints": ["Stay in the worktree"],
        "acceptance_criteria": ["result.txt contains done"],
        "required_tests": ["fake-check"],
        "risks": [],
        "open_questions": [],
    }


def execution_output() -> dict:
    return {
        "completed_steps": ["Write result.txt"],
        "modified_files": ["result.txt"],
        "tests": [],
        "deviations": [],
        "remaining_risks": [],
        "next_steps": [],
    }


def passing_review() -> dict:
    return {
        "verdict": "pass",
        "acceptance": [{"criterion": "result.txt contains done", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": "Passed",
    }


def revision_review() -> dict:
    return {
        "verdict": "revise_code",
        "acceptance": [{"criterion": "result.txt contains done", "passed": False}],
        "issues": [
            {
                "file": "result.txt",
                "line": 1,
                "severity": "warning",
                "message": "Not done",
                "suggestion": "Write done",
                "evidence": "The first line is first",
            }
        ],
        "recommended_tests": [],
        "summary": "Revise",
    }


def replan_review() -> dict:
    return {
        "verdict": "replan",
        "acceptance": [{"criterion": "result.txt contains done", "passed": False}],
        "issues": [],
        "recommended_tests": [],
        "summary": "Plan needs revision",
    }


class PassingValidator:
    def validate(self, task_id, workspace, plan: ExecutionPlan, policy):
        return ValidationResult(
            passed=True,
            checks=[
                {
                    "command": "fake-check",
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                }
            ],
        )


class SimulatedCrash(BaseException):
    pass


class CrashRuntime(AgentRuntime):
    def invoke(self, request):
        raise SimulatedCrash("service stopped")


class CrashOnRoleRuntime(AgentRuntime):
    def __init__(self, delegate: AgentRuntime, role: str):
        self.delegate = delegate
        self.role = role

    def invoke(self, request):
        if request.role == self.role:
            raise SimulatedCrash(f"crash during {self.role}")
        return self.delegate.invoke(request)

    def describe(self, request):
        return self.delegate.describe(request)


class CrashValidator:
    def validate(self, task_id, workspace, plan, policy):
        raise SimulatedCrash("crash during validation")


class BlockingPlannerRuntime(AgentRuntime):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(self, request):
        self.started.set()
        self.release.wait(timeout=10)
        return AgentResult(
            succeeded=True,
            output=plan(),
            session_id="blocking-planner-session",
            runtime="blocking",
            runtime_version="1",
            model="fake",
        )


class CostlyPlannerRuntime(AgentRuntime):
    def invoke(self, request):
        return AgentResult(
            succeeded=True,
            output=plan(),
            session_id="costly-planner-session",
            usage={"total_cost_usd": 2.0},
            runtime="costly",
            runtime_version="1",
            model="fake",
        )


class IdleTimeoutPlannerRuntime(AgentRuntime):
    def invoke(self, request):
        return AgentResult(
            succeeded=False,
            error="no events",
            error_type="idle_timeout",
            runtime="idle",
            runtime_version="1",
            model="fake",
        )


class SlowPlannerRuntime(AgentRuntime):
    def invoke(self, request):
        time.sleep(0.05)
        return AgentResult(
            succeeded=True,
            output=plan(),
            session_id="slow-planner-session",
            runtime="slow",
            runtime_version="1",
            model="fake",
        )


def create_workflow(root: Path, runtime: AgentRuntime) -> tuple[AgentWorkflow, str]:
    repository = create_repository(root)
    workflow = AgentWorkflow(root / "workloop-data", runtime, PassingValidator())
    project = workflow.register_project("Scheduler", repository, "main")
    return workflow, project.project_id


class PersistentAgentSchedulerTest(unittest.TestCase):
    def test_autopilot_replan_queues_the_revised_plan_without_a_hidden_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan()), FakeAgentStep(output=plan())],
                    "executor": [
                        FakeAgentStep(output=execution_output(), writes={"result.txt": "first\n"}),
                        FakeAgentStep(output=execution_output(), writes={"result.txt": "done\n"}),
                    ],
                    "reviewer": [
                        FakeAgentStep(output=replan_review()),
                        FakeAgentStep(output=passing_review()),
                    ],
                }
            )
            workflow, project_id = create_workflow(root, runtime)
            task = workflow.create_task(
                "Autopilot replan",
                "Create result.txt",
                project_id,
                workflow_id="autopilot",
            )
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)

            self.assertEqual(
                scheduler.run_next().status,
                AgentTaskStatus.QUEUED_FOR_EXECUTION,
            )
            self.assertEqual(
                scheduler.run_next().status,
                AgentTaskStatus.QUEUED_FOR_EXECUTION,
            )
            self.assertEqual(
                scheduler.run_next().status,
                AgentTaskStatus.READY_TO_DELIVER,
            )

    def test_autopilot_workflow_queues_execution_without_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan())],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            workflow, project_id = create_workflow(root, runtime)
            workflow.workflows.save(
                workflow_from_dict(
                    {
                        "workflow_id": "personal-auto",
                        "label": "Personal auto",
                        "nodes": [
                            {
                                "node_id": "plan",
                                "kind": "planner",
                                "label": "Plan",
                                "instructions": "Inspect public APIs first.",
                            },
                            {"node_id": "execute", "kind": "executor", "label": "Execute"},
                            {"node_id": "validate", "kind": "validation", "label": "Validate"},
                            {"node_id": "review", "kind": "reviewer", "label": "Review"},
                            {"node_id": "deliver", "kind": "delivery", "label": "Deliver"},
                        ],
                    }
                )
            )
            task = workflow.create_task(
                "Autopilot",
                "Create result.txt",
                project_id,
                workflow_id="personal-auto",
            )
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)

            planned = scheduler.run_next()

            self.assertEqual(planned.status, AgentTaskStatus.QUEUED_FOR_EXECUTION)
            self.assertEqual(len(scheduler.pending()), 1)

            completed = scheduler.run_next()

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual([request.role for request in runtime.requests], ["planner", "executor", "reviewer"])
            self.assertIn("Inspect public APIs first.", runtime.requests[0].instructions)

    def test_queue_order_persists_and_human_waiting_releases_the_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=plan(), session_id="planner-one"),
                        FakeAgentStep(output=plan(), session_id="planner-two"),
                    ],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            workflow, project_id = create_workflow(root, runtime)
            first = workflow.create_task("First", "First task", project_id)
            second = workflow.create_task("Second", "Second task", project_id)
            scheduler = PersistentAgentScheduler(workflow)

            first_entry = scheduler.enqueue_analysis(first.task_id)
            second_entry = scheduler.enqueue_analysis(second.task_id)

            self.assertLess(first_entry.sequence, second_entry.sequence)
            self.assertEqual(workflow.get_task(first.task_id).queue_position, 1)
            self.assertEqual(workflow.get_task(second.task_id).queue_position, 2)
            reloaded = PersistentAgentScheduler(
                AgentWorkflow(workflow.root, runtime, PassingValidator())
            )
            self.assertEqual(
                [entry.task_id for entry in reloaded.pending()],
                [first.task_id, second.task_id],
            )

            first_result = reloaded.run_next()

            self.assertEqual(
                first_result.status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )
            self.assertEqual(workflow.get_task(first.task_id).queue_position, 0)
            self.assertEqual(workflow.get_task(second.task_id).queue_position, 1)

            second_result = reloaded.run_next()

            self.assertEqual(
                second_result.status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )
            self.assertEqual(reloaded.pending(), [])
            reloaded.enqueue_execution(first.task_id)
            delivered = reloaded.run_next()
            self.assertEqual(delivered.status, AgentTaskStatus.READY_TO_DELIVER)

    def test_only_one_stage_runs_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = BlockingPlannerRuntime()
            workflow, project_id = create_workflow(root, runtime)
            first = workflow.create_task("First", "First task", project_id)
            second = workflow.create_task("Second", "Second task", project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(first.task_id)
            scheduler.enqueue_analysis(second.task_id)
            result: dict[str, object] = {}
            thread = threading.Thread(
                target=lambda: result.setdefault("task", scheduler.run_next())
            )
            thread.start()
            self.assertTrue(runtime.started.wait(timeout=5))

            concurrent = scheduler.run_next()

            self.assertIsNone(concurrent)
            self.assertEqual(len(scheduler.running()), 1)
            runtime.release.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(
                result["task"].status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )

    def test_unresolved_plan_never_enters_the_execution_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unresolved = plan()
            unresolved["open_questions"] = ["Choose the file format"]
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=unresolved)]}
            )
            workflow, project_id = create_workflow(root, runtime)
            task = workflow.create_task("Question", "Needs a decision", project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            scheduler.run_next()

            with self.assertRaisesRegex(ValueError, "未决问题"):
                scheduler.enqueue_execution(task.task_id)

            self.assertEqual(scheduler.pending(), [])
            self.assertEqual(
                workflow.get_task(task.task_id).status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )

    def test_startup_reconciles_partial_queue_commits_and_completed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=plan(), session_id="planner-one"),
                        FakeAgentStep(output=plan(), session_id="planner-two"),
                    ]
                }
            )
            workflow, project_id = create_workflow(root, runtime)
            completed_task = workflow.create_task("Completed", "Already done", project_id)
            orphan_task = workflow.create_task("Orphan", "Missing queue entry", project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(completed_task.task_id)
            scheduler.enqueue_analysis(orphan_task.task_id)
            state = scheduler.store.load()
            completed_entry = next(
                entry for entry in state.entries if entry.task_id == completed_task.task_id
            )
            completed_entry.status = QueueEntryStatus.RUNNING
            state.entries = [
                entry for entry in state.entries if entry.task_id != orphan_task.task_id
            ]
            scheduler.store.save(state)

            analyzed = workflow.analyze(completed_task.task_id)
            self.assertEqual(
                analyzed.status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )

            recovered = PersistentAgentScheduler(workflow)

            self.assertEqual(
                workflow.get_task(completed_task.task_id).status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )
            completed_history = [
                entry
                for entry in recovered.store.load().entries
                if entry.task_id == completed_task.task_id
            ]
            self.assertEqual(completed_history[-1].status, QueueEntryStatus.COMPLETED)
            self.assertEqual(
                [entry.task_id for entry in recovered.pending()],
                [orphan_task.task_id],
            )
            self.assertEqual(workflow.get_task(orphan_task.task_id).queue_position, 1)

    def test_startup_marks_running_queue_and_agent_run_interrupted_then_resumes_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crashing_workflow, project_id = create_workflow(root, CrashRuntime())
            task = crashing_workflow.create_task("Crash", "Resume me", project_id)
            task.sessions["planner"] = "planner-before-crash"
            crashing_workflow.store.save(task)
            crashing_scheduler = PersistentAgentScheduler(crashing_workflow)
            crashing_scheduler.enqueue_analysis(task.task_id)

            with self.assertRaises(SimulatedCrash):
                crashing_scheduler.run_next()

            scripted = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=plan(), session_id="planner-before-crash")
                    ]
                }
            )
            recovered_workflow = AgentWorkflow(
                crashing_workflow.root,
                scripted,
                PassingValidator(),
            )
            recovered = PersistentAgentScheduler(recovered_workflow)
            interrupted = recovered_workflow.get_task(task.task_id)

            self.assertEqual(interrupted.status, AgentTaskStatus.INTERRUPTED)
            self.assertEqual(interrupted.interrupted_status, "analyzing")
            interrupted_entries = recovered.interrupted()
            self.assertEqual(len(interrupted_entries), 1)
            self.assertEqual(interrupted_entries[0].status, QueueEntryStatus.INTERRUPTED)
            run_path = (
                recovered_workflow.store.task_dir(task.task_id)
                / "artifacts/runs/1-planner.json"
            )
            self.assertEqual(json.loads(run_path.read_text("utf-8"))["status"], "interrupted")

            recovered.resume(task.task_id)
            self.assertEqual(recovered.interrupted(), [])
            result = recovered.run_next()

            self.assertEqual(result.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(scripted.requests[0].session_id, "planner-before-crash")
            self.assertEqual(result.queue_position, 0)

    def test_rerun_current_stage_clears_only_the_interrupted_role_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crashing_workflow, project_id = create_workflow(root, CrashRuntime())
            task = crashing_workflow.create_task("Crash", "Rerun me", project_id)
            task.sessions.update(
                {
                    "planner": "old-planner-session",
                    "reviewer": "keep-reviewer-session",
                }
            )
            crashing_workflow.store.save(task)
            scheduler = PersistentAgentScheduler(crashing_workflow)
            scheduler.enqueue_analysis(task.task_id)
            with self.assertRaises(SimulatedCrash):
                scheduler.run_next()

            scripted = ScriptedFakeRuntime({"planner": [FakeAgentStep(output=plan())]})
            recovered_workflow = AgentWorkflow(
                crashing_workflow.root,
                scripted,
                PassingValidator(),
            )
            recovered = PersistentAgentScheduler(recovered_workflow)

            recovered.rerun(task.task_id)
            result = recovered.run_next()

            self.assertEqual(result.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(scripted.requests[0].session_id, "")
            self.assertEqual(result.sessions["reviewer"], "keep-reviewer-session")

    def test_task_cost_and_max_round_budgets_pause_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, project_id = create_workflow(root, CostlyPlannerRuntime())
            task = workflow.create_task(
                "Cost",
                "Stop on cost",
                project_id,
                budget=TaskBudget(max_cost_usd=1.0),
            )
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)

            paused = scheduler.run_next()

            self.assertEqual(paused.status, AgentTaskStatus.PAUSED)
            self.assertEqual(paused.pause_reason, "budget_exhausted")
            self.assertGreaterEqual(paused.budget.consumed_cost_usd, 2.0)
            self.assertEqual(PersistentAgentScheduler(workflow).pending(), [])
            cost_run = json.loads(
                (
                    workflow.store.task_dir(task.task_id)
                    / "artifacts/runs/1-planner.json"
                ).read_text("utf-8")
            )
            self.assertEqual(cost_run["task_budget"]["max_cost_usd"], 1.0)
            self.assertGreaterEqual(
                cost_run["task_budget"]["consumed_cost_usd"],
                2.0,
            )

            revision_runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan())],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "first\n"},
                        ),
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "done\n"},
                        ),
                    ],
                    "reviewer": [
                        FakeAgentStep(output=revision_review()),
                        FakeAgentStep(output=passing_review()),
                    ],
                }
            )
            second_workflow, second_project = create_workflow(
                root / "round-budget",
                revision_runtime,
            )
            second = second_workflow.create_task(
                "Rounds",
                "Stop after one round",
                second_project,
                budget=TaskBudget(max_iterations=1),
            )
            second_scheduler = PersistentAgentScheduler(second_workflow)
            second_scheduler.enqueue_analysis(second.task_id)
            second_scheduler.run_next()
            second_scheduler.enqueue_execution(second.task_id)

            rounds_paused = second_scheduler.run_next()

            self.assertEqual(rounds_paused.status, AgentTaskStatus.PAUSED)
            self.assertEqual(rounds_paused.pause_reason, "max_iterations")
            self.assertEqual(rounds_paused.plan_iteration, 1)

            second_scheduler.update_budget(
                second.task_id,
                TaskBudget(max_iterations=2),
            )
            second_scheduler.resume(second.task_id)
            self.assertEqual(second_scheduler.paused(), [])
            resumed = second_scheduler.run_next()

            self.assertEqual(resumed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(resumed.plan_iteration, 2)
            self.assertIn(
                "Not done",
                [
                    request.instructions
                    for request in revision_runtime.requests
                    if request.role == "executor"
                ][1],
            )

    def test_idle_and_total_time_budgets_pause_at_the_task_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idle_workflow, project_id = create_workflow(root, IdleTimeoutPlannerRuntime())
            idle_task = idle_workflow.create_task("Idle", "Stop on idle", project_id)
            idle_scheduler = PersistentAgentScheduler(idle_workflow)
            idle_scheduler.enqueue_analysis(idle_task.task_id)

            idle_paused = idle_scheduler.run_next()

            self.assertEqual(idle_paused.status, AgentTaskStatus.PAUSED)
            self.assertEqual(idle_paused.pause_reason, "idle_timeout")

            total_workflow, total_project = create_workflow(
                root / "total",
                SlowPlannerRuntime(),
            )
            total_task = total_workflow.create_task(
                "Total",
                "Stop on total time",
                total_project,
                budget=TaskBudget(
                    total_timeout_seconds=0.01,
                    call_timeout_seconds=1,
                    idle_timeout_seconds=1,
                ),
            )
            total_scheduler = PersistentAgentScheduler(total_workflow)
            total_scheduler.enqueue_analysis(total_task.task_id)

            total_paused = total_scheduler.run_next()

            self.assertEqual(total_paused.status, AgentTaskStatus.PAUSED)
            self.assertEqual(total_paused.pause_reason, "total_timeout")
            self.assertGreater(total_paused.budget.consumed_active_seconds, 0.01)

    def test_terminate_interrupted_task_removes_queue_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, project_id = create_workflow(root, CrashRuntime())
            task = workflow.create_task("Terminate", "Stop me", project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            with self.assertRaises(SimulatedCrash):
                scheduler.run_next()
            recovered = PersistentAgentScheduler(workflow)
            workspace = Path(task.workspace)

            cancelled = recovered.terminate(task.task_id)

            self.assertEqual(cancelled.status, AgentTaskStatus.CANCELLED)
            self.assertFalse(workspace.exists())
            self.assertEqual(recovered.pending(), [])
            self.assertEqual(recovered.interrupted(), [])

    def test_resume_interrupted_execution_reuses_current_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=plan(), session_id="planner-session")]}
            )
            crashing = CrashOnRoleRuntime(first_runtime, "executor")
            workflow, project_id = create_workflow(root, crashing)
            task = workflow.create_task("Execution crash", "Resume execution", project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            scheduler.run_next()
            scheduler.enqueue_execution(task.task_id)

            with self.assertRaises(SimulatedCrash):
                scheduler.run_next()

            resumed_runtime = ScriptedFakeRuntime(
                {
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "done\n"},
                            session_id="executor-session",
                        )
                    ],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            recovered_workflow = AgentWorkflow(
                workflow.root,
                resumed_runtime,
                PassingValidator(),
            )
            recovered = PersistentAgentScheduler(recovered_workflow)
            self.assertEqual(
                recovered_workflow.get_task(task.task_id).interrupted_status,
                "executing",
            )

            recovered.resume(task.task_id)
            completed = recovered.run_next()

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(completed.iteration, 1)
            self.assertEqual(completed.plan_iteration, 1)
            self.assertEqual(
                [request.role for request in resumed_runtime.requests],
                ["executor", "reviewer"],
            )

    def test_resume_validation_does_not_rerun_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan())],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                }
            )
            repository = create_repository(root)
            workflow = AgentWorkflow(
                root / "workloop-data",
                runtime,
                CrashValidator(),
            )
            project = workflow.register_project("Validation crash", repository, "main")
            task = workflow.create_task("Validation crash", "Resume validation", project.project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            scheduler.run_next()
            scheduler.enqueue_execution(task.task_id)

            with self.assertRaises(SimulatedCrash):
                scheduler.run_next()

            resumed_runtime = ScriptedFakeRuntime(
                {"reviewer": [FakeAgentStep(output=passing_review())]}
            )
            recovered_workflow = AgentWorkflow(
                workflow.root,
                resumed_runtime,
                PassingValidator(),
            )
            recovered = PersistentAgentScheduler(recovered_workflow)
            self.assertEqual(
                recovered_workflow.get_task(task.task_id).interrupted_status,
                "validating",
            )
            validation_run = json.loads(
                (
                    recovered_workflow.store.task_dir(task.task_id)
                    / "artifacts/rounds/1/validation-run.json"
                ).read_text("utf-8")
            )
            self.assertEqual(validation_run["status"], "interrupted")

            recovered.resume(task.task_id)
            completed = recovered.run_next()

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(
                [request.role for request in resumed_runtime.requests],
                ["reviewer"],
            )

    def test_resume_review_does_not_rerun_execution_or_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            delegate = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan())],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                }
            )
            crashing = CrashOnRoleRuntime(delegate, "reviewer")
            workflow, project_id = create_workflow(root, crashing)
            task = workflow.create_task("Review crash", "Resume review", project_id)
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            scheduler.run_next()
            scheduler.enqueue_execution(task.task_id)

            with self.assertRaises(SimulatedCrash):
                scheduler.run_next()

            resumed_runtime = ScriptedFakeRuntime(
                {"reviewer": [FakeAgentStep(output=passing_review())]}
            )

            class ValidatorMustNotRun:
                def validate(self, task_id, workspace, plan, policy):
                    raise AssertionError("validation must not rerun")

            recovered_workflow = AgentWorkflow(
                workflow.root,
                resumed_runtime,
                ValidatorMustNotRun(),
            )
            recovered = PersistentAgentScheduler(recovered_workflow)
            self.assertEqual(
                recovered_workflow.get_task(task.task_id).interrupted_status,
                "reviewing",
            )

            recovered.resume(task.task_id)
            completed = recovered.run_next()

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(
                [request.role for request in resumed_runtime.requests],
                ["reviewer"],
            )


if __name__ == "__main__":
    unittest.main()
