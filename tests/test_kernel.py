from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.contracts import ContextPack, ContextSection, PolicyBoundary, TaskState, TaskStatus
from app.core.workflow import WorkloopKernel
from app.decision.decision_engine import DecisionEngine
from app.evaluation.evaluators import ContextEvaluator
from app.policy.policy_checker import PolicyChecker


class WorkloopKernelTest(unittest.TestCase):
    def test_clear_input_moves_to_ready_for_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = kernel.create_task(
                title="异常订单处理优化",
                goal="形成可靠方案",
                raw_input="目标：减少异常订单人工处理时间。验收标准：能识别重复订单，并通过测试。",
            )

            self.assertEqual(task.status, TaskStatus.READY_FOR_PLAN)
            self.assertTrue(task.context_refs)
            self.assertTrue(task.evaluations)
            self.assertTrue(task.decisions)

    def test_conflict_requires_clarification_not_policy_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = kernel.create_task(
                title="异常订单处理优化",
                goal="形成可靠方案",
                raw_input="目标：减少异常订单人工处理时间。验收标准：通过测试。规则冲突：阈值不确定。",
            )

            self.assertEqual(task.status, TaskStatus.CLARIFICATION_REQUIRED)

    def test_low_confidence_is_policy_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = kernel.create_task(
                title="空输入",
                goal="形成可靠方案",
                raw_input="",
            )

            self.assertEqual(task.status, TaskStatus.POLICY_BLOCKED)

    def test_artifact_references_are_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            task = kernel.create_task(
                title="相对引用",
                goal="任务目录可整体迁移",
                raw_input="目标：验证引用可移植。验收标准：通过测试。",
            )

            task_dir = Path(tmp) / "tasks" / task.task_id
            refs = (
                task.context_refs
                + task.evaluations
                + task.decisions
                + task.events
                + list(task.artifacts.values())
            )
            self.assertTrue(refs)
            for ref in refs:
                self.assertFalse(Path(ref).is_absolute(), ref)
                self.assertNotIn("\\", ref)
                self.assertTrue((task_dir / ref).exists(), ref)

    def test_deny_path_glob_matches_root_and_nested(self) -> None:
        checker = PolicyChecker()
        policy = PolicyBoundary(deny_paths=["**/.env", "**/secrets/**"])

        self.assertFalse(checker.check_path(policy, ".env").passed)
        self.assertFalse(checker.check_path(policy, "config/.env").passed)
        self.assertFalse(checker.check_path(policy, "secrets/key.pem").passed)
        self.assertFalse(checker.check_path(policy, "a\\secrets\\key.pem").passed)
        self.assertTrue(checker.check_path(policy, "src/main.py").passed)

    def test_allow_paths_restrict_write_scope(self) -> None:
        checker = PolicyChecker()
        policy = PolicyBoundary(allow_paths=["src/**"])

        self.assertTrue(checker.check_path(policy, "src/a/b.py").passed)
        self.assertFalse(checker.check_path(policy, "docs/readme.md").passed)

    def test_restricted_tool_requires_human(self) -> None:
        checker = PolicyChecker()
        policy = PolicyBoundary(
            allowed_tools=["read_file", "write_file"],
            restricted_tools=["write_file"],
        )

        result = checker.check_tool(policy, "write_file")

        self.assertTrue(result.passed)
        self.assertTrue(result.requires_human)
        self.assertTrue(result.warnings)

    def test_strict_custom_threshold_blocks_via_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel = WorkloopKernel(Path(tmp))
            strict = PolicyBoundary(min_context_confidence=0.9)
            task = kernel.create_task(
                title="严格阈值",
                goal="验证配置传导",
                raw_input="目标：验证配置在生产路径生效。验收标准：通过测试。",
                policy=strict,
            )
            # score 固定 0.85 < 0.9：policy 门禁拦截；默认阈值 0.65 时同输入会放行
            self.assertEqual(task.status, TaskStatus.POLICY_BLOCKED)

    def test_decision_engine_does_not_second_guess_policy(self) -> None:
        pack = ContextPack(task_id="TASK-x", purpose="t")
        pack.sections.append(ContextSection(name="s", content="c", confidence=0.55))
        evaluation = ContextEvaluator().evaluate(pack)  # score 0.55，无任何 issue
        lenient = PolicyBoundary(min_context_confidence=0.5)
        check = PolicyChecker().check_context(lenient, evaluation.score, [])
        self.assertTrue(check.passed)

        decision = DecisionEngine().decide_after_context(
            TaskState(title="t", goal="g"), evaluation, check
        )
        # policy 已放行：决策不得以任何内置阈值二次拦截（旧硬编码 0.65 实现会在此失败）
        self.assertEqual(decision.next_state, TaskStatus.READY_FOR_PLAN)


if __name__ == "__main__":
    unittest.main()


