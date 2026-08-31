"""Long-horizon multi-round execution loop for Workloop V2.

Adapts the Manager -> Executor -> Auditor loop protocol from
AMAP-ML/LongHorizon-Harness (MIT License, Copyright (c) 2026
LongHorizon-Harness contributors).  The protocol is re-expressed as JSON
contracts so it flows through the existing model gateway and the atomic
workspace publish path:

- the Manager plans one subtask per round and maintains a durable task state
  plus a stable task contract; every claimed fact must cite an audit round;
- the Executor runs one fresh-context episode per round and proposes file
  writes through the standard ``file_changes`` contract;
- the Auditor independently verifies the round and returns a three-field
  verdict (``status`` / ``integrity`` / ``contract_audit``);
- a ``done`` route is only accepted when the latest audit is clean, and every
  protocol violation is fed back to the Manager as harness feedback instead of
  silently ending the run.

Rounds are persisted as session event messages (``metadata.longhorizon_round``)
so an approved gate can resume the loop where it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.models import Session, WorkflowNode
from app.domain.orchestrator import ModelGateway, OrchestrationEvent


ROUTES = ("execute", "done", "blocked", "ask")
AUDIT_STATUS = ("complete", "incomplete", "blocked")
AUDIT_INTEGRITY = ("clean", "suspect", "violation")
AUDIT_CONTRACT = ("aligned", "unknown", "needs_revision", "invalid")

DEFAULT_MAX_ROUNDS = 8
MAX_ROUNDS = 25
MAX_TOTAL_ROUNDS = 100
DEFAULT_EXECUTOR_OUTPUT_CHARS = 24_000
AUDIT_HISTORY_REPORT_CHARS = 4_000
SUBTASK_CLIP_CHARS = 1_600

INVALID_COMPLETION_FEEDBACK = (
    "协议反馈：你请求结束任务，但最近一次审计报告没有同时满足 "
    "status=complete、integrity=clean、contract_audit=aligned。"
    "在获得干净的审计结论之前禁止输出 route=done；请派发一个可被审计验证的收尾子任务，"
    "或请求一次只针对剩余验收约束的确认性审计。"
)


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _clip_preserve(text: str, limit: int) -> str:
    """Head/tail preserving truncation: keep 65% head and 35% tail."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(1, int(limit * 0.65))
    tail = max(1, limit - head)
    return f"{text[:head]}\n…（中间 {len(text) - limit} 字符已截断）…\n{text[-tail:]}"


def _string_list(value: Any, *, limit: int = 50) -> list[str]:
    if isinstance(value, str):
        value = [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()][:limit]


def normalize_task_state(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {key: _string_list(value.get(key)) for key in ("completed", "incomplete", "risks", "untrusted")}


def format_task_state(state: dict[str, list[str]]) -> str:
    if not state or not any(state.values()):
        return "（尚无维护的任务状态。）"
    labels = {"completed": "已完成", "incomplete": "未完成", "risks": "风险", "untrusted": "不可信/禁止复用"}
    return "\n".join(
        f"- {labels[key]}：{'；'.join(items) if items else '（无）'}"
        for key, items in state.items()
    )


def parse_manager_plan(output: Any) -> tuple[dict[str, Any] | None, str]:
    """Validate a Manager episode output; ``(None, problem)`` marks a violation."""
    if not isinstance(output, dict):
        return None, "管理输出必须是 JSON 对象"
    route = str(output.get("route", "")).strip().lower()
    if route not in ROUTES:
        return None, f"route 非法：{route!r}，必须是 execute/done/blocked/ask"
    task_state = normalize_task_state(output.get("task_state"))
    task_contract = str(output.get("task_contract", "")).strip()
    subtask = str(output.get("subtask", "")).strip()
    question = str(output.get("question", "")).strip()
    if route == "execute" and not subtask:
        return None, "route=execute 需要非空的 subtask"
    if route == "ask" and not question:
        return None, "route=ask 需要非空的 question"
    acceptance = _string_list(output.get("acceptance_criteria"))
    related: list[int] = []
    raw_related = output.get("related_rounds", [])
    if isinstance(raw_related, (int, str)):
        raw_related = [raw_related]
    if isinstance(raw_related, list):
        for item in raw_related:
            try:
                related.append(int(item))
            except (TypeError, ValueError):
                continue
    return {
        "route": route,
        "task_state": task_state,
        "task_contract": task_contract,
        "subtask": subtask,
        "acceptance_criteria": acceptance,
        "related_rounds": related,
        "question": question,
    }, ""


def normalize_audit(output: Any) -> tuple[dict[str, Any], bool]:
    """Normalize an Auditor episode output and apply the hard verdict guards.

    The second element reports whether the raw output was well formed; a
    malformed report is downgraded to an incomplete/suspect verdict the same
    way lh-harness synthesizes an invalid control header report.
    """
    if not isinstance(output, dict):
        return _suspect_audit(["审计输出不是 JSON 对象"]), False
    status = str(output.get("status", "")).strip().lower()
    integrity = str(output.get("integrity", "")).strip().lower()
    contract = str(output.get("contract_audit", "")).strip().lower()
    well_formed = status in AUDIT_STATUS and integrity in AUDIT_INTEGRITY and contract in AUDIT_CONTRACT
    if status not in AUDIT_STATUS:
        status = "incomplete"
    if integrity not in AUDIT_INTEGRITY:
        integrity = "suspect"
    if contract not in AUDIT_CONTRACT:
        contract = "unknown"
    facts = _string_list(output.get("facts"))
    gaps = _string_list(output.get("gaps"))
    blocking = _string_list(output.get("blocking_constraints"))
    state_update = str(output.get("state_update", "")).strip()
    # Hard guards: a verdict can never be complete over unresolved blocking
    # constraints, a violation, or an unaligned contract audit.
    if blocking and status == "complete":
        status = "incomplete"
    if status == "complete" and (integrity == "violation" or contract != "aligned"):
        status = "incomplete"
    return {
        "status": status,
        "integrity": integrity,
        "contract_audit": contract,
        "facts": facts,
        "gaps": gaps,
        "blocking_constraints": blocking,
        "state_update": state_update,
    }, well_formed


def _suspect_audit(gaps: list[str]) -> dict[str, Any]:
    return {
        "status": "incomplete", "integrity": "suspect", "contract_audit": "unknown",
        "facts": [], "gaps": gaps, "blocking_constraints": [], "state_update": "",
    }


def audit_is_clean(audit: dict[str, Any] | None) -> bool:
    return bool(audit) and (
        audit["status"] == "complete" and audit["integrity"] == "clean" and audit["contract_audit"] == "aligned"
    )


@dataclass
class LoopRound:
    """One Manager decision round; persisted as a session event message."""

    index: int
    route: str
    task_state: dict[str, list[str]] = field(default_factory=dict)
    task_contract: str = ""
    subtask: str = ""
    executor_summary: str = ""
    applied_files: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] | None = None
    harness_feedback: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "route": self.route,
            "task_state": {key: list(items) for key, items in self.task_state.items()},
            "task_contract": self.task_contract,
            "subtask": self.subtask,
            "executor_summary": self.executor_summary,
            "applied_files": [dict(item) for item in self.applied_files],
            "audit": dict(self.audit) if self.audit else None,
            "harness_feedback": self.harness_feedback,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LoopRound":
        audit = value.get("audit")
        return cls(
            index=int(value["index"]),
            route=str(value.get("route", "invalid")),
            task_state=normalize_task_state(value.get("task_state")),
            task_contract=str(value.get("task_contract", "")),
            subtask=str(value.get("subtask", "")),
            executor_summary=str(value.get("executor_summary", "")),
            applied_files=[dict(item) for item in value.get("applied_files", []) if isinstance(item, dict)],
            audit=dict(audit) if isinstance(audit, dict) else None,
            harness_feedback=str(value.get("harness_feedback", "")),
            error=str(value.get("error", "")),
        )


def format_audit_history(rounds: list[LoopRound], *, only_rounds: list[int] | None = None) -> str:
    selected = [
        item for item in rounds
        if item.audit is not None and (only_rounds is None or item.index in only_rounds)
    ]
    if not selected:
        return ""
    blocks = []
    for item in selected:
        audit = item.audit or {}
        facts = "；".join(audit.get("facts", []))
        gaps = "；".join(audit.get("gaps", []))
        blocks.append(
            "--- 轮次 {index} 审计报告 ---\n"
            "round_{index:03d}\n"
            "子任务：{subtask}\n"
            "结论：status={status}，integrity={integrity}，contract_audit={contract}\n"
            "审计事实：{facts}\n"
            "缺口：{gaps}\n"
            "状态更新建议：{update}".format(
                index=item.index,
                subtask=_clip_preserve(item.subtask, SUBTASK_CLIP_CHARS),
                status=audit.get("status", ""),
                integrity=audit.get("integrity", ""),
                contract=audit.get("contract_audit", ""),
                facts=_clip_preserve(facts, AUDIT_HISTORY_REPORT_CHARS),
                gaps=_clip_preserve(gaps, 1_000),
                update=_clip_preserve(audit.get("state_update", ""), 1_000),
            )
        )
    return "\n\n".join(blocks)


def build_manager_prompt(
    *,
    request: str,
    task_state: dict[str, list[str]],
    task_contract: str,
    audit_history: str,
    harness_feedback: str,
    round_index: int,
    max_rounds: int,
    workspace_root: str,
) -> str:
    remaining = max_rounds - round_index + 1
    return f"""你是长时程任务的管理者（Manager）。你不亲自执行任务，只负责拆解、路由与状态维护。每一轮你都会收到全新的上下文，唯一可信的进度来源是历史审计报告。

管理规则：
- 每轮必须输出完整的"任务状态" task_state：completed（已确认完成的事项）、incomplete（未完成事项）、risks（风险）、untrusted（不可信、禁止复用的产物）。completed 中的每一条都必须注明来源审计轮次（例如 "round 2"）；没有审计证据的事项只能写入 incomplete 或 untrusted，绝不能把执行者的未审计声明当作进展。
- 任务契约 task_contract 是跨轮稳定的目标描述：最终成功状态、验收约束、状态载体（哪些文件或结果承载成果）、可接受的证据、不可接受的捷径。除非发现契约本身有误，不要改写它；确需修订时在原契约后追加修订说明。
- 每轮只派发一个当前最重要的子任务 subtask，并给出可检验的 acceptance_criteria。
- related_rounds 只引用与该子任务相关的历史审计轮次编号。

输出协议：只输出一个 JSON 对象：
{{"route": "execute|done|blocked|ask", "task_state": {{"completed": [], "incomplete": [], "risks": [], "untrusted": []}}, "task_contract": "string", "subtask": "string", "acceptance_criteria": ["string"], "related_rounds": [1], "question": "仅 route=ask 时需要"}}
- route=execute：把子任务派发给执行者。
- route=done：任务完成。必须引用审计证据证明全部验收约束已满足；最近一次审计必须干净，否则结束请求会被驳回。
- route=blocked：无法继续推进，在 subtask 字段说明原因。
- route=ask：需要人工输入，给出 question。

原始任务：
{request}

上一轮任务状态：
{format_task_state(task_state)}

当前任务契约：
{task_contract or "（尚无契约，请在本轮从原始任务初始化。）"}

历史审计报告（可信中间状态的唯一来源）：
{audit_history or "（暂无审计报告。）"}

调度器反馈（协议纠错，不是审计结果）：
{harness_feedback or "（无。）"}

工作区根目录：{workspace_root or "（未配置）"}

轮次预算：本轮是第 {round_index} 轮，本次调用还剩 {remaining} 轮。只剩最后一轮时，不要派发只做前置准备的子任务；应派发能最大程度推进核心需求的工作，或在无法完成时输出 blocked。

只输出下一个管理结果 JSON。"""


def build_executor_prompt(
    *,
    request: str,
    task_state: dict[str, list[str]],
    task_contract: str,
    subtask: str,
    acceptance_criteria: list[str],
    related_reports: str,
    workspace_root: str,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in acceptance_criteria) or "-（管理者未给出，依据子任务自证。）"
    return f"""你是长时程任务的执行者（Executor）。本轮你只执行一个子任务；你没有历史对话记忆，与其他轮次的执行者互不可见。

执行规则：
- 只完成下面给出的子任务。不要重复已审计完成的工作，不要使用 untrusted 产物。
- 所有文件写入必须通过 file_changes 提交完整文件内容，路径必须是工作区相对路径。永远不要声称修改了未包含在 file_changes 中的文件。
- 如实报告：changes 字段描述你实际做了什么、观察到什么，供审计者独立核验。不要编造测试结果或文件状态。
- 如果上下文缺失或子任务无法完成，在 changes 中如实说明并停止，不要猜测或擅自扩大范围。

原始任务：
{request}

管理者的任务状态（事实均来自审计）：
{format_task_state(task_state)}

任务契约：
{task_contract or "（无独立契约，以子任务为准。）"}

本轮子任务：
{subtask}

验收标准：
{acceptance}

相关历史审计报告：
{related_reports or "（管理者未引用相关审计报告。）"}

工作区根目录：{workspace_root or "（未配置）"}

只输出一个 JSON 对象：
{{"changes": "string", "file_changes": [{{"operation": "write", "path": "relative/path", "content": "完整文件内容"}}], "artifacts": {{}}, "decisions": ["string"]}}"""


def build_auditor_prompt(
    *,
    request: str,
    task_state: dict[str, list[str]],
    task_contract: str,
    subtask: str,
    acceptance_criteria: list[str],
    executor_summary: str,
    applied_files: list[dict[str, Any]],
    workspace_root: str,
    round_index: int,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in acceptance_criteria) or "-（未给出，依据子任务自证。）"
    applied = "\n".join(
        f"- {item.get('path', '')}（{'新建' if item.get('created') else '修改'}，{item.get('characters', 0)} 字符）"
        for item in applied_files
    ) or "-（本轮没有应用任何文件写入。）"
    return f"""你是长时程任务的审计者（Auditor）。你独立、只读地核验执行者本轮子任务是否真实完成。执行者的报告只是"声称"，不是证据。

审计规则：
- 独立核验：把执行者的声称与工作区当前状态（文件列表、内容、验证结果）对照。无法核验的事项写入 facts 并在 gaps 中说明。
- blocking_constraints：列出阻止本轮被判定完成的验收约束（例如验证命令未通过、验收标准未满足、发现伪造内容）。只要存在未满足的验收约束，status 就不能是 complete。
- 发现伪造、越权修改或不可信产物时 integrity=violation，并在 facts 中说明；不要试图修复它们。
- state_update：给管理者的状态更新建议，说明哪些事实现在可信（引用本轮编号 round {round_index}）。

原始任务：
{request}

本轮子任务：
{subtask}

验收标准：
{acceptance}

任务契约（参考，不假定其正确）：
{task_contract or "（无独立契约。）"}

管理者任务状态（仅作背景，不作为本轮证据）：
{format_task_state(task_state)}

执行者报告：
{executor_summary}

调度器记录的已应用文件：
{applied}

工作区根目录：{workspace_root or "（未配置）"}

只输出一个 JSON 对象：
{{"status": "complete|incomplete|blocked", "integrity": "clean|suspect|violation", "contract_audit": "aligned|unknown|needs_revision", "facts": ["string"], "gaps": ["string"], "blocking_constraints": ["string"], "state_update": "string"}}"""


class LongHorizonLoop:
    """The Manager/Executor/Auditor loop behind the ``long_horizon`` node.

    The loop lives entirely inside the node handler: the orchestrator sees a
    single NodeRun, while each round is persisted as a session event message
    so an approved gate can resume the loop where it stopped.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        workspace_runtime: Any,
        *,
        event_sink: Any = None,
        store: Any = None,
    ):
        self.gateway = gateway
        self.workspace_runtime = workspace_runtime
        self.event_sink = event_sink
        self.store = store

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        session: Session = payload["session"]
        node: WorkflowNode = payload["node"]
        context = payload["context"]
        config = node.config if isinstance(node.config, dict) else {}
        max_rounds = _clamp_int(config.get("max_rounds"), DEFAULT_MAX_ROUNDS, 1, MAX_ROUNDS)
        executor_chars = _clamp_int(
            config.get("executor_output_chars"), DEFAULT_EXECUTOR_OUTPUT_CHARS, 1_000, 200_000
        )
        aliases = {
            "manager": str(config.get("manager_model_alias", "") or node.model_alias),
            "executor": str(config.get("executor_model_alias", "") or node.model_alias),
            "auditor": str(config.get("auditor_model_alias", "") or node.model_alias),
        }

        rounds = self._rebuild_rounds(session, node.node_id)
        start = len(rounds)
        if start >= MAX_TOTAL_ROUNDS:
            return self._final_output(rounds, completed=False, gate={
                "name": "longhorizon_rounds", "status": "blocked",
                "reason": f"累计轮次已达上限 {MAX_TOTAL_ROUNDS}，需要人工重开任务。",
            })

        completed = False
        gate: dict[str, Any] | None = None
        project = context.inputs.get("project", {})
        workspace_root = str(project.get("workspace_path", "")) if isinstance(project, dict) else ""
        request = str(context.inputs.get("request", "") or session.title)

        for offset in range(max_rounds):
            index = start + offset + 1
            feedback = rounds[-1].harness_feedback if rounds and rounds[-1].harness_feedback else ""
            prompt = build_manager_prompt(
                request=request,
                task_state=rounds[-1].task_state if rounds else {},
                task_contract=rounds[-1].task_contract if rounds else "",
                audit_history=format_audit_history(rounds),
                harness_feedback=feedback,
                round_index=index,
                max_rounds=max_rounds,
                workspace_root=workspace_root,
            )
            try:
                raw_plan = self._episode(session, node, context, "longhorizon_manager", prompt, aliases["manager"], index)
            except Exception as error:
                rounds.append(LoopRound(
                    index=index, route="invalid",
                    task_state=rounds[-1].task_state if rounds else {},
                    task_contract=rounds[-1].task_contract if rounds else "",
                    error=f"管理阶段调用失败：{error}",
                    harness_feedback=f"上一轮管理阶段调用失败（{error}），请重新输出完整管理结果 JSON。",
                ))
                self._persist(session, node, rounds[-1])
                continue
            plan, problem = parse_manager_plan(raw_plan)
            if plan is None:
                rounds.append(LoopRound(
                    index=index, route="invalid",
                    task_state=rounds[-1].task_state if rounds else {},
                    task_contract=rounds[-1].task_contract if rounds else "",
                    error=problem,
                    harness_feedback=f"协议反馈：{problem}。请重新输出完整管理结果 JSON。",
                ))
                self._persist(session, node, rounds[-1])
                continue

            task_state = plan["task_state"] or (rounds[-1].task_state if rounds else {})
            task_contract = plan["task_contract"] or (rounds[-1].task_contract if rounds else "")
            route = plan["route"]

            if route == "done":
                audited = [item for item in rounds if item.audit is not None]
                latest = audited[-1] if audited else None
                if latest is not None and latest.index == index - 1 and audit_is_clean(latest.audit):
                    rounds.append(LoopRound(index=index, route="done", task_state=task_state, task_contract=task_contract))
                    self._persist(session, node, rounds[-1])
                    completed = True
                    break
                rounds.append(LoopRound(
                    index=index, route="done", task_state=task_state, task_contract=task_contract,
                    harness_feedback=INVALID_COMPLETION_FEEDBACK,
                ))
                self._persist(session, node, rounds[-1])
                continue

            if route == "blocked":
                rounds.append(LoopRound(
                    index=index, route="blocked", task_state=task_state, task_contract=task_contract,
                    subtask=plan["subtask"],
                ))
                self._persist(session, node, rounds[-1])
                gate = {"name": "longhorizon_blocked", "status": "blocked", "reason": plan["subtask"]}
                break

            if route == "ask":
                rounds.append(LoopRound(
                    index=index, route="ask", task_state=task_state, task_contract=task_contract,
                ))
                self._persist(session, node, rounds[-1])
                gate = {"name": "longhorizon_input_needed", "status": "blocked", "reason": plan["question"]}
                break

            # route == "execute": one fresh-context executor episode, then one
            # read-only auditor episode for the same round.
            rounds.append(LoopRound(
                index=index, route="execute", task_state=task_state, task_contract=task_contract,
                subtask=plan["subtask"],
            ))
            current = rounds[-1]
            related = format_audit_history(rounds[:-1], only_rounds=plan["related_rounds"] or None)
            try:
                executor_prompt = build_executor_prompt(
                    request=request,
                    task_state=task_state,
                    task_contract=task_contract,
                    subtask=plan["subtask"],
                    acceptance_criteria=plan["acceptance_criteria"],
                    related_reports=related,
                    workspace_root=workspace_root,
                )
                raw_executor = self._episode(
                    session, node, context, "implementation", executor_prompt, aliases["executor"], index, role="executor",
                )
                executor_node = WorkflowNode(f"{node.node_id}:executor:{index}", "implementation")
                processed = self.workspace_runtime.process_output(session, executor_node, context, raw_executor)
            except Exception as error:
                current.error = f"执行阶段失败：{error}"
                current.harness_feedback = (
                    f"上一轮执行阶段失败（{error}）。请根据失败原因调整子任务或拆分工作。"
                )
                self._persist(session, node, current)
                continue
            context = context.merge(processed)
            current.executor_summary = _clip_preserve(str(processed.get("changes", "")), executor_chars)
            current.applied_files = [dict(item) for item in processed.get("applied_files", [])]

            audit_feedback = ""
            try:
                audit_prompt = build_auditor_prompt(
                    request=request,
                    task_state=task_state,
                    task_contract=task_contract,
                    subtask=plan["subtask"],
                    acceptance_criteria=plan["acceptance_criteria"],
                    executor_summary=current.executor_summary,
                    applied_files=current.applied_files,
                    workspace_root=workspace_root,
                    round_index=index,
                )
                raw_audit = self._episode(
                    session, node, context, "longhorizon_auditor", audit_prompt, aliases["auditor"], index, role="auditor",
                )
                audit, well_formed = normalize_audit(raw_audit)
            except Exception as error:
                audit, well_formed = _suspect_audit([f"审计阶段调用失败：{error}"]), False
            if not well_formed:
                audit_feedback = "协议反馈：上一轮审计报告格式不完整，已按未通过处理。"
            current.audit = audit
            current.harness_feedback = audit_feedback
            self._persist(session, node, current)

        if not completed and gate is None:
            gate = {
                "name": "longhorizon_rounds", "status": "blocked",
                "reason": f"本次调用已用完 {max_rounds} 轮，任务未获得干净的审计确认；审批后可继续。",
            }
        return self._final_output(rounds, completed=completed, gate=gate, context=context)

    def _episode(
        self,
        session: Session,
        node: WorkflowNode,
        context: Any,
        node_type: str,
        prompt: str,
        alias: str,
        index: int,
        *,
        role: str = "manager",
    ) -> dict[str, Any]:
        episode_node = WorkflowNode(
            f"{node.node_id}:{role}:{index}", node_type, model_alias=alias, prompt_template=prompt,
        )
        output = self.gateway.complete(model_alias=alias, node=episode_node, context=context)
        if "inputs" in output or "errors" in output:
            raise ValueError("模型输出不能包含 inputs 或 errors 字段")
        return output

    def _persist(self, session: Session, node: WorkflowNode, rnd: LoopRound) -> None:
        status = rnd.route
        if rnd.audit is not None:
            status = f"{rnd.route}/{rnd.audit.get('status', '')}"
        session.add_message(
            "event",
            f"longhorizon {node.node_id} round {rnd.index}: {status}",
            node_id=node.node_id,
            metadata={"longhorizon_round": rnd.to_dict()},
        )
        if self.store is not None:
            self.store.save(session, session.session_id)
        if self.event_sink is not None:
            self.event_sink(OrchestrationEvent(
                "longhorizon_round", session.session_id, node.node_id, status,
                {"round": rnd.to_dict()},
            ))

    @staticmethod
    def _rebuild_rounds(session: Session, node_id: str) -> list[LoopRound]:
        rounds: list[LoopRound] = []
        for message in session.messages:
            if message.role != "event" or message.node_id != node_id:
                continue
            data = message.metadata.get("longhorizon_round")
            if not isinstance(data, dict):
                continue
            try:
                rounds.append(LoopRound.from_dict(data))
            except (KeyError, TypeError, ValueError):
                continue
        rounds.sort(key=lambda item: item.index)
        return rounds

    def _final_output(
        self,
        rounds: list[LoopRound],
        *,
        completed: bool,
        gate: dict[str, Any] | None,
        context: Any = None,
    ) -> dict[str, Any]:
        last_state = rounds[-1].task_state if rounds else {}
        last_changes = next((item.executor_summary for item in reversed(rounds) if item.executor_summary), "")
        output: dict[str, Any] = {
            "facts": {
                "changes": last_changes,
                "rounds": len(rounds),
                "completed": completed,
                "task_state": last_state,
            },
            "artifacts": {},
            "decisions": [
                f"round {item.index}: route={item.route}"
                + (f"，audit={item.audit.get('status', '')}" if item.audit else "")
                for item in rounds[-30:]
            ],
        }
        if context is not None:
            output["inputs"] = {"workspace": context.inputs.get("workspace", {})}
        if gate is not None:
            output["gate"] = gate
        return output
