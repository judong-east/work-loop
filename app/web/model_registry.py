"""Model provider registry: manage providers, models, protocols, and routing.

Providers hold an API endpoint (base URL, optional proxy), one or more wire
protocols, and an API key stored in
``agent-runtime/keys/<provider>.key`` — the key itself never enters the
registry JSON, the generated catalog, task state, or logs. Models attach to
a provider, choose one of its protocols, and carry capabilities/access;
role bindings pin planner/executor/reviewer to specific models. Every
mutation re-materializes ``agent-profiles.json`` from the registry, so the
registry is the single source of truth and hand-edited catalogs are imported
once on first load.
"""

from __future__ import annotations

import json
import re
import secrets
import urllib.parse
from pathlib import Path

from app.agents.composition import ModelCatalog, ModelOption
from app.agents.contracts import AgentAccess
from app.agents.profiles import load_model_catalog
from app.core.atomic_files import write_json_atomic

REGISTRY_FILE = "model-providers.json"
CATALOG_FILE = "agent-profiles.json"
LEGACY_STATE_FILE = "native-runtime.json"
LEGACY_KEY_FILE = "native-api-key"


def _validate_endpoint(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须是 http(s):// 开头的完整地址，例如 https://api.deepseek.com/v1。")
    return base_url.rstrip("/")


def _validate_proxy(proxy: str) -> str:
    proxy = proxy.strip()
    if not proxy:
        return ""
    normalized = proxy if "://" in proxy else f"http://{proxy}"
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("代理地址格式不正确，例如 http://127.0.0.1:7897。")
    return normalized

ROLES = ("planner", "executor", "reviewer")
ROLE_ACCESS = {
    "planner": AgentAccess.READ_ONLY,
    "executor": AgentAccess.WORKSPACE_WRITE,
    "reviewer": AgentAccess.READ_ONLY,
}
PROTOCOLS = {"codex", "claude"}
LEGACY_PROTOCOL = "openai_chat"
_PROVIDER_ID = re.compile(r"^p-[a-z0-9]{8}$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def agent_runtime_dir(root: Path) -> Path:
    return Path(root) / "agent-runtime"


def registry_path(root: Path) -> Path:
    return agent_runtime_dir(root) / REGISTRY_FILE


def catalog_path(root: Path) -> Path:
    return Path(root) / CATALOG_FILE


def keys_dir(root: Path) -> Path:
    return agent_runtime_dir(root) / "keys"


def _empty_registry() -> dict:
    return {"schema_version": 2, "providers": [], "models": [], "roles": {}}


def _protocols(value: object, *, legacy_default: bool = False) -> list[str]:
    raw = value if isinstance(value, list) else []
    selected = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    if not selected:
        return [LEGACY_PROTOCOL if legacy_default else "codex"]
    invalid = [item for item in selected if item not in PROTOCOLS and item != LEGACY_PROTOCOL]
    if invalid:
        raise ValueError("协议必须是 codex 或 claude。")
    return selected


def load_registry(root: Path) -> dict:
    try:
        data = json.loads(registry_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    providers = data.get("providers")
    models = data.get("models")
    roles = data.get("roles")
    normalized_providers = [dict(p) for p in providers if isinstance(p, dict)] if isinstance(providers, list) else []
    for provider in normalized_providers:
        provider["protocols"] = _protocols(provider.get("protocols"), legacy_default=True)
    provider_protocols = {str(p.get("id", "")): p["protocols"] for p in normalized_providers}
    normalized_models = [dict(m) for m in models if isinstance(m, dict)] if isinstance(models, list) else []
    for model in normalized_models:
        choices = provider_protocols.get(str(model.get("provider_id", "")), [LEGACY_PROTOCOL])
        model["runtime"] = str(model.get("runtime", "native")).strip() or "native"
        model["protocol"] = str(model.get("protocol", "")).strip() or choices[0]
    return {
        "schema_version": 2,
        "providers": normalized_providers,
        "models": normalized_models,
        "roles": {k: v for k, v in roles.items() if k in ROLES and isinstance(v, str)} if isinstance(roles, dict) else {},
    }


def save_registry(root: Path, registry: dict) -> None:
    agent_runtime_dir(root).mkdir(parents=True, exist_ok=True)
    write_json_atomic(registry_path(root), registry)


def _write_key(root: Path, provider_id: str, api_key: str) -> str:
    keys_dir(root).mkdir(parents=True, exist_ok=True)
    relative = f"keys/{provider_id}.key"
    path = agent_runtime_dir(root) / relative
    path.write_text(api_key.strip(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return relative


def _key_set(root: Path, key_file: str) -> bool:
    if not key_file:
        return False
    path = agent_runtime_dir(root) / key_file
    try:
        return bool(path.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


# ---- migration ---------------------------------------------------------


def ensure_registry(root: Path) -> dict:
    """Load the registry, importing a legacy catalog on first use.

    Idempotent: once ``model-providers.json`` exists it is the source of
    truth. Before that, a hand-written or quick-setup ``agent-profiles.json``
    is converted so nothing the user already configured is lost.
    """
    root = Path(root)
    if registry_path(root).is_file():
        return load_registry(root)
    catalog_file = catalog_path(root)
    if not catalog_file.is_file():
        return _empty_registry()
    try:
        # load_model_catalog also adapts the schema-v1 roles format, so a
        # legacy roles file imports into the registry the same way.
        catalog = load_model_catalog(catalog_file)
    except (OSError, json.JSONDecodeError, ValueError):
        return _empty_registry()
    registry = _empty_registry()
    providers_by_url: dict[str, str] = {}
    legacy_key = agent_runtime_dir(root) / LEGACY_KEY_FILE
    legacy_state = _legacy_state(root)
    legacy_key_value = ""
    if legacy_key.is_file():
        try:
            legacy_key_value = legacy_key.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            legacy_key_value = ""
    for option in catalog.list_all():
        # CLI-era entries are intentionally not imported into the managed
        # registry. The new console is API-protocol only.
        if option.runtime != "native":
            continue
        provider_id = ""
        if option.base_url not in providers_by_url:
            provider_id = f"p-{secrets.token_hex(4)}"
            key_file = ""
            if legacy_state.get("base_url") == option.base_url and legacy_key_value:
                key_file = _write_key(root, provider_id, legacy_key_value)
            registry["providers"].append(
                {
                    "id": provider_id,
                    "label": option.provider
                    or urllib.parse.urlparse(option.base_url).netloc
                    or "默认供应商",
                    "base_url": option.base_url,
                    "proxy": str(legacy_state.get("proxy", "") or ""),
                    "key_file": key_file,
                    "protocols": [option.protocol or LEGACY_PROTOCOL],
                }
            )
            providers_by_url[option.base_url] = provider_id
        provider_id = providers_by_url[option.base_url]
        registry["models"].append(
            {
                "profile_id": option.profile_id,
                "label": option.label,
                "runtime": "native",
                "protocol": option.protocol or LEGACY_PROTOCOL,
                "provider_id": provider_id,
                "model": option.model,
                "access": option.access.value,
                "capabilities": list(option.capabilities),
                "thinking": option.thinking,
                "max_tokens": option.max_tokens,
                "quality": option.quality,
            }
        )
    registry["roles"] = _derive_roles(registry["models"])
    save_registry(root, registry)
    return registry


def _legacy_state(root: Path) -> dict:
    try:
        data = json.loads((agent_runtime_dir(root) / LEGACY_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _derive_roles(models: list[dict]) -> dict[str, str]:
    read_only = [m for m in models if m.get("access") == "read_only"]
    write = [m for m in models if m.get("access") == "workspace_write"]
    roles: dict[str, str] = {}
    if read_only:
        roles["planner"] = read_only[0]["profile_id"]
        roles["reviewer"] = read_only[-1]["profile_id"]
    if write:
        roles["executor"] = write[0]["profile_id"]
    return roles


# ---- catalog materialization --------------------------------------------


def sync_catalog(root: Path, registry: dict) -> ModelCatalog | None:
    """Write agent-profiles.json from the registry and return the catalog.

    Returns ``None`` when the registry has no models.
    """
    providers = {p["id"]: p for p in registry["providers"]}
    entries: list[ModelOption] = []
    for model in registry["models"]:
        provider = providers.get(str(model.get("provider_id", ""))) if model.get("provider_id") else None
        if model.get("runtime") != "native":
            entries.append(
                ModelOption(
                    profile_id=str(model["profile_id"]),
                    label=str(model.get("label") or model["profile_id"]),
                    runtime=str(model.get("runtime", "codex_cli")),
                    model=str(model.get("model", "")),
                    access=AgentAccess(str(model.get("access", "workspace_write"))),
                    capabilities=[str(c) for c in model.get("capabilities") or ["general"]],
                    quality=int(model.get("quality", 4)),
                    input_cost_per_million=0.0,
                    output_cost_per_million=0.0,
                    thinking=str(model.get("thinking", "medium")),
                    max_tokens=int(model.get("max_tokens", 0) or 0),
                )
            )
            continue
        if provider is None:
            continue
        entries.append(
            ModelOption(
                    profile_id=str(model["profile_id"]),
                    label=str(model.get("label") or model["profile_id"]),
                    runtime="native",
                    model=str(model.get("model", "")),
                    access=AgentAccess(str(model.get("access", "read_only"))),
                    capabilities=[str(c) for c in model.get("capabilities") or ["general"]],
                    quality=int(model.get("quality", 4)),
                    input_cost_per_million=0.0,
                    output_cost_per_million=0.0,
                    # ModelBinding.provider only accepts [A-Za-z0-9_.-]; the
                    # human-facing label stays in the registry for display.
                    provider=str(provider.get("id", "")),
                    thinking=str(model.get("thinking", "medium")),
                    base_url=str(provider.get("base_url", "")),
                    api_key_file=str(provider.get("key_file", "")),
                    proxy=str(provider.get("proxy", "") or ""),
                    protocol=str(model.get("protocol", "")) or str((provider.get("protocols") or ["codex"])[0]),
                    max_tokens=int(model.get("max_tokens", 0) or 0),
            )
        )
    if not entries:
        catalog_path(root).unlink(missing_ok=True)
        return None
    catalog = ModelCatalog(entries)
    write_json_atomic(catalog_path(root), catalog.to_dict())
    return catalog


# ---- registry status -----------------------------------------------------


def registry_status(root: Path) -> dict:
    root = Path(root)
    registry = ensure_registry(root)
    providers = []
    for provider in registry["providers"]:
        entry = dict(provider)
        entry["key_set"] = _key_set(root, str(provider.get("key_file", "")))
        entry["model_count"] = sum(
            1 for m in registry["models"] if m.get("provider_id") == provider.get("id")
        )
        providers.append(entry)
    models = [dict(model) for model in registry["models"]]
    by_id = {m["profile_id"]: m for m in models}
    roles = {}
    for role in ROLES:
        bound = registry["roles"].get(role, "")
        model = by_id.get(bound)
        provider = next(
            (p for p in registry["providers"] if p.get("id") == (model or {}).get("provider_id")),
            None,
        )
        roles[role] = {
            "profile_id": bound,
            "label": model.get("label", "") if model else "",
            "model": model.get("model", "") if model else "",
            "runtime": model.get("runtime", "") if model else "",
            "protocol": model.get("protocol", "") if model else "",
            "provider": provider.get("label", "") if provider else "",
        }
    configured = all(roles[role]["profile_id"] for role in ROLES) and any(
        p.get("key_set") for p in providers
    )
    return {
        "configured": configured,
        "providers": providers,
        "models": models,
        "roles": roles,
        "role_bindings": dict(registry["roles"]),
    }


# ---- mutations -----------------------------------------------------------


def save_provider(root: Path, body: dict) -> dict:
    root = Path(root)
    registry = ensure_registry(root)
    label = str(body.get("label", "")).strip()
    if not label or len(label) > 60:
        raise ValueError("供应商名称不能为空且不超过 60 字。")
    base_url = _validate_endpoint(str(body.get("base_url", "")))
    proxy = _validate_proxy(str(body.get("proxy", "")))
    api_key = str(body.get("api_key", "")).strip()
    provider_id = str(body.get("id", "")).strip()
    if provider_id and not _PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("供应商 id 不合法。")
    existing = next((p for p in registry["providers"] if p.get("id") == provider_id), None)
    if existing is None:
        provider_id = provider_id or f"p-{secrets.token_hex(4)}"
        existing = {"id": provider_id, "key_file": "", "protocols": ["codex"]}
        registry["providers"].append(existing)
    protocols = _protocols(
        body.get("protocols") if "protocols" in body else existing.get("protocols")
    )
    existing.update({"label": label, "base_url": base_url, "proxy": proxy, "protocols": protocols})
    if api_key:
        existing["key_file"] = _write_key(root, provider_id, api_key)
    save_registry(root, registry)
    return dict(existing)


def delete_provider(root: Path, provider_id: str) -> None:
    root = Path(root)
    registry = ensure_registry(root)
    registry["providers"] = [p for p in registry["providers"] if p.get("id") != provider_id]
    removed = {
        m["profile_id"]
        for m in registry["models"]
        if m.get("provider_id") == provider_id
    }
    registry["models"] = [m for m in registry["models"] if m.get("provider_id") != provider_id]
    registry["roles"] = {
        role: bound for role, bound in registry["roles"].items() if bound not in removed
    }
    save_registry(root, registry)
    (agent_runtime_dir(root) / f"keys/{provider_id}.key").unlink(missing_ok=True)


def save_model(root: Path, body: dict) -> dict:
    root = Path(root)
    registry = ensure_registry(root)
    profile_id = str(body.get("profile_id", "")).strip()
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError("模型标识必须以小写字母开头，仅含小写字母、数字、- 或 _。")
    runtime = str(body.get("runtime", "native")).strip()
    if runtime not in {"native", "claude_code", "codex_cli", "pi_rpc"}:
        raise ValueError("不支持的模型运行时。")
    model_name = str(body.get("model", "")).strip()
    if not model_name:
        raise ValueError("模型名不能为空。")
    access = str(body.get("access", "read_only")).strip()
    if access not in {"read_only", "workspace_write"}:
        raise ValueError("access 必须是 read_only 或 workspace_write。")
    provider_id = str(body.get("provider_id", "")).strip()
    provider = next((p for p in registry["providers"] if p.get("id") == provider_id), None)
    protocol = ""
    if runtime == "native":
        if provider is None:
            raise ValueError("模型必须选择一个已配置的供应商。")
        available_protocols = _protocols(provider.get("protocols"), legacy_default=True)
        protocol = str(body.get("protocol", "")).strip() or available_protocols[0]
        if protocol not in available_protocols:
            raise ValueError("所选协议未在该供应商上启用。")
    else:
        provider_id = ""
    capabilities = [str(c).strip().lower() for c in body.get("capabilities") or []]
    capabilities = [c for c in capabilities if c]
    if not capabilities:
        capabilities = ["general"]
    try:
        quality = int(body.get("quality", 4))
        max_tokens = int(body.get("max_tokens", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("quality / max_tokens 必须是整数。") from None
    thinking = str(body.get("thinking", "medium")).strip() or "medium"
    record = {
        "profile_id": profile_id,
        "label": str(body.get("label", "")).strip() or model_name,
        "runtime": runtime,
        "protocol": protocol,
        "provider_id": provider_id,
        "model": model_name,
        "access": access,
        "capabilities": capabilities,
        "thinking": thinking,
        "max_tokens": max_tokens,
        "quality": quality,
    }
    # Validate through the catalog constructor before persisting.
    trial = dict(registry)
    trial["models"] = [m for m in registry["models"] if m["profile_id"] != profile_id] + [record]
    try:
        sync_catalog(root, trial)
    except ValueError as error:
        raise ValueError(str(error)) from error
    registry["models"] = trial["models"]
    save_registry(root, registry)
    return record


def delete_model(root: Path, profile_id: str) -> None:
    root = Path(root)
    registry = ensure_registry(root)
    registry["models"] = [m for m in registry["models"] if m.get("profile_id") != profile_id]
    registry["roles"] = {
        role: bound for role, bound in registry["roles"].items() if bound != profile_id
    }
    save_registry(root, registry)


def save_roles(root: Path, body: dict) -> dict:
    root = Path(root)
    registry = ensure_registry(root)
    by_id = {m["profile_id"]: m for m in registry["models"]}
    roles: dict[str, str] = {}
    for role in ROLES:
        bound = str(body.get(role, "")).strip()
        if not bound:
            raise ValueError(f"角色 {role} 必须绑定一个模型。")
        model = by_id.get(bound)
        if model is None:
            raise ValueError(f"模型 {bound} 不存在。")
        if model.get("access") != ROLE_ACCESS[role].value:
            raise ValueError(
                f"角色 {role} 需要 "
                f"{'只读' if ROLE_ACCESS[role] is AgentAccess.READ_ONLY else '可写'}"
                "权限的模型。"
            )
        roles[role] = bound
    registry["roles"] = roles
    save_registry(root, registry)
    return roles


def provider_test_model(root: Path, provider_id: str) -> str:
    registry = ensure_registry(root)
    for model in registry["models"]:
        if model.get("provider_id") == provider_id:
            return str(model.get("model", ""))
    return ""
