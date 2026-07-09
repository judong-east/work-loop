from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.cli import build_backends, build_parser
from app.core.artifact_store import ArtifactStore
from app.core.contracts import ModelProfile, ModelRoutingConfig, TaskStatus, task_state_from_dict
from app.core.workflow import WorkloopKernel, default_policy_boundary
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


def changes_json(path: str = "src/hello.py", content: str = "print('你好')\n") -> str:
    return json.dumps({"changes": [{"path": path, "action": "write", "content": content}]}, ensure_ascii=False)


def review_json(verdict: str, issues: list[dict] | None = None) -> str:
    return json.dumps({"verdict": verdict, "summary": "结论", "issues": issues or []}, ensure_ascii=False)


class InspectingFakeBackend(FakeBackend):
    def __init__(self, task_dir: Path, responses: dict[str, str | list[str]] | None = None):
        super().__init__(responses=responses)
        self.task_dir = task_dir
        self.seen_statuses: list[tuple[str, str]] = []

    def invoke(self, profile: ModelProfile, request):
        calls_dir = self.task_dir / "artifacts" / "model_calls"
        role_dirs = sorted(
            calls_dir.glob(f"*-{request.role}"),
            key=lambda path: int(path.name.split("-", 1)[0]),
        )
        meta = json.loads((role_dirs[-1] / "meta.json").read_text(encoding="utf-8"))
        self.seen_statuses.append((request.role, meta["status"]))
        return super().invoke(profile, request)


class RaisingBackend(FakeBackend):
    def invoke(self, profile: ModelProfile, request):
        raise RuntimeError("boom")


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
            backend = FakeBackend(responses={"executor": changes_json()})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(result.iteration, 1)
            task_dir = Path(tmp) / "tasks" / task.task_id
            # executor 的变更真实落入沙箱
            self.assertEqual(
                (task_dir / "workspace" / "src" / "hello.py").read_text(encoding="utf-8"),
                "print('你好')\n",
            )
            # 每轮工件归档齐全
            round_dir = task_dir / "artifacts" / "rounds" / "1"
            for name in ["changes.json", "policy_check.json", "changes.diff", "review.json"]:
                self.assertTrue((round_dir / name).exists(), name)
            self.assertIn("+print('你好')", (round_dir / "changes.diff").read_text(encoding="utf-8"))
            self.assertTrue((task_dir / "artifacts" / "plan.md").exists())
            self.assertTrue((task_dir / "artifacts" / "model_calls" / "1-planner" / "prompt.txt").exists())
            self.assertTrue((task_dir / "artifacts" / "model_calls" / "3-reviewer" / "meta.json").exists())
            # 三个角色各被调用一次，顺序 planner -> executor -> reviewer
            self.assertEqual([r.role for r in backend.requests], ["planner", "executor", "reviewer"])
            # 工件引用是相对路径
            self.assertEqual(result.artifacts["plan"], "artifacts/plan.md")
            self.assertEqual(result.artifacts["diff"], "artifacts/rounds/1/changes.diff")
            self.assertEqual(result.artifacts["code_review"], "artifacts/rounds/1/review.json")

    def test_model_call_meta_is_running_before_backend_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            task_dir = Path(tmp) / "tasks" / task.task_id
            backend = InspectingFakeBackend(task_dir, responses={"executor": changes_json()})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(
                backend.seen_statuses,
                [("planner", "running"), ("executor", "running"), ("reviewer", "running")],
            )
            meta = json.loads((task_dir / "artifacts" / "model_calls" / "2-executor" / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "succeeded")
            self.assertEqual(meta["role"], "executor")

    def test_revise_then_pass_iterates_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(
                responses={
                    "executor": [
                        changes_json(content="print('第一版')\n"),
                        changes_json(content="print('第二版')\n"),
                    ],
                    "reviewer": [
                        review_json("revise", [{"file": "src/hello.py", "line": 1, "severity": "warning",
                                                "message": "输出文案不达标", "suggestion": "改为第二版"}]),
                        review_json("pass"),
                    ],
                }
            )
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(result.iteration, 2)
            roles = [r.role for r in backend.requests]
            self.assertEqual(roles, ["planner", "executor", "reviewer", "executor", "reviewer"])
            # 第二轮 executor 收到上一轮审核意见
            second_executor_prompt = backend.requests[3].prompt
            self.assertIn("输出文案不达标", second_executor_prompt)
            self.assertIn("改为第二版", second_executor_prompt)
            # 沙箱是第二版内容，两轮工件都归档
            task_dir = Path(tmp) / "tasks" / task.task_id
            self.assertIn("第二版", (task_dir / "workspace" / "src" / "hello.py").read_text(encoding="utf-8"))
            self.assertTrue((task_dir / "artifacts" / "rounds" / "2" / "review.json").exists())

    def test_iterations_exhausted_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": review_json("revise", [{"message": "还不行"}])})
            policy = default_policy_boundary()
            policy.max_iterations = 2
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend}, policy=policy)

            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)
            self.assertEqual(result.iteration, 2)
            self.assertEqual([r.role for r in backend.requests].count("executor"), 2)

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

    def test_retry_after_failure_keeps_previous_model_call_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            first = kernel.run_model_loop(task.task_id, make_routing(), {"fake": RaisingBackend()})
            self.assertEqual(first.status, TaskStatus.FAILED)

            failed_task = kernel.store.load_task(task.task_id)
            failed_task.transition(TaskStatus.READY_FOR_PLAN)
            kernel.store.save_task(failed_task)

            second = kernel.run_model_loop(
                task.task_id,
                make_routing(),
                {"fake": FakeBackend(responses={"executor": changes_json()})},
            )
            self.assertEqual(second.status, TaskStatus.DONE)

            calls_dir = Path(tmp) / "tasks" / task.task_id / "artifacts" / "model_calls"
            self.assertTrue((calls_dir / "1-planner" / "meta.json").exists())
            self.assertTrue((calls_dir / "2-planner" / "meta.json").exists())
            self.assertTrue((calls_dir / "3-executor" / "meta.json").exists())
            self.assertTrue((calls_dir / "4-reviewer" / "meta.json").exists())
            first_meta = json.loads((calls_dir / "1-planner" / "meta.json").read_text(encoding="utf-8"))
            second_meta = json.loads((calls_dir / "2-planner" / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(first_meta["status"], "failed")
            self.assertEqual(second_meta["status"], "succeeded")
    def test_executor_unparseable_output_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"executor": "我直接改好了，不用 JSON。"})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_denied_change_path_is_policy_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"executor": changes_json(path=".env", content="TOKEN=1\n")})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.POLICY_BLOCKED)
            # 违规变更不得写入沙箱，reviewer 不得被调用
            self.assertFalse((Path(tmp) / "tasks" / task.task_id / "workspace" / ".env").exists())
            self.assertNotIn("reviewer", [r.role for r in backend.requests])

    def test_reviewer_block_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": review_json("block", [{"message": "方向错误"}])})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)
            self.assertEqual(result.iteration, 1)  # block 不返修，直接终止

    def test_reviewer_invalid_json_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": "看起来不错！"})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_fenced_json_outputs_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(
                responses={
                    "executor": f"```json\n{changes_json()}\n```",
                    "reviewer": f"```json\n{review_json('pass')}\n```",
                }
            )
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})
            self.assertEqual(result.status, TaskStatus.DONE)

    def test_missing_backend_provider_marks_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            result = kernel.run_model_loop(task.task_id, make_routing(), {})  # 无任何后端
            self.assertEqual(result.status, TaskStatus.FAILED)

    def test_backend_exception_marks_call_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": RaisingBackend()})
            self.assertEqual(result.status, TaskStatus.FAILED)
            meta = json.loads(
                (Path(tmp) / "tasks" / task.task_id / "artifacts" / "model_calls" / "1-planner" / "meta.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(meta["status"], "failed")
            self.assertIn("boom", meta["error"])


class SeededWorkspaceTest(unittest.TestCase):
    def test_seeded_loop_diffs_only_executor_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_dir = Path(tmp) / "proj"
            seed_dir.mkdir()
            (seed_dir / "existing.py").write_text("x = 1\n", encoding="utf-8")

            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"executor": changes_json(path="new.py", content="y = 2\n")})
            result = kernel.run_model_loop(
                task.task_id, make_routing(), {"fake": backend}, workspace_from=seed_dir
            )

            self.assertEqual(result.status, TaskStatus.DONE)
            task_dir = Path(tmp) / "tasks" / task.task_id
            # 基线快照记录种子内容
            base = json.loads((task_dir / "artifacts" / "workspace_base.json").read_text(encoding="utf-8"))
            self.assertEqual(base, {"existing.py": "x = 1\n"})
            # executor 能看到种子文件清单
            executor_prompt = [r for r in backend.requests if r.role == "executor"][0].prompt
            self.assertIn("existing.py", executor_prompt)
            # diff 只包含 executor 的新增，不把种子文件当变更
            diff = (task_dir / "artifacts" / "rounds" / "1" / "changes.diff").read_text(encoding="utf-8")
            self.assertIn("new.py", diff)
            self.assertNotIn("existing.py", diff)

    def test_missing_seed_directory_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            with self.assertRaisesRegex(ValueError, "播种目录"):
                kernel.run_model_loop(
                    task.task_id, make_routing(), {"fake": FakeBackend()},
                    workspace_from=Path(tmp) / "no-such-dir",
                )


class ParseRetryTest(unittest.TestCase):
    def test_executor_retry_once_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"executor": ["我直接改好了。", changes_json()]})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            executor_requests = [r for r in backend.requests if r.role == "executor"]
            self.assertEqual(len(executor_requests), 2)
            self.assertIn("无法解析", executor_requests[1].prompt)  # 纠错提示
            # 重试调用工件独立编号，不覆盖
            calls_dir = Path(tmp) / "tasks" / task.task_id / "artifacts" / "model_calls"
            self.assertTrue((calls_dir / "2-executor").exists())
            self.assertTrue((calls_dir / "3-executor").exists())

    def test_executor_retry_exhausted_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"executor": ["散文一。", "散文二。"]})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.CLARIFICATION_REQUIRED)
            self.assertEqual(len([r for r in backend.requests if r.role == "executor"]), 2)

    def test_reviewer_retry_once_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = make_ready_task(kernel)
            backend = FakeBackend(responses={"reviewer": ["看起来不错！", review_json("pass")]})
            result = kernel.run_model_loop(task.task_id, make_routing(), {"fake": backend})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(len([r for r in backend.requests if r.role == "reviewer"]), 2)


class CliTest(unittest.TestCase):
    def test_run_loop_args_parse(self) -> None:
        args = build_parser().parse_args(
            ["run-loop", "--task-id", "TASK-1", "--root", "/tmp/x", "--models-config", "m.json"]
        )
        self.assertEqual(args.command, "run-loop")
        self.assertEqual(args.task_id, "TASK-1")
        self.assertEqual(args.models_config, "m.json")

    def test_new_subcommands_and_flags_parse(self) -> None:
        parser = build_parser()

        create = parser.parse_args(
            ["create-task", "--title", "t", "--goal", "g", "--input", "i",
             "--context-file", "docs/需求.md", "--context-file", "src"]
        )
        self.assertEqual(create.context_file, ["docs/需求.md", "src"])

        loop = parser.parse_args(["run-loop", "--task-id", "T", "--workspace-from", "proj"])
        self.assertEqual(loop.workspace_from, "proj")

        resume = parser.parse_args(["resume", "--task-id", "T", "--answer", "确认"])
        self.assertEqual(resume.answer, "确认")

        deliver = parser.parse_args(["deliver", "--task-id", "T", "--dest", "out", "--yes"])
        self.assertTrue(deliver.yes)

    def test_build_backends_covers_both_providers(self) -> None:
        backends = build_backends()
        self.assertIn("cli", backends)
        self.assertIn("fake", backends)


if __name__ == "__main__":
    unittest.main()
