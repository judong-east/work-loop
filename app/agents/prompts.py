"""Role prompt assembly for the agent workflow.

Every model-facing instruction string lives here: the three strict
output-schema instructions appended for text-JSON runtimes (Pi, native —
Claude enforces the same shapes via tool input_schema, where they are
redundant but harmless), the four role instructions, the graph-node
instruction composer, and the one-shot repair prompts. The workflow class
gathers state (experiences, workflow snapshots, artifact paths) and calls
these pure builders.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.agents.context_ledger import MAX_CONTEXT_PROMPT_CHARS, ContextPack
from app.agents.contracts import (
    AgentTask,
    ExecutionPlan,
    ReviewResult,
    ValidationResult,
)
from app.agents.plan_graph import PlanNode
from app.core.contracts import to_plain

# PiRpcRuntime and NativeHarnessRuntime get structured output by parsing the
# model's final text message as JSON (see runtime_common), so executor prompts
# must explicitly request an ExecutionResult JSON object. Without it a
# text-JSON runtime's executor emits a natural-language summary and
# ExecutionResult.from_dict rejects it.
EXECUTOR_OUTPUT_INSTRUCTION = (
    "\n\n# 输出要求\n"
    "完成上述工作区修改后，只输出一个符合 ExecutionResult Schema 的完整 JSON "
    "对象，不要 Markdown、代码围栏或解释文字。对象至少包含字段："
    "completed_steps（字符串数组）、modified_files（字符串数组）、"
    "tests（数组）、deviations（字符串数组）、remaining_risks（字符串数组）、"
    "next_steps（字符串数组）。\n"
    "tests 仅记录你实际通过 bash 运行过的测试/验证命令的结果；没有运行过命令时必须"
    "为空数组 []，切勿填入描述性条目。每个 tests 元素必须是 "
    "{\"command\": \"<运行的命令>\", \"exit_code\": <整数退出码>, "
    "\"stdout\": \"<标准输出>\", \"stderr\": \"<标准错误>\"}，其中 exit_code 必须是"
    "整数（例如 0 表示成功）。"
)

# ReviewResult has no lenient fallback (unlike the planner), so a text-JSON
# runtime must be told the exact field names or the model invents its own and
# ReviewResult.from_dict rejects it.
REVIEWER_OUTPUT_INSTRUCTION = (
    "\n\n# 输出要求\n"
    "只输出一个符合 ReviewResult Schema 的完整 JSON 对象，不要 Markdown、代码"
    "围栏或解释文字。对象必须且仅包含这些顶层字段：\n"
    "- verdict：字符串，取值之一 \"pass\"、\"revise_code\"、\"replan\"、\"blocked\"；"
    "全部验收通过且无阻断问题时用 \"pass\"。\n"
    "- acceptance：数组，每个元素是 {\"criterion\": <验收标准字符串>, \"passed\": "
    "true/false}；criterion 必须与计划中的 acceptance_criteria 完全一致（逐字"
    "相同），每个验收标准都要出现且仅出现一次。\n"
    "- issues：数组，无问题时为 []；每项为 {\"file\": \"\", \"line\": 0, "
    "\"severity\": \"info|warning|blocker\", \"message\": \"...\", \"evidence\": "
    "\"...\", \"suggestion\": \"\"}。\n"
    "- recommended_tests：字符串数组，可为 []。\n"
    "- summary：字符串，简要总结。"
)

# ExecutionPlan.from_dict is strict on requirement_understanding/steps/
# acceptance_criteria/required_tests, and the lenient fallback only recognizes
# a few step-key aliases, so a text-JSON runtime must be told the exact field
# names or the model invents its own shape and planning fails
# nondeterministically.
PLANNER_OUTPUT_INSTRUCTION = (
    "\n\n# 输出要求\n"
    "只输出一个符合 ExecutionPlan Schema 的完整 JSON 对象，不要 Markdown、代码"
    "围栏或解释文字。对象必须且仅包含这些顶层字段：\n"
    "- requirement_understanding：字符串，对需求的理解（非空）。\n"
    "- non_goals：字符串数组，可为 []。\n"
    "- files_and_symbols：字符串数组，涉及的关键文件/符号，可为 []。\n"
    "- steps：字符串数组，每个元素是一条可执行的实现步骤描述（纯字符串，不要写成"
    "对象），至少一条。\n"
    "- constraints：字符串数组，可为 []。\n"
    "- acceptance_criteria：字符串数组，验收标准（非空、不重复），应与需求中的验收"
    "项逐字对应。\n"
    "- required_tests：字符串数组，需要运行的项目验证命令名（非空），每一项都必须"
    "逐字取自需求中列出的项目验证命令名，不要自造命令名或写成 shell 命令行。\n"
    "- risks：字符串数组，可为 []。\n"
    "- open_questions：字符串数组，未决问题，可为 []。\n"
    "验证由系统在执行完成后自动运行，不要把运行验证命令列为 steps 中的实现步骤；"
    "steps 只应包含实际的编码或文件修改动作。"
)


def with_node_instructions(base: str, additional: str) -> str:
    if not additional:
        return base
    return f"{base}\n工作流节点附加要求：\n{additional}"


def planner_instructions(
    task: AgentTask,
    *,
    experiences: list[dict[str, str]],
    validation_command_names: list[str] | None = None,
    additional_instructions: str = "",
) -> str:
    payload: dict[str, Any] = {
        "title": task.title,
        "requirement": task.requirement,
        "clarifications": task.clarifications,
    }
    if experiences:
        payload["approved_experience"] = experiences
    # The planner may only select named commands from the project policy
    # (ProjectPolicy.required_commands rejects anything else), so give it
    # the real list instead of leaving it to infer names from prose.
    # None means no policy was supplied; an empty list still carries the key.
    if validation_command_names is not None:
        payload["available_validation_commands"] = validation_command_names
    instructions = (
        "分析任务并生成结构化 ExecutionPlan。每次最多保留一个高影响未决问题；"
        "已有澄清答复必须作为需求约束。只输出符合 ExecutionPlan Schema 的完整 "
        "JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
        + json.dumps(payload, ensure_ascii=False)
        + PLANNER_OUTPUT_INSTRUCTION
    )
    return with_node_instructions(instructions, additional_instructions)


def executor_instructions(
    plan: ExecutionPlan,
    review_feedback: ReviewResult | None,
    *,
    additional_instructions: str = "",
) -> str:
    payload = {"plan": to_plain(plan), "review_feedback": to_plain(review_feedback)}
    instructions = "按照已批准的 ExecutionPlan 修改当前工作区。\n" + json.dumps(
        payload, ensure_ascii=False
    ) + EXECUTOR_OUTPUT_INSTRUCTION
    return with_node_instructions(instructions, additional_instructions)


def reviewer_instructions(
    task: AgentTask,
    plan: ExecutionPlan,
    diff: str,
    validation: ValidationResult | None,
    *,
    diff_artifact: str,
    additional_instructions: str = "",
) -> str:
    changed_files = list(
        dict.fromkeys(
            match.group(1)
            for match in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)
            if match.group(1) != "/dev/null"
        )
    )
    payload = {
        "requirement": task.requirement,
        "acceptance_criteria": list(plan.acceptance_criteria),
        "constraints": list(plan.constraints),
        "required_tests": list(plan.required_tests),
        "change_evidence": {
            "changed_files": changed_files,
            "diff_lines": len(diff.splitlines()),
            "diff_artifact": diff_artifact,
            "instruction": "Inspect the read-only workspace and diff artifact when details are needed.",
        },
        "validation": (
            {**to_plain(validation)}
            if validation is not None
            else {"available": False, "reason": "No validation node has run yet."}
        ),
    }
    instructions = (
        "独立审核当前只读工作区，并输出结构化 ReviewResult。只输出符合 "
        "ReviewResult Schema 的完整 JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
        + json.dumps(payload, ensure_ascii=False)
        + REVIEWER_OUTPUT_INSTRUCTION
    )
    return with_node_instructions(instructions, additional_instructions)


def replanner_instructions(
    task: AgentTask,
    previous_plan: ExecutionPlan,
    review: ReviewResult,
    *,
    additional_instructions: str = "",
) -> str:
    payload = {
        "title": task.title,
        "requirement": task.requirement,
        "previous_plan": to_plain(previous_plan),
        "review": to_plain(review),
    }
    instructions = (
        "审核认定已批准计划需要重做。重新检查当前只读工作区并生成新的 "
        "ExecutionPlan；新计划必须再次由用户批准。只输出符合 ExecutionPlan "
        "Schema 的完整 JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
        + json.dumps(payload, ensure_ascii=False)
        + PLANNER_OUTPUT_INSTRUCTION
    )
    return with_node_instructions(instructions, additional_instructions)


def node_instructions(
    node: PlanNode,
    context: ContextPack | None,
    review_feedback: ReviewResult | None = None,
    workflow_instructions: str = "",
    artifact_root: Path | None = None,
) -> str:
    base = (node.instructions or node.title).strip() or node.title
    body = base
    if context is not None:
        lines: list[str] = []
        if context.summary:
            lines.append(f"任务目标：{context.summary[:1000]}")
        if context.inputs:
            lines.append("任务输入：")
            lines.extend(f"- {item[:1000]}" for item in context.inputs[-3:])
        if context.artifacts:
            lines.append("相关工件（按引用读取，不在上下文中复制内容）：")
            for artifact in context.artifacts[:20]:
                path = Path(artifact)
                if artifact_root is not None and not path.is_absolute():
                    path = artifact_root / path
                lines.append(f"- {str(path.resolve())[:1000]}")
        if context.facts:
            lines.append("已完成的关键事实：")
            lines.extend(f"- {fact}" for fact in context.facts)
        if context.constraints:
            lines.append("约束：")
            lines.extend(f"- {constraint}" for constraint in context.constraints)
        if context.decisions:
            lines.append("已确定的决策：")
            lines.extend(f"- {decision}" for decision in context.decisions)
        if context.open_questions:
            lines.append("未决问题：")
            lines.extend(f"- {question[:700]}" for question in context.open_questions[:8])
        if lines:
            handoff = "\n".join(lines)[:MAX_CONTEXT_PROMPT_CHARS]
            body = base + "\n\n# 上游节点交接的压缩上下文\n" + handoff
    if review_feedback is not None:
        # A revision round re-runs this node. Without the reviewer's
        # findings it would only reproduce the result that was rejected,
        # so the feedback has to reach the node prompt the same way
        # executor_instructions delivers it on the single-executor path.
        body += "\n\n# 上一轮审核要求返修\n" + json.dumps(
            to_plain(review_feedback), ensure_ascii=False
        )
    if workflow_instructions.strip():
        body += "\n\n# 工作流执行阶段附加要求\n" + workflow_instructions.strip()
    return body + EXECUTOR_OUTPUT_INSTRUCTION


def executor_repair_prompt(error: str, bad_output: dict) -> str:
    snippet = json.dumps(bad_output, ensure_ascii=False)[:2000]
    return (
        "\n\n# 上一条输出无法解析为执行结果，请修正后只重新输出该 JSON 对象\n"
        f"解析错误：{error}\n"
        f"上一条输出（片段）：{snippet}\n"
        "请只输出一个严格符合 ExecutionResult schema 的 JSON 对象：\n"
        "- completed_steps：字符串数组\n"
        "- modified_files：字符串数组\n"
        "- tests：数组，每项为 {\"command\":字符串,\"exit_code\":整数,"
        "\"stdout\":字符串,\"stderr\":字符串}；未运行命令时为 []\n"
        "- deviations：字符串数组\n"
        "- remaining_risks：字符串数组\n"
        "- next_steps：字符串数组\n"
        "不要使用 name/status/detail 等其它键名包裹测试结果。"
    )


def reviewer_repair_prompt(error: str, bad_output: dict) -> str:
    snippet = json.dumps(bad_output, ensure_ascii=False)[:2000]
    return (
        "\n\n# 上一条审核输出无法解析，请修正后只重新输出该 JSON 对象\n"
        f"解析错误：{error}\n"
        f"上一条输出（片段）：{snippet}\n"
        "请只输出一个严格符合 ReviewResult schema 的 JSON 对象：\n"
        "- verdict：\"pass\"、\"revise_code\"、\"replan\" 或 \"blocked\"\n"
        "- acceptance：数组，每项 {\"criterion\":字符串,\"passed\":布尔}，"
        "criterion 与计划 acceptance_criteria 逐字一致\n"
        "- issues：数组，每项 {\"file\":字符串,\"line\":非负整数,"
        "\"severity\":\"info|warning|blocker\",\"message\":非空字符串,"
        "\"evidence\":非空字符串,\"suggestion\":字符串}；无问题为 []\n"
        "- recommended_tests：字符串数组\n"
        "- summary：字符串\n"
        "不要使用 acceptance_results、diff_review 等其它键名。"
    )
