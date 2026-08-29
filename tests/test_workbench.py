from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from threading import Thread
from unittest import mock

from app.application.workbench import WorkbenchService
from app.domain.models import NodeDefinition, Session, SessionMode, TaskPolicy, WorkflowDefinition, WorkflowNode
from app.domain.node_registry import NodeRegistry
from app.domain.orchestrator import DagOrchestrator
from app.domain.strategy_presets import list_strategy_presets
from app.infrastructure.resource_center import ResourceCenter
from app.infrastructure.model_gateway import OpenAICompatibleGateway
from app.web.server import make_server


class RecordingGateway:
    def __init__(self):
        self.calls: list[str] = []

    def complete(self, *, model_alias, node, context):
        self.calls.append(node.node_id)
        if node.node_type == "requirement":
            return {"facts": {"understanding": node.node_id}, "acceptance_criteria": [], "open_questions": []}
        if node.node_type == "planning":
            return {"facts": {"steps": [node.node_id]}, "risks": [], "artifacts": {}}
        if node.node_type == "implementation":
            return {"facts": {"changes": node.node_id}, "artifacts": {}, "decisions": []}
        if node.node_type == "review":
            return {"facts": {"verdict": "pass"}, "issues": [], "decisions": []}
        return {"facts": {"checks": []}, "risks": [], "decisions": []}


class WorkspaceGateway(RecordingGateway):
    def __init__(self, file_path="hello.txt"):
        super().__init__()
        self.file_path = file_path
        self.review_context = None

    def complete(self, *, model_alias, node, context):
        if node.node_type == "implementation":
            self.calls.append(node.node_id)
            return {
                "changes": "write hello.txt",
                "file_changes": [{
                    "operation": "write", "path": self.file_path, "content": "hello\n",
                }],
                "artifacts": {}, "decisions": [],
            }
        if node.node_type == "tool":
            self.calls.append(node.node_id)
            return {"result": "模型已回答", "model": model_alias or "default"}
        if node.node_type == "review":
            self.review_context = context.to_dict()
        return super().complete(model_alias=model_alias, node=node, context=context)


class WorkbenchDomainTest(unittest.TestCase):
    def test_project_update_rejects_malformed_workspace_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp) / "data")
            project = service.create_project("Strict")
            with self.assertRaisesRegex(ValueError, "argv list"):
                service.update_project(project.project_id, {
                    "validation_commands": ["python -m unittest"],
                })
            with self.assertRaisesRegex(ValueError, "knowledge_refs"):
                service.update_project(project.project_id, {
                    "knowledge_refs": "README.md",
                })

    def test_registry_loads_custom_json_and_rejects_builtin_override(self):
        registry = NodeRegistry()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.json"
            path.write_text(json.dumps({"nodes": [{
                "node_type": "summarize", "label": "摘要", "output_fields": ["result"]
            }]}), encoding="utf-8")
            loaded = registry.load_file(path)
        self.assertEqual(loaded[0].node_type, "summarize")
        with self.assertRaises(ValueError):
            registry.register(NodeDefinition("planning", "替换", "不允许"))

    def test_orchestrator_persists_shared_context_and_topological_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp), gateway=RecordingGateway())
            project = service.create_project("DAG")
            session = service.create_session(project.project_id, "task", mode=SessionMode.TASK)
            session.add_message("user", "build an app")
            service.sessions.save(session, session.session_id)
            result = service.run_task(session.session_id)
            self.assertEqual(result.status, "completed")
            self.assertEqual(service.gateway.calls, ["requirement", "planning", "implementation", "review"])
            self.assertEqual(result.context.version, 9)
            restored = service.get_session(session.session_id)
            self.assertEqual(restored.status, "completed")
            self.assertTrue(any(message.node_id == "review" for message in restored.messages))

    def test_failure_policy_skip_allows_downstream_dag_to_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = NodeRegistry()
            registry.register(NodeDefinition("broken", "坏节点", "", output_fields=("result",)))
            registry.register_handler("broken", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
            registry.register(NodeDefinition("after", "后续", "", output_fields=("result",)))
            registry.register_handler("after", lambda _: {"result": "ok"})
            service = WorkbenchService(Path(tmp))
            service.registry = registry
            service.orchestrator = DagOrchestrator(registry, service.sessions, service.gateway)
            project = service.create_project("DAG")
            session = service.create_session(project.project_id, "task", mode=SessionMode.TASK)
            workflow = WorkflowDefinition("failure", "failure", [
                WorkflowNode("broken", "broken", on_failure="skip"),
                WorkflowNode("after", "after", ("broken",)),
            ])
            result = service.orchestrator.run(session, workflow)
            self.assertEqual(result.status, "completed")
            self.assertIn("result", result.context.facts)

    def test_resource_center_separates_credentials_from_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ModelAlias, ModelProvider
            provider = center.save_provider(
                ModelProvider("vendor", "Vendor", "https://example.test/v1"),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias("fast-model", provider.provider_id, "fast-1"))
            raw = (Path(tmp) / "providers.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-key-value", raw)
            self.assertEqual(center.credential("vendor"), "secret-key-value")
            center.save_provider(ModelProvider("vendor", "Vendor renamed", "https://example.test/v1"))
            self.assertTrue(center.list_providers()[0].credential_ref)
            self.assertEqual(center.credential("vendor"), "secret-key-value")
            resolved_provider, resolved_model = center.resolve("fast-model")
            self.assertEqual(resolved_provider.provider_id, "vendor")
            self.assertEqual(resolved_model.model, "fast-1")

    def test_provider_supports_multiple_protocol_models_and_auth_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ModelAlias, ModelProvider
            provider = center.save_provider(
                ModelProvider(
                    "gateway", "Gateway", "https://gateway.test/v1",
                    protocols=["openai", "claude"], auth_type="custom_header",
                    auth_header="X-Workspace-Key", auth_prefix="Token",
                ),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias("chat-model", provider.provider_id, "gpt-5", protocol="openai"))
            center.save_model(ModelAlias("claude-model", provider.provider_id, "claude-sonnet", protocol="claude"))
            self.assertEqual([item.protocol for item in center.list_models()], ["openai", "claude"])
            with self.assertRaisesRegex(ValueError, "still has models"):
                center.save_provider(ModelProvider(
                    "gateway", "Gateway", "https://gateway.test/v1", protocols=["openai"],
                    auth_type="custom_header", auth_header="X-Workspace-Key", auth_prefix="Token",
                ))
            with self.assertRaisesRegex(ValueError, "does not support"):
                other = center.save_provider(ModelProvider("openai-only", "OpenAI", "https://openai.test/v1"))
                center.save_model(ModelAlias("wrong", other.provider_id, "claude", protocol="claude"))

            no_auth = center.save_provider(ModelProvider(
                "local", "Local", "http://127.0.0.1:11434/v1", auth_type="none",
            ))
            self.assertTrue(next(item for item in center.health() if item["provider_id"] == no_auth.provider_id)["configured"])

    def test_openai_gateway_resolves_alias_and_parses_structured_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ContextState, ModelAlias, ModelProvider
            center.save_provider(
                ModelProvider("vendor", "Vendor", "https://example.test/v1"),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias("fast-model", "vendor", "fast-1", ["planning"]))

            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return None
                def read(self):
                    return json.dumps({"choices": [{"message": {"content": json.dumps({
                        "steps": ["one"], "risks": [], "artifacts": {}
                    })}}]}).encode()

            with mock.patch("urllib.request.urlopen", return_value=Response()) as opener:
                output = OpenAICompatibleGateway(center).complete(
                    model_alias="",
                    node=WorkflowNode("plan", "planning"),
                    context=ContextState(inputs={"request": "build"}),
                )
            self.assertEqual(output["steps"], ["one"])
            self.assertEqual(output["model"], "fast-model")
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer secret-key-value")

    def test_claude_gateway_uses_messages_protocol_and_api_key_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ContextState, ModelAlias, ModelProvider
            center.save_provider(
                ModelProvider(
                    "anthropic", "Anthropic", "https://api.anthropic.com",
                    protocols=["claude"], auth_type="api_key", auth_prefix="",
                ),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias(
                "claude-model", "anthropic", "claude-sonnet", ["review"], protocol="claude",
            ))

            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return None
                def read(self):
                    return json.dumps({"content": [{"type": "text", "text": json.dumps({
                        "verdict": "pass", "issues": [], "decisions": [],
                    })}]}).encode()

            with mock.patch("urllib.request.urlopen", return_value=Response()) as opener:
                output = OpenAICompatibleGateway(center).complete(
                    model_alias="claude-model",
                    node=WorkflowNode("review", "review"),
                    context=ContextState(inputs={"request": "review"}),
                )
            self.assertEqual(output["verdict"], "pass")
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
            self.assertEqual(request.headers["X-api-key"], "secret-key-value")
            self.assertEqual(request.headers["Anthropic-version"], "2023-06-01")
            payload = json.loads(request.data.decode())
            self.assertIn("system", payload)
            self.assertEqual(payload["messages"][0]["role"], "user")
            self.assertEqual(payload["max_tokens"], 4096)

    def test_custom_header_auth_is_applied_without_leaking_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ContextState, ModelAlias, ModelProvider
            center.save_provider(
                ModelProvider(
                    "custom", "Custom", "https://custom.test/v1",
                    auth_type="custom_header", auth_header="X-Auth-Token", auth_prefix="Key",
                ),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias("custom-model", "custom", "model-1"))

            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return None
                def read(self):
                    return json.dumps({"choices": [{"message": {"content": json.dumps({"result": "ok"})}}]}).encode()

            with mock.patch("urllib.request.urlopen", return_value=Response()) as opener:
                OpenAICompatibleGateway(center).complete(
                    model_alias="custom-model", node=WorkflowNode("tool", "tool"), context=ContextState(),
                )
            self.assertEqual(opener.call_args.args[0].headers["X-auth-token"], "Key secret-key-value")
            raw = (Path(tmp) / "providers.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-key-value", raw)

    def test_gateway_probe_supports_openai_and_claude_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ModelAlias, ModelProvider
            center.save_provider(
                ModelProvider(
                    "gateway", "Gateway", "https://gateway.test/v1",
                    protocols=["openai", "claude"], auth_type="api_key", auth_prefix="",
                ),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias("chat", "gateway", "chat-1", protocol="openai"))
            center.save_model(ModelAlias("claude", "gateway", "claude-1", protocol="claude"))

            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return None
                def read(self, *_): return b"{}"

            with mock.patch("urllib.request.urlopen", return_value=Response()) as opener:
                openai_result = OpenAICompatibleGateway(center).probe("chat")
                openai_request = opener.call_args.args[0]
                claude_result = OpenAICompatibleGateway(center).probe("claude")
                claude_request = opener.call_args.args[0]

            self.assertTrue(openai_result["ok"])
            self.assertEqual(openai_request.full_url, "https://gateway.test/v1/chat/completions")
            self.assertEqual(openai_request.headers["X-api-key"], "secret-key-value")
            self.assertEqual(json.loads(openai_request.data.decode())["model"], "chat-1")
            self.assertTrue(claude_result["ok"])
            self.assertEqual(claude_request.full_url, "https://gateway.test/v1/messages")
            self.assertEqual(claude_request.headers["X-api-key"], "secret-key-value")
            self.assertEqual(claude_request.headers["Anthropic-version"], "2023-06-01")

    def test_gateway_probe_maps_authentication_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            center = ResourceCenter(Path(tmp))
            from app.domain.models import ModelAlias, ModelProvider
            center.save_provider(
                ModelProvider("vendor", "Vendor", "https://example.test/v1"),
                api_key="secret-key-value",
            )
            center.save_model(ModelAlias("model", "vendor", "model-1"))
            response = urllib.error.HTTPError(
                "https://example.test/v1/chat/completions", 401, "Unauthorized", None,
                io.BytesIO(b'{"error":"bad key"}'),
            )
            with mock.patch("urllib.request.urlopen", side_effect=response):
                result = OpenAICompatibleGateway(center).probe("model")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_type"], "authentication_failed")
            self.assertIn("HTTP 401", result["error"])

    def test_provider_test_probes_one_model_per_protocol_and_persists_health(self):
        class ProbeGateway:
            def __init__(self):
                self.calls = []

            def complete(self, **_):
                return {}

            def probe(self, alias):
                self.calls.append(alias)
                protocol = "claude" if alias.startswith("claude") else "openai"
                return {
                    "ok": True, "alias": alias, "protocol": protocol,
                    "error_type": "", "error": "", "latency_ms": 12,
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = ProbeGateway()
            service = WorkbenchService(root, gateway=gateway)
            service.save_provider({
                "provider_id": "gateway", "label": "Gateway", "base_url": "https://gateway.test/v1",
                "protocols": ["openai", "claude"], "auth_type": "none",
            })
            service.save_model({"alias": "chat-a", "provider_id": "gateway", "model": "chat-a", "protocol": "openai"})
            service.save_model({"alias": "chat-b", "provider_id": "gateway", "model": "chat-b", "protocol": "openai"})
            service.save_model({"alias": "claude-a", "provider_id": "gateway", "model": "claude-a", "protocol": "claude"})

            result = service.test_provider("gateway")
            self.assertTrue(result["ok"])
            self.assertEqual(gateway.calls, ["chat-a", "claude-a"])
            self.assertEqual(len(result["checks"]), 2)

            restored = WorkbenchService(root).resource_status()
            last_check = next(item for item in restored["health"] if item["provider_id"] == "gateway")["last_check"]
            self.assertTrue(last_check["ok"])
            self.assertTrue(last_check["checked_at"])
            self.assertEqual(len(last_check["checks"]), 2)

    def test_provider_test_reports_no_models_and_disabled_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp))
            service.save_provider({
                "provider_id": "empty", "label": "Empty", "base_url": "http://127.0.0.1:9/v1",
                "auth_type": "none",
            })
            self.assertEqual(service.test_provider("empty")["error_type"], "no_models")
            service.save_provider({
                "provider_id": "empty", "label": "Empty", "base_url": "http://127.0.0.1:9/v1",
                "auth_type": "none", "enabled": False,
            })
            self.assertEqual(service.test_provider("empty")["error_type"], "provider_disabled")
            service.delete_provider("empty")
            health = json.loads((Path(tmp) / "resources" / "health.json").read_text(encoding="utf-8"))
            self.assertNotIn("empty", health["providers"])

    def test_custom_node_output_contract_is_sent_to_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            center = ResourceCenter(root / "resources")
            from app.domain.models import ModelAlias, ModelProvider
            center.save_provider(ModelProvider("vendor", "Vendor", "https://example.test/v1"), api_key="secret-key-value")
            center.save_model(ModelAlias("secure-model", "vendor", "secure-1"))

            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return None
                def read(self):
                    return json.dumps({"choices": [{"message": {"content": json.dumps({
                        "verdict": "pass", "issues": [],
                    })}}]}).encode()

            service = WorkbenchService(root / "workbench", gateway=OpenAICompatibleGateway(center))
            service.save_node({
                "node_type": "security_review", "label": "安全审核",
                "output_fields": ["verdict", "issues"],
            })
            project = service.create_project("Secure")
            session = service.create_session(project.project_id, "review", mode=SessionMode.TASK)
            session.add_message("user", "review this")
            service.sessions.save(session, session.session_id)
            workflow = WorkflowDefinition("secure", "secure", [
                WorkflowNode("security", "security_review", model_alias="secure-model"),
            ])
            with mock.patch("urllib.request.urlopen", return_value=Response()) as opener:
                result = service.run_task(session.session_id, workflow)
            self.assertEqual(result.status, "completed")
            body = json.loads(opener.call_args.args[0].data.decode())
            system_prompt = body["messages"][0]["content"]
            self.assertIn('"verdict": "any"', system_prompt)
            self.assertIn('"issues": "any"', system_prompt)
            user_payload = json.loads(body["messages"][1]["content"])
            self.assertNotIn("_output_fields", user_payload["config"])

    def test_workflow_rejects_a_dependency_cycle_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp))
            workflow = WorkflowDefinition("cycle", "cycle", [
                WorkflowNode("a", "tool", ("b",)),
                WorkflowNode("b", "tool", ("a",)),
            ])
            with self.assertRaisesRegex(ValueError, "DAG"):
                service.workflows.validate_dag(workflow)

    def test_custom_nodes_persist_and_referenced_nodes_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = WorkbenchService(root)
            saved = service.save_node({
                "node_type": "security_review",
                "label": "安全审核",
                "description": "检查安全风险",
                "input_fields": ["changes"],
                "output_fields": ["verdict"],
                "capabilities": ["security"],
            })
            self.assertEqual(saved.node_type, "security_review")
            restored = WorkbenchService(root)
            self.assertIn("security_review", restored.registry)
            restored.save_workflow(WorkflowDefinition(
                "secure-flow",
                "安全工作流",
                [WorkflowNode("security", "security_review")],
            ))
            with self.assertRaisesRegex(ValueError, "secure-flow"):
                restored.delete_node("security_review")
            with self.assertRaisesRegex(ValueError, "built-in"):
                restored.delete_node("planning")

    def test_workflow_persists_explicit_node_model_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = WorkbenchService(root)
            service.save_provider({
                "provider_id": "vendor",
                "label": "Vendor",
                "base_url": "https://example.test/v1",
            })
            service.save_model({"alias": "review-model", "provider_id": "vendor", "model": "review-1"})
            service.save_workflow(WorkflowDefinition(
                "bound-flow",
                "绑定工作流",
                [WorkflowNode("review", "review", model_alias="review-model")],
            ))
            restored = WorkbenchService(root).workflows.get("bound-flow")
            self.assertEqual(restored.nodes[0].model_alias, "review-model")
            with self.assertRaisesRegex(ValueError, "bound-flow"):
                service.delete_model("review-model")

    def test_v2_api_exposes_resource_catalog_projects_and_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = make_server(root, 0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request(method, path, value=None):
                data = json.dumps(value).encode() if value is not None else None
                req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())

            status, catalog = request("GET", "/api/v2/catalog")
            self.assertEqual(status, 200)
            self.assertTrue(any(node["node_type"] == "requirement" for node in catalog["nodes"]))
            status, strategies = request("GET", "/api/v2/strategies")
            self.assertEqual(status, 200)
            self.assertTrue(any(item["strategy"] == "guided-develop" for item in strategies))
            _, project = request("POST", "/api/v2/projects", {"name": "API project"})
            status, project = request("POST", f"/api/v2/projects/{project['project_id']}", {
                "workspace_path": str(root),
                "validation_commands": [[sys.executable, "-c", "print('ok')"]],
            })
            self.assertEqual(status, 200)
            self.assertEqual(project["workspace_path"], str(root.resolve()))
            status, workspace = request("GET", f"/api/v2/projects/{project['project_id']}/workspace")
            self.assertEqual(status, 200)
            self.assertTrue(workspace["configured"])
            status, session = request("POST", f"/api/v2/projects/{project['project_id']}/sessions", {"title": "Task", "mode": "task", "policy": {"strategy": "quick-implement", "complexity": "S"}})
            self.assertEqual(status, 201)
            self.assertEqual(session["mode"], "task")
            self.assertEqual(session["policy"]["strategy"], "quick-implement")
            status, session = request("POST", f"/api/v2/sessions/{session['session_id']}/policy", {"risk": "high"})
            self.assertEqual(status, 200)
            self.assertEqual(session["policy"]["risk"], "high")
            status, detail = request("GET", f"/api/v2/sessions/{session['session_id']}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["project_id"], project["project_id"])

    def test_v2_management_api_and_root_workbench(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Path(tmp), 0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request(method, path, value=None):
                data = json.dumps(value).encode() if value is not None else None
                req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())

            with urllib.request.urlopen(base + "/", timeout=5) as response:
                page = response.read().decode()
            self.assertIn("Workloop 工作台", page)
            self.assertIn("协同任务", page)
            self.assertIn("/static/collaboration.js", page)
            self.assertEqual(page.count("data-close-project"), 2)
            self.assertNotIn("经典控制台", page)

            with urllib.request.urlopen(base + "/static/collaboration.js", timeout=5) as response:
                collaboration_script = response.read().decode()
            self.assertIn("workloop:project-selected", collaboration_script)

            with self.assertRaises(urllib.error.HTTPError) as removed:
                request("GET", "/api/agent/tasks")
            self.assertEqual(removed.exception.code, 404)

            cross_site = urllib.request.Request(
                base + "/api/v2/projects",
                data=json.dumps({"name": "blocked"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(cross_site, timeout=5)
            self.assertEqual(rejected.exception.code, 403)

            request("POST", "/api/v2/resources/providers", {
                "provider_id": "vendor", "label": "Vendor", "base_url": "https://example.test/v1",
            })
            request("POST", "/api/v2/resources/models", {
                "alias": "secure-model", "provider_id": "vendor", "model": "secure-1",
            })

            class ProbeGateway:
                def complete(self, **_): return {}
                def probe(self, alias):
                    return {
                        "ok": True, "alias": alias, "protocol": "openai",
                        "error_type": "", "error": "", "latency_ms": 7,
                    }

            server.workbench.gateway = ProbeGateway()
            status, probe = request("POST", "/api/v2/resources/providers/vendor/test", {})
            self.assertEqual(status, 200)
            self.assertTrue(probe["ok"])
            self.assertEqual(probe["checks"][0]["latency_ms"], 7)
            _, resources = request("GET", "/api/v2/resources")
            vendor_health = next(item for item in resources["health"] if item["provider_id"] == "vendor")
            self.assertTrue(vendor_health["last_check"]["checked_at"])

            request("POST", "/api/v2/nodes", {
                "node_type": "security_review", "label": "安全审核", "output_fields": ["verdict"],
                "default_model": "secure-model",
            })
            status, workflow = request("POST", "/api/v2/workflows", {
                "workflow_id": "secure-flow", "label": "安全流", "nodes": [{
                    "node_id": "security", "node_type": "security_review", "model_alias": "secure-model",
                }],
            })
            self.assertEqual(status, 201)
            self.assertEqual(workflow["nodes"][0]["model_alias"], "secure-model")
            _, catalog = request("GET", "/api/v2/catalog")
            self.assertTrue(any(item["node_type"] == "security_review" for item in catalog["nodes"]))

            with self.assertRaises(urllib.error.HTTPError) as blocked:
                request("DELETE", "/api/v2/resources/models/secure-model")
            self.assertEqual(blocked.exception.code, 400)
            request("DELETE", "/api/v2/workflows/secure-flow")
            request("DELETE", "/api/v2/nodes/security_review")
            request("DELETE", "/api/v2/resources/models/secure-model")
            status, _ = request("DELETE", "/api/v2/resources/providers/vendor")
            self.assertEqual(status, 200)

    def test_task_policy_defaults_and_missing_policy_compatibility(self):
        policy = TaskPolicy.from_dict({})
        self.assertEqual(policy.strategy, "guided-develop")
        self.assertEqual(policy.complexity, "M")
        self.assertEqual(Session.from_dict({
            "session_id": "existing", "project_id": "p", "title": "old",
        }).policy.gate_status, "open")
        with self.assertRaisesRegex(ValueError, "unknown strategy"):
            TaskPolicy(strategy="missing").validate()
        self.assertGreaterEqual(len(list_strategy_presets()), 8)

    def test_policy_is_injected_into_custom_node_context_pack(self):
        captured = {}
        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp))
            service.registry.register(NodeDefinition("capture", "捕获", "", output_fields=("result",)))
            service.registry.register_handler("capture", lambda payload: captured.update(payload) or {"result": "ok"})
            project = service.create_project("Policy")
            session = service.create_session(
                project.project_id, "task", mode=SessionMode.TASK,
                policy={"strategy": "refactor-safely", "complexity": "L", "risk": "high"},
            )
            result = service.run_task(session.session_id, WorkflowDefinition(
                "capture-flow", "Capture", [WorkflowNode("capture", "capture")],
            ))
            self.assertEqual(result.status, "completed")
            self.assertEqual(captured["context_pack"]["task"]["policy"]["strategy"], "refactor-safely")
            self.assertEqual(captured["context_pack"]["shared_context"]["version"], 4)

    def test_repeated_failed_phase_is_blocked_for_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = NodeRegistry()
            registry.register(NodeDefinition("stuck", "卡住", "", output_fields=("result",)))
            registry.register_handler("stuck", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
            service = WorkbenchService(Path(tmp))
            service.registry = registry
            service.orchestrator = DagOrchestrator(registry, service.sessions, service.gateway)
            project = service.create_project("Loop")
            workflow = WorkflowDefinition("stuck-flow", "Stuck", [WorkflowNode("stuck", "stuck", on_failure="retry")])
            session = service.create_session(project.project_id, "task", mode=SessionMode.TASK)
            for _ in range(3):
                result = service.orchestrator.run(session, workflow)
                session = service.get_session(session.session_id)
            self.assertEqual(result.status, "needs_replan")
            self.assertEqual(result.policy.gate, "loop_detected")
            self.assertEqual(result.policy.gate_status, "blocked")

    def test_v2_workspace_applies_model_files_and_runs_real_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            gateway = WorkspaceGateway()
            service = WorkbenchService(Path(tmp) / "data", gateway=gateway)
            project = service.create_project(
                "Writable",
                workspace_path=str(workspace),
                validation_commands=[[
                    sys.executable, "-c",
                    "from pathlib import Path; assert Path('hello.txt').read_text() == 'hello\\n'",
                ]],
            )
            session = service.create_session(project.project_id, "write", mode=SessionMode.TASK)
            service.send_message(session.session_id, "create hello.txt")
            result = service.run_task(session.session_id)

            self.assertEqual(result.status, "completed")
            self.assertEqual((workspace / "hello.txt").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(result.context.facts["applied_files"][0]["path"], "hello.txt")
            checks = result.context.facts["checks"]
            self.assertEqual(checks[0]["status"], "passed")
            self.assertEqual(gateway.calls, ["requirement", "planning", "implementation", "review"])
            reviewed_workspace = gateway.review_context["inputs"]["workspace"]
            reviewed_file = next(item for item in reviewed_workspace["files"] if item["path"] == "hello.txt")
            self.assertEqual(reviewed_file["content"], "hello\n")
            implementation_event = next(
                message for message in result.messages if message.node_id == "implementation"
            )
            self.assertNotIn("file_changes", implementation_event.metadata["node_run"]["output"])
            self.assertNotIn("file_changes", result.context.facts)

    def test_validation_failure_blocks_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            service = WorkbenchService(Path(tmp) / "data", gateway=WorkspaceGateway())
            project = service.create_project(
                "Failing",
                workspace_path=str(workspace),
                validation_commands=[[sys.executable, "-c", "raise SystemExit(2)"]],
            )
            session = service.create_session(project.project_id, "write", mode=SessionMode.TASK)
            service.send_message(session.session_id, "create hello.txt")
            result = service.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertEqual(result.policy.gate, "quality_review")
            self.assertEqual(result.policy.gate_status, "blocked")
            self.assertEqual(result.context.facts["checks"][0]["exit_code"], 2)

    def test_workspace_rejects_model_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            service = WorkbenchService(Path(tmp) / "data", gateway=WorkspaceGateway("../outside.txt"))
            project = service.create_project("Safe", workspace_path=str(workspace))
            session = service.create_session(project.project_id, "write", mode=SessionMode.TASK)
            service.send_message(session.session_id, "write outside")
            result = service.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertFalse((Path(tmp) / "outside.txt").exists())
            self.assertIn("越出工作区", result.context.errors[-1])

    def test_workspace_rejects_nested_repository_metadata_and_binary_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            nested_git = workspace / "src" / ".git"
            nested_git.mkdir(parents=True)
            binary = workspace / "binary.dat"
            binary.write_bytes(b"\xff\xfe\x00\x01")

            protected = WorkbenchService(
                Path(tmp) / "protected-data",
                gateway=WorkspaceGateway("src/.git/config"),
            )
            project = protected.create_project("Protected", workspace_path=str(workspace))
            session = protected.create_session(project.project_id, "write", mode=SessionMode.TASK)
            protected.send_message(session.session_id, "write metadata")
            result = protected.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertFalse((nested_git / "config").exists())

            binary_service = WorkbenchService(
                Path(tmp) / "binary-data",
                gateway=WorkspaceGateway("binary.dat"),
            )
            project = binary_service.create_project("Binary", workspace_path=str(workspace))
            session = binary_service.create_session(project.project_id, "write", mode=SessionMode.TASK)
            binary_service.send_message(session.session_id, "replace binary")
            result = binary_service.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertEqual(binary.read_bytes(), b"\xff\xfe\x00\x01")

    def test_malformed_model_state_is_recorded_as_node_failure(self):
        class MalformedGateway(RecordingGateway):
            def complete(self, *, model_alias, node, context):
                if node.node_type == "requirement":
                    self.calls.append(node.node_id)
                    return {
                        "facts": [],
                        "understanding": "bad envelope",
                        "acceptance_criteria": [],
                        "open_questions": [],
                    }
                return super().complete(model_alias=model_alias, node=node, context=context)

        with tempfile.TemporaryDirectory() as tmp:
            service = WorkbenchService(Path(tmp), gateway=MalformedGateway())
            project = service.create_project("Malformed")
            session = service.create_session(project.project_id, "bad", mode=SessionMode.TASK)
            service.send_message(session.session_id, "run")
            result = service.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertIn("facts must be an object", result.context.errors[-1])

    def test_workspace_prevalidates_entire_change_batch(self):
        class BatchGateway(WorkspaceGateway):
            def complete(self, *, model_alias, node, context):
                if node.node_type != "implementation":
                    return super().complete(model_alias=model_alias, node=node, context=context)
                self.calls.append(node.node_id)
                return {
                    "changes": "batch",
                    "file_changes": [
                        {"operation": "write", "path": "would-write.txt", "content": "first"},
                        {"operation": "write", "path": "../escape.txt", "content": "second"},
                    ],
                    "artifacts": {}, "decisions": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            service = WorkbenchService(Path(tmp) / "data", gateway=BatchGateway())
            project = service.create_project("Batch", workspace_path=str(workspace))
            session = service.create_session(project.project_id, "batch", mode=SessionMode.TASK)
            service.send_message(session.session_id, "write batch")
            result = service.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertFalse((workspace / "would-write.txt").exists())
            self.assertFalse((Path(tmp) / "escape.txt").exists())

    def test_workspace_does_not_write_before_output_contract_validation(self):
        class IncompleteGateway(WorkspaceGateway):
            def complete(self, *, model_alias, node, context):
                if node.node_type != "implementation":
                    return super().complete(model_alias=model_alias, node=node, context=context)
                self.calls.append(node.node_id)
                return {
                    "file_changes": [{
                        "operation": "write", "path": "invalid.txt", "content": "must not publish",
                    }],
                    "artifacts": {},
                    "decisions": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            service = WorkbenchService(Path(tmp) / "data", gateway=IncompleteGateway())
            project = service.create_project("Contract", workspace_path=str(workspace))
            session = service.create_session(project.project_id, "invalid", mode=SessionMode.TASK)
            service.send_message(session.session_id, "write invalid output")
            result = service.run_task(session.session_id)
            self.assertEqual(result.status, "waiting_for_human")
            self.assertFalse((workspace / "invalid.txt").exists())
            self.assertIn("output missing: changes", result.context.errors[-1])

    def test_chat_mode_calls_the_v2_model_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = WorkspaceGateway()
            service = WorkbenchService(Path(tmp), gateway=gateway)
            project = service.create_project("Chat")
            session = service.create_session(project.project_id, "chat", mode=SessionMode.CHAT)
            result = service.send_message(session.session_id, "你好")
            self.assertEqual(result.messages[-1].content, "模型已回答")
            self.assertEqual(gateway.calls, ["chat"])


if __name__ == "__main__":
    unittest.main()
