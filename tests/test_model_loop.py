from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.cli import build_backends, build_parser
from app.core.artifact_store import ArtifactStore
from app.core.contracts import ModelProfile, ModelRoutingConfig, TaskStatus, task_state_from_dict
from app.core.workflow import WorkloopKernel
from app.models.backends.fake_backend import FakeBackend


def make_routing(planner="m-plan", executor="m-exec", reviewer="m-review") -> ModelRoutingConfig:
    profiles = {
        "p": ModelProfile(name="p", provider="fake", model=planner),
        "e": ModelProfile(name="e", provider="fake", model=executor),
        "r": ModelProfile(name="r", provider="fake", model=reviewer),
    }
    roles = {"planner": "p", "executor": "e", "reviewer": "r", "default": "e"}
    return ModelRoutingConfig(profiles=profiles, roles=roles)


def make_ready_task(kernel: WorkloopKernel):
    return kernel.create_task(
        title="循环",
        goal="验证多模型循环",
        raw_input="目标：验证多模型循环。验收标准：通过测试。",
    )


class TaskLoadTest(unittest.TestCase):
    def test_task_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            created = kernel.create_task(
                title="往返",
                goal="验证加载",
                raw_input="目标：验证任务加载。验收标准：通过测试。",
            )
            store = ArtifactStore(Path(tmp) / "tasks")
            loaded = store.load_task(created.task_id)
            self.assertEqual(loaded.task_id, created.task_id)
            self.assertEqual(loaded.status, TaskStatus.READY_FOR_PLAN)
            self.assertEqual(loaded.title, "往返")
            self.assertEqual(loaded.context_refs, created.context_refs)

    def test_task_state_from_dict_defaults(self) -> None:
        task = task_state_from_dict({"title": "t", "goal": "g", "task_id": "TASK-1"})
        self.assertEqual(task.status, TaskStatus.CREATED)
        self.assertEqual(task.iteration, 0)
        self.assertEqual(task.artifacts, {})


class RunModelLoopTest(unittest.TestCase):
    def test_happy_path_reaches_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend()
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            task_dir = Path(tmp) / "tasks" / task.task_id
            self.assertTrue((task_dir / "artifacts" / "plan.md").exists())
            self.assertTrue((task_dir / "artifacts" / "execution.md").exists())
            self.assertTrue((task_dir / "artifacts" / "review.json").exists())
            self.assertTrue((task_dir / "artifacts" / "model_calls" / "1-planner" / "prompt.txt").exists())
            self.assertTrue((task_dir / "artifacts" / "model_calls" / "3-reviewer" / "meta.json").exists())
            # 三个角色各被调用一次，顺序 planner -> executor -> reviewer
            self.assertEqual([r.role for r in backend.requests], ["planner", "executor", "reviewer"])
            # 新增工件引用是相对路径
            self.assertEqual(result.artifacts["plan"], "artifacts/plan.md")

    def test_same_executor_reviewer_model_is_policy_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend()
            routing = make_routing(executor="m-same", reviewer="m-same")
            result = kernel.run_model_loop(task.task_id, routing, {"fake": backend})

            self.assertEqual(result.status, TaskStatus.POLICY_BLOCKED)
            self.assertEqual(backend.requests, [])  # 任何模型都不得被调用

    def test_wrong_initial_status_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = kernel.create_task(title="空", goal="g", raw_input="")  # -> POLICY_BLOCKED
            with self.assertRaisesRegex(ValueError, "ready_for_plan"):
                kernel.run_model_loop(task.task_id, make_routing(), {"fake": FakeBackend()})


class RunModelLoopFailureTest(unittest.TestCase):
    def test_executor_failure_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(failures={"executor"})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.FAILED)
            call_dir = Path(tmp) / "tasks" / task.task_id / "artifacts" / "model_calls" / "2-executor"
            self.assertTrue((call_dir / "meta.json").exists())  # 失败调用同样落盘
            meta = json.loads((call_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertFalse(meta["succeeded"])

    def test_reviewer_revise_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": '{"verdict": "revise", "issues": ["不达标"]}'})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_reviewer_invalid_json_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": "看起来不错！"})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_reviewer_json_in_code_fence_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            fenced = '```json\n{"verdict": "pass", "issues": []}\n```'
            backend = FakeBackend(responses={"reviewer": fenced})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.DONE)

    def test_missing_backend_provider_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            result = kernel.run_model_loop(task.task_id, make_routing(), {})  # 无任何后端
            self.assertEqual(result.status, TaskStatus.FAILED)


class CliTest(unittest.TestCase):
    def test_run_loop_args_parse(self) -> None:
        args = build_parser().parse_args(
            ["run-loop", "--task-id", "TASK-1", "--root", "/tmp/x", "--models-config", "m.json"]
        )
        self.assertEqual(args.command, "run-loop")
        self.assertEqual(args.task_id, "TASK-1")
        self.assertEqual(args.models_config, "m.json")

    def test_build_backends_covers_both_providers(self) -> None:
        backends = build_backends()
        self.assertIn("cli", backends)
        self.assertIn("fake", backends)


if __name__ == "__main__":
    unittest.main()
