from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.agents.contracts import AgentResult, AgentTask, AgentTaskStatus
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.scheduler import PersistentAgentScheduler
from app.agents.store import AgentTaskStore
from app.agents.workflow import AgentWorkflow
from app.agents.runtime import AgentRuntime
from app.projects.contracts import ValidationCommand
from app.projects.policy import ProjectPolicyLoader
from app.validation.runner import CodexCommandSandbox, ProcessOutcome
from tests.git_support import create_repository
from tests.test_agent_workflow import (
    PassingValidator,
    execution_plan,
    passing_review,
    project_workflow,
)


def plan_with_questions() -> dict:
    plan = execution_plan()
    plan["open_questions"] = ["使用哪种换行符？", "文件编码是什么？"]
    return plan


class QuickPresetTest(unittest.TestCase):
    def test_new_tasks_default_to_the_quick_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入 result.txt"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [FakeAgentStep(output=passing_review("通过"))],
                }
            )
            workflow, project = project_workflow(root := Path(tmp), runtime, PassingValidator())
            task = workflow.create_task("Quick 默认", "Create result.txt", project.project_id)

            self.assertEqual(workflow.get_workflow(task.task_id).workflow_id, "quick")
            self.assertFalse(workflow.requires_plan_approval(task.task_id))

            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            analyzed = scheduler.run_next()
            executed = scheduler.run_next()

            # quick has no approval gate: analysis auto-enqueues execution and
            # the second slot run carries the task straight to delivery.
            self.assertEqual(analyzed.status, AgentTaskStatus.QUEUED_FOR_EXECUTION)
            self.assertEqual(executed.status, AgentTaskStatus.READY_TO_DELIVER)


class TaskEventLogTest(unittest.TestCase):
    def test_save_emits_created_and_change_events_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentTaskStore(Path(tmp))
            task = AgentTask(title="事件", requirement="记录轨迹")

            store.save(task)
            task.transition(AgentTaskStatus.QUEUED_FOR_ANALYSIS, reason="test")
            store.save(task)
            # A save with no observable change emits nothing.
            store.save(task)

            events = store.read_events(task.task_id)
            self.assertEqual(
                [event["type"] for event in events],
                ["task_created", "task_changed"],
            )
            self.assertEqual(events[1]["status"], "queued_for_analysis")
            self.assertEqual([event["seq"] for event in events], [1, 2])
            self.assertEqual(store.read_events(task.task_id, after=1)[0]["seq"], 2)
            self.assertEqual(store.read_events(task.task_id, after=2), [])

    def test_load_seeds_the_change_detector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentTaskStore(Path(tmp))
            task = AgentTask(title="种子", requirement="重载后不误报")
            store.save(task)

            reloaded = AgentTaskStore(Path(tmp))
            loaded = reloaded.load(task.task_id)
            reloaded.save(loaded)

            self.assertEqual(
                [event["type"] for event in reloaded.read_events(task.task_id)],
                ["task_created"],
            )

    def test_torn_tail_line_does_not_break_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentTaskStore(Path(tmp))
            task = AgentTask(title="撕裂", requirement="容忍半写行")
            store.save(task)
            log = store.task_dir(task.task_id) / "logs" / "events.jsonl"
            with log.open("a", encoding="utf-8") as stream:
                stream.write('{"seq": 2, "type": "task_cha')

            events = store.read_events(task.task_id)
            self.assertEqual(len(events), 1)
            store.append_event(task.task_id, {"type": "task_changed", "status": "paused"})
            refreshed = store.read_events(task.task_id)
            self.assertEqual(refreshed[-1]["seq"], 3)


class BatchClarificationTest(unittest.TestCase):
    def test_all_open_questions_are_answered_in_one_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=plan_with_questions()),
                        FakeAgentStep(output=execution_plan()),
                    ],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入 result.txt"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [FakeAgentStep(output=passing_review("通过"))],
                }
            )
            workflow, project = project_workflow(Path(tmp), runtime, PassingValidator())
            task = workflow.create_task(
                "批量澄清", "Create result.txt", project.project_id, workflow_id="guarded"
            )
            scheduler = PersistentAgentScheduler(workflow)
            scheduler.enqueue_analysis(task.task_id)
            scheduler.run_next()

            entry = scheduler.answer_clarifications(
                task.task_id,
                [{"question": "使用哪种换行符？"}, {"question": "文件编码是什么？"}],
                ["LF", "UTF-8"],
            )
            self.assertEqual(entry.status.value, "queued")

            replanned = scheduler.run_next()
            self.assertEqual(replanned.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(replanned.plan_version, 2)
            answers = {item["question"]: item["answer"] for item in replanned.clarifications}
            self.assertEqual(
                answers,
                {"使用哪种换行符？": "LF", "文件编码是什么？": "UTF-8"},
            )

            scheduler.enqueue_execution(task.task_id)
            self.assertEqual(scheduler.run_next().status, AgentTaskStatus.READY_TO_DELIVER)

    def test_unknown_question_and_empty_answer_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan_with_questions())],
                    "executor": [],
                    "reviewer": [],
                }
            )
            workflow, project = project_workflow(Path(tmp), runtime, PassingValidator())
            task = workflow.create_task(
                "非法澄清", "Create result.txt", project.project_id, workflow_id="guarded"
            )
            workflow.analyze(task.task_id)

            with self.assertRaisesRegex(ValueError, "不在当前计划"):
                workflow.record_clarifications(
                    task.task_id,
                    [{"question": "从哪来的问题？"}],
                    ["不知道"],
                )
            with self.assertRaisesRegex(ValueError, "不能为空"):
                workflow.record_clarifications(
                    task.task_id,
                    [{"question": "使用哪种换行符？"}],
                    ["   "],
                )


class NetworkAllowPolicyTest(unittest.TestCase):
    def _policy_file(self, root: Path, network: str, name: str = "repository") -> Path:
        repository = create_repository(root, name)
        policy = repository / ".workloop" / "project.toml"
        policy.write_text(
            policy.read_text(encoding="utf-8").replace('network = "deny"', f'network = "{network}"'),
            encoding="utf-8",
        )
        return repository

    def test_loader_accepts_explicit_allow_and_rejects_other_values(self) -> None:
        loader = ProjectPolicyLoader()
        with tempfile.TemporaryDirectory() as tmp:
            allowed = loader.load(
                self._policy_file(Path(tmp), "allow"), ".workloop/project.toml"
            )
            self.assertEqual(allowed.network, "allow")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "deny 或 allow"):
                loader.load(
                    self._policy_file(Path(tmp), "sometimes"), ".workloop/project.toml"
                )

    def test_agent_policy_maps_project_network_allow_to_the_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, _ = project_workflow(root, ScriptedFakeRuntime({}), PassingValidator())
            repository = self._policy_file(root, "allow", name="policy-repo")
            policy = ProjectPolicyLoader().load(repository, ".workloop/project.toml")

            agent_policy = workflow._agent_policy(policy, ["fake-check"])

            self.assertTrue(agent_policy.network_allowed)

    def test_sandbox_allows_network_without_disable_flag_or_network_canary(self) -> None:
        class RecordingProcesses:
            def __init__(self):
                self.calls: list[list[str]] = []

            def run(self, argv, workspace, timeout_seconds, environment):
                self.calls.append(list(argv))
                return ProcessOutcome(exit_code=0, stdout="ok")

        with tempfile.TemporaryDirectory() as tmp:
            processes = RecordingProcesses()
            sandbox = CodexCommandSandbox(processes=processes, executable=sys.executable)

            outcome = sandbox.run(
                ValidationCommand("unit", [sys.executable, "-c", "print('target')"]),
                Path(tmp),
                5,
                "allow",
            )

            self.assertEqual(outcome.exit_code, 0)
            joined = [" ".join(argv) for argv in processes.calls]
            target_calls = [call for call in joined if "print('target')" in call]
            self.assertEqual(len(target_calls), 1)
            self.assertNotIn("--sandbox-state-disable-network", target_calls[0])
            # No network canary runs under an allowing policy; the file-read
            # boundary probe still runs.
            self.assertFalse(any("create_connection" in call for call in joined))
            self.assertTrue(any("OUTSIDE_READ_OPEN" in call for call in joined))

    def test_canary_health_check_runs_once_per_window(self) -> None:
        class RecordingProcesses:
            def __init__(self):
                self.calls: list[list[str]] = []

            def run(self, argv, workspace, timeout_seconds, environment):
                self.calls.append(list(argv))
                return ProcessOutcome(exit_code=0, stdout="ok")

        with tempfile.TemporaryDirectory() as tmp:
            processes = RecordingProcesses()
            sandbox = CodexCommandSandbox(processes=processes, executable=sys.executable)

            for _ in range(2):
                outcome = sandbox.run(
                    ValidationCommand("unit", [sys.executable, "-c", "print('target')"]),
                    Path(tmp),
                    5,
                    "deny",
                )
                self.assertEqual(outcome.exit_code, 0)

            # First run: network canary + file canary + target. Second run: the
            # cached health check means only the target command executes.
            self.assertEqual(len(processes.calls), 4)


class BarrierPlannerRuntime(AgentRuntime):
    """Planner whose turns all block until `parties` of them are in flight.

    Executor and reviewer turns pass straight through, so the barrier only
    proves whether two ANALYZE dispatches overlap in time.
    """

    def __init__(self, plan_output: dict, parties: int, timeout: float = 10.0):
        self.plan_output = plan_output
        self.barrier = threading.Barrier(parties)
        self.timeout = timeout
        self.in_flight = 0
        self.max_in_flight = 0
        self.turns = 0
        self._guard = threading.Lock()

    def invoke(self, request) -> AgentResult:
        if request.role == "planner":
            with self._guard:
                self.in_flight += 1
                self.turns += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                # A serialized scheduler leaves this party alone until the
                # timeout; a concurrent one releases both immediately.
                self.barrier.wait(self.timeout)
            except threading.BrokenBarrierError:
                pass
            finally:
                with self._guard:
                    self.in_flight -= 1
        return AgentResult(
            succeeded=True,
            output=dict(self.plan_output),
            session_id=request.session_id or f"planner-{request.task_id}",
            runtime="fake",
            runtime_version="1",
            model="barrier",
        )


class ParallelSlotsTest(unittest.TestCase):
    def _two_project_tasks(self, root: Path, runtime: AgentRuntime):
        workflow = AgentWorkflow(root / "workloop-data", runtime=runtime, validator=PassingValidator())
        project_ids = []
        for name in ("repo-a", "repo-b"):
            repository = create_repository(root, name)
            project = workflow.register_project(name, repository, "main")
            project_ids.append(project.project_id)
        tasks = [
            workflow.create_task(
                f"Task {label}", "Create result.txt", project_id, workflow_id="guarded"
            )
            for label, project_id in zip(("A", "B"), project_ids)
        ]
        return workflow, tasks

    @staticmethod
    def _run_in_threads(scheduler: PersistentAgentScheduler, count: int) -> list:
        results: list = [None] * count
        def run(index: int) -> None:
            results[index] = scheduler.run_next()
        threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        return results

    def test_two_slots_run_two_projects_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = BarrierPlannerRuntime(execution_plan(), parties=2)
            workflow, tasks = self._two_project_tasks(root, runtime)
            scheduler = PersistentAgentScheduler(workflow, slots=2)
            for task in tasks:
                scheduler.enqueue_analysis(task.task_id)

            results = self._run_in_threads(scheduler, 2)

            self.assertEqual(
                [result.status for result in results],
                [AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL] * 2,
            )
            # Both planner turns were in flight at the same time; with a single
            # slot the second barrier party would never arrive.
            self.assertEqual(runtime.turns, 2)
            self.assertEqual(runtime.max_in_flight, 2)

    def test_single_slot_serializes_second_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = BarrierPlannerRuntime(execution_plan(), parties=2, timeout=0.5)
            workflow, tasks = self._two_project_tasks(root, runtime)
            scheduler = PersistentAgentScheduler(workflow)  # default slots = 1
            self.assertEqual(scheduler.slots, 1)
            for task in tasks:
                scheduler.enqueue_analysis(task.task_id)

            results = self._run_in_threads(scheduler, 2)

            # The second caller finds the only slot busy and gets None back;
            # the first task finishes its analysis alone.
            self.assertIn(None, results)
            finished = [result for result in results if result is not None]
            self.assertEqual(finished[0].status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(runtime.max_in_flight, 1)

            # The queued task runs as soon as the slot frees.
            deferred = scheduler.run_next()
            self.assertEqual(deferred.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(runtime.turns, 2)
            self.assertEqual(runtime.max_in_flight, 1)

    def test_slot_configuration_is_clamped(self) -> None:
        self.assertEqual(PersistentAgentScheduler._resolve_slots(0), 1)
        self.assertEqual(PersistentAgentScheduler._resolve_slots(-3), 1)
        self.assertEqual(PersistentAgentScheduler._resolve_slots(99), PersistentAgentScheduler.MAX_SLOTS)
        self.assertEqual(PersistentAgentScheduler._resolve_slots("not-a-number"), 1)
        self.assertEqual(PersistentAgentScheduler._resolve_slots(4), 4)


if __name__ == "__main__":
    unittest.main()
