from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from app.domain.models import ContextState, ModelAlias, ModelProvider, WorkflowNode

from .resource_center import ResourceCenter


_OUTPUT_CONTRACTS = {
    "requirement": {"understanding": "string", "acceptance_criteria": ["string"], "open_questions": ["string"]},
    "planning": {"steps": ["string"], "risks": ["string"], "artifacts": {}},
    "implementation": {
        "changes": "string",
        "file_changes": [{"operation": "write", "path": "relative/path", "content": "complete file text"}],
        "artifacts": {},
        "decisions": ["string"],
    },
    "review": {"verdict": "pass|revise|blocked", "issues": [], "decisions": ["string"]},
    "testing": {"checks": [], "risks": ["string"], "decisions": ["string"]},
    "tool": {"result": "any"},
}


class OpenAICompatibleGateway:
    """Call OpenAI Chat Completions or Claude Messages resource models."""

    def __init__(self, resources: ResourceCenter, *, timeout_seconds: float = 120):
        self.resources = resources
        self.timeout_seconds = timeout_seconds

    def complete(self, *, model_alias: str, node: WorkflowNode, context: ContextState) -> dict[str, Any]:
        project = context.inputs.get("project", {})
        project_default = str(project.get("default_model", "")) if isinstance(project, dict) else ""
        alias = model_alias or project_default or self.resources.default_alias(node.node_type)
        if not alias:
            raise ValueError(f"节点 {node.node_id} 没有可用模型，请先在资源中心登记模型别名。")
        provider, model = self.resources.resolve(alias)
        protocol = model.protocol or provider.protocols[0]
        credential = self.resources.credential(provider.provider_id)
        if provider.auth_type != "none" and not credential:
            raise ValueError(f"供应商 {provider.label} 尚未配置认证密钥。")

        messages = self._messages(node, context)
        payload = self._payload(protocol, model, messages)
        request = urllib.request.Request(
            self._endpoint(provider.base_url, protocol),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(provider, credential, protocol),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"模型接口返回 HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RuntimeError(f"无法连接模型接口: {error}") from error

        content = self._content(raw, protocol)
        output = self._json_object(content)
        output.setdefault("model", alias)
        return output

    def probe(self, model_alias: str) -> dict[str, Any]:
        provider, model = self.resources.resolve(model_alias)
        protocol = model.protocol or provider.protocols[0]
        credential = self.resources.credential(provider.provider_id)
        if provider.auth_type != "none" and not credential:
            return {
                "ok": False, "alias": model.alias, "protocol": protocol,
                "error_type": "authentication_missing", "error": "尚未配置认证密钥。",
            }
        if protocol == "claude":
            payload: dict[str, Any] = {
                "model": model.model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1,
            }
        else:
            payload = {
                "model": model.model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1,
            }
        request = urllib.request.Request(
            self._endpoint(provider.base_url, protocol),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(provider, credential, protocol),
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout_seconds, 20)) as response:
                response.read(256)
        except urllib.error.HTTPError as error:
            error_type = "authentication_failed" if error.code in {401, 403} else "rate_limited" if error.code == 429 else "http_error"
            detail = error.read().decode("utf-8", errors="replace")[:300]
            return {
                "ok": False, "alias": model.alias, "protocol": protocol,
                "error_type": error_type, "error": f"HTTP {error.code}: {detail}",
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return {
                "ok": False, "alias": model.alias, "protocol": protocol,
                "error_type": "connection_failed", "error": str(error),
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        return {
            "ok": True, "alias": model.alias, "protocol": protocol,
            "error_type": "", "error": "",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

    @staticmethod
    def _messages(node: WorkflowNode, context: ContextState) -> list[dict[str, str]]:
        contract = _OUTPUT_CONTRACTS.get(node.node_type)
        if contract is None:
            fields = node.config.get("_output_fields", [])
            contract = {str(field): "any" for field in fields} or {"result": "any"}
        public_config = {key: value for key, value in node.config.items() if not key.startswith("_")}
        context_pack = node.config.get("_context_pack")
        user_payload: dict[str, Any] = {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "instructions": node.prompt_template,
            "config": public_config,
            "shared_context": context.to_dict(),
        }
        if isinstance(context_pack, dict):
            user_payload["context_pack"] = context_pack
        return [
            {
                "role": "system",
                "content": (
                    "You are one node in a durable multi-agent workflow. "
                    "Return one JSON object only, without markdown fences. "
                    "Never claim a file was changed unless it is included in file_changes. "
                    "File paths must be workspace-relative and file contents must be complete. "
                    f"Required output shape: {json.dumps(contract, ensure_ascii=False)}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ]

    @staticmethod
    def _payload(protocol: str, model: ModelAlias, messages: list[dict[str, str]]) -> dict[str, Any]:
        if protocol == "claude":
            payload: dict[str, Any] = {
                "model": model.model,
                "system": "\n\n".join(item["content"] for item in messages if item["role"] == "system"),
                "messages": [item for item in messages if item["role"] != "system"],
                "max_tokens": model.max_tokens or 4096,
            }
        else:
            payload = {"model": model.model, "messages": messages}
            if model.max_tokens is not None:
                payload["max_tokens"] = model.max_tokens
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        return payload

    @staticmethod
    def _headers(provider: ModelProvider, credential: str, protocol: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if protocol == "claude":
            headers["anthropic-version"] = "2023-06-01"
        if provider.auth_type == "none":
            return headers
        if provider.auth_type == "bearer":
            prefix = provider.auth_prefix.strip() or "Bearer"
            headers["Authorization"] = f"{prefix} {credential}".strip()
        elif provider.auth_type == "api_key":
            headers["x-api-key"] = credential
        else:
            value = f"{provider.auth_prefix.strip()} {credential}".strip()
            headers[provider.auth_header] = value
        return headers

    @staticmethod
    def _endpoint(base_url: str, protocol: str) -> str:
        value = base_url.rstrip("/")
        if protocol == "claude":
            if value.endswith("/messages"):
                return value
            return value + ("/messages" if value.endswith("/v1") else "/v1/messages")
        return value if value.endswith("/chat/completions") else value + "/chat/completions"

    @staticmethod
    def _content(raw: dict[str, Any], protocol: str) -> str:
        if protocol == "claude":
            blocks = raw.get("content")
            if not isinstance(blocks, list):
                raise ValueError("Claude 响应缺少 content 数组")
            return "".join(
                str(item.get("text", ""))
                for item in blocks
                if isinstance(item, dict) and item.get("type") == "text"
            )
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("OpenAI 响应缺少 choices[0].message.content") from error
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if not isinstance(content, str):
            raise ValueError("模型响应内容必须是字符串")
        return content

    @staticmethod
    def _json_object(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("模型没有返回合法 JSON 对象") from error
        if not isinstance(value, dict):
            raise ValueError("模型输出必须是 JSON 对象")
        return value
