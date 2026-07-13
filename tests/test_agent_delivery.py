from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.contracts import AgentTaskStatus, DeliveryReport, ExecutionPlan, ValidationResult
from app.agents.delivery import DeliveryService
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.workflow import AgentWorkflow
from tests.git_support import create_repository, run_git


def plan(files: list[str] | None = None) -> dict:
    return {
        "requirement_understanding": "Implement the requested result",
        "non_goals": ["Do not change project policy"],
        "files_and_symbols": files or ["result.txt"],
        "steps": ["Write the requested result"],
        "constraints": ["Stay in the task worktree"],
        "acceptance_criteria": ["requested content is present"],
        "required_tests": ["fake-check"],
        "risks": ["Target branch may advance"],
        "open_questions": [],
    }


def execution_output(files: list[str]) -> dict:
    return {
        "completed_steps": ["Implemented requested content"],
        "modified_files": files,
        "tests": [],
        "deviations": [],
        "remaining_risks": ["Manual delivery is still required"],
        "next_steps": ["Confirm delivery"],
    }


def passing_review(summary: str = "All criteria pass") -> dict:
    return {
        "verdict": "pass",
        "acceptance": [{"criterion": "requested content is present", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": summary,
    }


class PassingValidator:
    def validate(self, task_id, workspace, plan: ExecutionPlan, policy):
        return ValidationResult(
            passed=True,
            checks=[
                {
                    "name": "fake-check",
                    "command": ["fake-check"],
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "error": "",
                }
            ],
        )


def ready_task(
    root: Path,
    writes: dict[str, str],
    integration_review: bool = False,
) -> tuple[AgentWorkflow, DeliveryService, object, Path]:
    repository = create_repository(root)
    reviews = [FakeAgentStep(output=passing_review("Initial review passed"))]
    if integration_review:
        reviews.append(FakeAgentStep(output=passing_review("Integrated review passed")))
    runtime = ScriptedFakeRuntime(
        {
            "planner": [FakeAgentStep(output=plan(list(writes)))],
            "executor": [
                FakeAgentStep(
                    output=execution_output(list(writes)),
                    writes=writes,
                )
            ],
            "reviewer": reviews,
        }
    )
    workflow = AgentWorkflow(root / "workloop-data", runtime, PassingValidator())
    project = workflow.register_project("Delivery", repository, "main")
    task = workflow.create_task("Deliver result", "Implement requested content", project.project_id)
    workflow.analyze(task.task_id)
    completed = workflow.approve_plan(task.task_id)
    if completed.status is not AgentTaskStatus.READY_TO_DELIVER:
        raise AssertionError(completed.error)
    return workflow, DeliveryService(workflow), completed, repository


class DeliveryServiceTest(unittest.TestCase):
    def test_prepare_creates_auditable_commit_and_complete_report_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, delivery, task, repository = ready_task(
                root,
                {"result.txt": "done\n"},
            )
            target_before = run_git(repository, "rev-parse", "main").stdout.strip()

            prepared = delivery.prepare(task.task_id)
            report = delivery.load_report(task.task_id)

            self.assertEqual(prepared.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertTrue(prepared.task_commit)
            self.assertEqual(report.task_commit, prepared.task_commit)
            self.assertEqual(report.target_commit, target_before)
            self.assertEqual(report.modified_files, ["result.txt"])
            self.assertTrue(all(item.passed for item in report.acceptance))
            self.assertTrue(report.validation_evidence)
            self.assertEqual(report.review_verdict, "pass")
            self.assertIn("Manual delivery is still required", report.known_risks)
            self.assertIn("Confirm delivery", report.human_next_steps)
            self.assertEqual(report.task_branch, prepared.task_branch)
            self.assertEqual(run_git(repository, "rev-parse", "main").stdout.strip(), target_before)
            self.assertFalse((repository / "result.txt").exists())
            self.assertEqual(
                run_git(Path(prepared.workspace), "status", "--porcelain").stdout.strip(),
                "",
            )
            self.assertEqual(
                run_git(Path(prepared.workspace), "rev-parse", "HEAD^").stdout.strip(),
                target_before,
            )

            prepared_again = delivery.prepare(task.task_id)
            self.assertEqual(prepared_again.task_commit, prepared.task_commit)

    def test_delivery_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, delivery, task, repository = ready_task(
                Path(tmp),
                {"result.txt": "done\n"},
            )
            prepared = delivery.prepare(task.task_id)
            target_before = run_git(repository, "rev-parse", "main").stdout.strip()

            with self.assertRaisesRegex(ValueError, "明确确认"):
                delivery.deliver(task.task_id, strategy="merge", confirmed=False)

            self.assertEqual(run_git(repository, "rev-parse", "main").stdout.strip(), target_before)
            self.assertFalse((repository / "result.txt").exists())

            delivered = delivery.deliver(task.task_id, strategy="merge", confirmed=True)

            self.assertEqual(delivered.status, AgentTaskStatus.DELIVERED)
            self.assertEqual((repository / "result.txt").read_text("utf-8"), "done\n")
            self.assertEqual(delivered.delivered_commit, prepared.task_commit)

    def test_cherry_pick_delivery_also_requires_a_bound_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow, delivery, task, repository = ready_task(
                Path(tmp),
                {"result.txt": "done\n"},
            )

            with self.assertRaisesRegex(FileNotFoundError, "DeliveryReport"):
                delivery.deliver(task.task_id, strategy="cherry-pick", confirmed=True)

            prepared = delivery.prepare(task.task_id)
            delivered = delivery.deliver(
                task.task_id,
                strategy="cherry-pick",
                confirmed=True,
            )

            self.assertEqual(delivered.status, AgentTaskStatus.DELIVERED)
            self.assertEqual((repository / "result.txt").read_text("utf-8"), "done\n")
            self.assertEqual(
                run_git(repository, "show", "--format=%s", "--no-patch", "HEAD").stdout.strip(),
                f"workloop({task.task_id}): {task.title}",
            )
            self.assertTrue(prepared.task_commit)

    def test_target_advance_rebases_then_revalidates_and_rereviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, delivery, task, repository = ready_task(
                root,
                {"result.txt": "done\n"},
                integration_review=True,
            )
            (repository / "upstream.txt").write_text("new upstream\n", encoding="utf-8")
            run_git(repository, "add", "upstream.txt")
            run_git(repository, "commit", "-m", "advance target")
            advanced_target = run_git(repository, "rev-parse", "main").stdout.strip()

            stale = delivery.prepare(task.task_id)

            self.assertEqual(stale.status, AgentTaskStatus.INTEGRATION_REQUIRED)
            self.assertNotIn("delivery_report", stale.artifacts)
            old_review = (
                workflow.store.task_dir(task.task_id)
                / "artifacts/rounds/1/review.json"
            )
            self.assertTrue(old_review.is_file())

            integrated = delivery.integrate(task.task_id)
            self.assertEqual(
                integrated.status,
                AgentTaskStatus.READY_TO_DELIVER,
                integrated.error,
            )
            report = delivery.load_report(task.task_id)

            self.assertEqual(report.target_commit, advanced_target)
            self.assertEqual(
                run_git(Path(integrated.workspace), "rev-parse", "HEAD^").stdout.strip(),
                advanced_target,
            )
            self.assertTrue(
                (
                    workflow.store.task_dir(task.task_id)
                    / "artifacts/rounds/2/validation.json"
                ).is_file()
            )
            self.assertEqual(report.review_summary, "Integrated review passed")
            self.assertEqual(run_git(repository, "rev-parse", "main").stdout.strip(), advanced_target)

    def test_integration_conflict_pauses_for_human_without_changing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, delivery, task, repository = ready_task(
                root,
                {"app.txt": "task version\n"},
            )
            (repository / "app.txt").write_text("target version\n", encoding="utf-8")
            run_git(repository, "add", "app.txt")
            run_git(repository, "commit", "-m", "conflicting target change")
            target_commit = run_git(repository, "rev-parse", "main").stdout.strip()
            delivery.prepare(task.task_id)

            blocked = delivery.integrate(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertEqual(blocked.pause_reason, "integration_conflict")
            self.assertIn("冲突", blocked.error)
            self.assertEqual(run_git(repository, "rev-parse", "main").stdout.strip(), target_commit)
            self.assertEqual((repository / "app.txt").read_text("utf-8"), "target version\n")
            self.assertNotEqual(
                run_git(Path(blocked.workspace), "diff", "--name-only", "--diff-filter=U").stdout.strip(),
                "",
            )

    def test_target_advance_after_report_invalidates_delivery_instead_of_mutating_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, delivery, task, repository = ready_task(
                root,
                {"result.txt": "done\n"},
            )
            delivery.prepare(task.task_id)
            (repository / "late.txt").write_text("late\n", encoding="utf-8")
            run_git(repository, "add", "late.txt")
            run_git(repository, "commit", "-m", "late target advance")
            advanced = run_git(repository, "rev-parse", "main").stdout.strip()

            stale = delivery.deliver(task.task_id, strategy="merge", confirmed=True)

            self.assertEqual(stale.status, AgentTaskStatus.INTEGRATION_REQUIRED)
            self.assertNotIn("delivery_report", stale.artifacts)
            self.assertEqual(run_git(repository, "rev-parse", "main").stdout.strip(), advanced)
            self.assertFalse((repository / "result.txt").exists())


if __name__ == "__main__":
    unittest.main()
