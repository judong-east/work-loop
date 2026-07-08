from __future__ import annotations

import json
from pathlib import Path

from app.callbacks.event_bus import EventBus
from app.context.context_pack import ContextPackBuilder
from app.core.artifact_store import ArtifactStore
from app.core.contracts import (
    ModelRequest,
    ModelResponse,
    ModelRoutingConfig,
    PolicyBoundary,
    TaskState,
    TaskStatus,
)
from app.decision.decision_engine import DecisionEngine
from app.evaluation.evaluators import ContextEvaluator
from app.models.backends.base import ModelBackend
from app.models.router import ModelRouter
from app.policy.policy_checker import PolicyChecker

REVIEW_VERDICTS = {"pass", "revise", "block"}

PLANNER_PROMPT = "你是计划制定者。\n任务：{title}\n目标：{goal}\n上下文：\n{context}\n请输出实现该目标的分步计划。"
EXECUTOR_PROMPT = "你是执行者。\n目标：{goal}\n计划：\n{plan}\n请严格按计划产出执行结果。"
REVIEWER_PROMPT = (
    "你是独立审核者，与执行者不是同一模型。\n目标：{goal}\n计划：\n{plan}\n执行结果：\n{execution}\n"
    '请严格审核执行结果是否达成目标。只输出 JSON：{{"verdict": "pass|revise|block", "issues": ["问题列表"]}}'
)


def default_policy_boundary() -> PolicyBoundary:
    return PolicyBoundary(
        allowed_tools=["read_file", "search_code", "run_tests"],
        restricted_tools=["write_file", "db_migration"],
        forbidden_tools=["deploy_prod", "delete_data"],
        deny_paths=["**/.env", "**/secrets/**", "**/prod/**"],
    )


class WorkloopKernel:
    def __init__(self, root: Path):
        self.store = ArtifactStore(root / "tasks")
        self.context_builder = ContextPackBuilder()
        self.context_evaluator = ContextEvaluator()
        self.policy_checker = PolicyChecker()
        self.decision_engine = DecisionEngine()
        self.events = EventBus(self.store)

    def create_task(self, title: str, goal: str, raw_input: str, policy: PolicyBoundary | None = None) -> TaskState:
        boundary = policy or default_policy_boundary()

        task = TaskState(title=title, goal=goal, inputs=["input://inline"])
        # 所有工件引用统一存相对 task_dir 的正斜杠路径，任务目录整体迁移后仍可回放
        task_dir = self.store.task_dir(task.task_id)
        self.store.save_task(task)
        self.events.publish(task, "task.created", {"title": title, "goal": goal})

        task.transition(TaskStatus.CONTEXT_BUILDING)
        self.store.save_task(task)

        context = self.context_builder.build_from_text(
            task=task,
            purpose="requirement_analysis",
            raw_text=raw_input,
        )
        context_ref = f"contexts/{context.context_id}.json"
        self.store.write_json(task_dir / context_ref, context)
        task.context_refs.append(context_ref)
        self.events.publish(task, "context.built", {"context_id": context.context_id})

        evaluation = self.context_evaluator.evaluate(context)
        eval_ref = "evaluations/context_evaluation.json"
        self.store.write_json(task_dir / eval_ref, evaluation)
        task.evaluations.append(eval_ref)
        self.events.publish(task, "evaluation.completed", {"evaluator": evaluation.evaluator, "status": evaluation.status})

        confidence = evaluation.score
        policy_check = self.policy_checker.check_context(boundary, confidence, context.conflicts)
        policy_ref = "artifacts/policy_check.json"
        self.store.write_json(task_dir / policy_ref, policy_check)
        task.artifacts["policy_check"] = policy_ref

        decision = self.decision_engine.decide_after_context(task, evaluation, policy_check)
        decision_ref = "decisions/after_context.json"
        self.store.write_json(task_dir / decision_ref, decision)
        task.decisions.append(decision_ref)
        task.transition(decision.next_state)
        self.events.publish(
            task,
            "decision.made",
            {
                "action": decision.action.value,
                "next_state": decision.next_state.value,
                "reason": decision.reason,
            },
        )

        self.store.save_task(task)
        self.store.append_audit(task.task_id, "task.updated", {"status": task.status.value})
        return task

    def run_model_loop(
        self,
        task_id: str,
        routing: ModelRoutingConfig,
        backends: dict[str, ModelBackend],
        policy: PolicyBoundary | None = None,
    ) -> TaskState:
        task = self.store.load_task(task_id)
        if task.status is not TaskStatus.READY_FOR_PLAN:
            raise ValueError(f"任务 {task_id} 状态为 {task.status.value}，run-loop 要求 ready_for_plan。")
        boundary = policy or default_policy_boundary()
        task_dir = self.store.task_dir(task_id)

        assignment = self.policy_checker.check_model_assignment(boundary, routing)
        self.store.write_json(task_dir / "artifacts" / "model_assignment_check.json", assignment)
        task.artifacts["model_assignment_check"] = "artifacts/model_assignment_check.json"
        if not assignment.passed:
            task.transition(TaskStatus.POLICY_BLOCKED)
            self.events.publish(task, "policy.blocked", {"issues": assignment.issues})
            self.store.save_task(task)
            self.store.append_audit(task_id, "task.updated", {"status": task.status.value})
            return task

        router = ModelRouter(routing)
        context_text = self._load_context_text(task_dir)

        plan = self._invoke_role(
            task, router, backends, "planner", 1,
            PLANNER_PROMPT.format(title=task.title, goal=task.goal, context=context_text),
        )
        if not plan.succeeded:
            return self._fail(task, "planner", plan)
        self.store.write_text(task_dir / "artifacts" / "plan.md", plan.text)
        task.artifacts["plan"] = "artifacts/plan.md"
        task.transition(TaskStatus.READY_FOR_IMPLEMENTATION)
        self.store.save_task(task)

        execution = self._invoke_role(
            task, router, backends, "executor", 2,
            EXECUTOR_PROMPT.format(goal=task.goal, plan=plan.text),
        )
        if not execution.succeeded:
            return self._fail(task, "executor", execution)
        self.store.write_text(task_dir / "artifacts" / "execution.md", execution.text)
        task.artifacts["execution"] = "artifacts/execution.md"
        task.transition(TaskStatus.VALIDATION)
        self.store.save_task(task)

        review = self._invoke_role(
            task, router, backends, "reviewer", 3,
            REVIEWER_PROMPT.format(goal=task.goal, plan=plan.text, execution=execution.text),
        )
        if not review.succeeded:
            return self._fail(task, "reviewer", review)
        self.store.write_text(task_dir / "artifacts" / "review.json", review.text)
        task.artifacts["review"] = "artifacts/review.json"

        verdict = self._parse_review(review.text)
        if verdict is None:
            task.transition(TaskStatus.CLARIFICATION_REQUIRED)
            self.events.publish(task, "review.unparseable", {"raw_prefix": review.text[:200]})
        elif verdict["verdict"] == "pass":
            task.transition(TaskStatus.DONE)
            self.events.publish(task, "review.passed", {"issues": verdict["issues"]})
        else:
            task.transition(TaskStatus.CLARIFICATION_REQUIRED)
            self.events.publish(
                task, "review.rejected",
                {"verdict": verdict["verdict"], "issues": verdict["issues"]},
            )

        self.store.save_task(task)
        self.store.append_audit(task_id, "task.updated", {"status": task.status.value})
        return task

    def _load_context_text(self, task_dir: Path) -> str:
        parts: list[str] = []
        contexts_dir = task_dir / "contexts"
        for path in sorted(contexts_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for section in data.get("sections", []):
                parts.append(f"[{section.get('name', '')}] {section.get('content', '')}")
        return "\n".join(parts) if parts else "（无上下文）"

    def _invoke_role(
        self,
        task: TaskState,
        router: ModelRouter,
        backends: dict[str, ModelBackend],
        role: str,
        call_index: int,
        prompt: str,
    ) -> ModelResponse:
        profile, fallback = router.resolve(role)
        self.store.append_audit(
            task.task_id, "model.routed",
            {"role": role, "profile": profile.name, "model": profile.model, "fallback": fallback},
        )
        backend = backends.get(profile.provider)
        if backend is None:
            response = ModelResponse(
                text="", profile_name=profile.name, model=profile.model,
                duration_seconds=0.0, succeeded=False,
                error=f"没有可用的 {profile.provider} 后端。",
            )
        else:
            response = backend.invoke(profile, ModelRequest(task_id=task.task_id, role=role, prompt=prompt))

        call_dir = self.store.task_dir(task.task_id) / "artifacts" / "model_calls" / f"{call_index}-{role}"
        self.store.write_text(call_dir / "prompt.txt", prompt)
        self.store.write_text(call_dir / "response.txt", response.text if response.succeeded else response.error)
        self.store.write_json(call_dir / "meta.json", response)

        event_type = "model.invoked" if response.succeeded else "model.failed"
        self.events.publish(
            task, event_type,
            {"role": role, "profile": profile.name, "model": profile.model, "error": response.error},
        )
        return response

    def _fail(self, task: TaskState, role: str, response: ModelResponse) -> TaskState:
        task.transition(TaskStatus.FAILED)
        self.store.save_task(task)
        self.store.append_audit(
            task.task_id, "task.updated",
            {"status": task.status.value, "failed_role": role, "error": response.error},
        )
        return task

    def _parse_review(self, text: str) -> dict | None:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
            stripped = "\n".join(lines).strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("verdict") not in REVIEW_VERDICTS:
            return None
        return {"verdict": data["verdict"], "issues": [str(item) for item in data.get("issues", [])]}
