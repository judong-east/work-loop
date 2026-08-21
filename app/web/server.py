from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.agents.contracts import AgentTaskStatus, TaskBudget
from app.agents.delivery import DeliveryService
from app.agents.plan_graph import PlanGraph
from app.agents.profiles import load_model_catalog, migrate_legacy_profiles
from app.agents.runtime_factory import build_runtime_stack, default_model_catalog
from app.agents.scheduler import PersistentAgentScheduler
from app.agents.status_groups import task_status_group, task_status_priority
from app.agents.workflow import AgentWorkflow
from app.agents.workflow_config import WorkflowNodeKind, workflow_from_dict
from app.core.contracts import to_plain, utc_now
from app.memory.experience_store import ExperienceStore
from app.web.model_registry import (
    agent_runtime_dir,
    delete_model,
    delete_provider,
    ensure_registry,
    provider_test_model,
    registry_status,
    save_model,
    save_provider,
    save_roles,
    sync_catalog,
)
from app.web.native_setup import (
    native_status,
    save_native_config,
    test_native_connection,
)
from app.application.workbench import WorkbenchService
from app.domain.models import SessionMode, WorkflowDefinition, WorkflowNode

MAX_BODY_BYTES = 10 * 1024 * 1024

# The server binds 127.0.0.1, which stops remote clients but not the browser the
# user already has open: a page on any origin can POST here (a JSON body with the
# default text/plain content type is a "simple request", so no preflight is sent
# and the request goes through), and a hostname that resolves to 127.0.0.1 can
# both send and read responses via DNS rebinding. Every write endpoint is
# authority-bearing — /deliver merges into the target branch and takes its
# `confirmed` flag straight from the request body — so cross-site requests have
# to be rejected before they reach a handler.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _filesystem_roots() -> list[Path]:
    if os.name != "nt":
        return [Path("/")]
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [Path(f"{chr(65 + index)}:\\") for index in range(26) if mask & (1 << index)]


def _resolve_static_dir() -> Path:
    """Return the static directory, handling both normal and PyInstaller-frozen modes."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app" / "web" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _resolve_static_dir()


def _read_json_if_exists(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


class WorkloopRequestHandler(BaseHTTPRequestHandler):
    server: "WorkloopServer"
    query_params: dict[str, list[str]]

    GET_ROUTES = [
        (re.compile(r"^/$"), "handle_index"),
        (re.compile(r"^/workbench/?$"), "handle_workbench"),
        (re.compile(r"^/static/(workbench\.css|workbench\.js)$"), "handle_workbench_asset"),
        (re.compile(r"^/api/v2/catalog$"), "handle_v2_catalog"),
        (re.compile(r"^/api/v2/projects$"), "handle_v2_projects"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/sessions$"), "handle_v2_project_sessions"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)$"), "handle_v2_session"),
        (re.compile(r"^/api/v2/resources$"), "handle_v2_resources"),
        (re.compile(r"^/api/agent/projects$"), "handle_agent_projects"),
        (re.compile(r"^/api/agent/tasks$"), "handle_agent_tasks"),
        (re.compile(r"^/api/agent/metrics$"), "handle_agent_metrics"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/plan-graph$"), "handle_agent_plan_graph"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/events$"), "handle_agent_task_events"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)$"), "handle_agent_task_detail"),
        (re.compile(r"^/api/agent/queue$"), "handle_agent_queue"),
        (re.compile(r"^/api/agent/runtime-health$"), "handle_agent_runtime_health"),
        (re.compile(r"^/api/agent/native-config$"), "handle_agent_native_config"),
        (re.compile(r"^/api/agent/model-registry$"), "handle_model_registry"),
        (re.compile(r"^/api/agent/workflows$"), "handle_agent_workflows"),
        (re.compile(r"^/api/agent/history$"), "handle_agent_history"),
        (re.compile(r"^/api/agent/history/([\w-]+)$"), "handle_agent_history_detail"),
    ]
    POST_ROUTES = [
        (re.compile(r"^/api/v2/projects$"), "handle_v2_create_project"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/sessions$"), "handle_v2_create_session"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/messages$"), "handle_v2_message"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/run$"), "handle_v2_run"),
        (re.compile(r"^/api/v2/resources/providers$"), "handle_v2_save_provider"),
        (re.compile(r"^/api/v2/resources/providers/([\w-]+)/test$"), "handle_v2_test_provider"),
        (re.compile(r"^/api/v2/resources/models$"), "handle_v2_save_model"),
        (re.compile(r"^/api/v2/nodes$"), "handle_v2_save_node"),
        (re.compile(r"^/api/v2/workflows$"), "handle_v2_save_workflow"),
        (re.compile(r"^/api/agent/projects/browse-directories$"), "handle_agent_browse_directories"),
        (re.compile(r"^/api/agent/projects$"), "handle_agent_register_project"),
        (re.compile(r"^/api/agent/workflows$"), "handle_agent_save_workflow"),
        (re.compile(r"^/api/agent/tasks$"), "handle_agent_create_task"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/plan-graph$"), "handle_agent_save_plan_graph"),
        (re.compile(r"^/api/agent/profiles/migrate$"), "handle_agent_migrate_profiles"),
        (re.compile(r"^/api/agent/native-config$"), "handle_agent_save_native_config"),
        (re.compile(r"^/api/agent/native-config/test$"), "handle_agent_test_native_config"),
        (re.compile(r"^/api/agent/model-registry/providers$"), "handle_model_registry_save_provider"),
        (re.compile(r"^/api/agent/model-registry/providers/([\w-]+)/test$"), "handle_model_registry_test_provider"),
        (re.compile(r"^/api/agent/model-registry/models$"), "handle_model_registry_save_model"),
        (re.compile(r"^/api/agent/model-registry/roles$"), "handle_model_registry_save_roles"),
        (re.compile(r"^/api/agent/queue/run-next$"), "handle_agent_run_next"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/approve$"), "handle_agent_approve"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/clarify$"), "handle_agent_clarify"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/(resume|rerun)$"), "handle_agent_recover"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/terminate$"), "handle_agent_terminate"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/budget$"), "handle_agent_budget"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/prepare-delivery$"), "handle_agent_prepare_delivery"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/integrate$"), "handle_agent_integrate"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/deliver$"), "handle_agent_deliver"),
        (re.compile(r"^/api/agent/tasks/([\w-]+)/delete$"), "handle_agent_delete_task"),
        (re.compile(r"^/api/agent/history/([\w-]+)/delete$"), "handle_agent_delete_history"),
    ]
    DELETE_ROUTES = [
        (re.compile(r"^/api/v2/resources/providers/([\w-]+)$"), "handle_v2_delete_provider"),
        (re.compile(r"^/api/v2/resources/models/([\w-]+)$"), "handle_v2_delete_model"),
        (re.compile(r"^/api/v2/nodes/([\w-]+)$"), "handle_v2_delete_node"),
        (re.compile(r"^/api/v2/workflows/([\w-]+)$"), "handle_v2_delete_workflow"),
        (re.compile(r"^/api/agent/workflows/([\w-]+)$"), "handle_agent_delete_workflow"),
        (re.compile(r"^/api/agent/model-registry/providers/([\w-]+)$"), "handle_model_registry_delete_provider"),
        (re.compile(r"^/api/agent/model-registry/models/([\w-]+)$"), "handle_model_registry_delete_model"),
    ]

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass  # 本机工具，静默访问日志

    # ---- 分发 ----

    def do_GET(self) -> None:
        self._dispatch(self.GET_ROUTES, needs_body=False)

    def do_POST(self) -> None:
        self._dispatch(self.POST_ROUTES, needs_body=True)

    def do_DELETE(self) -> None:
        self._dispatch(self.DELETE_ROUTES, needs_body=False)

    def _dispatch(self, routes, needs_body: bool) -> None:
        parsed = urlsplit(self.path)
        self.query_params = parse_qs(parsed.query)
        rejection = self._same_origin_rejection(self.command not in {"GET", "HEAD"})
        if rejection:
            # Drain the body first so Windows does not reset the connection
            # while the client is still writing.
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(min(length, MAX_BODY_BYTES))
            self._send_json(403, {"error": rejection})
            return
        if needs_body and parsed.path.startswith(
            ("/api/tasks", "/api/models", "/api/workflow", "/api/memory")
        ):
            # Consume the request body before replying so Windows does not reset the
            # connection when the client is still sending a deprecated write request.
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            self._send_json(
                410,
                {
                    "error": (
                        "旧工作流写接口已移除。请通过 /api/agent/projects 和 "
                        "/api/agent/tasks 创建新任务；历史任务仅支持只读访问。"
                    ),
                    "migration": "/api/agent/runtime-health",
                },
            )
            return
        for pattern, name in routes:
            match = pattern.match(parsed.path)
            if not match:
                continue
            try:
                if needs_body:
                    body = self._read_body()
                    getattr(self, name)(*match.groups(), body=body)
                else:
                    getattr(self, name)(*match.groups())
            except _HttpError as error:
                self._send_json(error.status, {"error": error.message})
            except FileNotFoundError as error:
                self._send_json(404, {"error": str(error)})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 - 顶层兜底
                self._send_json(500, {"error": f"服务器内部错误：{error}"})
            return
        self._send_json(404, {"error": "接口不存在。"})

    def _same_origin_rejection(self, is_write: bool) -> str:
        """Return a rejection reason for a cross-site request, or "".

        Two independent checks:

        * ``Host`` must name the loopback interface. A DNS-rebinding attacker
          reaches 127.0.0.1 through their own hostname, which arrives here in
          ``Host``; rejecting it keeps them from reading responses.
        * For state-changing methods, ``Origin`` (which browsers always attach
          to a cross-site POST, preflighted or not) and ``Sec-Fetch-Site`` must
          say same-origin.

        Non-browser clients — the CLI, tests, curl — send neither ``Origin`` nor
        ``Sec-Fetch-Site``, so they are unaffected."""
        host = self.headers.get("Host", "")
        if host.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8765
            hostname = host[: host.find("]") + 1]
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        if hostname and hostname.strip("[]") not in {h.strip("[]") for h in _LOCAL_HOSTS}:
            return "请求 Host 不是本机回环地址，已拒绝（可能是 DNS rebinding）。"
        if not is_write:
            return ""
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            return "跨站请求已拒绝。"
        origin = self.headers.get("Origin", "")
        if origin:
            parsed = urlsplit(origin)
            port = self.server.server_address[1]
            allowed = {f"http://{name}:{port}" for name in ("127.0.0.1", "localhost")}
            allowed.add(f"http://[::1]:{port}")
            if f"{parsed.scheme}://{parsed.netloc}" not in allowed:
                return "跨站请求已拒绝：Origin 不是本机控制台。"
        return ""

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            raise _HttpError(413, "请求体过大。")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _HttpError(400, "请求体不是合法 JSON。")
        if not isinstance(data, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象。")
        return data

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tasks_root(self) -> Path:
        return self.server.workloop_root / "tasks"

    def _query_value(self, name: str, default: str = "") -> str:
        values = self.query_params.get(name, [])
        return values[0] if values else default

    def _root_relative_path(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self.server.workloop_root / path

    def _agent_config_path(self, raw: str, default: str) -> Path:
        relative = Path(raw.strip() or default)
        if relative.is_absolute():
            raise _HttpError(400, "Agent 配置路径必须位于 Workloop 数据根内。")
        path = (self.server.workloop_root / relative).resolve()
        try:
            path.relative_to(self.server.workloop_root)
        except ValueError:
            raise _HttpError(400, "Agent 配置路径越出 Workloop 数据根。")
        return path

    # ---- Agent workflow API ----

    def _agent_workflow(self) -> AgentWorkflow:
        return self.server.agent_workflow

    def _agent_actions(self, task) -> list[dict]:
        action = lambda name, label, confirm=False, manual=False, description="": {
            "id": name,
            "label": label,
            "requires_confirmation": confirm,
            "manual": manual,
            "description": description,
        }
        if task.status is AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL:
            try:
                plan = self._agent_workflow().get_plan(task.task_id)
            except (FileNotFoundError, KeyError, ValueError):
                return []
            return (
                [
                    action(
                        "clarify",
                        "回答澄清",
                        description=f"计划提出 {len(plan.open_questions)} 个待澄清问题",
                    )
                ]
                if plan.open_questions
                else [action("approve", "批准计划")]
            )
        if task.status is AgentTaskStatus.INTERRUPTED:
            return [
                action("resume", "恢复阶段"),
                action("rerun", "重新运行阶段"),
                action("terminate", "终止任务", True),
            ]
        if task.status is AgentTaskStatus.PAUSED:
            if task.pause_reason == "permission_required":
                return [
                    action(
                        "permission_required",
                        "检查项目权限策略",
                        manual=True,
                        description="在版本控制的项目策略中授权后重新运行当前阶段。",
                    ),
                    action("rerun", "重新运行阶段"),
                    action("terminate", "终止任务", True),
                ]
            return [
                action("update_budget", "调整预算"),
                action("resume", "恢复任务"),
                action("terminate", "终止任务", True),
            ]
        if task.status is AgentTaskStatus.INTEGRATION_REQUIRED:
            return [action("integrate", "重新整合目标分支")]
        if task.status is AgentTaskStatus.READY_TO_DELIVER:
            return [
                action(
                    "deliver",
                    "确认交付",
                    True,
                    description="自动生成交付报告并合并到目标分支（可先在下方查看变更与验证证据）。",
                )
            ]
        if task.status is AgentTaskStatus.BLOCKED and task.pause_reason == "integration_conflict":
            return [
                action(
                    "resolve_conflict",
                    "处理 Git 冲突",
                    manual=True,
                    description="在任务 worktree 中解决冲突后重新发起整合。",
                )
            ]
        if task.status is AgentTaskStatus.BLOCKED and task.error.startswith("验证 "):
            return [
                action("resume", "重新运行验证"),
                action("terminate", "终止任务", True),
            ]
        if task.status is AgentTaskStatus.BLOCKED:
            return [
                action(
                    "review_policy_block",
                    "检查策略阻塞",
                    manual=True,
                    description="查看策略证据并修正越权变更或项目策略。",
                )
            ]
        if task.status is AgentTaskStatus.FAILED:
            return [
                action(
                    "inspect_failure",
                    "检查运行失败",
                    manual=True,
                    description="查看最后一个 AgentRun 的错误分类、事件和运行时健康状态。",
                )
            ]
        if task.status in {
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            AgentTaskStatus.QUEUED_FOR_EXECUTION,
            AgentTaskStatus.QUEUED_FOR_RECOVERY,
            AgentTaskStatus.ANALYZING,
            AgentTaskStatus.EXECUTING,
            AgentTaskStatus.VALIDATING,
            AgentTaskStatus.REVIEWING,
            AgentTaskStatus.REPLANNING,
        }:
            return [action("terminate", "终止任务", True)]
        return []

    def _agent_summary(self, task) -> dict:
        payload = to_plain(task)
        payload["actions"] = self._agent_actions(task)
        payload["workflow_version"] = "agent-runtime-v1"
        payload["read_only"] = False
        payload["detail_url"] = f"/api/agent/tasks/{task.task_id}"
        payload["project_name"] = ""
        try:
            payload["project_name"] = self._agent_workflow().get_project(task.project_id).name
        except (FileNotFoundError, ValueError):
            pass
        return payload

    def _safe_agent_artifact(self, task_dir: Path, reference: str, text=False):
        if not reference:
            return None
        relative = Path(reference)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            return {"available": False, "error": "工件路径越出任务目录。"}
        path = task_dir / relative
        try:
            path.parent.resolve().relative_to(task_dir.resolve())
        except ValueError:
            return {"available": False, "error": "工件路径越出任务目录。"}
        if path.is_symlink():
            return {"available": False, "error": "工件路径越出任务目录。"}
        if not path.is_file():
            return {"available": False, "error": "工件不存在。"}
        if text:
            content = _read_text_if_exists(path)
            return content if content else {"available": False, "error": "工件不可读。"}
        data = _read_json_if_exists(path)
        return data if data is not None else {"available": False, "error": "工件不可解析。"}

    def _agent_detail(self, task) -> dict:
        payload = self._agent_summary(task)
        task_dir = self._agent_workflow().store.task_dir(task.task_id)
        payload["plan"] = self._safe_agent_artifact(
            task_dir,
            task.artifacts.get("plan", ""),
        )
        payload["plan_graph"] = task.plan_graph or self._safe_agent_artifact(
            task_dir,
            task.artifacts.get("plan_graph", ""),
        )
        composer = self._agent_workflow().composer
        payload["model_catalog"] = (
            composer.catalog.to_dict()["models"] if composer is not None else []
        )
        rounds = []
        rounds_root = task_dir / "artifacts" / "rounds"
        for path in sorted(
            (item for item in rounds_root.iterdir() if item.is_dir()),
            key=lambda item: int(item.name) if item.name.isdigit() else 10**9,
        ):
            rounds.append(
                {
                    "round": path.name,
                    "execution": _read_json_if_exists(path / "execution.json"),
                    "validation": _read_json_if_exists(path / "validation.json"),
                    "review": _read_json_if_exists(path / "review.json"),
                    "diff": _read_text_if_exists(path / "changes.diff"),
                    "policy": _read_json_if_exists(path / "policy-validation.json"),
                }
            )
        payload["rounds"] = rounds
        runs = []
        for path in sorted((task_dir / "artifacts" / "runs").glob("*.json")):
            data = _read_json_if_exists(path)
            if isinstance(data, dict):
                runs.append(data)
        payload["runs"] = runs
        payload["delivery_report"] = self._safe_agent_artifact(
            task_dir,
            task.artifacts.get("delivery_report", ""),
        )
        queue = self.server.agent_scheduler.store.load()
        payload["queue_entries"] = [
            to_plain(entry) for entry in queue.entries if entry.task_id == task.task_id
        ]
        payload["task_events"] = self._agent_workflow().store.read_events(task.task_id)[-80:]
        return payload

    def handle_agent_projects(self) -> None:
        projects = self._agent_workflow().projects.list_all()
        self._send_json(200, [to_plain(project) for project in projects])

    # ---- Layered workbench API (v2) -------------------------------------

    def handle_v2_catalog(self) -> None:
        self._send_json(200, {
            "schema_version": 1,
            "nodes": self.server.workbench.list_node_types(),
            "workflows": [self._v2_workflow_payload(item) for item in self.server.workbench.list_workflows()],
        })

    def handle_v2_projects(self) -> None:
        self._send_json(200, [item.to_dict() for item in self.server.workbench.list_projects()])

    def handle_v2_create_project(self, body: dict) -> None:
        try:
            project = self.server.workbench.create_project(
                str(body.get("name", "")),
                instructions=str(body.get("instructions", "")),
                knowledge_refs=[str(item) for item in body.get("knowledge_refs", [])],
                default_model=str(body.get("default_model", "")),
            )
        except (TypeError, ValueError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(201, project.to_dict())

    def handle_v2_project_sessions(self, project_id: str) -> None:
        try:
            self.server.workbench.get_project(project_id)
        except FileNotFoundError as error:
            raise _HttpError(404, str(error)) from error
        self._send_json(200, [item.to_dict() for item in self.server.workbench.list_sessions(project_id)])

    def handle_v2_create_session(self, project_id: str, body: dict) -> None:
        try:
            mode = SessionMode(str(body.get("mode", SessionMode.CHAT.value)))
            session = self.server.workbench.create_session(
                project_id,
                str(body.get("title", "未命名会话")),
                mode=mode,
                workflow_id=str(body.get("workflow_id", "")),
            )
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(201, session.to_dict())

    def handle_v2_session(self, session_id: str) -> None:
        self._send_json(200, self.server.workbench.get_session(session_id).to_dict())

    def handle_v2_message(self, session_id: str, body: dict) -> None:
        try:
            session = self.server.workbench.send_message(session_id, str(body.get("content", "")))
        except (TypeError, ValueError, FileNotFoundError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(200, session.to_dict())

    def handle_v2_run(self, session_id: str, body: dict) -> None:
        try:
            workflow = self._v2_workflow_from_dict(body["workflow"]) if isinstance(body.get("workflow"), dict) else None
            session = self.server.workbench.run_task(session_id, workflow)
        except (TypeError, ValueError, KeyError, FileNotFoundError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(200, session.to_dict())

    def handle_v2_resources(self) -> None:
        self._send_json(200, self.server.workbench.resource_status())

    def handle_v2_save_provider(self, body: dict) -> None:
        try:
            provider = self.server.workbench.save_provider(body)
        except (KeyError, TypeError, ValueError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(201, provider.to_dict())

    def handle_v2_save_model(self, body: dict) -> None:
        try:
            model = self.server.workbench.save_model(body)
        except (KeyError, TypeError, ValueError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(201, model.to_dict())

    def handle_v2_test_provider(self, provider_id: str, body: dict) -> None:
        try:
            result = self.server.workbench.test_provider(provider_id)
        except KeyError as error:
            raise _HttpError(404, f"provider not found: {provider_id}") from error
        except (TypeError, ValueError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(200, result)

    def handle_v2_delete_provider(self, provider_id: str) -> None:
        try:
            self.server.workbench.delete_provider(provider_id)
        except KeyError as error:
            raise _HttpError(404, f"provider not found: {provider_id}") from error
        self._send_json(200, {"deleted": provider_id})

    def handle_v2_delete_model(self, alias: str) -> None:
        try:
            self.server.workbench.delete_model(alias)
        except KeyError as error:
            raise _HttpError(404, f"model not found: {alias}") from error
        self._send_json(200, {"deleted": alias})

    def handle_v2_save_node(self, body: dict) -> None:
        try:
            node = self.server.workbench.save_node(body)
        except (KeyError, TypeError, ValueError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(201, self._v2_node_payload(node))

    def handle_v2_delete_node(self, node_type: str) -> None:
        try:
            self.server.workbench.delete_node(node_type)
        except KeyError as error:
            raise _HttpError(404, f"node not found: {node_type}") from error
        self._send_json(200, {"deleted": node_type})

    def handle_v2_save_workflow(self, body: dict) -> None:
        try:
            workflow = self.server.workbench.save_workflow(self._v2_workflow_from_dict(body))
        except (KeyError, TypeError, ValueError) as error:
            raise _HttpError(400, str(error)) from error
        self._send_json(201, self._v2_workflow_payload(workflow))

    def handle_v2_delete_workflow(self, workflow_id: str) -> None:
        try:
            self.server.workbench.delete_workflow(workflow_id)
        except KeyError as error:
            raise _HttpError(404, f"workflow not found: {workflow_id}") from error
        self._send_json(200, {"deleted": workflow_id})

    @staticmethod
    def _v2_node_payload(node) -> dict:
        return {
            "node_type": node.node_type,
            "label": node.label,
            "description": node.description,
            "input_fields": list(node.input_fields),
            "output_fields": list(node.output_fields),
            "capabilities": list(node.capabilities),
            "default_model": node.default_model,
            "builtin": node.builtin,
        }

    @staticmethod
    def _v2_workflow_payload(workflow: WorkflowDefinition) -> dict:
        return {
            "workflow_id": workflow.workflow_id,
            "label": workflow.label,
            "description": workflow.description,
            "builtin": workflow.builtin,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "depends_on": list(node.depends_on),
                    "model_alias": node.model_alias,
                    "prompt_template": node.prompt_template,
                    "on_failure": node.on_failure,
                    "config": dict(node.config),
                    "position": list(node.position),
                }
                for node in workflow.nodes
            ],
        }

    @staticmethod
    def _v2_workflow_from_dict(value: dict) -> WorkflowDefinition:
        return WorkflowDefinition(
            workflow_id=str(value.get("workflow_id", "inline")),
            label=str(value.get("label", "Inline workflow")),
            description=str(value.get("description", "")),
            nodes=[
                WorkflowNode(
                    node_id=str(item["node_id"]),
                    node_type=str(item["node_type"]),
                    depends_on=tuple(str(dep) for dep in item.get("depends_on", [])),
                    model_alias=str(item.get("model_alias", "")),
                    prompt_template=str(item.get("prompt_template", "")),
                    on_failure=str(item.get("on_failure", "human")),
                    config=dict(item.get("config", {})),
                    position=tuple(float(coord) for coord in item.get("position", [0, 0]))[:2],
                )
                for item in value.get("nodes", [])
            ],
        )

    def handle_agent_workflows(self) -> None:
        workflows = self._agent_workflow().workflows.list_all()
        self._send_json(200, [to_plain(workflow) for workflow in workflows])

    def handle_agent_tasks(self) -> None:
        tasks = self._agent_workflow().store.list_all()
        tasks.sort(
            key=lambda task: (
                task_status_priority(task.status),
                -datetime.fromisoformat(task.updated_at).timestamp(),
            )
        )
        self._send_json(200, [self._agent_summary(task) for task in tasks])

    def handle_agent_metrics(self) -> None:
        counts = {
            "running": 0,
            "waiting_for_human": 0,
            "failed": 0,
            "blocked": 0,
            "ready_to_deliver": 0,
            "other": 0,
            "total": 0,
        }
        for task in self._agent_workflow().store.list_all():
            counts[task_status_group(task.status)] += 1
            counts["total"] += 1
        self._send_json(
            200,
            {
                "schema_version": 1,
                "generated_at": utc_now(),
                "tasks": counts,
                "scheduler": {
                    "queued": len(self.server.agent_scheduler.pending()),
                    "running": len(self.server.agent_scheduler.running()),
                },
            },
        )

    def handle_agent_task_detail(self, task_id: str) -> None:
        task = self._agent_workflow().get_task(task_id)
        self._send_json(200, self._agent_detail(task))

    def handle_agent_task_events(self, task_id: str) -> None:
        """Stream the task's append-only event log over SSE.

        The client passes `after` (or reconnects with Last-Event-ID); new
        trajectory lines are pushed within ~1s so the console stops relying on
        4-second polling to notice progress. The stream self-closes after five
        minutes and the browser reconnects with its last sequence number.
        """
        header_cursor = self.headers.get("Last-Event-ID", "")
        try:
            after = int(header_cursor or self._query_value("after", "0"))
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                events = self._agent_workflow().store.read_events(task_id, after)
                for event in events:
                    after = max(after, int(event.get("seq", 0)))
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"id: {after}\ndata: {payload}\n\n".encode("utf-8"))
                if not events:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the console navigated away or reconnected; nothing to report

    def handle_agent_plan_graph(self, task_id: str) -> None:
        self._send_json(200, self._agent_workflow().get_plan_graph(task_id).to_dict())

    def handle_agent_queue(self) -> None:
        state = self.server.agent_scheduler.store.load()
        self._send_json(200, to_plain(state))

    def handle_agent_runtime_health(self) -> None:
        self._send_json(
            200,
            {
                "profiles": self.server.agent_profiles,
                "health": self._agent_workflow().runtime.health_check(),
                "worker_error": self.server.agent_worker_error,
            },
        )

    def handle_agent_native_config(self) -> None:
        self._send_json(200, native_status(self.server.workloop_root))

    def handle_agent_save_native_config(self, body: dict) -> None:
        try:
            save_native_config(self.server.workloop_root, body)
        except ValueError as error:
            raise _HttpError(400, str(error)) from error
        self.server.reload_agent_profiles()
        self._send_json(200, native_status(self.server.workloop_root))

    def handle_agent_test_native_config(self, body: dict) -> None:
        ok, detail = test_native_connection(self.server.workloop_root, body)
        self._send_json(200 if ok else 400, {"ok": ok, "detail": detail})

    # ---- 模型供应商 / 模型 / 角色绑定 ----

    def handle_model_registry(self) -> None:
        self._send_json(200, registry_status(self.server.workloop_root))

    def handle_model_registry_save_provider(self, body: dict) -> None:
        try:
            provider = save_provider(self.server.workloop_root, body)
            sync_catalog(self.server.workloop_root, ensure_registry(self.server.workloop_root))
        except ValueError as error:
            raise _HttpError(400, str(error)) from error
        self.server.reload_agent_profiles()
        self._send_json(200, registry_status(self.server.workloop_root))

    def handle_model_registry_delete_provider(self, provider_id: str) -> None:
        delete_provider(self.server.workloop_root, provider_id)
        sync_catalog(self.server.workloop_root, ensure_registry(self.server.workloop_root))
        self.server.reload_agent_profiles()
        self._send_json(200, registry_status(self.server.workloop_root))

    def handle_model_registry_test_provider(self, provider_id: str, body: dict) -> None:
        registry = ensure_registry(self.server.workloop_root)
        provider = next(
            (p for p in registry["providers"] if p.get("id") == provider_id), None
        )
        if provider is None:
            raise _HttpError(404, f"供应商 {provider_id} 不存在。")
        model = str(body.get("model", "")).strip() or provider_test_model(
            self.server.workloop_root, provider_id
        )
        if not model:
            raise _HttpError(400, "请先为该供应商添加模型，或提供要测试的模型名。")
        model_entry = next(
            (
                item for item in registry["models"]
                if item.get("provider_id") == provider_id and item.get("model") == model
            ),
            None,
        )
        protocol = str(body.get("protocol", "")).strip() or str(
            (model_entry or {}).get("protocol", "")
        ) or str((provider.get("protocols") or ["codex"])[0])
        ok, detail = test_native_connection(
            self.server.workloop_root,
            {
                "base_url": str(provider.get("base_url", "")),
                "model": model,
                "api_key": str(body.get("api_key", "")),
                "proxy": str(provider.get("proxy", "") or ""),
                "provider_id": provider_id,
                "protocol": protocol,
            },
        )
        self._send_json(200 if ok else 400, {"ok": ok, "detail": detail})

    def handle_model_registry_save_model(self, body: dict) -> None:
        try:
            save_model(self.server.workloop_root, body)
            sync_catalog(self.server.workloop_root, ensure_registry(self.server.workloop_root))
        except ValueError as error:
            raise _HttpError(400, str(error)) from error
        self.server.reload_agent_profiles()
        self._send_json(200, registry_status(self.server.workloop_root))

    def handle_model_registry_delete_model(self, profile_id: str) -> None:
        delete_model(self.server.workloop_root, profile_id)
        sync_catalog(self.server.workloop_root, ensure_registry(self.server.workloop_root))
        self.server.reload_agent_profiles()
        self._send_json(200, registry_status(self.server.workloop_root))

    def handle_model_registry_save_roles(self, body: dict) -> None:
        try:
            save_roles(self.server.workloop_root, body)
            sync_catalog(self.server.workloop_root, ensure_registry(self.server.workloop_root))
        except ValueError as error:
            raise _HttpError(400, str(error)) from error
        self.server.reload_agent_profiles()
        self._send_json(200, registry_status(self.server.workloop_root))

    def _legacy_summary(self, state: dict, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "task_key": f"legacy:{task_id}",
            "title": str(state.get("title", "")),
            "project_name": "历史工作流",
            "status": str(state.get("status", "unknown")),
            "iteration": int(state.get("iteration", 0) or 0),
            "updated_at": str(state.get("updated_at", "")),
            "workflow_version": "legacy-v1",
            "read_only": True,
            "detail_url": f"/api/agent/history/{task_id}",
            "actions": [],
        }

    def _legacy_artifact(self, task_dir: Path, reference: str, text: bool = False):
        if not reference:
            return {"available": False, "error": "工件引用为空。"}
        raw = Path(reference)
        if raw.is_absolute():
            return {"available": False, "error": "历史绝对路径工件不再自动读取。"}
        path = (task_dir / raw).resolve()
        try:
            path.relative_to(task_dir.resolve())
        except ValueError:
            return {"available": False, "error": "工件路径越出历史任务目录。"}
        if not path.is_file():
            return {"available": False, "error": f"工件不存在：{reference}"}
        if text:
            try:
                return {"available": True, "content": path.read_text(encoding="utf-8")}
            except (OSError, UnicodeDecodeError):
                return {"available": False, "error": f"工件不可读：{reference}"}
        data = _read_json_if_exists(path)
        return (
            {"available": True, "content": data}
            if data is not None
            else {"available": False, "error": f"工件不可解析：{reference}"}
        )

    def _legacy_detail(self, task_id: str) -> dict:
        task_dir = self._tasks_root() / task_id
        state_path = task_dir / "state.json"
        state = _read_json_if_exists(state_path)
        if not isinstance(state, dict):
            raise FileNotFoundError(f"历史任务 {task_id} 不存在或状态已损坏。")
        detail = self._legacy_summary(state, task_id)
        detail["task"] = state
        detail["goal"] = str(state.get("goal", ""))
        detail["artifacts"] = {
            str(name): self._legacy_artifact(
                task_dir,
                str(reference),
                text=str(reference).lower().endswith((".md", ".txt", ".diff", ".jsonl")),
            )
            for name, reference in state.get("artifacts", {}).items()
            if isinstance(reference, str)
        }
        detail["plan"] = self._legacy_artifact(task_dir, "artifacts/plan.md", text=True)
        rounds = []
        rounds_dir = task_dir / "artifacts" / "rounds"
        if rounds_dir.is_dir():
            for round_dir in sorted(
                rounds_dir.iterdir(),
                key=lambda path: int(path.name) if path.name.isdigit() else 10**9,
            ):
                if not round_dir.is_dir():
                    continue
                rounds.append(
                    {
                        "round": round_dir.name,
                        "diff": self._legacy_artifact(
                            task_dir,
                            str((round_dir / "changes.diff").relative_to(task_dir)),
                            text=True,
                        ),
                        "review": self._legacy_artifact(
                            task_dir,
                            str((round_dir / "review.json").relative_to(task_dir)),
                        ),
                    }
                )
        detail["rounds"] = rounds
        return detail

    def handle_agent_history(self) -> None:
        items = []
        tasks_root = self._tasks_root()
        if tasks_root.is_dir():
            for state_path in tasks_root.glob("*/state.json"):
                state = _read_json_if_exists(state_path)
                if isinstance(state, dict):
                    items.append(
                        self._legacy_summary(
                            state,
                            str(state.get("task_id", state_path.parent.name)),
                        )
                    )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        self._send_json(200, items)

    def handle_agent_history_detail(self, task_id: str) -> None:
        self._send_json(200, self._legacy_detail(task_id))

    # ---- GET ----

    def handle_index(self) -> None:
        page = STATIC_DIR / "workbench.html"
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_workbench(self) -> None:
        self.send_response(308)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_workbench_asset(self, filename: str) -> None:
        asset = STATIC_DIR / filename
        if not asset.is_file():
            raise _HttpError(404, "静态资源不存在。")
        body = asset.read_bytes()
        content_type = "text/css; charset=utf-8" if filename.endswith(".css") else "text/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_agent_register_project(self, body: dict) -> None:
        name = str(body.get("name", "")).strip()
        repository = str(body.get("repository", "")).strip()
        branch = str(body.get("default_branch", "")).strip()
        config_path = str(body.get("config_path", ".workloop/project.toml")).strip()
        instructions = str(body.get("instructions", "")).strip()
        if len(instructions) > 8000:
            raise _HttpError(400, "项目说明不能超过 8000 个字符。")
        if not name or not repository:
            raise _HttpError(400, "name 和 repository 不能为空。")
        path = self._root_relative_path(repository)
        resolved = path.resolve()
        for existing in self._agent_workflow().projects.list_all():
            source = Path(existing.source_directory or existing.repository).resolve()
            if source == resolved:
                self._send_json(200, to_plain(existing))
                return
        project = self._agent_workflow().register_project(
            name,
            path,
            branch,
            config_path or ".workloop/project.toml",
        )
        project.instructions = instructions
        self._agent_workflow().projects.add(project)
        self._send_json(200, to_plain(project))

    def handle_agent_browse_directories(self, body: dict) -> None:
        raw_path = str(body.get("path", "")).strip()
        current = Path(raw_path).expanduser() if raw_path else Path.home()
        if not current.is_absolute():
            current = self.server.workloop_root / current
        current = current.resolve()
        if not current.is_dir():
            raise _HttpError(400, f"目录不存在或不可访问：{current}")
        try:
            directories = []
            for child in current.iterdir():
                try:
                    if child.is_dir():
                        directories.append({"name": child.name, "path": str(child.resolve())})
                except OSError:
                    continue
        except OSError as error:
            raise _HttpError(400, f"无法读取目录 {current}：{error}") from error
        directories.sort(key=lambda item: item["name"].casefold())
        truncated = len(directories) > 500
        self._send_json(
            200,
            {
                "path": str(current),
                "parent": "" if current.parent == current else str(current.parent),
                "roots": [str(root) for root in _filesystem_roots()],
                "directories": directories[:500],
                "truncated": truncated,
            },
        )

    def handle_agent_save_workflow(self, body: dict) -> None:
        try:
            workflow = workflow_from_dict(body)
            registry = ensure_registry(self.server.workloop_root)
            models = {item["profile_id"]: item for item in registry["models"]}
            for node in workflow.nodes:
                if not node.model_profile_id:
                    continue
                model = models.get(node.model_profile_id)
                if model is None:
                    raise ValueError(f"节点 {node.label} 绑定的模型不存在。")
                expected = (
                    "workspace_write"
                    if node.kind is WorkflowNodeKind.EXECUTOR
                    else "read_only"
                )
                if model.get("access") != expected:
                    raise ValueError(f"节点 {node.label} 绑定的模型权限不匹配。")
        except ValueError as error:
            raise _HttpError(400, str(error)) from error
        saved = self._agent_workflow().workflows.save(workflow)
        self._send_json(200, to_plain(saved))

    def handle_agent_delete_workflow(self, workflow_id: str) -> None:
        self._agent_workflow().workflows.delete(workflow_id)
        self._send_json(200, {"deleted": workflow_id})

    def handle_agent_create_task(self, body: dict) -> None:
        title = str(body.get("title", "")).strip()
        requirement = str(body.get("requirement", "")).strip()
        project_id = str(body.get("project_id", "")).strip()
        if not title or not requirement or not project_id:
            raise _HttpError(400, "title、requirement 和 project_id 不能为空。")
        raw_budget = body.get("budget")
        budget = None
        if raw_budget is not None:
            if not isinstance(raw_budget, dict):
                raise _HttpError(400, "budget 必须是对象。")
            budget = TaskBudget(
                total_timeout_seconds=float(raw_budget.get("total_timeout_seconds", 7200)),
                call_timeout_seconds=float(raw_budget.get("call_timeout_seconds", 1800)),
                idle_timeout_seconds=float(raw_budget.get("idle_timeout_seconds", 120)),
                max_cost_usd=(
                    float(raw_budget["max_cost_usd"])
                    if raw_budget.get("max_cost_usd") is not None
                    else None
                ),
                max_total_tokens=(
                    int(raw_budget["max_total_tokens"])
                    if raw_budget.get("max_total_tokens") is not None
                    else None
                ),
                max_input_tokens=(
                    int(raw_budget["max_input_tokens"])
                    if raw_budget.get("max_input_tokens") is not None
                    else None
                ),
                max_output_tokens=(
                    int(raw_budget["max_output_tokens"])
                    if raw_budget.get("max_output_tokens") is not None
                    else None
                ),
                max_iterations=int(raw_budget.get("max_iterations", 3)),
            )
            budget.validate()
        task = self._agent_workflow().create_task(
            title,
            requirement,
            project_id,
            budget=budget,
            workflow_id=str(body.get("workflow_id", "quick")).strip() or "quick",
        )
        self.server.agent_scheduler.enqueue_analysis(task.task_id)
        self.server.kick_agent_worker()
        self._send_json(202, self._agent_summary(self._agent_workflow().get_task(task.task_id)))

    def handle_agent_save_plan_graph(self, task_id: str, body: dict) -> None:
        graph = PlanGraph.from_dict(body)
        task = self._agent_workflow().save_plan_graph(task_id, graph)
        self._send_json(200, self._agent_detail(task))

    def handle_agent_migrate_profiles(self, body: dict) -> None:
        source = self._agent_config_path(str(body.get("source", "")), "models.json")
        destination = self._agent_config_path(
            str(body.get("destination", "")), "agent-profiles.json"
        )
        payload = migrate_legacy_profiles(source, destination)
        self._send_json(
            200,
            {
                "path": str(destination),
                "profiles": payload["roles"],
                "commands_discarded": True,
                "restart_required": True,
            },
        )

    def handle_agent_run_next(self, body: dict) -> None:
        del body
        task = self.server.agent_scheduler.run_next()
        self._send_json(200, self._agent_summary(task) if task is not None else {})

    def handle_agent_approve(self, task_id: str, body: dict) -> None:
        del body
        self.server.agent_scheduler.enqueue_execution(task_id)
        self.server.kick_agent_worker()
        self._send_json(202, self._agent_summary(self._agent_workflow().get_task(task_id)))

    def handle_agent_clarify(self, task_id: str, body: dict) -> None:
        raw_answers = body.get("answers")
        if isinstance(raw_answers, list):
            questions: list[dict] = []
            answers: list[str] = []
            for index, item in enumerate(raw_answers, start=1):
                if not isinstance(item, dict):
                    raise _HttpError(400, f"第 {index} 条澄清答复必须是对象。")
                answer = str(item.get("answer", "")).strip()
                if not answer:
                    raise _HttpError(400, f"第 {index} 条澄清答复不能为空。")
                questions.append({"question": str(item.get("question", "")).strip()})
                answers.append(answer)
            if not answers:
                raise _HttpError(400, "answers 不能为空。")
            self.server.agent_scheduler.answer_clarifications(task_id, questions, answers)
        else:
            answer = str(body.get("answer", "")).strip()
            if not answer:
                raise _HttpError(400, "answer 不能为空。")
            self.server.agent_scheduler.answer_clarification(task_id, answer)
        self.server.kick_agent_worker()
        self._send_json(202, self._agent_summary(self._agent_workflow().get_task(task_id)))

    def handle_agent_recover(self, task_id: str, action: str, body: dict) -> None:
        del body
        if action == "resume":
            self.server.agent_scheduler.resume(task_id)
        else:
            self.server.agent_scheduler.rerun(task_id)
        self.server.kick_agent_worker()
        self._send_json(202, self._agent_summary(self._agent_workflow().get_task(task_id)))

    def handle_agent_terminate(self, task_id: str, body: dict) -> None:
        del body
        task = self.server.agent_scheduler.terminate(task_id)
        self._send_json(200, self._agent_summary(task))

    def handle_agent_budget(self, task_id: str, body: dict) -> None:
        task = self._agent_workflow().get_task(task_id)
        current = task.budget
        budget = TaskBudget(
            total_timeout_seconds=float(
                body.get("total_timeout_seconds", current.total_timeout_seconds)
            ),
            call_timeout_seconds=float(
                body.get("call_timeout_seconds", current.call_timeout_seconds)
            ),
            idle_timeout_seconds=float(
                body.get("idle_timeout_seconds", current.idle_timeout_seconds)
            ),
            max_cost_usd=(
                float(body["max_cost_usd"])
                if body.get("max_cost_usd") is not None
                else current.max_cost_usd
            ),
            max_total_tokens=(
                int(body["max_total_tokens"])
                if body.get("max_total_tokens") is not None
                else current.max_total_tokens
            ),
            max_input_tokens=(
                int(body["max_input_tokens"])
                if body.get("max_input_tokens") is not None
                else current.max_input_tokens
            ),
            max_output_tokens=(
                int(body["max_output_tokens"])
                if body.get("max_output_tokens") is not None
                else current.max_output_tokens
            ),
            max_iterations=int(body.get("max_iterations", current.max_iterations)),
        )
        updated = self.server.agent_scheduler.update_budget(task_id, budget)
        self._send_json(200, self._agent_summary(updated))

    def handle_agent_prepare_delivery(self, task_id: str, body: dict) -> None:
        del body
        task = self.server.agent_delivery.prepare(task_id)
        self._send_json(200, self._agent_detail(task))

    def handle_agent_integrate(self, task_id: str, body: dict) -> None:
        del body
        task = self.server.agent_delivery.integrate(task_id)
        self._send_json(200, self._agent_detail(task))

    def handle_agent_deliver(self, task_id: str, body: dict) -> None:
        task = self.server.agent_delivery.deliver(
            task_id,
            strategy=str(body.get("strategy", "merge")),
            confirmed=body.get("confirmed") is True,
        )
        self._send_json(200, self._agent_detail(task))

    def handle_agent_delete_task(self, task_id: str, body: dict) -> None:
        del body
        self.server.agent_scheduler.remove_task(task_id)
        self._send_json(200, {"deleted": task_id})

    def handle_agent_delete_history(self, task_id: str, body: dict) -> None:
        del body
        if not re.fullmatch(r"[\w-]+", task_id):
            raise _HttpError(400, "task_id 不合法。")
        task_dir = self._tasks_root() / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
        self._send_json(200, {"deleted": task_id})


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class WorkloopServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        root: Path,
        port: int,
        agent_workflow: AgentWorkflow | None = None,
        agent_scheduler: PersistentAgentScheduler | None = None,
        agent_delivery: DeliveryService | None = None,
        auto_run_agent: bool = True,
    ):
        super().__init__(("127.0.0.1", port), WorkloopRequestHandler)
        self.workloop_root = Path(root).resolve()
        # New three-layer workbench lives beside the compatibility agent runtime.
        # Existing /api/agent routes remain stable while clients migrate to /api/v2.
        self.workbench = WorkbenchService(self.workloop_root / "workbench")
        if agent_workflow is None:
            profile_path = self.workloop_root / "agent-profiles.json"
            legacy_catalog = None
            if profile_path.is_file():
                try:
                    legacy_catalog = load_model_catalog(profile_path)
                except (OSError, json.JSONDecodeError, ValueError):
                    legacy_catalog = None
            registry = ensure_registry(self.workloop_root)
            # A roles-only CLI catalog has no managed API providers. Preserve
            # that valid legacy stack instead of materializing an empty
            # registry over agent-profiles.json during incremental migration.
            catalog = (
                sync_catalog(self.workloop_root, registry)
                if registry.get("models")
                else legacy_catalog
            )
            if catalog is None:
                catalog = (
                    load_model_catalog(profile_path)
                    if profile_path.is_file()
                    else default_model_catalog()
                )
                if any(option.runtime != "native" for option in catalog.list_all()):
                    catalog = default_model_catalog()
            runtime, composer = build_runtime_stack(
                catalog,
                role_bindings=registry.get("roles") or None,
                key_root=agent_runtime_dir(self.workloop_root),
            )
            agent_workflow = AgentWorkflow(
                self.workloop_root / "agent-runtime",
                runtime,
                composer=composer,
                experience_store=ExperienceStore(self.workloop_root / "memory"),
            )
        self.agent_workflow = agent_workflow
        self.agent_scheduler = agent_scheduler or PersistentAgentScheduler(agent_workflow)
        self.agent_delivery = agent_delivery or DeliveryService(agent_workflow)
        self.auto_run_agent = auto_run_agent
        self.agent_worker_error = ""
        self._agent_worker_lock = threading.Lock()
        self._agent_worker_count = 0
        self.agent_profiles = self._agent_profile_payload()


    def _agent_profile_payload(self) -> dict:
        runtime = self.agent_workflow.runtime
        routed = getattr(runtime, "runtimes", {})
        payload = {}
        for role, selected in routed.items():
            profile = getattr(selected, "profile", None)
            payload[role] = {
                "runtime": type(selected).__name__,
                "model": str(getattr(profile, "model", "")),
                "access": "workspace_write" if role == "executor" else "read_only",
            }
        if not payload:
            payload["default"] = {
                "runtime": type(runtime).__name__,
                "model": "",
                "access": "role_defined",
            }
        return payload

    def reload_agent_profiles(self) -> None:
        """Re-read the model registry and swap the live runtime stack.

        Scheduler and delivery keep their AgentWorkflow references, so the
        catalog switch is an attribute swap; a task that is mid-flight keeps
        the runtime object it already resolved.
        """
        profile_path = self.workloop_root / "agent-profiles.json"
        legacy_catalog = None
        if profile_path.is_file():
            try:
                legacy_catalog = load_model_catalog(profile_path)
            except (OSError, json.JSONDecodeError, ValueError):
                legacy_catalog = None
        registry = ensure_registry(self.workloop_root)
        catalog = (
            sync_catalog(self.workloop_root, registry)
            if registry.get("models")
            else legacy_catalog
        )
        if catalog is None:
            catalog = (
                load_model_catalog(profile_path)
                if profile_path.is_file()
                else default_model_catalog()
            )
            if any(option.runtime != "native" for option in catalog.list_all()):
                catalog = default_model_catalog()
        runtime, composer = build_runtime_stack(
            catalog,
            role_bindings=registry.get("roles") or None,
            key_root=agent_runtime_dir(self.workloop_root),
        )
        self.agent_workflow.runtime = runtime
        self.agent_workflow.composer = composer
        self.agent_profiles = self._agent_profile_payload()

    def kick_agent_worker(self) -> None:
        """Keep one drain thread alive per scheduler slot.

        Each thread loops run_next() until it comes back empty, so up to
        `slots` tasks execute in parallel; a thread that finds every slot busy
        exits immediately and gets respawned on the next kick.
        """
        if not self.auto_run_agent:
            return
        with self._agent_worker_lock:
            while self._agent_worker_count < self.agent_scheduler.slots:
                self._agent_worker_count += 1
                threading.Thread(target=self._drain_agent_queue, daemon=True).start()

    def _drain_agent_queue(self) -> None:
        try:
            while self.agent_scheduler.run_next() is not None:
                pass
            self.agent_worker_error = ""
        except Exception as error:  # noqa: BLE001 - surfaced by runtime health endpoint
            self.agent_worker_error = str(error)
        finally:
            with self._agent_worker_lock:
                self._agent_worker_count -= 1
            # An entry may have been enqueued between this worker's empty check
            # and its exit; never leave the queue without a live worker.
            if self.agent_scheduler.pending():
                self.kick_agent_worker()


def make_server(
    root: Path,
    port: int = 8765,
    open_browser: bool = False,
    **kwargs,
) -> WorkloopServer:
    server = WorkloopServer(root, port, **kwargs)
    if open_browser:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    return server
