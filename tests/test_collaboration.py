from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from threading import Thread

from app.application.collaboration import CollaborationService
from app.application.workbench import WorkbenchService
from app.domain.collaboration import (
    CollaborationTask,
    TaskGraph,
    TaskOutcome,
    TaskStatus,
)
from app.web.server import make_server


def make_service(root: Path, execute_task, validate_model=None) -> CollaborationService:
    return CollaborationService(
        root,
        validate_role=lambda role: role.validate(),
        execute_task=execute_task,
        validate_project=lambda project_id: project_id == "PROJECT-1"
        or (_ for _ in ()).throw(KeyError(project_id)),
        validate_model=validate_model,
    )


class CollaborationTest(unittest.TestCase):
    def test_dependency_graph_runs_roles_and_publishes_handoffs(self):
        calls: list[tuple[str, list[str]]] = []

        def execute(task, role, handoffs):
            calls.append((task.task_id, [item.from_task_id for item in handoffs]))
            return TaskOutcome(
                TaskStatus.COMPLETED,
                session_id=f"SESSION-{task.task_id}",
                result={"facts": {"role": role.role_id}},
            )

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), execute)
            analysis = service.create_task("PROJECT-1", {
                "title": "分析", "description": "澄清需求", "role_id": "analyst",
            })
            planning = service.create_task("PROJECT-1", {
                "title": "设计", "description": "设计方案", "role_id": "architect",
                "depends_on": [analysis.task_id],
            })
            implementation = service.create_task("PROJECT-1", {
                "title": "实现", "description": "完成代码", "role_id": "developer",
                "depends_on": [planning.task_id],
            })

            state = service.coordinate("PROJECT-1")

            self.assertTrue(all(item["status"] == "completed" for item in state["tasks"]))
            self.assertEqual([item[0] for item in calls], [
                analysis.task_id, planning.task_id, implementation.task_id,
            ])
            self.assertEqual(calls[1][1], [analysis.task_id])
            self.assertEqual(calls[2][1], [planning.task_id])
            self.assertEqual(len(state["handoffs"]), 2)

    def test_independent_read_roles_run_in_parallel(self):
        barrier = threading.Barrier(2)

        def execute(task, role, handoffs):
            del task, role, handoffs
            barrier.wait(timeout=2)
            return TaskOutcome(TaskStatus.COMPLETED)

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), execute)
            service.create_task("PROJECT-1", {
                "title": "分析 A", "description": "并行分析 A", "role_id": "analyst",
            })
            service.create_task("PROJECT-1", {
                "title": "分析 B", "description": "并行分析 B", "role_id": "architect",
            })
            state = service.coordinate("PROJECT-1")
            self.assertEqual(state["counts"]["completed"], 2)

    def test_workspace_writers_are_serialized(self):
        guard = threading.Lock()
        active = 0
        maximum = 0

        def execute(task, role, handoffs):
            nonlocal active, maximum
            del task, role, handoffs
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return TaskOutcome(TaskStatus.COMPLETED)

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), execute)
            for suffix in ("A", "B"):
                service.create_task("PROJECT-1", {
                    "title": f"实现 {suffix}",
                    "description": f"写入模块 {suffix}",
                    "role_id": "developer",
                })
            service.coordinate("PROJECT-1")
            self.assertEqual(maximum, 1)

    def test_failed_dependency_blocks_downstream_task(self):
        def execute(task, role, handoffs):
            del role, handoffs
            if task.title == "失败":
                return TaskOutcome(TaskStatus.FAILED, error="boom")
            return TaskOutcome(TaskStatus.COMPLETED)

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), execute)
            failed = service.create_task("PROJECT-1", {
                "title": "失败", "description": "失败任务", "role_id": "analyst",
            })
            downstream = service.create_task("PROJECT-1", {
                "title": "下游", "description": "依赖失败任务", "role_id": "architect",
                "depends_on": [failed.task_id],
            })
            state = service.coordinate("PROJECT-1")
            by_id = {item["task_id"]: item for item in state["tasks"]}
            self.assertEqual(by_id[failed.task_id]["status"], "failed")
            self.assertEqual(by_id[downstream.task_id]["status"], "blocked")

    def test_invalid_executor_result_becomes_a_recoverable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), lambda task, role, handoffs: object())
            task = service.create_task("PROJECT-1", {
                "title": "异常执行器", "description": "返回错误类型", "role_id": "analyst",
            })
            state = service.coordinate("PROJECT-1")
            restored = next(item for item in state["tasks"] if item["task_id"] == task.task_id)
            self.assertEqual(restored["status"], "failed")
            self.assertIn("invalid terminal status", restored["error"])

    def test_task_graph_rejects_cycles(self):
        first = CollaborationTask(
            "TASK-A", "PROJECT-1", "A", "task A", "analyst", depends_on=("TASK-B",),
        )
        second = CollaborationTask(
            "TASK-B", "PROJECT-1", "B", "task B", "architect", depends_on=("TASK-A",),
        )
        with self.assertRaisesRegex(ValueError, "DAG"):
            TaskGraph.validate([first, second])

    def test_workbench_executes_role_sessions_with_dependency_context(self):
        planning_handoffs: list[dict] = []
        selected_models: list[str] = []

        class Gateway:
            def complete(self, *, model_alias, node, context):
                selected_models.append(model_alias)
                if node.node_type == "requirement":
                    return {
                        "understanding": "需求已明确",
                        "acceptance_criteria": ["可验证"],
                        "open_questions": [],
                    }
                planning_handoffs.extend(context.inputs["collaboration"]["handoffs"])
                return {"steps": ["实现"], "risks": [], "artifacts": {}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbench = WorkbenchService(root / "data", gateway=Gateway())
            workbench.save_provider({
                "provider_id": "local",
                "label": "Local",
                "base_url": "http://127.0.0.1:11434/v1",
                "auth_type": "none",
            })
            workbench.save_model({
                "alias": "analysis-model",
                "provider_id": "local",
                "model": "analysis-model",
            })
            project = workbench.create_project("协同项目")
            service = CollaborationService(
                root / "data",
                validate_role=workbench.validate_role,
                execute_task=workbench.execute_role_task,
                validate_project=workbench.get_project,
            )
            service.save_role({"role_id": "analyst", "model_alias": "analysis-model"})
            requirement = service.create_task(project.project_id, {
                "title": "需求", "description": "整理需求", "role_id": "analyst",
            })
            service.create_task(project.project_id, {
                "title": "架构", "description": "制定计划", "role_id": "architect",
                "depends_on": [requirement.task_id],
            })

            state = service.coordinate(project.project_id)

            self.assertEqual(state["counts"]["completed"], 2)
            self.assertEqual(selected_models, ["analysis-model", ""])
            self.assertEqual(planning_handoffs[0]["from_task_id"], requirement.task_id)
            self.assertEqual(workbench.list_sessions(project.project_id), [])
            self.assertEqual(
                len(workbench.list_sessions(project.project_id, include_collaboration=True)), 2
            )

    def test_collaboration_api_exposes_roles_tasks_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Path(tmp), 0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request(method, path, value=None):
                data = json.dumps(value).encode() if value is not None else None
                req = urllib.request.Request(
                    base + path,
                    data=data,
                    method=method,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())

            status, roles = request("GET", "/api/v2/roles")
            self.assertEqual(status, 200)
            self.assertEqual({item["role_id"] for item in roles}, {
                "analyst", "architect", "developer", "tester", "reviewer",
            })
            _, project = request("POST", "/api/v2/projects", {"name": "API"})
            status, task = request(
                "POST",
                f"/api/v2/projects/{project['project_id']}/tasks",
                {"title": "分析", "description": "分析 API", "role_id": "analyst"},
            )
            self.assertEqual(status, 201)
            status, state = request(
                "GET", f"/api/v2/projects/{project['project_id']}/collaboration"
            )
            self.assertEqual(status, 200)
            self.assertEqual(state["tasks"][0]["task_id"], task["task_id"])


class CoordinationRecoveryTest(unittest.TestCase):
    def test_blocked_dependent_auto_unblocks_after_dependency_retry(self):
        runs = {"count": 0}

        def execute(task, role, handoffs):
            del role, handoffs
            if task.title == "上游":
                runs["count"] += 1
                if runs["count"] == 1:
                    return TaskOutcome(TaskStatus.FAILED, error="first run fails")
            return TaskOutcome(TaskStatus.COMPLETED)

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), execute)
            upstream = service.create_task("PROJECT-1", {
                "title": "上游", "description": "第一次执行会失败", "role_id": "analyst",
            })
            downstream = service.create_task("PROJECT-1", {
                "title": "下游", "description": "依赖上游", "role_id": "architect",
                "depends_on": [upstream.task_id],
            })
            first = service.coordinate("PROJECT-1")
            first_by_id = {item["task_id"]: item for item in first["tasks"]}
            self.assertEqual(first_by_id[upstream.task_id]["status"], "failed")
            self.assertEqual(first_by_id[downstream.task_id]["status"], "blocked")
            self.assertFalse(first["coordinating"])

            service.retry_task(upstream.task_id)
            second = service.coordinate("PROJECT-1")
            self.assertTrue(all(item["status"] == "completed" for item in second["tasks"]))


class WaveConcurrencyTest(unittest.TestCase):
    def test_writer_runs_while_reader_is_still_executing(self):
        writer_started = threading.Event()

        def execute(task, role, handoffs):
            del task, handoffs
            if role.workspace_access.value == "read":
                # With the old two-phase wave the writer could never start
                # before this reader finished, and the wait below would fail.
                if not writer_started.wait(timeout=3):
                    raise AssertionError("writer did not start while reader was running")
            else:
                writer_started.set()
            return TaskOutcome(TaskStatus.COMPLETED)

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), execute)
            service.create_task("PROJECT-1", {"title": "分析", "description": "读者任务", "role_id": "analyst"})
            service.create_task("PROJECT-1", {"title": "实现", "description": "写者任务", "role_id": "developer"})
            state = service.coordinate("PROJECT-1")
            self.assertEqual(state["counts"]["completed"], 2)


class UpdateTaskTest(unittest.TestCase):
    def test_update_task_fields_dependencies_and_protections(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), lambda task, role, handoffs: TaskOutcome(TaskStatus.COMPLETED))
            first = service.create_task("PROJECT-1", {"title": "A", "description": "任务 A", "role_id": "analyst"})
            second = service.create_task("PROJECT-1", {
                "title": "B", "description": "任务 B", "role_id": "architect", "depends_on": [first.task_id],
            })

            updated = service.update_task(second.task_id, {"title": "B 改名", "priority": 80})
            self.assertEqual(updated.title, "B 改名")
            self.assertEqual(updated.priority, 80)
            self.assertEqual(updated.depends_on, (first.task_id,))
            self.assertEqual(updated.status, TaskStatus.PENDING)

            with self.assertRaisesRegex(ValueError, "DAG"):
                service.update_task(first.task_id, {"depends_on": [second.task_id]})

            with self.assertRaises(FileNotFoundError):
                service.update_task(second.task_id, {"role_id": "ghost"})

            running = service.repository.tasks.get(second.task_id)
            running.status = TaskStatus.RUNNING
            service.repository.save_task(running)
            with self.assertRaisesRegex(ValueError, "running"):
                service.update_task(second.task_id, {"title": "X"})

    def test_model_alias_is_validated_on_create_and_update(self):
        def validator(alias: str) -> None:
            if alias != "known-model":
                raise ValueError(f"unknown model alias: {alias}")

        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(
                Path(tmp),
                lambda task, role, handoffs: TaskOutcome(TaskStatus.COMPLETED),
                validate_model=validator,
            )
            with self.assertRaisesRegex(ValueError, "unknown model alias: ghost"):
                service.create_task("PROJECT-1", {
                    "title": "A", "description": "d", "role_id": "analyst", "model_alias": "ghost",
                })
            task = service.create_task("PROJECT-1", {
                "title": "A", "description": "d", "role_id": "analyst", "model_alias": "known-model",
            })
            self.assertEqual(task.model_alias, "known-model")
            with self.assertRaisesRegex(ValueError, "unknown model alias: other"):
                service.update_task(task.task_id, {"model_alias": "other"})


class CoordinateApiAsyncTest(unittest.TestCase):
    def test_async_coordinate_event_stream_and_task_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Path(tmp), 0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request(method, path, value=None):
                data = json.dumps(value).encode() if value is not None else None
                req = urllib.request.Request(
                    base + path,
                    data=data,
                    method=method,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, json.loads(response.read().decode())

            _, project = request("POST", "/api/v2/projects", {"name": "Async"})
            project_id = project["project_id"]
            _, task = request(
                "POST",
                f"/api/v2/projects/{project_id}/tasks",
                {"title": "分析", "description": "没有模型会阻塞", "role_id": "analyst"},
            )
            task_id = task["task_id"]

            with server._coordination_guard:
                server._coordinating_projects.add(project_id)
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                request("POST", f"/api/v2/projects/{project_id}/coordinate", {"async": True})
            self.assertEqual(rejected.exception.code, 409)
            with server._coordination_guard:
                server._coordinating_projects.discard(project_id)

            status, started = request("POST", f"/api/v2/projects/{project_id}/coordinate", {"async": True})
            self.assertEqual(status, 202)
            self.assertTrue(started["started"])

            state = {}
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, state = request("GET", f"/api/v2/projects/{project_id}/collaboration")
                if not state["coordinating"] and state["counts"]["pending"] == 0:
                    break
                time.sleep(0.2)
            self.assertFalse(state["coordinating"])
            self.assertEqual(state["counts"]["blocked"], 1)

            status, events = request("GET", "/api/v2/events")
            self.assertEqual(status, 200)
            self.assertGreaterEqual(events["total"], 1)
            self.assertTrue(any(item.get("event_type") == "node_failed" for item in events["events"]))

            status, page = request("GET", "/api/v2/events?after=0&limit=2")
            self.assertEqual(status, 200)
            self.assertEqual(page["next"], min(2, page["total"]))

            status, updated = request("POST", f"/api/v2/tasks/{task_id}", {"title": "改名", "priority": 80})
            self.assertEqual(status, 200)
            self.assertEqual(updated["priority"], 80)
            self.assertEqual(updated["title"], "改名")


if __name__ == "__main__":
    unittest.main()
