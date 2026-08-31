from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.core.atomic_files import write_json_atomic, write_text_atomic
from app.core.contracts import utc_now

from app.domain.models import ModelAlias, ModelProvider


class ResourceCenter:
    """Provider, credential-reference, alias, and health-check boundary.

    Secrets are written to separate files with restrictive permissions where the
    host supports them.  Catalog JSON only stores a ``credential_ref`` and is
    therefore safe to expose to the UI.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.providers_path = self.root / "providers.json"
        self.models_path = self.root / "models.json"
        self.health_path = self.root / "health.json"
        self.secrets_dir = self.root / "secrets"
        self._lock = threading.RLock()
        self.secrets_dir.mkdir(parents=True, exist_ok=True)

    def list_providers(self) -> list[ModelProvider]:
        return [ModelProvider.from_dict(item) for item in self._read(self.providers_path).get("providers", [])]

    def list_models(self) -> list[ModelAlias]:
        return [ModelAlias.from_dict(item) for item in self._read(self.models_path).get("models", [])]

    def save_provider(self, provider: ModelProvider, *, api_key: str = "") -> ModelProvider:
        with self._lock:
            provider.validate()
            providers = self.list_providers()
            existing = next((item for item in providers if item.provider_id == provider.provider_id), None)
            used_protocols = {
                model.protocol
                for model in self.list_models()
                if model.provider_id == provider.provider_id and model.protocol
            }
            unsupported = used_protocols - set(provider.protocols)
            if unsupported:
                raise ValueError(
                    f"provider still has models using protocols: {', '.join(sorted(unsupported))}"
                )
            items = [item for item in providers if item.provider_id != provider.provider_id]
            if provider.auth_type == "none":
                provider.credential_ref = ""
                self._delete_secret(provider.provider_id)
            elif api_key:
                self._write_secret(provider.provider_id, api_key)
                provider.credential_ref = f"secrets/{provider.provider_id}.key"
            elif existing is not None and not provider.credential_ref:
                provider.credential_ref = existing.credential_ref
            items.append(provider)
            write_json_atomic(self.providers_path, {"schema_version": 1, "providers": [item.to_dict() for item in items]})
            return provider

    def save_model(self, model: ModelAlias) -> ModelAlias:
        with self._lock:
            provider = next((item for item in self.list_providers() if item.provider_id == model.provider_id), None)
            if provider is None:
                raise ValueError(f"unknown provider: {model.provider_id}")
            model.protocol = model.protocol or provider.protocols[0]
            model.validate()
            if model.protocol not in provider.protocols:
                raise ValueError(f"provider {provider.provider_id} does not support protocol: {model.protocol}")
            items = [item for item in self.list_models() if item.alias != model.alias]
            items.append(model)
            write_json_atomic(self.models_path, {"schema_version": 1, "models": [item.to_dict() for item in items]})
            return model

    def delete_provider(self, provider_id: str) -> None:
        with self._lock:
            providers = self.list_providers()
            items = [item for item in providers if item.provider_id != provider_id]
            if len(items) == len(providers):
                raise KeyError(provider_id)
            if any(item.provider_id == provider_id for item in self.list_models()):
                raise ValueError("provider still has model aliases")
            write_json_atomic(self.providers_path, {"schema_version": 1, "providers": [item.to_dict() for item in items]})
            self._delete_secret(provider_id)
            health = dict(self._read(self.health_path).get("providers", {}))
            if provider_id in health:
                del health[provider_id]
                write_json_atomic(self.health_path, {"schema_version": 1, "providers": health})

    def delete_model(self, alias: str) -> None:
        with self._lock:
            models = self.list_models()
            items = [item for item in models if item.alias != alias]
            if len(items) == len(models):
                raise KeyError(alias)
            write_json_atomic(self.models_path, {"schema_version": 1, "models": [item.to_dict() for item in items]})

    def resolve(self, alias: str) -> tuple[ModelProvider, ModelAlias]:
        model = next((item for item in self.list_models() if item.alias == alias and item.enabled), None)
        if model is None:
            raise KeyError(f"model alias: {alias}")
        provider = next((item for item in self.list_providers() if item.provider_id == model.provider_id and item.enabled), None)
        if provider is None:
            raise KeyError(f"provider: {model.provider_id}")
        return provider, model

    def default_alias(self, capability: str = "general") -> str:
        enabled_providers = {item.provider_id for item in self.list_providers() if item.enabled}
        models = [
            item
            for item in self.list_models()
            if item.enabled and item.provider_id in enabled_providers
        ]
        matched = [item for item in models if capability in item.capabilities]
        selected = matched[0] if matched else (models[0] if models else None)
        return selected.alias if selected is not None else ""

    def credential(self, provider_id: str) -> str:
        path = self.secrets_dir / f"{provider_id}.key"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def health(self) -> list[dict[str, Any]]:
        checks = self._read(self.health_path).get("providers", {})
        return [
            {
                "provider_id": provider.provider_id,
                "label": provider.label,
                "enabled": provider.enabled,
                "configured": provider.auth_type == "none" or bool(provider.credential_ref and self.credential(provider.provider_id)),
                "auth_type": provider.auth_type,
                "credential_required": provider.auth_type != "none",
                "base_url": provider.base_url,
                "last_check": checks.get(provider.provider_id, {}),
            }
            for provider in self.list_providers()
        ]

    def record_health(self, provider_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            providers = dict(self._read(self.health_path).get("providers", {}))
            providers[provider_id] = {**result, "checked_at": utc_now()}
            write_json_atomic(self.health_path, {"schema_version": 1, "providers": providers})

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid resource catalog: {path}") from error
        return value if isinstance(value, dict) else {}

    def _write_secret(self, provider_id: str, api_key: str) -> None:
        if len(api_key.strip()) < 8:
            raise ValueError("credential is too short")
        if any(char in api_key for char in "\r\n"):
            raise ValueError("credential cannot contain newlines")
        path = self.secrets_dir / f"{provider_id}.key"
        write_text_atomic(path, api_key.strip() + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _delete_secret(self, provider_id: str) -> None:
        path = self.secrets_dir / f"{provider_id}.key"
        if path.is_file():
            path.unlink()
