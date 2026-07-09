from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.cli import build_backends
from app.core.contracts import TaskStatus, to_plain, utc_now
from app.core.workflow import WorkloopKernel
from app.models.backends.cli_backend import cancel_task_processes, clear_task_cancel
from app.models.config import load_routing_config

MAX_BODY_BYTES = 10 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
WORKFLOW_KINDS = {"parse", "planner", "executor", "reviewer", "delivery"}
ROLE_KINDS = {"planner", "executor", "reviewer"}


class RunRegistry:
    """进程内唯一状态：记录哪些任务正在后台跑循环，防止同一任务并发执行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._errors: dict[str, str] = {}

    def try_start(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._running:
                return False
            self._running.add(task_id)
            self._errors.pop(task_id, None)
            return True

    def finish(self, task_id: str, error: str = "") -> None:
        with self._lock:
            self._running.discard(task_id)
            if error:
                self._errors[task_id] = error

    def is_running(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._running

    def error_of(self, task_id: str) -> str:
        with self._lock:
            return self._errors.get(task_id, "")


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
        (re.compile(r"^/api/tasks$"), "handle_list_tasks"),
        (re.compile(r"^/api/tasks/([\w-]+)$"), "handle_task_detail"),
        (re.compile(r"^/api/models/config$"), "handle_model_config"),
        (re.compile(r"^/api/workflow/config$"), "handle_workflow_config"),
        (re.compile(r"^/api/memory$"), "handle_list_memory"),
    ]
    POST_ROUTES = [
        (re.compile(r"^/api/tasks$"), "handle_create_task"),
        (re.compile(r"^/api/tasks/([\w-]+)/run$"), "handle_run"),
        (re.compile(r"^/api/tasks/([\w-]+)/continue$"), "handle_continue"),
        (re.compile(r"^/api/tasks/([\w-]+)/interrupt$"), "handle_interrupt"),
        (re.compile(r"^/api/tasks/([\w-]+)/resume$"), "handle_resume"),
        (re.compile(r"^/api/tasks/([\w-]+)/deliver$"), "handle_deliver"),
        (re.compile(r"^/api/models/config$"), "handle_save_model_config"),
        (re.compile(r"^/api/workflow/config$"), "handle_save_workflow_config"),
        (re.compile(r"^/api/memory$"), "handle_add_memory"),
        (re.compile(r"^/api/memory/([\w-]+)/(approve|reject)$"), "handle_review_memory"),
    ]

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass  # 本机工具，静默访问日志

    # ---- 分发 ----

    def do_GET(self) -> None:
        self._dispatch(self.GET_ROUTES, needs_body=False)

    def do_POST(self) -> None:
        self._dispatch(self.POST_ROUTES, needs_body=True)

    def _dispatch(self, routes, needs_body: bool) -> None:
        parsed = urlsplit(self.path)
        self.query_params = parse_qs(parsed.query)
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

    def _kernel(self) -> WorkloopKernel:
        return WorkloopKernel(self.server.workloop_root)

    def _tasks_root(self) -> Path:
        return self.server.workloop_root / "tasks"

    def _query_value(self, name: str, default: str = "") -> str:
        values = self.query_params.get(name, [])
        return values[0] if values else default

    def _root_relative_path(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self.server.workloop_root / path

    def _model_config_payload(self, config_path: Path) -> dict:
        routing = load_routing_config(config_path)
        return {
            "path": str(config_path),
            "profiles": [to_plain(profile) for profile in routing.profiles.values()],
            "roles": dict(routing.roles),
        }

    def _normalize_model_config(self, body: dict) -> dict:
        raw_profiles = body.get("profiles", [])
        if not isinstance(raw_profiles, list):
            raise _HttpError(400, "profiles 必须是数组。")
        if not raw_profiles:
            raise _HttpError(400, "至少需要配置一个模型 profile。")

        profiles: list[dict] = []
        names: set[str] = set()
        for index, item in enumerate(raw_profiles, start=1):
            if not isinstance(item, dict):
                raise _HttpError(400, "每个模型 profile 必须是对象。")
            name = str(item.get("name", "")).strip()
            provider = str(item.get("provider", "")).strip()
            model = str(item.get("model", "")).strip()
            if not name:
                raise _HttpError(400, f"第 {index} 个模型配置缺少 name。")
            if name in names:
                raise _HttpError(400, f"模型配置名重复：{name}。")
            if not provider:
                raise _HttpError(400, f"模型配置 {name} 缺少 provider。")
            if not model:
                raise _HttpError(400, f"模型配置 {name} 缺少 model。")

            raw_command = item.get("command", [])
            if isinstance(raw_command, str):
                command = [part.strip() for part in raw_command.splitlines() if part.strip()]
            elif isinstance(raw_command, list):
                command = [str(part).strip() for part in raw_command if str(part).strip()]
            else:
                raise _HttpError(400, f"模型配置 {name} 的 command 必须是数组。")

            try:
                timeout_seconds = int(item.get("timeout_seconds", 300))
            except (TypeError, ValueError):
                raise _HttpError(400, f"模型配置 {name} 的 timeout_seconds 必须是整数。")
            if timeout_seconds <= 0:
                raise _HttpError(400, f"模型配置 {name} 的 timeout_seconds 必须大于 0。")
            if provider == "cli":
                if not command:
                    raise _HttpError(400, f"CLI 模型配置 {name} 缺少 command。")
                if not any("{prompt}" in part for part in command):
                    raise _HttpError(400, f"CLI 模型配置 {name} 的 command 缺少 {{prompt}} 占位符。")

            names.add(name)
            profiles.append(
                {
                    "name": name,
                    "provider": provider,
                    "model": model,
                    "command": command,
                    "timeout_seconds": timeout_seconds,
                }
            )

        raw_roles = body.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise _HttpError(400, "roles 必须是对象。")
        roles: dict[str, str] = {}
        for role, profile_name in raw_roles.items():
            role_name = str(role).strip()
            selected = str(profile_name).strip()
            if not role_name or not selected:
                continue
            if selected not in names:
                raise _HttpError(400, f"角色 {role_name} 引用了不存在的模型配置 {selected}。")
            roles[role_name] = selected
        if "default" not in roles:
            roles["default"] = profiles[0]["name"]

        return {"profiles": profiles, "roles": roles}

    def _save_model_config(self, path: Path, body: dict) -> dict:
        payload = self._normalize_model_config(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._model_config_payload(path)

    def _workflow_path(self) -> Path:
        raw_path = self._query_value("path", "workflow.json").strip() or "workflow.json"
        return self._root_relative_path(raw_path)

    def _workflow_config_payload(self, workflow_path: Path, models_config_path: Path) -> dict:
        routing = load_routing_config(models_config_path)
        if workflow_path.exists():
            data = json.loads(workflow_path.read_text(encoding="utf-8"))
            nodes = self._normalize_workflow_nodes(data.get("nodes", []), routing)
        else:
            nodes = self._default_workflow_nodes(routing)
        return {"path": str(workflow_path), "models_config": str(models_config_path), "nodes": nodes}

    def _default_workflow_nodes(self, routing) -> list[dict]:
        role = lambda name: routing.roles.get(name, routing.roles.get("default", ""))
        return [
            {"id": "parse", "label": "解析", "kind": "parse", "role": "planner", "profile": role("planner"), "enabled": True},
            {"id": "plan", "label": "制定计划", "kind": "planner", "role": "planner", "profile": role("planner"), "enabled": True},
            {"id": "execute", "label": "执行任务", "kind": "executor", "role": "executor", "profile": role("executor"), "enabled": True},
            {"id": "review", "label": "审核", "kind": "reviewer", "role": "reviewer", "profile": role("reviewer"), "enabled": True},
            {"id": "delivery", "label": "交付", "kind": "delivery", "role": "reviewer", "profile": role("reviewer"), "enabled": True},
        ]
    def _normalize_workflow_nodes(self, raw_nodes, routing) -> list[dict]:
        if not isinstance(raw_nodes, list):
            raise _HttpError(400, "workflow.nodes 必须是数组。")
        nodes: list[dict] = []
        for index, item in enumerate(raw_nodes, start=1):
            if not isinstance(item, dict):
                raise _HttpError(400, "每个流程节点必须是对象。")
            kind = str(item.get("kind", "planner")).strip()
            if kind not in WORKFLOW_KINDS:
                raise _HttpError(400, f"不支持的节点类型：{kind}。")
            role = str(item.get("role") or (kind if kind in ROLE_KINDS else "planner")).strip()
            profile = str(item.get("profile", "")).strip()
            if kind in ROLE_KINDS:
                if role not in ROLE_KINDS:
                    raise _HttpError(400, f"节点 {index} 的 role 不支持：{role}。")
                if not profile:
                    profile = routing.roles.get(role, routing.roles.get("default", ""))
                if profile not in routing.profiles:
                    raise _HttpError(400, f"节点 {index} 引用了不存在的模型配置 {profile}。")
            elif profile and profile not in routing.profiles:
                raise _HttpError(400, f"节点 {index} 引用了不存在的模型配置 {profile}。")
            nodes.append(
                {
                    "id": str(item.get("id") or f"node-{index}").strip() or f"node-{index}",
                    "label": str(item.get("label") or kind).strip() or kind,
                    "kind": kind,
                    "role": role,
                    "profile": profile,
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        if not any(node["enabled"] and node["kind"] == "planner" for node in nodes):
            raise _HttpError(400, "流程必须至少包含一个启用的制定计划节点。")
        if not any(node["enabled"] and node["kind"] == "executor" for node in nodes):
            raise _HttpError(400, "流程必须至少包含一个启用的执行任务节点。")
        if not any(node["enabled"] and node["kind"] == "reviewer" for node in nodes):
            raise _HttpError(400, "流程必须至少包含一个启用的审核节点。")
        return nodes

    def _apply_workflow_roles(self, routing, raw_workflow) -> None:
        if raw_workflow is None:
            workflow_path = self.server.workloop_root / "workflow.json"
            if not workflow_path.exists():
                return
            raw_workflow = _read_json_if_exists(workflow_path)
        if not isinstance(raw_workflow, dict):
            return
        nodes = self._normalize_workflow_nodes(raw_workflow.get("nodes", []), routing)
        seen: set[str] = set()
        for node in nodes:
            role = node["role"]
            profile = node["profile"]
            if not node["enabled"] or node["kind"] not in ROLE_KINDS or role in seen or not profile:
                continue
            routing.roles[role] = profile
            seen.add(role)

    def _save_workflow_config(self, path: Path, models_config_path: Path, body: dict) -> dict:
        routing = load_routing_config(models_config_path)
        nodes = self._normalize_workflow_nodes(body.get("nodes", []), routing)
        payload = {"nodes": nodes, "updated_at": utc_now()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(path), "models_config": str(models_config_path), **payload}


    def _model_calls(self, task_dir: Path) -> list[dict]:
        calls_dir = task_dir / "artifacts" / "model_calls"
        if not calls_dir.is_dir():
            return []

        calls: list[dict] = []
        for call_dir in calls_dir.iterdir():
            if not call_dir.is_dir():
                continue
            meta = _read_json_if_exists(call_dir / "meta.json")
            if not isinstance(meta, dict):
                continue
            index, role_from_name = self._parse_call_dir(call_dir.name)
            status = str(meta.get("status") or "")
            if not status:
                if meta.get("succeeded") is True:
                    status = "succeeded"
                elif meta.get("succeeded") is False:
                    status = "failed"
                else:
                    status = "unknown"
            elapsed = self._elapsed_seconds(meta.get("started_at")) if status == "running" else None
            calls.append(
                {
                    "call_index": int(meta.get("call_index") or index),
                    "role": str(meta.get("role") or role_from_name),
                    "status": status,
                    "profile": str(meta.get("profile") or meta.get("profile_name") or ""),
                    "provider": str(meta.get("provider") or ""),
                    "model": str(meta.get("model") or ""),
                    "command": list(meta.get("command") or []),
                    "fallback": bool(meta.get("fallback", False)),
                    "started_at": str(meta.get("started_at") or ""),
                    "finished_at": str(meta.get("finished_at") or ""),
                    "duration_seconds": meta.get("duration_seconds"),
                    "elapsed_seconds": elapsed,
                    "succeeded": meta.get("succeeded"),
                    "error": str(meta.get("error") or ""),
                }
            )
        calls.sort(key=lambda item: item["call_index"])
        return calls

    def _parse_call_dir(self, name: str) -> tuple[int, str]:
        match = re.match(r"^(\d+)-(.+)$", name)
        if not match:
            return 0, name
        return int(match.group(1)), match.group(2)

    def _elapsed_seconds(self, started_at) -> float | None:
        if not started_at:
            return None
        try:
            started = datetime.fromisoformat(str(started_at))
        except ValueError:
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - started).total_seconds(), 1)

    def _apply_role_overrides(self, routing, raw_roles) -> None:
        if raw_roles is None:
            return
        if not isinstance(raw_roles, dict):
            raise _HttpError(400, "roles 必须是 JSON 对象。")
        for raw_role, raw_profile in raw_roles.items():
            role = str(raw_role).strip()
            profile_name = str(raw_profile).strip()
            if not role or not profile_name:
                continue
            if profile_name not in routing.profiles:
                raise _HttpError(400, f"角色 {role} 引用了不存在的模型配置 {profile_name}。")
            routing.roles[role] = profile_name

    def _prepare_run(self, body: dict):
        models_config = str(body.get("models_config", "models.json")).strip() or "models.json"
        routing = load_routing_config(self._root_relative_path(models_config))
        self._apply_workflow_roles(routing, body.get("workflow"))
        self._apply_role_overrides(routing, body.get("roles"))

        workspace_from_raw = str(body.get("workspace_from", "") or "").strip()
        workspace_from = self._root_relative_path(workspace_from_raw) if workspace_from_raw else None
        if workspace_from is not None and not workspace_from.is_dir():
            raise _HttpError(400, f"播种目录 {workspace_from} 不存在。")
        return routing, workspace_from

    def _start_run(self, task_id: str, routing, workspace_from: Path | None, before_start=None) -> dict:
        registry = self.server.registry
        if not registry.try_start(task_id):
            raise _HttpError(409, "该任务正在执行中。")

        clear_task_cancel(task_id)
        root = self.server.workloop_root

        try:
            if before_start is not None:
                before_start()
        except Exception:
            registry.finish(task_id)
            raise

        def worker() -> None:
            error = ""
            try:
                WorkloopKernel(root).run_model_loop(
                    task_id, routing, build_backends(), workspace_from=workspace_from
                )
            except Exception as exc:  # noqa: BLE001 - 后台线程兜底，错误面向页面展示
                error = str(exc)
            finally:
                registry.finish(task_id, error)

        threading.Thread(target=worker, name=f"run-loop-{task_id}", daemon=True).start()
        return {"started": True, "task_id": task_id, "roles": dict(routing.roles)}

    # ---- GET ----

    def handle_index(self) -> None:
        page = STATIC_DIR / "index.html"
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_list_tasks(self) -> None:
        items = []
        tasks_root = self._tasks_root()
        if tasks_root.is_dir():
            for state_path in tasks_root.glob("*/state.json"):
                state = _read_json_if_exists(state_path)
                if not state:
                    continue
                task_id = state.get("task_id", state_path.parent.name)
                items.append(
                    {
                        "task_id": task_id,
                        "title": state.get("title", ""),
                        "status": state.get("status", ""),
                        "iteration": state.get("iteration", 0),
                        "updated_at": state.get("updated_at", ""),
                        "running": self.server.registry.is_running(task_id),
                    }
                )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        self._send_json(200, items)

    def handle_task_detail(self, task_id: str) -> None:
        kernel = self._kernel()
        task = kernel.store.load_task(task_id)  # 不存在 -> FileNotFoundError -> 404
        task_dir = self._tasks_root() / task_id

        rounds = []
        rounds_dir = task_dir / "artifacts" / "rounds"
        if rounds_dir.is_dir():
            for round_dir in sorted(rounds_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
                if not round_dir.is_dir():
                    continue
                rounds.append(
                    {
                        "index": round_dir.name,
                        "changes": _read_json_if_exists(round_dir / "changes.json"),
                        "policy_check": _read_json_if_exists(round_dir / "policy_check.json"),
                        "diff": _read_text_if_exists(round_dir / "changes.diff"),
                        "review": _read_json_if_exists(round_dir / "review.json"),
                    }
                )

        running = self.server.registry.is_running(task_id)
        detail = {
            "task": to_plain(task),
            "running": running,
            "run_error": self.server.registry.error_of(task_id),
            "plan": _read_text_if_exists(task_dir / "artifacts" / "plan.md"),
            "rounds": rounds,
            "model_calls": self._model_calls(task_dir),
            "pending_questions": kernel.pending_questions(task_id),
            "pending_delivery": [],
            "delivery": _read_json_if_exists(task_dir / "artifacts" / "delivery.json"),
        }
        if task.status.value == "done" and not running:
            detail["pending_delivery"] = to_plain(kernel.pending_delivery(task_id))
        self._send_json(200, detail)

    def handle_model_config(self) -> None:
        raw_path = self._query_value("path", "models.json").strip() or "models.json"
        self._send_json(200, self._model_config_payload(self._root_relative_path(raw_path)))

    def handle_save_model_config(self, body: dict) -> None:
        raw_path = self._query_value("path", "models.json").strip() or "models.json"
        payload = self._save_model_config(self._root_relative_path(raw_path), body)
        self._send_json(200, payload)

    def handle_workflow_config(self) -> None:
        raw_models = self._query_value("models_config", "models.json").strip() or "models.json"
        payload = self._workflow_config_payload(self._workflow_path(), self._root_relative_path(raw_models))
        self._send_json(200, payload)

    def handle_list_memory(self) -> None:
        records = self._kernel().experience.list_all()
        records.sort(key=lambda record: record.updated_at, reverse=True)
        self._send_json(200, [to_plain(record) for record in records])

    def handle_save_workflow_config(self, body: dict) -> None:
        raw_models = self._query_value("models_config", "models.json").strip() or "models.json"
        payload = self._save_workflow_config(
            self._workflow_path(),
            self._root_relative_path(raw_models),
            body,
        )
        self._send_json(200, payload)

    # ---- POST ----

    def handle_create_task(self, body: dict) -> None:
        title = str(body.get("title", "")).strip()
        goal = str(body.get("goal", "")).strip()
        raw_input = str(body.get("input", ""))
        if not title or not goal:
            raise _HttpError(400, "title 和 goal 不能为空。")
        context_files = [Path(str(item)) for item in body.get("context_files", []) if str(item).strip()]
        task = self._kernel().create_task(title, goal, raw_input, context_files=context_files)
        self._send_json(200, to_plain(task))

    def handle_run(self, task_id: str, body: dict) -> None:
        task = self._kernel().store.load_task(task_id)
        if task.status is not TaskStatus.READY_FOR_PLAN:
            raise _HttpError(400, f"任务状态为 {task.status.value}，只有 ready_for_plan 可以开始执行。")

        routing, workspace_from = self._prepare_run(body)
        payload = self._start_run(task_id, routing, workspace_from)
        self._send_json(202, payload)

    def handle_continue(self, task_id: str, body: dict) -> None:
        kernel = self._kernel()
        task = kernel.store.load_task(task_id)
        if task.status is not TaskStatus.FAILED:
            raise _HttpError(400, f"任务状态为 {task.status.value}，只有 failed 可以继续执行。")

        routing, workspace_from = self._prepare_run(body)

        def mark_ready() -> None:
            task.transition(TaskStatus.READY_FOR_PLAN)
            kernel.store.save_task(task)

        payload = self._start_run(task_id, routing, workspace_from, before_start=mark_ready)
        self._send_json(202, payload)

    def handle_interrupt(self, task_id: str, body: dict) -> None:
        if not self.server.registry.is_running(task_id):
            raise _HttpError(409, "该任务当前没有在执行。")
        killed = cancel_task_processes(task_id)
        self._send_json(
            200,
            {"interrupted": True, "task_id": task_id, "killed_processes": killed},
        )

    def handle_resume(self, task_id: str, body: dict) -> None:
        answer = str(body.get("answer", "")).strip()
        if not answer:
            raise _HttpError(400, "answer 不能为空。")
        task = self._kernel().resume_task(task_id, answer)
        self._send_json(200, to_plain(task))

    def handle_deliver(self, task_id: str, body: dict) -> None:
        if body.get("confirm") is not True:
            raise _HttpError(400, "缺少交付确认（confirm 必须为 true）。")
        dest = str(body.get("dest", "")).strip()
        if not dest:
            raise _HttpError(400, "dest 不能为空。")
        if self.server.registry.is_running(task_id):
            raise _HttpError(409, "该任务正在执行中。")
        delivered = self._kernel().deliver(task_id, Path(dest))
        self._send_json(200, {"delivered": to_plain(delivered), "dest": dest})

    def handle_add_memory(self, body: dict) -> None:
        text = str(body.get("text", "")).strip()
        if not text:
            raise _HttpError(400, "text 不能为空。")
        record = self._kernel().experience.add_manual(text)
        self._send_json(200, to_plain(record))

    def handle_review_memory(self, experience_id: str, action: str, body: dict) -> None:
        store = self._kernel().experience
        # 不存在 -> FileNotFoundError -> 404
        record = store.approve(experience_id) if action == "approve" else store.reject(experience_id)
        self._send_json(200, to_plain(record))


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class WorkloopServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, port: int):
        super().__init__(("127.0.0.1", port), WorkloopRequestHandler)
        self.workloop_root = Path(root).resolve()
        self.registry = RunRegistry()


def make_server(root: Path, port: int = 8765) -> WorkloopServer:
    return WorkloopServer(root, port)
