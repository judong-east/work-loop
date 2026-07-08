from __future__ import annotations

import json
from pathlib import Path

from app.callbacks.event_bus import EventBus
from app.context.context_pack import ContextPackBuilder
from app.core.artifact_store import ArtifactStore
from app.core.contracts import (
    CodeReviewIssue,
    CodeReviewResult,
    ContextPack,
    DecisionResult,
    FileChange,
    ModelRequest,
    ModelResponse,
    ModelRoutingConfig,
    PolicyBoundary,
    Severity,
    TaskState,
    TaskStatus,
    context_pack_from_dict,
    utc_now,
)
from app.decision.decision_engine import DecisionEngine
from app.evaluation.evaluators import ContextEvaluator
from app.models.backends.base import ModelBackend
from app.models.router import ModelRouter
from app.policy.policy_checker import PolicyChecker
from app.tools.workspace import CHANGE_ACTIONS, Workspace

REVIEW_VERDICTS = {"pass", "revise", "block"}

PLANNER_PROMPT = "你是计划制定者。\n任务：{title}\n目标：{goal}\n上下文：\n{context}\n请输出实现该目标的分步计划。"
EXECUTOR_PROMPT = (
    "你是执行者。\n目标：{goal}\n计划：\n{plan}\n当前工作区文件：\n{files}\n{review_feedback}"
    "请产出达成目标所需的文件变更。只输出 JSON："
    '{{"changes": [{{"path": "相对路径", "action": "write|delete", "content": "文件完整内容"}}], "notes": "说明"}}'
)
CODE_REVIEWER_PROMPT = (
    "你是独立代码审核者，与执行者不是同一模型，请独立严格审核。\n目标：{goal}\n计划：\n{plan}\n"
    "累计代码变更（统一 diff）：\n{diff}\n"
    "请审核变更是否正确、安全并达成目标。只输出 JSON："
    '{{"verdict": "pass|revise|block", "summary": "总体结论", '
    '"issues": [{{"file": "文件", "line": 0, "severity": "info|warning|blocker", '
    '"message": "问题描述", "suggestion": "修改建议"}}]}}'
)
RETRY_PROMPT_PREFIX = (
    "你上一次的输出无法解析（前 200 字：{raw_prefix}）。请严格只输出要求的 JSON，不要任何多余文字。\n\n"
)


def default_policy_boundary() -> PolicyBoundary:
    return PolicyBoundary(
        allowed_tools=["read_file", "search_code", "run_tests"],
        restricted_tools=["write_file", "db_migration"],
        forbidden_tools=["deploy_prod", "delete_data"],
        deny_paths=["**/.env", "**/secrets/**", "**/prod/**"],
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_file_changes(text: str) -> list[FileChange] | None:
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        return None
    changes: list[FileChange] = []
    for item in data["changes"]:
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        action = item.get("action", "write")
        content = item.get("content", "")
        if not isinstance(path, str) or not path or action not in CHANGE_ACTIONS or not isinstance(content, str):
            return None
        changes.append(FileChange(path=path, content=content, action=action))
    return changes


def _parse_code_review(text: str) -> CodeReviewResult | None:
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in REVIEW_VERDICTS:
        return None
    issues: list[CodeReviewIssue] = []
    for item in data.get("issues", []):
        # 真实模型输出常有字段缺失或松散类型，这里容错构造而不是整体拒绝。
        if isinstance(item, dict):
            try:
                severity = Severity(str(item.get("severity", "warning")))
            except ValueError:
                severity = Severity.WARNING
            try:
                line = int(item.get("line", 0))
            except (TypeError, ValueError):
                line = 0
            issues.append(
                CodeReviewIssue(
                    file=str(item.get("file", "")),
                    message=str(item.get("message", "")),
                    line=line,
                    severity=severity,
                    suggestion=str(item.get("suggestion", "")),
                )
            )
        else:
            issues.append(CodeReviewIssue(file="", message=str(item)))
    return CodeReviewResult(verdict=data["verdict"], issues=issues, summary=str(data.get("summary", "")))


def _format_review_feedback(review: CodeReviewResult | None) -> str:
    if review is None:
        return ""
    lines = ["上一轮审核未通过，需要修复以下问题："]
    if review.summary:
        lines.append(f"总体结论：{review.summary}")
    for issue in review.issues:
        location = f"{issue.file}:{issue.line}" if issue.file else "（未定位）"
        suffix = f"（建议：{issue.suggestion}）" if issue.suggestion else ""
        lines.append(f"- [{issue.severity.value}] {location} {issue.message}{suffix}")
    return "\n".join(lines) + "\n"


class WorkloopKernel:
    def __init__(self, root: Path):
        self.store = ArtifactStore(root / "tasks")
        self.context_builder = ContextPackBuilder()
        self.context_evaluator = ContextEvaluator()
        self.policy_checker = PolicyChecker()
        self.decision_engine = DecisionEngine()
        self.events = EventBus(self.store)

    def create_task(
        self,
        title: str,
        goal: str,
        raw_input: str,
        policy: PolicyBoundary | None = None,
        context_files: list[Path] | None = None,
    ) -> TaskState:
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
            context_files=context_files,
        )
        context_ref = f"contexts/{context.context_id}.json"
        self.store.write_json(task_dir / context_ref, context)
        task.context_refs.append(context_ref)
        self.events.publish(task, "context.built", {"context_id": context.context_id})

        self._gate_and_decide(task, task_dir, boundary, context)
        return self._finish(task)

    def resume_task(self, task_id: str, answer: str, policy: PolicyBoundary | None = None) -> TaskState:
        task = self.store.load_task(task_id)
        if task.status not in (TaskStatus.CLARIFICATION_REQUIRED, TaskStatus.POLICY_BLOCKED):
            raise ValueError(
                f"任务 {task_id} 状态为 {task.status.value}，resume 只接受 clarification_required/policy_blocked。"
            )
        boundary = policy or default_policy_boundary()
        task_dir = self.store.task_dir(task_id)

        answer_pack = self.context_builder.build_from_text(
            task=task,
            purpose="human_clarification",
            raw_text=answer,
            source_uri="input://human",
        )
        answer_ref = f"contexts/{answer_pack.context_id}.json"
        self.store.write_json(task_dir / answer_ref, answer_pack)
        task.context_refs.append(answer_ref)
        self.events.publish(task, "clarification.answered", {"answer_prefix": answer[:200]})

        # 门禁用合并视图重评：段落取全部历史上下文；缺失/冲突只看新答复的分析，
        # 人工答复是权威输入，旧告警视为已被回应。
        merged = ContextPack(
            task_id=task.task_id,
            purpose="resume_gate",
            sections=self._all_sections(task_dir),
            missing_context=list(answer_pack.missing_context),
            conflicts=list(answer_pack.conflicts),
        )
        self._gate_and_decide(task, task_dir, boundary, merged)
        return self._finish(task)

    def pending_questions(self, task_id: str) -> list[str]:
        task = self.store.load_task(task_id)
        task_dir = self.store.task_dir(task_id)
        questions: list[str] = []
        if task.decisions:
            decision = json.loads((task_dir / task.decisions[-1]).read_text(encoding="utf-8"))
            questions.extend(str(item) for item in decision.get("required_inputs", []))
        review_ref = task.artifacts.get("code_review", "")
        if review_ref and (task_dir / review_ref).exists():
            review = json.loads((task_dir / review_ref).read_text(encoding="utf-8"))
            if review.get("verdict") in ("revise", "block"):
                if review.get("summary"):
                    questions.append(str(review["summary"]))
                for issue in review.get("issues", []):
                    if isinstance(issue, dict):
                        prefix = f"{issue.get('file', '')}: " if issue.get("file") else ""
                        questions.append(f"{prefix}{issue.get('message', '')}")
                    else:
                        questions.append(str(issue))
        return [q for q in questions if q]

    def run_model_loop(
        self,
        task_id: str,
        routing: ModelRoutingConfig,
        backends: dict[str, ModelBackend],
        policy: PolicyBoundary | None = None,
        workspace_from: Path | None = None,
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
            return self._finish(task)

        workspace = Workspace(task_dir / "workspace")
        if workspace_from is not None:
            source = Path(workspace_from)
            if not source.is_dir():
                raise ValueError(f"播种目录 {source} 不存在。")
            seeded = workspace.seed(source)
            self.events.publish(task, "workspace.seeded", {"source": str(source), "files": seeded})
        base = workspace.snapshot()
        self.store.write_json(task_dir / "artifacts" / "workspace_base.json", base)
        task.artifacts["workspace_base"] = "artifacts/workspace_base.json"

        router = ModelRouter(routing)
        context_text = self._load_context_text(task_dir)
        counter = [0]

        plan = self._invoke_role(
            task, router, backends, "planner", self._next_index(counter),
            PLANNER_PROMPT.format(title=task.title, goal=task.goal, context=context_text),
        )
        if not plan.succeeded:
            return self._fail(task, "planner", plan)
        self.store.write_text(task_dir / "artifacts" / "plan.md", plan.text)
        task.artifacts["plan"] = "artifacts/plan.md"
        task.transition(TaskStatus.READY_FOR_IMPLEMENTATION)
        self.store.save_task(task)

        last_review: CodeReviewResult | None = None

        for round_index in range(1, boundary.max_iterations + 1):
            task.iteration = round_index
            round_ref = f"artifacts/rounds/{round_index}"

            execution, changes = self._invoke_and_parse(
                task, router, backends, "executor", counter,
                EXECUTOR_PROMPT.format(
                    goal=task.goal,
                    plan=plan.text,
                    files="\n".join(sorted(workspace.snapshot())) or "（空）",
                    review_feedback=_format_review_feedback(last_review),
                ),
                _parse_file_changes,
            )
            if not execution.succeeded:
                return self._fail(task, "executor", execution)
            if changes is None:
                task.transition(TaskStatus.CLARIFICATION_REQUIRED)
                self.events.publish(task, "execution.unparseable", {"round": round_index, "raw_prefix": execution.text[:200]})
                return self._finish(task)
            self.store.write_json(task_dir / round_ref / "changes.json", changes)
            task.artifacts["changes"] = f"{round_ref}/changes.json"

            change_check = workspace.validate(changes, boundary, self.policy_checker)
            self.store.write_json(task_dir / round_ref / "policy_check.json", change_check)
            if not change_check.passed:
                task.transition(TaskStatus.POLICY_BLOCKED)
                self.events.publish(task, "policy.blocked", {"round": round_index, "issues": change_check.issues})
                return self._finish(task)

            applied = workspace.apply(changes)
            self.events.publish(task, "execution.applied", {"round": round_index, "files": applied})

            diff = workspace.diff(base, workspace.snapshot())
            self.store.write_text(task_dir / round_ref / "changes.diff", diff)
            task.artifacts["diff"] = f"{round_ref}/changes.diff"
            if task.status is not TaskStatus.VALIDATION:
                task.transition(TaskStatus.VALIDATION)
            self.store.save_task(task)

            review_response, review = self._invoke_and_parse(
                task, router, backends, "reviewer", counter,
                CODE_REVIEWER_PROMPT.format(goal=task.goal, plan=plan.text, diff=diff or "（无变更）"),
                _parse_code_review,
            )
            if not review_response.succeeded:
                return self._fail(task, "reviewer", review_response)
            if review is None:
                task.transition(TaskStatus.CLARIFICATION_REQUIRED)
                self.events.publish(task, "review.unparseable", {"round": round_index, "raw_prefix": review_response.text[:200]})
                return self._finish(task)
            self.store.write_json(task_dir / round_ref / "review.json", review)
            task.artifacts["code_review"] = f"{round_ref}/review.json"

            issue_payload = [f"{issue.file}: {issue.message}" for issue in review.issues]
            if review.verdict == "pass":
                task.transition(TaskStatus.DONE)
                self.events.publish(task, "review.passed", {"round": round_index, "issues": issue_payload})
                return self._finish(task)
            if review.verdict == "block":
                task.transition(TaskStatus.CLARIFICATION_REQUIRED)
                self.events.publish(task, "review.rejected", {"round": round_index, "verdict": "block", "issues": issue_payload})
                return self._finish(task)

            self.events.publish(task, "review.rejected", {"round": round_index, "verdict": "revise", "issues": issue_payload})
            self.store.save_task(task)
            last_review = review

        task.transition(TaskStatus.CLARIFICATION_REQUIRED)
        self.events.publish(task, "review.exhausted", {"iterations": boundary.max_iterations})
        return self._finish(task)

    def pending_delivery(self, task_id: str) -> list[FileChange]:
        task = self.store.load_task(task_id)
        if task.status is not TaskStatus.DONE:
            raise ValueError(f"任务 {task_id} 状态为 {task.status.value}，deliver 只接受 done。")
        task_dir = self.store.task_dir(task_id)
        workspace = Workspace(task_dir / "workspace")
        return workspace.changes_since(self._load_workspace_base(task_dir))

    def deliver(self, task_id: str, dest: Path, policy: PolicyBoundary | None = None) -> list[FileChange]:
        task = self.store.load_task(task_id)
        changes = self.pending_delivery(task_id)
        if not changes:
            return []
        boundary = policy or default_policy_boundary()
        task_dir = self.store.task_dir(task_id)

        target = Workspace(Path(dest))
        check = target.validate(changes, boundary, self.policy_checker)
        if not check.passed:
            self.store.append_audit(task_id, "delivery.blocked", {"dest": str(dest), "issues": check.issues})
            raise ValueError("交付被策略拦截：" + "；".join(check.issues))

        applied = target.apply(changes)
        self.store.write_json(
            task_dir / "artifacts" / "delivery.json",
            {"dest": str(Path(dest).resolve()), "files": applied, "delivered_at": utc_now()},
        )
        task.artifacts["delivery"] = "artifacts/delivery.json"
        self.events.publish(task, "delivery.completed", {"dest": str(dest), "files": applied})
        self._finish(task)
        return changes

    def _gate_and_decide(self, task: TaskState, task_dir: Path, boundary: PolicyBoundary, pack: ContextPack) -> DecisionResult:
        index = len(task.evaluations) + 1

        evaluation = self.context_evaluator.evaluate(pack)
        eval_ref = f"evaluations/context_evaluation_{index}.json"
        self.store.write_json(task_dir / eval_ref, evaluation)
        task.evaluations.append(eval_ref)
        self.events.publish(task, "evaluation.completed", {"evaluator": evaluation.evaluator, "status": evaluation.status})

        policy_check = self.policy_checker.check_context(boundary, evaluation.score, pack.conflicts)
        policy_ref = f"artifacts/policy_check_{index}.json"
        self.store.write_json(task_dir / policy_ref, policy_check)
        task.artifacts["policy_check"] = policy_ref

        decision = self.decision_engine.decide_after_context(task, evaluation, policy_check)
        decision_ref = f"decisions/after_context_{index}.json"
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
        return decision

    def _all_sections(self, task_dir: Path) -> list:
        sections = []
        for path in sorted((task_dir / "contexts").glob("*.json")):
            pack = context_pack_from_dict(json.loads(path.read_text(encoding="utf-8")))
            sections.extend(pack.sections)
        return sections

    def _load_workspace_base(self, task_dir: Path) -> dict[str, str]:
        base_path = task_dir / "artifacts" / "workspace_base.json"
        if not base_path.exists():
            return {}
        data = json.loads(base_path.read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items()}

    def _finish(self, task: TaskState) -> TaskState:
        self.store.save_task(task)
        self.store.append_audit(task.task_id, "task.updated", {"status": task.status.value})
        return task

    def _load_context_text(self, task_dir: Path) -> str:
        parts: list[str] = []
        contexts_dir = task_dir / "contexts"
        for path in sorted(contexts_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for section in data.get("sections", []):
                parts.append(f"[{section.get('name', '')}] {section.get('content', '')}")
        return "\n".join(parts) if parts else "（无上下文）"

    def _next_index(self, counter: list[int]) -> int:
        counter[0] += 1
        return counter[0]

    def _invoke_and_parse(
        self,
        task: TaskState,
        router: ModelRouter,
        backends: dict[str, ModelBackend],
        role: str,
        counter: list[int],
        prompt: str,
        parser,
    ) -> tuple[ModelResponse, object | None]:
        response = self._invoke_role(task, router, backends, role, self._next_index(counter), prompt)
        if not response.succeeded:
            return response, None
        parsed = parser(response.text)
        if parsed is not None:
            return response, parsed
        # 真实 CLI 常混入散文，给一次带纠错提示的重试机会
        retry_prompt = RETRY_PROMPT_PREFIX.format(raw_prefix=response.text[:200]) + prompt
        response = self._invoke_role(task, router, backends, role, self._next_index(counter), retry_prompt)
        if not response.succeeded:
            return response, None
        return response, parser(response.text)

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
