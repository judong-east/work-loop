from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from threading import Thread

from app.application.collaboration import CollaborationService
from app.application.decomposition import GoalDecomposer
from app.application.workbench import WorkbenchService
from app.web.server import make_server


PLAN = {
    "summary": "先分析，再设计，最后实现",
    "subtasks": [
        {"ref": "1", "title": "需求分析", "description": "澄清目标与验收条件", "role_id": "analyst", "depends_on": [], "priority": 60},
        {"ref": "2", "title": "方案设计", "description": "输出模块设计与风险清单", "role_id": "architect", "depends_on": ["1"]},
        {"ref": "3", "title": "代码实现", "description": "完成实现并通过验证命令", "role_id": "developer", "depends_on": ["1", "2"]},
    ],
}


class PlanGateway:
    """Gateway that answers decomposition prompts with a fixed plan."""

    def __init__(self, plan: dict):
        self.plan = plan
        self.calls: list[str] = []

    def complete(self, *, model_alias, node, context):
        del model_alias, context
        self.calls.append(node.node_type)
        if node.node_type == "tool":
            return dict(self.plan)
        raise AssertionError(f"unexpected node type in model-only test: {node.node_type}")


class RoleGateway:
    """Gateway that plans and then plays the read-only role nodes."""

    def __init__(self, plan: dict):
        self.plan = plan

    def complete(self, *, model_alias, node, context):
        del model_alias, context
        if node.node_type == "tool":
            return dict(self.plan)
        if node.node_type == "requirement":
            return {"understanding": "需求已明确", "acceptance_criteria": ["可验证"], "open_questions": []}
        if node.node_type == "planning":
            return {"steps": ["实现"], "risks": [], "artifacts": {}}
        raise AssertionError(f"unexpected node type: {node.node_type}")


def build(root: Path, gateway):
    workbench = WorkbenchService(root / "data", gateway=gateway)
    workbench.save_provider({
        "provider_id": "local",
        "label": "Local",
        "base_url": "http://127.0.0.1:11434/v1",
        "auth_type": "none",
    })
    workbench.save_model({
        "alias": "planner",
        "provider_id": "local",
        "model": "planner-model",
    })
    project = workbench.create_project("拆分演示")
    collaboration = CollaborationService(
        root / "data",
        validate_role=workbench.validate_role,
        execute_task=workbench.execute_role_task,
        validate_project=workbench.get_project,
    )
    decomposer = GoalDecomposer(
        gateway=workbench.gateway,
        collaboration=collaboration,
        project_loader=workbench.get_project,
        project_context=WorkbenchService._project_context,
        workspace_snapshot=workbench.workspace_runtime.snapshot,
    )
    return collaboration, decomposer, project


class GoalDecompositionTest(unittest.TestCase):
    def test_model_plan_creates_tasks_with_mapped_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = PlanGateway(PLAN)
            collaboration, decomposer, project = build(Path(tmp), gateway)
            collaboration.save_role({"role_id": "architect", "model_alias": "planner"})

            result = decomposer.decompose(project.project_id, "实现用户认证模块")

            self.assertEqual(len(result["task_ids"]), 3)
            tasks = {task["task_id"]: task for task in result["state"]["tasks"]}
            self.assertEqual(len(tasks), 3)
            first, second, third = (tasks[task_id] for task_id in result["task_ids"])
            self.assertEqual(first["depends_on"], [])
            self.assertEqual(second["depends_on"], [first["task_id"]])
            self.assertEqual(sorted(third["depends_on"]), sorted([first["task_id"], second["task_id"]]))
            self.assertEqual({task["goal_id"] for task in tasks.values()}, {result["goal"]["goal_id"]})
            self.assertEqual(result["goal"]["task_ids"], result["task_ids"])
            self.assertEqual(result["goal"]["summary"], PLAN["summary"])
            self.assertEqual(
                {item["status"] for item in result["state"]["tasks"]}, {"pending"}
            )
            self.assertEqual(gateway.calls, ["tool"])

    def test_plan_referencing_unknown_role_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = PlanGateway({"summary": "", "subtasks": [
                {"ref": "1", "title": "调度", "description": "调度集群", "role_id": "ops"},
            ]})
            collaboration, decomposer, project = build(Path(tmp), gateway)

            with self.assertRaisesRegex(ValueError, "未知角色 ops"):
                decomposer.decompose(project.project_id, "部署服务")
            self.assertEqual(collaboration.list_tasks(project.project_id), [])
            self.assertEqual(collaboration.list_goals(project.project_id), [])

    def test_cyclic_manual_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            collaboration, decomposer, project = build(Path(tmp), PlanGateway(PLAN))
            subtasks = [
                {"ref": "1", "title": "A", "description": "任务 A", "role_id": "analyst", "depends_on": ["2"]},
                {"ref": "2", "title": "B", "description": "任务 B", "role_id": "architect", "depends_on": ["1"]},
            ]

            with self.assertRaisesRegex(ValueError, "DAG"):
                decomposer.decompose(project.project_id, "循环依赖目标", subtasks=subtasks)
            self.assertEqual(collaboration.list_tasks(project.project_id), [])

    def test_manual_subtasks_skip_the_model(self):
        class StrictGateway:
            def complete(self, **kwargs):
                raise AssertionError("manual plan must not call the model")

        with tempfile.TemporaryDirectory() as tmp:
            collaboration, decomposer, project = build(Path(tmp), StrictGateway())

            result = decomposer.decompose(
                project.project_id,
                "手工拆分目标",
                subtasks=[{"ref": "only", "title": "唯一任务", "description": "完成条件：可验证", "role_id": "analyst"}],
            )

            self.assertEqual(len(result["task_ids"]), 1)
            self.assertEqual(result["state"]["counts"]["pending"], 1)

    def test_auto_coordinate_executes_the_split_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = RoleGateway({
                "summary": "先分析再设计",
                "subtasks": [
                    {"ref": "1", "title": "需求分析", "description": "澄清验收条件", "role_id": "analyst"},
                    {"ref": "2", "title": "方案设计", "description": "输出设计步骤", "role_id": "architect", "depends_on": ["1"]},
                ],
            })
            collaboration, decomposer, project = build(Path(tmp), gateway)

            result = decomposer.decompose(
                project.project_id, "梳理并设计", auto_coordinate=True
            )

            self.assertEqual(result["state"]["counts"]["completed"], 2)
            self.assertEqual(
                {task["status"] for task in result["state"]["tasks"]}, {"completed"}
            )

    def test_empty_goal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            collaboration, decomposer, project = build(Path(tmp), PlanGateway(PLAN))
            with self.assertRaisesRegex(ValueError, "goal cannot be empty"):
                decomposer.decompose(project.project_id, "   ")
            self.assertEqual(collaboration.list_tasks(project.project_id), [])


class GoalApiTest(unittest.TestCase):
    def test_goal_endpoints_create_list_and_delete(self):
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

            _, project = request("POST", "/api/v2/projects", {"name": "API"})
            project_id = project["project_id"]
            payload = {
                "goal": "完成登录功能",
                "subtasks": [
                    {"ref": "1", "title": "分析", "description": "澄清登录需求", "role_id": "analyst"},
                    {"ref": "2", "title": "实现", "description": "实现登录并通过验证", "role_id": "developer", "depends_on": ["1"]},
                ],
            }
            status, result = request("POST", f"/api/v2/projects/{project_id}/goals", payload)
            self.assertEqual(status, 201)
            self.assertEqual(len(result["task_ids"]), 2)
            goal_id = result["goal"]["goal_id"]

            _, state = request("GET", f"/api/v2/projects/{project_id}/collaboration")
            self.assertEqual(len(state["goals"]), 1)
            self.assertEqual(state["goals"][0]["goal_id"], goal_id)

            with self.assertRaises(urllib.error.HTTPError) as blocked:
                request("DELETE", f"/api/v2/goals/{goal_id}")
            self.assertEqual(blocked.exception.code, 400)

            for task_id in reversed(result["task_ids"]):
                request("DELETE", f"/api/v2/tasks/{task_id}")
            status, _ = request("DELETE", f"/api/v2/goals/{goal_id}")
            self.assertEqual(status, 200)
            _, state = request("GET", f"/api/v2/projects/{project_id}/collaboration")
            self.assertEqual(state["goals"], [])


    def test_task_model_override_beats_role_model(self):
        aliases: list[str] = []

        class CaptureGateway:
            def complete(self, *, model_alias, node, context):
                del context
                aliases.append(model_alias)
                if node.node_type == "requirement":
                    return {"understanding": "需求已明确", "acceptance_criteria": ["可验证"], "open_questions": []}
                raise AssertionError(f"unexpected node type: {node.node_type}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbench = WorkbenchService(root / "data", gateway=CaptureGateway())
            workbench.save_provider({
                "provider_id": "local",
                "label": "Local",
                "base_url": "http://127.0.0.1:11434/v1",
                "auth_type": "none",
            })
            for alias in ("role-model", "task-model"):
                workbench.save_model({"alias": alias, "provider_id": "local", "model": f"m-{alias}"})
            project = workbench.create_project("覆盖模型")
            collaboration = CollaborationService(
                root / "data",
                validate_role=workbench.validate_role,
                execute_task=workbench.execute_role_task,
                validate_project=workbench.get_project,
                validate_model=workbench.validate_model_alias,
            )
            collaboration.save_role({"role_id": "analyst", "model_alias": "role-model"})

            with self.assertRaisesRegex(ValueError, "unknown model alias: ghost"):
                collaboration.create_task(project.project_id, {
                    "title": "需求", "description": "整理需求", "role_id": "analyst", "model_alias": "ghost",
                })
            collaboration.create_task(project.project_id, {
                "title": "需求", "description": "整理需求", "role_id": "analyst", "model_alias": "task-model",
            })

            state = collaboration.coordinate(project.project_id)

            self.assertEqual(state["counts"]["completed"], 1)
            self.assertEqual(aliases, ["task-model"])


if __name__ == "__main__":
    unittest.main()
