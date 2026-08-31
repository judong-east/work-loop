from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.application.collaboration import CollaborationService
from app.application.decomposition import GoalDecomposer
from app.application.workbench import WorkbenchService
from app.domain.models import SessionMode, WorkflowDefinition, WorkflowNode


MAX_BODY_BYTES = 10 * 1024 * 1024
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _resolve_static_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app" / "web" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _resolve_static_dir()


class WorkloopRequestHandler(BaseHTTPRequestHandler):
    server: "WorkloopServer"
    query_params: dict[str, list[str]]

    GET_ROUTES = [
        (re.compile(r"^/$"), "handle_index"),
        (re.compile(r"^/workbench/?$"), "handle_workbench"),
        (re.compile(r"^/static/(workbench\.css|workbench\.js|collaboration\.js|app-icon\.png|logo-wide\.png|logo\.svg)$"), "handle_asset"),
        (re.compile(r"^/api/v2/catalog$"), "handle_catalog"),
        (re.compile(r"^/api/v2/strategies$"), "handle_strategies"),
        (re.compile(r"^/api/v2/projects$"), "handle_projects"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/sessions$"), "handle_project_sessions"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/workspace$"), "handle_workspace"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/collaboration$"), "handle_collaboration"),
        (re.compile(r"^/api/v2/roles$"), "handle_roles"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)$"), "handle_session"),
        (re.compile(r"^/api/v2/resources$"), "handle_resources"),
        (re.compile(r"^/api/v2/events$"), "handle_events"),
    ]
    POST_ROUTES = [
        (re.compile(r"^/api/v2/projects$"), "handle_create_project"),
        (re.compile(r"^/api/v2/projects/([\w-]+)$"), "handle_update_project"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/sessions$"), "handle_create_session"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/tasks$"), "handle_create_collaboration_task"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/coordinate$"), "handle_coordinate"),
        (re.compile(r"^/api/v2/tasks/([\w-]+)$"), "handle_update_collaboration_task"),
        (re.compile(r"^/api/v2/projects/([\w-]+)/goals$"), "handle_decompose_goal"),
        (re.compile(r"^/api/v2/tasks/([\w-]+)/retry$"), "handle_retry_collaboration_task"),
        (re.compile(r"^/api/v2/roles$"), "handle_save_role"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/messages$"), "handle_message"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/run$"), "handle_run"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/policy$"), "handle_update_policy"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/policy/approve$"), "handle_approve_policy"),
        (re.compile(r"^/api/v2/sessions/([\w-]+)/policy/replan$"), "handle_replan_policy"),
        (re.compile(r"^/api/v2/resources/providers$"), "handle_save_provider"),
        (re.compile(r"^/api/v2/resources/providers/([\w-]+)/models/discover$"), "handle_discover_provider_models"),
        (re.compile(r"^/api/v2/resources/providers/([\w-]+)/test$"), "handle_test_provider"),
        (re.compile(r"^/api/v2/resources/models$"), "handle_save_model"),
        (re.compile(r"^/api/v2/nodes$"), "handle_save_node"),
        (re.compile(r"^/api/v2/workflows$"), "handle_save_workflow"),
    ]
    DELETE_ROUTES = [
        (re.compile(r"^/api/v2/sessions/([\w-]+)$"), "handle_delete_session"),
        (re.compile(r"^/api/v2/resources/providers/([\w-]+)$"), "handle_delete_provider"),
        (re.compile(r"^/api/v2/resources/models/([\w-]+)$"), "handle_delete_model"),
        (re.compile(r"^/api/v2/nodes/([\w-]+)$"), "handle_delete_node"),
        (re.compile(r"^/api/v2/workflows/([\w-]+)$"), "handle_delete_workflow"),
        (re.compile(r"^/api/v2/roles/([\w-]+)$"), "handle_delete_role"),
        (re.compile(r"^/api/v2/goals/([\w-]+)$"), "handle_delete_goal"),
        (re.compile(r"^/api/v2/tasks/([\w-]+)$"), "handle_delete_collaboration_task"),
    ]

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        del format, args

    def do_GET(self) -> None:
        self._dispatch(self.GET_ROUTES, needs_body=False)

    def do_POST(self) -> None:
        self._dispatch(self.POST_ROUTES, needs_body=True)

    def do_DELETE(self) -> None:
        self._dispatch(self.DELETE_ROUTES, needs_body=False)

    def _dispatch(self, routes, *, needs_body: bool) -> None:
        parsed = urlsplit(self.path)
        self.query_params = parse_qs(parsed.query)
        rejection = self._same_origin_rejection(self.command not in {"GET", "HEAD"})
        if rejection:
            self._drain_body()
            self._send_json(403, {"error": rejection})
            return
        for pattern, handler_name in routes:
            match = pattern.fullmatch(parsed.path)
            if match is None:
                continue
            try:
                body = self._read_json_body() if needs_body else None
                handler = getattr(self, handler_name)
                if needs_body:
                    handler(*match.groups(), body)
                else:
                    handler(*match.groups())
            except _HttpError as error:
                self._send_json(error.status, {"error": error.message})
            except FileNotFoundError as error:
                self._send_json(404, {"error": str(error)})
            except (KeyError, TypeError, ValueError) as error:
                self._send_json(400, {"error": str(error)})
            except Exception as error:  # noqa: BLE001
                self._send_json(500, {"error": str(error)})
            return
        self._send_json(404, {"error": "接口不存在。当前服务仅提供 /api/v2。"})

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise _HttpError(400, "Content-Length 不合法。") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise _HttpError(413, "请求体过大。")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _HttpError(400, "请求体必须是 UTF-8 JSON 对象。") from error
        if not isinstance(value, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象。")
        return value

    def _drain_body(self) -> None:
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), MAX_BODY_BYTES)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(length)

    def _same_origin_rejection(self, mutating: bool) -> str:
        if not mutating:
            return ""
        host_name = urlsplit(f"//{self.headers.get('Host', '')}").hostname or ""
        if host_name not in _LOCAL_HOSTS:
            return "请求 Host 不是本机地址。"
        origin = self.headers.get("Origin", "")
        if not origin:
            return ""
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOCAL_HOSTS:
            return "拒绝跨站写请求。"
        if parsed.netloc.lower() != self.headers.get("Host", "").lower():
            return "拒绝不同端口的写请求。"
        return ""

    def handle_index(self) -> None:
        self._send_file(STATIC_DIR / "workbench.html", "text/html; charset=utf-8")

    def handle_workbench(self) -> None:
        self.send_response(308)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_asset(self, filename: str) -> None:
        if filename.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif filename.endswith(".svg"):
            content_type = "image/svg+xml"
        elif filename.endswith(".png"):
            content_type = "image/png"
        else:
            content_type = "text/javascript; charset=utf-8"
        self._send_file(STATIC_DIR / filename, content_type)

    def handle_catalog(self) -> None:
        self._send_json(200, {
            "schema_version": 2,
            "nodes": self.server.workbench.list_node_types(),
            "workflows": [self._workflow_payload(item) for item in self.server.workbench.list_workflows()],
        })

    def handle_strategies(self) -> None:
        self._send_json(200, self.server.workbench.list_strategies())

    def handle_projects(self) -> None:
        self._send_json(200, [item.to_dict() for item in self.server.workbench.list_projects()])

    def handle_create_project(self, body: dict) -> None:
        commands = body.get("validation_commands", [])
        if not isinstance(commands, list):
            raise _HttpError(400, "validation_commands 必须是数组。")
        knowledge_refs = body.get("knowledge_refs", [])
        if not isinstance(knowledge_refs, list):
            raise _HttpError(400, "knowledge_refs 必须是数组。")
        project = self.server.workbench.create_project(
            str(body.get("name", "")),
            instructions=str(body.get("instructions", "")),
            knowledge_refs=knowledge_refs,
            default_model=str(body.get("default_model", "")),
            workspace_path=str(body.get("workspace_path", "")),
            validation_commands=commands,
        )
        self._send_json(201, project.to_dict())

    def handle_project_sessions(self, project_id: str) -> None:
        self.server.workbench.get_project(project_id)
        self._send_json(200, [item.to_dict() for item in self.server.workbench.list_sessions(project_id)])

    def handle_update_project(self, project_id: str, body: dict) -> None:
        project = self.server.workbench.update_project(project_id, body)
        self._send_json(200, project.to_dict())

    def handle_workspace(self, project_id: str) -> None:
        self._send_json(200, self.server.workbench.workspace_status(project_id))

    def handle_collaboration(self, project_id: str) -> None:
        self._send_json(200, self.server.collaboration.project_state(project_id))

    def handle_roles(self) -> None:
        self._send_json(200, [item.to_dict() for item in self.server.collaboration.list_roles()])

    def handle_create_session(self, project_id: str, body: dict) -> None:
        mode = SessionMode(str(body.get("mode", SessionMode.CHAT.value)))
        session = self.server.workbench.create_session(
            project_id,
            str(body.get("title", "未命名会话")),
            mode=mode,
            workflow_id=str(body.get("workflow_id", "")),
            policy=body.get("policy") if isinstance(body.get("policy"), dict) else None,
        )
        self._send_json(201, session.to_dict())

    def handle_create_collaboration_task(self, project_id: str, body: dict) -> None:
        dependencies = body.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise _HttpError(400, "depends_on 必须是数组。")
        task = self.server.collaboration.create_task(project_id, body)
        self._send_json(201, task.to_dict())

    def handle_coordinate(self, project_id: str, body: dict) -> None:
        if not body.get("async"):
            self._send_json(200, self.server.collaboration.coordinate(project_id))
            return
        with self.server._coordination_guard:
            if (
                project_id in self.server._coordinating_projects
                or self.server.collaboration.is_coordinating(project_id)
            ):
                raise _HttpError(409, "该项目正在协同执行中。")
            self.server._coordinating_projects.add(project_id)
        threading.Thread(
            target=self._coordinate_worker, args=(project_id,), daemon=True
        ).start()
        self._send_json(202, {"started": True, "project_id": project_id})

    def _coordinate_worker(self, project_id: str) -> None:
        try:
            self.server.collaboration.coordinate(project_id)
        except Exception as error:  # noqa: BLE001 - surface background failures in the event log
            try:
                self.server.workbench.record_event(
                    "coordination_failed",
                    {"project_id": project_id, "error": str(error)},
                )
            except OSError:
                pass
        finally:
            with self.server._coordination_guard:
                self.server._coordinating_projects.discard(project_id)

    def handle_update_collaboration_task(self, task_id: str, body: dict) -> None:
        self._send_json(200, self.server.collaboration.update_task(task_id, body).to_dict())

    def handle_events(self) -> None:
        after = int(self.query_params.get("after", ["0"])[0])
        limit = int(self.query_params.get("limit", ["500"])[0])
        self._send_json(200, self.server.workbench.read_events(after=after, limit=limit))

    def handle_decompose_goal(self, project_id: str, body: dict) -> None:
        subtasks = body.get("subtasks")
        result = self.server.decomposition.decompose(
            project_id,
            str(body.get("goal", "")),
            max_subtasks=int(body.get("max_subtasks", 8)),
            subtasks=subtasks if isinstance(subtasks, list) else None,
            auto_coordinate=bool(body.get("auto_coordinate", False)),
        )
        self._send_json(201, result)

    def handle_delete_goal(self, goal_id: str) -> None:
        self.server.collaboration.delete_goal(goal_id)
        self._send_json(200, {"deleted": goal_id})

    def handle_retry_collaboration_task(self, task_id: str, body: dict) -> None:
        del body
        self._send_json(200, self.server.collaboration.retry_task(task_id).to_dict())

    def handle_save_role(self, body: dict) -> None:
        self._send_json(201, self.server.collaboration.save_role(body).to_dict())

    def handle_session(self, session_id: str) -> None:
        self._send_json(200, self.server.workbench.get_session(session_id).to_dict())

    def handle_delete_session(self, session_id: str) -> None:
        self.server.workbench.delete_session(session_id)
        self._send_json(200, {"deleted": session_id})

    def handle_message(self, session_id: str, body: dict) -> None:
        session = self.server.workbench.send_message(session_id, str(body.get("content", "")))
        self._send_json(200, session.to_dict())

    def handle_run(self, session_id: str, body: dict) -> None:
        workflow = self._workflow_from_dict(body["workflow"]) if isinstance(body.get("workflow"), dict) else None
        self._send_json(200, self.server.workbench.run_task(session_id, workflow).to_dict())

    def handle_update_policy(self, session_id: str, body: dict) -> None:
        self._send_json(200, self.server.workbench.update_policy(session_id, body).to_dict())

    def handle_approve_policy(self, session_id: str, body: dict) -> None:
        del body
        self._send_json(200, self.server.workbench.approve_policy(session_id).to_dict())

    def handle_replan_policy(self, session_id: str, body: dict) -> None:
        session = self.server.workbench.replan_policy(session_id, reason=str(body.get("reason", "")))
        self._send_json(200, session.to_dict())

    def handle_resources(self) -> None:
        self._send_json(200, self.server.workbench.resource_status())

    def handle_save_provider(self, body: dict) -> None:
        self._send_json(201, self.server.workbench.save_provider(body).to_dict())

    def handle_save_model(self, body: dict) -> None:
        self._send_json(201, self.server.workbench.save_model(body).to_dict())

    def handle_discover_provider_models(self, provider_id: str, body: dict) -> None:
        self._send_json(
            200,
            self.server.workbench.discover_provider_models(
                provider_id,
                str(body.get("protocol", "")),
            ),
        )

    def handle_test_provider(self, provider_id: str, body: dict) -> None:
        del body
        self._send_json(200, self.server.workbench.test_provider(provider_id))

    def handle_delete_provider(self, provider_id: str) -> None:
        self.server.workbench.delete_provider(provider_id)
        self._send_json(200, {"deleted": provider_id})

    def handle_delete_model(self, alias: str) -> None:
        task_references = self.server.collaboration.tasks_using_model(alias)
        if task_references:
            raise ValueError(f"模型仍被协同任务使用：{', '.join(task_references)}。请先更换任务模型。")
        self.server.workbench.ensure_model_deletable(alias)
        alternatives = [
            model.alias
            for model in self.server.workbench.resources.list_models()
            if model.alias != alias and model.enabled
        ]
        binding_changes = self.server.collaboration.release_model_bindings(
            alias, alternatives[0] if alternatives else ""
        )
        self.server.workbench.delete_model(alias)
        self._send_json(200, {"deleted": alias, **binding_changes})

    def handle_save_node(self, body: dict) -> None:
        self._send_json(201, self._node_payload(self.server.workbench.save_node(body)))

    def handle_delete_node(self, node_type: str) -> None:
        role_references = self.server.collaboration.roles_using_node(node_type)
        if role_references:
            raise ValueError(f"node is still used by roles: {', '.join(role_references)}")
        self.server.workbench.delete_node(node_type)
        self._send_json(200, {"deleted": node_type})

    def handle_save_workflow(self, body: dict) -> None:
        workflow = self.server.workbench.save_workflow(self._workflow_from_dict(body))
        self._send_json(201, self._workflow_payload(workflow))

    def handle_delete_workflow(self, workflow_id: str) -> None:
        self.server.workbench.delete_workflow(workflow_id)
        self._send_json(200, {"deleted": workflow_id})

    def handle_delete_role(self, role_id: str) -> None:
        self.server.collaboration.delete_role(role_id)
        self._send_json(200, {"deleted": role_id})

    def handle_delete_collaboration_task(self, task_id: str) -> None:
        self.server.collaboration.delete_task(task_id)
        self._send_json(200, {"deleted": task_id})

    @staticmethod
    def _node_payload(node) -> dict:
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
    def _workflow_payload(workflow: WorkflowDefinition) -> dict:
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
    def _workflow_from_dict(value: dict) -> WorkflowDefinition:
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

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            raise _HttpError(404, "静态资源不存在。")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
        self.workbench = WorkbenchService(self.workloop_root / "workbench")
        self.collaboration = CollaborationService(
            self.workloop_root / "workbench",
            validate_role=self.workbench.validate_role,
            execute_task=self.workbench.execute_role_task,
            validate_project=self.workbench.get_project,
            validate_model=self.workbench.validate_model_alias,
            default_model_for_node=self.workbench.default_model_for_node,
        )
        self._coordination_guard = threading.Lock()
        self._coordinating_projects: set[str] = set()
        self.decomposition = GoalDecomposer(
            gateway=self.workbench.gateway,
            collaboration=self.collaboration,
            project_loader=self.workbench.get_project,
            project_context=WorkbenchService._project_context,
            workspace_snapshot=self.workbench.workspace_runtime.snapshot,
        )


def make_server(root: Path, port: int = 8765, open_browser: bool = False) -> WorkloopServer:
    server = WorkloopServer(root, port)
    if open_browser:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    return server
