from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from app.agents.contracts import (
    AgentTask,
    AgentTaskStatus,
    ExecutionPlan,
    ValidationResult,
)
from app.agents.delivery import DeliveryService
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.scheduler import PersistentAgentScheduler
from app.agents.workflow import AgentWorkflow
from app.web.server import make_server
from tests.git_support import create_repository


def plan() -> dict:
    return {
        "requirement_understanding": "Create result.txt",
        "non_goals": [],
        "files_and_symbols": ["result.txt"],
        "steps": ["Write result.txt"],
        "constraints": ["Stay in worktree"],
        "acceptance_criteria": ["result.txt contains done"],
        "required_tests": ["fake-check"],
        "risks": [],
        "open_questions": [],
    }


def plan_with_question() -> dict:
    payload = plan()
    payload["open_questions"] = ["结果文件应使用哪种换行符？"]
    return payload


class AccessibilityParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.dialog_labels: list[str] = []
        self.ids: set[str] = set()
        self.buttons: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "dialog":
            self.dialog_labels.append(values.get("aria-labelledby", ""))
        if tag == "button":
            self.buttons.append(values)


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


class AgentWebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = create_repository(self.root)
        runtime = ScriptedFakeRuntime(
            {
                "planner": [FakeAgentStep(output=plan(), session_id="planner-session")],
                "executor": [
                    FakeAgentStep(
                        output={
                            "completed_steps": ["Write result.txt"],
                            "modified_files": ["result.txt"],
                            "tests": [],
                            "deviations": [],
                            "remaining_risks": [],
                            "next_steps": ["Confirm delivery"],
                        },
                        writes={"result.txt": "done\n"},
                        session_id="executor-session",
                    )
                ],
                "reviewer": [
                    FakeAgentStep(
                        output={
                            "verdict": "pass",
                            "acceptance": [
                                {"criterion": "result.txt contains done", "passed": True}
                            ],
                            "issues": [],
                            "recommended_tests": [],
                            "summary": "Passed",
                        },
                        session_id="reviewer-session",
                    )
                ],
            }
        )
        workflow = AgentWorkflow(
            self.root / "web-data" / "agent-runtime",
            runtime,
            PassingValidator(),
        )
        scheduler = PersistentAgentScheduler(workflow)
        delivery = DeliveryService(workflow)
        self.server = make_server(
            self.root / "web-data",
            0,
            agent_workflow=workflow,
            agent_scheduler=scheduler,
            agent_delivery=delivery,
            auto_run_agent=False,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.runtime = runtime
        self.workflow = workflow

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def request_text(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def register_project(self) -> dict:
        status, project = self.request(
            "POST",
            "/api/agent/projects",
            {
                "name": "Web project",
                "repository": str(self.repository),
                "default_branch": "main",
            },
        )
        self.assertEqual(status, 200)
        return project

    def test_workflow_api_persists_controlled_definition(self) -> None:
        status, workflows = self.request("GET", "/api/agent/workflows")
        self.assertEqual(status, 200)
        self.assertEqual(
            {item["workflow_id"] for item in workflows},
            {"guarded", "autopilot"},
        )

        status, saved = self.request(
            "POST",
            "/api/agent/workflows",
            {
                "workflow_id": "personal",
                "label": "Personal",
                "nodes": [
                    {"node_id": "plan", "kind": "planner", "label": "Plan"},
                    {"node_id": "execute", "kind": "executor", "label": "Execute"},
                    {"node_id": "validate", "kind": "validation", "label": "Validate"},
                    {"node_id": "review", "kind": "reviewer", "label": "Review"},
                    {"node_id": "deliver", "kind": "delivery", "label": "Deliver"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["workflow_id"], "personal")

        project = self.register_project()
        status, task = self.request(
            "POST",
            "/api/agent/tasks",
            {
                "project_id": project["project_id"],
                "workflow_id": "personal",
                "title": "Configured task",
                "requirement": "Create result.txt",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(task["workflow_id"], "personal")
        self.assertEqual(task["workflow"]["label"], "Personal")

    def test_agent_api_runs_state_driven_task_and_confirmed_delivery(self) -> None:
        project = self.register_project()

        status, task = self.request(
            "POST",
            "/api/agent/tasks",
            {
                "title": "Web task",
                "requirement": "Create result.txt",
                "project_id": project["project_id"],
            },
        )
        self.assertEqual(status, 202)
        task_id = task["task_id"]
        self.assertEqual(task["status"], "queued_for_analysis")
        self.assertEqual(task["queue_position"], 1)

        _, tasks = self.request("GET", "/api/agent/tasks")
        self.assertEqual(tasks[0]["task_id"], task_id)
        _, queue = self.request("GET", "/api/agent/queue")
        self.assertEqual(queue["entries"][0]["status"], "queued")

        status, analyzed = self.request("POST", "/api/agent/queue/run-next")
        self.assertEqual(status, 200)
        self.assertEqual(analyzed["status"], "waiting_for_plan_approval")
        status, detail = self.request("GET", f"/api/agent/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["plan"]["steps"], ["Write result.txt"])
        self.assertEqual([item["id"] for item in detail["actions"]], ["approve"])

        status, queued = self.request("POST", f"/api/agent/tasks/{task_id}/approve")
        self.assertEqual(status, 202)
        self.assertEqual(queued["status"], "queued_for_execution")
        _, ready = self.request("POST", "/api/agent/queue/run-next")
        self.assertEqual(ready["status"], "ready_to_deliver")

        _, detail = self.request("GET", f"/api/agent/tasks/{task_id}")
        self.assertEqual(detail["rounds"][0]["validation"]["passed"], True)
        self.assertEqual(detail["rounds"][0]["review"]["verdict"], "pass")
        self.assertTrue(detail["runs"][0]["events"] == [] or isinstance(detail["runs"], list))
        self.assertEqual(
            [item["id"] for item in detail["actions"]],
            ["prepare_delivery"],
        )

        status, prepared = self.request(
            "POST",
            f"/api/agent/tasks/{task_id}/prepare-delivery",
        )
        self.assertEqual(status, 200)
        self.assertIn("task_commit", prepared, prepared)
        self.assertIn("task_commit", prepared["delivery_report"], prepared)
        self.assertEqual(prepared["delivery_report"]["task_commit"], prepared["task_commit"])
        self.assertEqual([item["id"] for item in prepared["actions"]], ["deliver"])

        status, error = self.request(
            "POST",
            f"/api/agent/tasks/{task_id}/deliver",
            {"strategy": "merge", "confirmed": False},
        )
        self.assertEqual(status, 400)
        self.assertIn("明确确认", error["error"])
        self.assertFalse((self.repository / "result.txt").exists())

        status, delivered = self.request(
            "POST",
            f"/api/agent/tasks/{task_id}/deliver",
            {"strategy": "merge", "confirmed": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual((self.repository / "result.txt").read_text("utf-8"), "done\n")

    def test_health_exposes_supported_profiles_without_editable_commands(self) -> None:
        status, payload = self.request("GET", "/api/agent/runtime-health")

        self.assertEqual(status, 200)
        self.assertIn("profiles", payload)
        self.assertNotIn("command", json.dumps(payload["profiles"]))
        self.assertTrue(payload["health"]["available"])

    def test_clarification_is_persisted_and_requeues_the_same_planner_session(self) -> None:
        self.runtime.scripts["planner"] = [
            FakeAgentStep(output=plan_with_question(), session_id="clarify-session"),
            FakeAgentStep(output=plan(), session_id="clarify-session"),
        ]
        project = self.register_project()
        _, task = self.request(
            "POST",
            "/api/agent/tasks",
            {
                "title": "Clarify task",
                "requirement": "Create result.txt",
                "project_id": project["project_id"],
            },
        )
        task_id = task["task_id"]

        _, analyzed = self.request("POST", "/api/agent/queue/run-next")
        self.assertEqual(analyzed["status"], "waiting_for_plan_approval")
        self.assertEqual([item["id"] for item in analyzed["actions"]], ["clarify"])
        self.assertEqual(
            analyzed["actions"][0]["description"],
            "结果文件应使用哪种换行符？",
        )

        status, error = self.request(
            "POST", f"/api/agent/tasks/{task_id}/clarify", {"answer": ""}
        )
        self.assertEqual(status, 400)
        self.assertIn("answer", error["error"])

        status, queued = self.request(
            "POST",
            f"/api/agent/tasks/{task_id}/clarify",
            {"answer": "使用 LF"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(queued["status"], "queued_for_analysis")
        _, replanned = self.request("POST", "/api/agent/queue/run-next")
        self.assertEqual(replanned["status"], "waiting_for_plan_approval")
        self.assertEqual([item["id"] for item in replanned["actions"]], ["approve"])
        self.assertEqual(replanned["clarifications"][0]["answer"], "使用 LF")
        self.assertEqual(self.runtime.requests[-1].session_id, "clarify-session")
        self.assertIn("使用 LF", self.runtime.requests[-1].instructions)

    def test_task_list_prioritizes_operational_attention_states(self) -> None:
        statuses = [
            AgentTaskStatus.DELIVERED,
            AgentTaskStatus.READY_TO_DELIVER,
            AgentTaskStatus.BLOCKED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            AgentTaskStatus.EXECUTING,
        ]
        for index, status in enumerate(statuses):
            self.workflow.store.save(
                AgentTask(
                    title=f"Priority {index}",
                    requirement="Inspect ordering",
                    status=status,
                )
            )

        status, tasks = self.request("GET", "/api/agent/tasks")

        self.assertEqual(status, 200, tasks)
        self.assertEqual(
            [item["status"] for item in tasks],
            [
                "executing",
                "waiting_for_plan_approval",
                "failed",
                "blocked",
                "ready_to_deliver",
                "delivered",
            ],
        )

    def test_metrics_groups_tasks_and_reports_scheduler_counts(self) -> None:
        statuses = [
            AgentTaskStatus.EXECUTING,
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.BLOCKED,
            AgentTaskStatus.READY_TO_DELIVER,
            AgentTaskStatus.DELIVERED,
        ]
        for index, task_status in enumerate(statuses):
            task = AgentTask(
                title=f"Metric {index}",
                requirement="Count task",
                status=(
                    AgentTaskStatus.DRAFT
                    if task_status is AgentTaskStatus.QUEUED_FOR_ANALYSIS
                    else task_status
                ),
            )
            self.workflow.store.save(task)
            if task_status is AgentTaskStatus.QUEUED_FOR_ANALYSIS:
                self.server.agent_scheduler.enqueue_analysis(task.task_id)

        status, metrics = self.request("GET", "/api/agent/metrics")

        self.assertEqual(status, 200)
        self.assertEqual(metrics["schema_version"], 1)
        self.assertTrue(datetime.fromisoformat(metrics["generated_at"]))
        self.assertEqual(
            metrics["tasks"],
            {
                "running": 2,
                "waiting_for_human": 1,
                "failed": 1,
                "blocked": 1,
                "ready_to_deliver": 1,
                "other": 1,
                "total": 7,
            },
        )
        self.assertEqual(metrics["scheduler"], {"queued": 1, "running": 0})

    def test_blocking_reasons_expose_distinct_directed_resolutions(self) -> None:
        expected = {
            (AgentTaskStatus.PAUSED, "permission_required"): [
                "permission_required",
                "rerun",
                "terminate",
            ],
            (AgentTaskStatus.BLOCKED, "integration_conflict"): ["resolve_conflict"],
            (AgentTaskStatus.BLOCKED, "policy_blocked"): ["review_policy_block"],
            (AgentTaskStatus.FAILED, ""): ["inspect_failure"],
        }
        for index, ((status, reason), action_ids) in enumerate(expected.items()):
            task = AgentTask(
                title=f"Blocked {index}",
                requirement="Inspect action",
                status=status,
                pause_reason=reason,
            )
            self.workflow.store.save(task)
            _, detail = self.request("GET", f"/api/agent/tasks/{task.task_id}")
            self.assertEqual([item["id"] for item in detail["actions"]], action_ids)
            if detail["actions"][0]["manual"]:
                self.assertTrue(detail["actions"][0]["description"])

    def test_console_has_responsive_keyboard_and_accessibility_contracts(self) -> None:
        status, page = self.request_text("/")

        self.assertEqual(status, 200)
        self.assertIn('@media (max-width: 820px)', page)
        self.assertIn('@media (max-width: 520px)', page)
        self.assertIn('id="metrics" role="status" aria-live="polite"', page)
        self.assertIn('api("/api/agent/metrics")', page)
        self.assertIn('class="skip-link" href="#workspace"', page)
        self.assertIn(":focus-visible", page)
        self.assertIn('aria-current="step"', page)
        self.assertNotIn("流程编排", page)
        self.assertNotIn("经验记忆", page)
        self.assertNotIn("/api/models/config", page)

        parser = AccessibilityParser()
        parser.feed(page)
        self.assertTrue(parser.dialog_labels)
        self.assertTrue(all(label in parser.ids for label in parser.dialog_labels))
        self.assertTrue(
            all(
                button.get("aria-label")
                or button.get("title")
                or button.get("value")
                or button.get("id")
                or button.get("data-filter")
                for button in parser.buttons
            )
        )

    def test_legacy_history_is_read_only_and_broken_artifacts_are_local_failures(self) -> None:
        task_dir = self.root / "web-data" / "tasks" / "TASK-legacy"
        (task_dir / "artifacts").mkdir(parents=True)
        (task_dir / "artifacts" / "note.txt").write_text("legacy evidence", "utf-8")
        (task_dir / "state.json").write_text(
            json.dumps(
                {
                    "task_id": "TASK-legacy",
                    "title": "Historical task",
                    "goal": "Keep audit evidence",
                    "status": "done",
                    "updated_at": "2026-07-10T00:00:00+00:00",
                    "artifacts": {
                        "note": "artifacts/note.txt",
                        "missing": "artifacts/missing.json",
                        "absolute": str(self.root / "outside.json"),
                    },
                },
                ensure_ascii=False,
            ),
            "utf-8",
        )

        status, history = self.request("GET", "/api/agent/history")
        self.assertEqual(status, 200)
        self.assertEqual(history[0]["task_key"], "legacy:TASK-legacy")
        self.assertTrue(history[0]["read_only"])
        self.assertEqual(history[0]["workflow_version"], "legacy-v1")

        status, detail = self.request("GET", "/api/agent/history/TASK-legacy")
        self.assertEqual(status, 200)
        self.assertEqual(detail["artifacts"]["note"]["content"], "legacy evidence")
        self.assertFalse(detail["artifacts"]["missing"]["available"])
        self.assertIn("绝对路径", detail["artifacts"]["absolute"]["error"])

        status, payload = self.request(
            "POST", "/api/tasks", {"title": "No legacy writes"}
        )
        self.assertEqual(status, 410)
        self.assertIn("旧工作流写接口已移除", payload["error"])

    def test_legacy_model_config_migrates_without_command_templates(self) -> None:
        config = self.root / "web-data" / "models.json"
        config.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "name": "legacy",
                            "provider": "cli",
                            "model": "legacy-model",
                            "command": ["unsafe-cli", "{prompt}"],
                        }
                    ],
                    "roles": {"default": "legacy"},
                }
            ),
            "utf-8",
        )

        status, migrated = self.request(
            "POST", "/api/agent/profiles/migrate", {"source": "models.json"}
        )

        self.assertEqual(status, 200)
        self.assertTrue(migrated["commands_discarded"])
        saved = json.loads(
            (self.root / "web-data" / "agent-profiles.json").read_text("utf-8")
        )
        self.assertNotIn("command", json.dumps(saved))
        self.assertEqual(saved["roles"]["executor"]["runtime"], "codex_cli")
        self.assertEqual(saved["roles"]["executor"]["access"], "workspace_write")

        status, error = self.request(
            "POST",
            "/api/agent/profiles/migrate",
            {"source": str(config.resolve())},
        )
        self.assertEqual(status, 400)
        self.assertIn("数据根", error["error"])


if __name__ == "__main__":
    unittest.main()
