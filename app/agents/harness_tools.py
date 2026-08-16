"""In-process tool layer for the native harness runtime.

The native runtime replaces CLI-agent subprocesses (Claude Code, Codex CLI,
Pi) with a direct model-API tool loop. These tools are the "Harness" half of
"Model + Harness = Agent": the model decides which tools to call and in what
order; Workloop implements the tools, confines every file path to the task
worktree, enforces protected paths, and keeps shell execution behind the same
unsandboxed opt-in that gates Pi's unconfined tools.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from app.agents.contracts import AgentAccess, AgentPolicy
from app.core.process_tree import ProcessTreeHandle, process_group_options
from app.tools.files import PRUNE_DIRS

# Tool output is model context, not an artifact: cap it so one huge file or a
# chatty command cannot crowd out the rest of the conversation.
MAX_TOOL_RESULT_CHARS = 60_000
MAX_READ_CHARS = 120_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_COMMAND_OUTPUT_CHARS = 40_000


class ToolError(Exception):
    """A rejected tool call; the message is returned to the model verbatim."""


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    policy: AgentPolicy
    access: AgentAccess
    cancel_event: threading.Event | None = None

    def resolve(self, raw_path: str) -> Path:
        candidate = Path(raw_path.strip().strip('"').strip("'"))
        resolved_workspace = self.workspace.resolve()
        target = candidate if candidate.is_absolute() else self.workspace / candidate
        try:
            resolved = target.resolve()
        except OSError as error:
            raise ToolError(f"无法解析路径 {raw_path}: {error}") from error
        if resolved != resolved_workspace and not resolved.is_relative_to(resolved_workspace):
            raise ToolError(
                f"路径 {raw_path} 超出任务工作区 {self.workspace}，工具只能访问工作区内文件。"
            )
        relative = resolved.relative_to(resolved_workspace)
        if _is_protected(relative, self.policy.protected_paths):
            raise ToolError(f"路径 {raw_path} 受项目策略保护，禁止访问。")
        return resolved

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace.resolve()).as_posix()


def _is_protected(relative: PurePosixPath, patterns: list[str]) -> bool:
    text = relative.as_posix()
    for pattern in patterns:
        cleaned = pattern.strip()
        if not cleaned:
            continue
        if cleaned.endswith("/**"):
            prefix = cleaned[:-3].rstrip("/")
            if text == prefix or text.startswith(f"{prefix}/"):
                return True
        elif fnmatch.fnmatch(text, cleaned):
            return True
    return False


def _bound_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit // 2
    return (
        text[:keep]
        + f"\n... [截断：共 {len(text)} 字符，仅保留前后各 {keep} 字符] ...\n"
        + text[-keep:]
    )


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"参数 {key} 必须是非空字符串。")
    return value


def _optional_int(args: dict[str, Any], key: str, default: int, maximum: int) -> int:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError(f"参数 {key} 必须是非负整数。")
    return min(value, maximum)


@dataclass(frozen=True)
class HarnessTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], str]

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _tool_read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(_require_str(args, "path"))
    if not path.is_file():
        raise ToolError(f"文件不存在：{ctx.relative(path)}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ToolError(f"无法读取 {ctx.relative(path)}: {error}") from error
    lines = text.splitlines(keepends=True)
    offset = _optional_int(args, "offset", 1, max(1, len(lines) or 1))
    limit = _optional_int(args, "limit", 2000, 5000)
    selected = lines[offset - 1 : offset - 1 + limit]
    body = _bound_text("".join(selected), MAX_READ_CHARS)
    header = f"[{ctx.relative(path)} 共 {len(lines)} 行，显示第 {offset}-{offset + len(selected) - 1} 行]\n"
    return header + body


def _tool_list_files(args: dict[str, Any], ctx: ToolContext) -> str:
    base = ctx.resolve(str(args.get("path") or ".")) if args.get("path") else ctx.workspace
    if not base.is_dir():
        raise ToolError(f"目录不存在：{ctx.relative(base)}")
    maximum = _optional_int(args, "max_entries", MAX_LIST_ENTRIES, 2000)
    entries: list[str] = []
    try:
        for path in sorted(base.rglob("*")):
            if len(entries) >= maximum:
                entries.append(f"... [已达 {maximum} 条上限，截断]")
                break
            relative = path.relative_to(ctx.workspace)
            if any(part in PRUNE_DIRS for part in relative.parts[:-1]):
                continue
            if relative.parts and relative.parts[-1] in PRUNE_DIRS:
                continue
            suffix = "/" if path.is_dir() else ""
            entries.append(relative.as_posix() + suffix)
    except OSError as error:
        raise ToolError(f"无法遍历目录：{error}") from error
    return "\n".join(entries) if entries else "(空目录)"


def _tool_search_content(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = _require_str(args, "pattern")
    try:
        matcher = re.compile(pattern)
    except re.error:
        matcher = re.compile(re.escape(pattern))
    base = ctx.resolve(str(args.get("path") or ".")) if args.get("path") else ctx.workspace
    if not base.is_dir():
        raise ToolError(f"目录不存在：{ctx.relative(base)}")
    maximum = _optional_int(args, "max_matches", MAX_SEARCH_MATCHES, 500)
    matches: list[str] = []
    scanned = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ctx.workspace)
        if any(part in PRUNE_DIRS for part in relative.parts):
            continue
        try:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                matches.append(f"{relative.as_posix()}:{number}: {line.strip()[:400]}")
                if len(matches) >= maximum:
                    matches.append(f"... [已达 {maximum} 条上限，截断]")
                    return "\n".join(matches)
    return "\n".join(matches) if matches else f"(在 {scanned} 个文件中未匹配到 {pattern!r})"


def _tool_write_file(args: dict[str, Any], ctx: ToolContext) -> str:
    if ctx.access is not AgentAccess.WORKSPACE_WRITE:
        raise ToolError("只读任务不允许写入文件。")
    path = ctx.resolve(_require_str(args, "path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("参数 content 必须是字符串。")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as error:
        raise ToolError(f"无法写入 {ctx.relative(path)}: {error}") from error
    return f"已写入 {ctx.relative(path)}（{len(content)} 字符）。"


def _tool_edit_file(args: dict[str, Any], ctx: ToolContext) -> str:
    if ctx.access is not AgentAccess.WORKSPACE_WRITE:
        raise ToolError("只读任务不允许修改文件。")
    path = ctx.resolve(_require_str(args, "path"))
    old_string = _require_str(args, "old_string")
    new_string = args.get("new_string")
    if not isinstance(new_string, str):
        raise ToolError("参数 new_string 必须是字符串。")
    if not path.is_file():
        raise ToolError(f"文件不存在：{ctx.relative(path)}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ToolError(f"无法读取 {ctx.relative(path)}: {error}") from error
    occurrences = text.count(old_string)
    replace_all = args.get("replace_all") is True
    if occurrences == 0:
        raise ToolError(f"old_string 在 {ctx.relative(path)} 中未出现，请先读取文件核对内容。")
    if occurrences > 1 and not replace_all:
        raise ToolError(
            f"old_string 在 {ctx.relative(path)} 中出现 {occurrences} 次；"
            "请提供更长的上下文使其唯一，或设置 replace_all=true。"
        )
    updated = text.replace(old_string, new_string) if replace_all else text.replace(
        old_string, new_string, 1
    )
    try:
        path.write_text(updated, encoding="utf-8", newline="")
    except OSError as error:
        raise ToolError(f"无法写入 {ctx.relative(path)}: {error}") from error
    return f"已修改 {ctx.relative(path)}（替换 {occurrences if replace_all else 1} 处）。"


def _tool_run_command(args: dict[str, Any], ctx: ToolContext) -> str:
    command = _require_str(args, "command")
    requested = args.get("timeout_seconds")
    if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0):
        raise ToolError("参数 timeout_seconds 必须是正整数。")
    timeout = min(requested or ctx.policy.timeout_seconds, ctx.policy.timeout_seconds)
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise ToolError("任务已取消，命令未执行。")
    try:
        process = subprocess.Popen(
            command,
            cwd=ctx.workspace,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_group_options(),
        )
    except OSError as error:
        raise ToolError(f"无法启动命令: {error}") from error
    tree = ProcessTreeHandle(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        exit_code = process.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        tree.terminate()
        stdout, stderr = process.communicate()
        exit_code = None
        timed_out = True
    finally:
        tree.close()
    sections = []
    if timed_out:
        sections.append(f"命令超时（{timeout} 秒），进程树已被终止。")
    sections.append(f"exit_code: {exit_code}")
    sections.append("--- stdout ---")
    sections.append(_bound_text(stdout or "", MAX_COMMAND_OUTPUT_CHARS))
    sections.append("--- stderr ---")
    sections.append(_bound_text(stderr or "", MAX_COMMAND_OUTPUT_CHARS))
    return "\n".join(sections)


_READ_TOOLS: tuple[HarnessTool, ...] = (
    HarnessTool(
        name="read_file",
        description=(
            "读取工作区内一个文本文件的内容。path 是相对工作区的路径；"
            "offset/limit 按行号分页读取大文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区内文件路径"},
                "offset": {"type": "integer", "description": "起始行号，从 1 开始"},
                "limit": {"type": "integer", "description": "最多返回的行数"},
            },
            "required": ["path"],
        },
        handler=_tool_read_file,
    ),
    HarnessTool(
        name="list_files",
        description="递归列出工作区内文件与目录（自动跳过 .git、node_modules 等）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "起始目录，默认工作区根"},
                "max_entries": {"type": "integer"},
            },
        },
        handler=_tool_list_files,
    ),
    HarnessTool(
        name="search_content",
        description="用正则表达式在工作区文件内容中搜索，返回 文件:行号: 行。pattern 无效时按字面量处理。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "限定搜索的子目录"},
                "max_matches": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        handler=_tool_search_content,
    ),
)

_WRITE_TOOLS: tuple[HarnessTool, ...] = (
    HarnessTool(
        name="write_file",
        description="把完整内容写入工作区内文件（覆盖已有内容，自动创建父目录）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        handler=_tool_write_file,
    ),
    HarnessTool(
        name="edit_file",
        description=(
            "精确替换文件内容：old_string 必须与文件内容逐字一致且唯一"
            "（多处匹配时提供更长上下文或设 replace_all=true）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_tool_edit_file,
    ),
)

_SHELL_TOOL = HarnessTool(
    name="run_command",
    description=(
        "在工作区内执行一条 shell 命令（构建、测试、检查）。命令以工作区为当前目录，"
        "超时受项目策略限制；运行测试或检查后把真实结果如实记入最终 JSON 的 tests 字段。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "完整的 shell 命令行"},
            "timeout_seconds": {"type": "integer"},
        },
        "required": ["command"],
    },
    handler=_tool_run_command,
)


def tools_for(
    access: AgentAccess,
    policy: AgentPolicy,
    shell_allowed: bool,
) -> list[HarnessTool]:
    """The tool set exposed to the model for one request.

    Read-only roles get read/list/search. Write roles additionally get the
    file mutation tools. The shell tool is offered only when the operator has
    accepted unconfined execution for this request (see
    WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR): unlike the file tools it cannot be
    confined to the worktree in-process, so it follows the Pi gate.
    """
    tools = list(_READ_TOOLS)
    if access is AgentAccess.WORKSPACE_WRITE:
        tools.extend(_WRITE_TOOLS)
        if shell_allowed:
            tools.append(_SHELL_TOOL)
    return tools


def execute_tool(
    tools: list[HarnessTool],
    name: str,
    arguments_json: str,
    ctx: ToolContext,
) -> str:
    """Run one model tool call; a rejected call returns an error message, not an exception."""
    known = {tool.name: tool for tool in tools}
    tool = known.get(name)
    if tool is None:
        available = ", ".join(sorted(known)) or "(无)"
        return f"ERROR: 未知工具 {name}。可用工具：{available}"
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as error:
        return f"ERROR: 工具 {name} 的参数不是合法 JSON: {error}"
    if not isinstance(args, dict):
        return f"ERROR: 工具 {name} 的参数必须是 JSON 对象。"
    try:
        return tool.handler(args, ctx)
    except ToolError as error:
        return f"ERROR: {error}"
    except Exception as error:  # noqa: BLE001 - tool failures become model feedback
        return f"ERROR: 工具 {name} 执行失败: {error}"
