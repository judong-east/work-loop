"""Console-side model wiring: quick setup and connectivity checks.

Quick setup is a thin wrapper over the model registry (``model_registry``):
one OpenAI-compatible endpoint becomes one provider with two models bound to
every role. The full management surface — multiple providers, per-model
capabilities, role routing — lives in the registry; both paths share the
same storage, so a quick setup can later be refined in 模型管理 without
migration.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.native_harness import (
    normalize_protocol_response,
    protocol_payload,
    urllib_transport,
)

from app.web.model_registry import (
    _validate_endpoint,
    _validate_proxy,
    ensure_registry,
    save_model,
    save_provider,
    save_roles,
    sync_catalog,
)

QUICK_PROVIDER_ID = "p-quick001"
QUICK_READONLY_PROFILE = "native-readonly"
QUICK_WRITER_PROFILE = "native-writer"


def save_native_config(root: Path, body: dict) -> None:
    """Quick start: one provider, one read + one write model, all roles bound."""
    root = Path(root)
    base_url = _validate_endpoint(str(body.get("base_url", "")))
    model = str(body.get("model", "")).strip()
    if not model:
        raise ValueError("模型名不能为空，例如 deepseek-chat。")
    provider = str(body.get("provider", "")).strip() or "快速接入"
    thinking = str(body.get("thinking", "medium")).strip() or "medium"
    proxy = _validate_proxy(str(body.get("proxy", "")))
    api_key = str(body.get("api_key", "")).strip()

    registry = ensure_registry(root)
    existing = next(
        (p for p in registry["providers"] if p.get("id") == QUICK_PROVIDER_ID), None
    )
    if not api_key and not (existing or {}).get("key_file"):
        raise ValueError("API key 不能为空。")
    save_provider(
        root,
        {
            "id": QUICK_PROVIDER_ID,
            "label": provider,
            "base_url": base_url,
            "proxy": proxy,
            "api_key": api_key,
            "protocols": ["openai_chat"],
        },
    )
    common = {"runtime": "native", "provider_id": QUICK_PROVIDER_ID, "model": model, "thinking": thinking}
    save_model(root, {**common, "profile_id": QUICK_READONLY_PROFILE, "label": f"{model}（只读）", "access": "read_only", "capabilities": ["planning", "review", "general"]})
    save_model(root, {**common, "profile_id": QUICK_WRITER_PROFILE, "label": f"{model}（可写）", "access": "workspace_write", "capabilities": ["implementation", "general"]})
    save_roles(root, {
        "planner": QUICK_READONLY_PROFILE,
        "executor": QUICK_WRITER_PROFILE,
        "reviewer": QUICK_READONLY_PROFILE,
    })
    sync_catalog(root, ensure_registry(root))


def native_status(root: Path) -> dict:
    """Summary for the model chip: current executor model and endpoint."""
    from app.web.model_registry import registry_status

    status = registry_status(Path(root))
    roles = status["roles"]
    executor = roles.get("executor", {})
    provider_label = executor.get("provider", "")
    provider = next(
        (p for p in status["providers"] if p.get("label") == provider_label),
        {},
    )
    return {
        "configured": bool(status["configured"]),
        "base_url": str(provider.get("base_url", "")),
        "model": str(executor.get("model", "")),
        "provider": provider_label,
        "proxy": str(provider.get("proxy", "") or ""),
        "api_key_set": bool(provider.get("key_set")),
    }


def _read_key_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _resolve_test_key(root: Path, provider_id: str, api_key: str) -> str:
    from app.web.model_registry import agent_runtime_dir, ensure_registry

    api_key = api_key.strip()
    if api_key:
        return api_key
    if not provider_id:
        return ""
    registry = ensure_registry(root)
    provider = next((p for p in registry["providers"] if p.get("id") == provider_id), None)
    if provider and provider.get("key_file"):
        return _read_key_text(agent_runtime_dir(root) / provider["key_file"])
    return ""


def test_native_connection(root: Path, body: dict) -> tuple[bool, str]:
    """One tiny request to verify URL, key, model, protocol, and proxy."""
    root = Path(root)
    try:
        base_url = _validate_endpoint(str(body.get("base_url", "")))
        model = str(body.get("model", "")).strip()
        if not model:
            return False, "模型名不能为空。"
        proxy = _validate_proxy(str(body.get("proxy", "")))
        protocol = str(body.get("protocol", "codex")).strip() or "codex"
        if protocol not in {"codex", "claude", "openai_chat"}:
            return False, "协议必须是 codex 或 claude。"
        api_key = _resolve_test_key(root, str(body.get("provider_id", "")), str(body.get("api_key", "")))
        if not api_key:
            return False, "API key 不能为空。"
    except ValueError as error:
        return False, str(error)

    payload = protocol_payload(
        protocol,
        model,
        [{"role": "user", "content": "只回复 pong"}],
        [],
        16,
    )
    try:
        decoded = urllib_transport(base_url, api_key, proxy, protocol)(payload, 20)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return False, f"连接失败：{error}"
    message, _finish = normalize_protocol_response(protocol, decoded)
    if message is None:
        return False, f"接口已连通，但响应不符合 {protocol} 协议。"
    return True, f"连通正常：模型 {model} 已通过 {protocol} 协议应答。"
