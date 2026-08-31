from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.application.workbench import WorkbenchService
from app.domain.longhorizon import (
    LoopRound,
    audit_is_clean,
    normalize_audit,
    parse_manager_plan,
)
from app.domain.models import SessionMode, WorkflowDefinition, WorkflowNode


def planning_output() -> dict:
    return {"steps": ["实现功能"], "risks": [], "artifacts": {}}


def mgr(route: str, **overrides) -> dict:
    value = {
        "route": route,
        "task_state": {"completed": [], "incomplete": ["全部"], "risks": [], "untrusted": []},
        "task_contract": "实现功能并通过验证",
        "subtask": "",
        "acceptance_criteria": [],
        "related_rounds": [],
        "question": "",
    }
    value.update(overrides)
    return value


def audit(status="complete", integrity="clean", contract_audit="aligned", **overrides) -> dict:
    value = {
        "status": status,
        "integrity": integrity,
        "contract_audit": contract_audit,
        "facts": ["文件存在（round 1）"],
        "gaps": [],
        "blocking_constraints": [],
        "state_update": "功能已实现",
    }
    value.update(overrides)
    return value


def executor(changes="已完成子任务", files=None) -> dict:
    return {"changes": changes, "file_changes": files or [], "artifacts": {}, "decisions": []}


def review_pass() -> dict:
    return {"verdict": "pass", "issues": [], "decisions": []}


class ScriptedGateway:
    """Returns scripted outputs in call order; records every episode prompt."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(self, *, model_alias, node, context):
        self.calls.append({
            "node_type": node.node_type,
            "node_id": node.node_id,
            "prompt": node.prompt_template,
        })
        if not self.script:
            raise AssertionError(f"gateway script exhausted at {node.node_type}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class LongHorizonTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        self.workspace.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def make_service(self, script) -> tuple[WorkbenchService, ScriptedGateway, str]:
        gateway = ScriptedGateway(script)
        service = WorkbenchService(Path(self._tmp.name) / "root", gateway=gateway)
        project = service.create_project("demo", workspace_path=str(self.workspace))
        session = service.create_session(
            project.project_id, "长时程任务", mode=SessionMode.TASK, workflow_id="long-horizon-task",
        )
        service.send_message(session.session_id, "实现一个功能")
        return service, gateway, session.session_id

    def rounds(self, service: WorkbenchService, session_id: str) -> list[dict]:
        session = service.get_session(session_id)
        return [
            message.metadata["longhorizon_round"]
            for message in session.messages
            if isinstance(message.metadata.get("longhorizon_round"), dict)
        ]


class TestLongHorizonLoop(LongHorizonTestBase):
    def test_full_loop_completes_and_publishes_files(self):
        service, gateway, session_id = self.make_service([
            planning_output(),
            mgr("execute", subtask="创建文件", acceptance_criteria=["src/a.txt 存在"]),
            executor(files=[{"operation": "write", "path": "src/a.txt", "content": "hello"}]),
            audit(),
            mgr("done"),
            review_pass(),
        ])
        session = service.run_task(session_id)
        self.assertEqual(session.status, "completed")
        self.assertEqual((self.workspace / "src" / "a.txt").read_text(encoding="utf-8"), "hello")
        rounds = self.rounds(service, session_id)
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0]["route"], "execute")
        self.assertEqual(rounds[0]["audit"]["status"], "complete")
        self.assertEqual(rounds[1]["route"], "done")
        self.assertEqual(session.context.facts["rounds"], 2)
        self.assertTrue(session.context.facts["completed"])
        event_types = [json.loads(line)["event_type"] for line in service.events_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertIn("longhorizon_round", event_types)

    def test_incomplete_audit_triggers_repair_round(self):
        service, gateway, session_id = self.make_service([
            planning_output(),
            mgr("execute", subtask="第一版"),
            executor(changes="第一版完成"),
            audit("incomplete", gaps=["目标文件不存在"]),
            mgr("execute", subtask="修复", related_rounds=[1]),
            executor(files=[{"operation": "write", "path": "b.txt", "content": "ok"}]),
            audit(),
            mgr("done"),
            review_pass(),
        ])
        session = service.run_task(session_id)
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.context.facts["rounds"], 3)
        executor_prompts = [item["prompt"] for item in gateway.calls if item["node_type"] == "implementation"]
        self.assertIn("round_001", executor_prompts[1])
        self.assertIn("目标文件不存在", executor_prompts[1])

    def test_premature_done_is_rejected_with_feedback(self):
        service, gateway, session_id = self.make_service([
            planning_output(),
            mgr("done"),
            mgr("execute", subtask="实现"),
            executor(files=[{"operation": "write", "path": "c.txt", "content": "x"}]),
            audit(),
            mgr("done"),
            review_pass(),
        ])
        session = service.run_task(session_id)
        self.assertEqual(session.status, "completed")
        rounds = self.rounds(service, session_id)
        self.assertEqual(len(rounds), 3)
        self.assertEqual(rounds[0]["route"], "done")
        self.assertIn("禁止输出 route=done", rounds[0]["harness_feedback"])

    def test_invalid_plan_gets_protocol_feedback(self):
        service, gateway, session_id = self.make_service([
            planning_output(),
            {"route": "nonsense"},
            mgr("execute", subtask="实现"),
            executor(files=[{"operation": "write", "path": "d.txt", "content": "x"}]),
            audit(),
            mgr("done"),
            review_pass(),
        ])
        session = service.run_task(session_id)
        self.assertEqual(session.status, "completed")
        rounds = self.rounds(service, session_id)
        self.assertEqual(rounds[0]["route"], "invalid")
        self.assertIn("route 非法", rounds[0]["harness_feedback"])
        self.assertEqual(session.context.facts["rounds"], 3)

    def test_budget_exhaustion_blocks_then_resume_completes(self):
        gateway = ScriptedGateway([
            mgr("execute", subtask="第一轮"),
            executor(changes="部分完成"),
            audit("incomplete", gaps=["验证未通过"]),
            mgr("execute", subtask="第二轮"),
            executor(changes="仍然部分完成"),
            audit("incomplete", gaps=["目标文件仍不存在"]),
        ])
        service = WorkbenchService(Path(self._tmp.name) / "root2", gateway=gateway)
        project = service.create_project("demo", workspace_path=str(self.workspace))
        service.save_workflow(WorkflowDefinition(
            workflow_id="lh-single",
            label="单轮长时程",
            nodes=[WorkflowNode("lh", "long_horizon", config={"max_rounds": 2})],
        ))
        session = service.create_session(
            project.project_id, "预算任务", mode=SessionMode.TASK, workflow_id="lh-single",
        )
        service.send_message(session.session_id, "实现一个功能")
        blocked = service.run_task(session.session_id)
        self.assertEqual(blocked.status, "waiting_for_human")
        self.assertEqual(blocked.policy.gate, "longhorizon_rounds")
        self.assertEqual(blocked.policy.gate_status, "blocked")

        service.approve_policy(session.session_id)
        gateway.script.extend([
            mgr("execute", subtask="第三轮", related_rounds=[1, 2]),
            executor(files=[{"operation": "write", "path": "e.txt", "content": "done"}]),
            audit(),
            mgr("done"),
        ])
        resumed = service.run_task(session.session_id)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.context.facts["rounds"], 4)
        self.assertEqual((self.workspace / "e.txt").read_text(encoding="utf-8"), "done")
        rounds = self.rounds(service, session.session_id)
        self.assertEqual([item["index"] for item in rounds], [1, 2, 3, 4])

    def test_unsafe_path_fails_round_and_loop_recovers(self):
        service, gateway, session_id = self.make_service([
            planning_output(),
            mgr("execute", subtask="越权写入"),
            executor(files=[{"operation": "write", "path": "../evil.txt", "content": "x"}]),
            mgr("execute", subtask="安全写入"),
            executor(files=[{"operation": "write", "path": "f.txt", "content": "y"}]),
            audit(),
            mgr("done"),
            review_pass(),
        ])
        session = service.run_task(session_id)
        self.assertEqual(session.status, "completed")
        self.assertFalse((self.workspace.parent / "evil.txt").exists())
        self.assertEqual((self.workspace / "f.txt").read_text(encoding="utf-8"), "y")
        rounds = self.rounds(service, session_id)
        self.assertIn("越出工作区", rounds[0]["error"])
        self.assertIn("越出工作区", rounds[0]["harness_feedback"])

    def test_ask_route_blocks_for_human_input(self):
        gateway = ScriptedGateway([
            mgr("ask", question="需要确认目标数据库版本"),
        ])
        service = WorkbenchService(Path(self._tmp.name) / "root3", gateway=gateway)
        project = service.create_project("demo", workspace_path=str(self.workspace))
        service.save_workflow(WorkflowDefinition(
            workflow_id="lh-ask",
            label="问答长时程",
            nodes=[WorkflowNode("lh", "long_horizon", config={"max_rounds": 2})],
        ))
        session = service.create_session(
            project.project_id, "提问任务", mode=SessionMode.TASK, workflow_id="lh-ask",
        )
        service.send_message(session.session_id, "实现一个功能")
        blocked = service.run_task(session.session_id)
        self.assertEqual(blocked.status, "waiting_for_human")
        self.assertEqual(blocked.policy.gate, "longhorizon_input_needed")


class TestLongHorizonProtocol(unittest.TestCase):
    def test_normalize_audit_guards(self):
        guarded, well_formed = normalize_audit({
            "status": "complete", "integrity": "clean", "contract_audit": "aligned",
            "blocking_constraints": ["验证命令未通过"],
        })
        self.assertTrue(well_formed)
        self.assertEqual(guarded["status"], "incomplete")

        violated, _ = normalize_audit({
            "status": "complete", "integrity": "violation", "contract_audit": "aligned",
        })
        self.assertEqual(violated["status"], "incomplete")

        misaligned, _ = normalize_audit({
            "status": "complete", "integrity": "clean", "contract_audit": "unknown",
        })
        self.assertEqual(misaligned["status"], "incomplete")

        malformed, well_formed = normalize_audit({"unexpected": True})
        self.assertFalse(well_formed)
        self.assertEqual(malformed["status"], "incomplete")
        self.assertEqual(malformed["integrity"], "suspect")
        self.assertFalse(audit_is_clean(malformed))

        clean, _ = normalize_audit(audit())
        self.assertTrue(audit_is_clean(clean))

    def test_parse_manager_plan(self):
        plan, problem = parse_manager_plan(mgr("execute", subtask="任务"))
        self.assertEqual(problem, "")
        self.assertEqual(plan["route"], "execute")

        _, problem = parse_manager_plan({"route": "fly"})
        self.assertIn("route 非法", problem)
        _, problem = parse_manager_plan(mgr("execute"))
        self.assertIn("subtask", problem)
        _, problem = parse_manager_plan(mgr("ask"))
        self.assertIn("question", problem)
        _, problem = parse_manager_plan("not json")
        self.assertTrue(problem)

        plan, _ = parse_manager_plan(mgr("execute", subtask="任务", related_rounds=["1", 2, "x"]))
        self.assertEqual(plan["related_rounds"], [1, 2])


class TestLoopRoundPersistence(unittest.TestCase):
    def test_round_dict_roundtrip(self):
        rnd = LoopRound(
            index=3, route="execute",
            task_state={"completed": ["round 2 的事实"], "incomplete": [], "risks": [], "untrusted": []},
            task_contract="契约", subtask="子任务", executor_summary="摘要",
            applied_files=[{"path": "a.txt", "created": True}],
            audit=audit(), harness_feedback="",
        )
        restored = LoopRound.from_dict(rnd.to_dict())
        self.assertEqual(restored.index, 3)
        self.assertEqual(restored.task_state["completed"], ["round 2 的事实"])
        self.assertEqual(restored.audit["status"], "complete")
        self.assertEqual(restored.applied_files[0]["path"], "a.txt")


if __name__ == "__main__":
    unittest.main()
