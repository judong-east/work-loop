from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agents.composition import ModelCatalog
from app.agents.runtime_factory import build_runtime_stack
from app.web.model_registry import (
    agent_runtime_dir,
    delete_model,
    delete_provider,
    ensure_registry,
    registry_status,
    save_model,
    save_provider,
    save_roles,
    sync_catalog,
)


class ModelRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _add_provider(self, provider_id: str = "", label: str = "DeepSeek") -> dict:
        return save_provider(
            self.root,
            {
                "id": provider_id,
                "label": label,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-abc",
            },
        )

    def test_provider_crud_and_key_storage(self) -> None:
        provider = self._add_provider()
        self.assertRegex(provider["id"], r"^p-[a-z0-9]{8}$")
        key_path = agent_runtime_dir(self.root) / provider["key_file"]
        self.assertEqual(key_path.read_text(encoding="utf-8"), "sk-abc")
        status = registry_status(self.root)
        self.assertTrue(status["providers"][0]["key_set"])
        # Key never appears in the registry JSON.
        registry_text = (agent_runtime_dir(self.root) / "model-providers.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sk-abc", registry_text)
        delete_provider(self.root, provider["id"])
        self.assertEqual(registry_status(self.root)["providers"], [])
        self.assertFalse(key_path.exists())

    def test_model_requires_provider_for_native(self) -> None:
        with self.assertRaises(ValueError):
            save_model(
                self.root,
                {"profile_id": "m1", "runtime": "native", "model": "x", "access": "read_only"},
            )
        provider = self._add_provider()
        model = save_model(
            self.root,
            {
                "profile_id": "deepseek-chat",
                "label": "DeepSeek Chat",
                "runtime": "native",
                "provider_id": provider["id"],
                "model": "deepseek-chat",
                "access": "workspace_write",
                "capabilities": ["implementation"],
            },
        )
        self.assertEqual(model["profile_id"], "deepseek-chat")

    def test_role_binding_access_rules(self) -> None:
        provider = self._add_provider()
        save_model(
            self.root,
            {
                "profile_id": "writer",
                "runtime": "native",
                "provider_id": provider["id"],
                "model": "m",
                "access": "workspace_write",
            },
        )
        with self.assertRaises(ValueError):
            save_roles(self.root, {"planner": "writer", "executor": "writer", "reviewer": "writer"})
        save_model(
            self.root,
            {
                "profile_id": "reader",
                "runtime": "native",
                "provider_id": provider["id"],
                "model": "m",
                "access": "read_only",
            },
        )
        save_roles(self.root, {"planner": "reader", "executor": "writer", "reviewer": "reader"})
        status = registry_status(self.root)
        self.assertEqual(status["role_bindings"]["executor"], "writer")
        self.assertEqual(status["roles"]["planner"]["model"], "m")

    def test_delete_provider_cascades_models_and_bindings(self) -> None:
        provider = self._add_provider()
        save_model(
            self.root,
            {"profile_id": "reader", "runtime": "native", "provider_id": provider["id"], "model": "m", "access": "read_only"},
        )
        save_model(
            self.root,
            {"profile_id": "writer", "runtime": "native", "provider_id": provider["id"], "model": "m", "access": "workspace_write"},
        )
        save_roles(self.root, {"planner": "reader", "executor": "writer", "reviewer": "reader"})
        delete_provider(self.root, provider["id"])
        status = registry_status(self.root)
        self.assertEqual(status["models"], [])
        self.assertEqual(status["role_bindings"], {})

    def test_sync_catalog_materializes_native_wiring(self) -> None:
        provider = save_provider(
            self.root,
            {
                "label": "Mock",
                "base_url": "http://127.0.0.1:8977/v1",
                "api_key": "k",
                "proxy": "http://127.0.0.1:7897",
            },
        )
        save_model(
            self.root,
            {"profile_id": "mock-writer", "runtime": "native", "provider_id": provider["id"], "model": "mock", "access": "workspace_write"},
        )
        registry = ensure_registry(self.root)
        catalog = sync_catalog(self.root, registry)
        entry = catalog.get("mock-writer")
        self.assertEqual(entry.base_url, "http://127.0.0.1:8977/v1")
        self.assertEqual(entry.api_key_file, f"keys/{provider['id']}.key")
        self.assertEqual(entry.proxy, "http://127.0.0.1:7897")
        on_disk = json.loads((self.root / "agent-profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["models"][0]["api_key_file"], f"keys/{provider['id']}.key")

    def test_sync_catalog_empty_removes_catalog_file(self) -> None:
        registry = ensure_registry(self.root)
        save_model(
            self.root,
            {"profile_id": "cli-model", "runtime": "codex_cli", "model": "gpt-5.2-codex", "access": "workspace_write"},
        )
        self.assertTrue((self.root / "agent-profiles.json").exists())
        delete_model(self.root, "cli-model")
        self.assertIsNone(sync_catalog(self.root, ensure_registry(self.root)))
        self.assertFalse((self.root / "agent-profiles.json").exists())

    def test_build_runtime_stack_honors_explicit_roles_and_key_root(self) -> None:
        provider = self._add_provider()
        save_model(
            self.root,
            {"profile_id": "reader", "runtime": "native", "provider_id": provider["id"], "model": "m", "access": "read_only", "capabilities": ["planning", "review", "general"]},
        )
        save_model(
            self.root,
            {"profile_id": "writer", "runtime": "native", "provider_id": provider["id"], "model": "m", "access": "workspace_write", "capabilities": ["implementation"]},
        )
        save_roles(self.root, {"planner": "reader", "executor": "writer", "reviewer": "reader"})
        registry = ensure_registry(self.root)
        catalog = sync_catalog(self.root, registry)
        runtime, _composer = build_runtime_stack(
            catalog,
            role_bindings=registry["roles"],
            key_root=agent_runtime_dir(self.root),
        )
        from app.agents.native_harness import NativeHarnessRuntime

        routed = runtime.runtimes["executor"]
        self.assertIsInstance(routed, NativeHarnessRuntime)
        key_path = Path(routed.profile.api_key_file)
        self.assertEqual(key_path.parent.name, "keys")
        self.assertEqual(key_path.name, f"{provider['id']}.key")
        self.assertTrue(key_path.is_file())

    def test_legacy_quick_setup_catalog_is_imported(self) -> None:
        # Simulate the pre-registry quick setup: catalog + key + state files.
        runtime_dir = self.root / "agent-runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "native-api-key").write_text("sk-legacy", encoding="utf-8")
        (runtime_dir / "native-runtime.json").write_text(
            json.dumps({"base_url": "https://api.test/v1", "proxy": ""}), encoding="utf-8"
        )
        catalog = ModelCatalog.from_dict(
            {
                "schema_version": 2,
                "models": [
                    {
                        "profile_id": "planner",
                        "label": "Planner",
                        "runtime": "native",
                        "model": "m",
                        "access": "read_only",
                        "capabilities": ["planning", "general"],
                        "quality": 4,
                        "input_cost_per_million": 0,
                        "output_cost_per_million": 0,
                        "base_url": "https://api.test/v1",
                        "api_key_env": "WORKLOOP_NATIVE_API_KEY",
                    },
                    {
                        "profile_id": "executor",
                        "label": "Executor",
                        "runtime": "native",
                        "model": "m",
                        "access": "workspace_write",
                        "capabilities": ["implementation"],
                        "quality": 4,
                        "input_cost_per_million": 0,
                        "output_cost_per_million": 0,
                        "base_url": "https://api.test/v1",
                        "api_key_env": "WORKLOOP_NATIVE_API_KEY",
                    },
                ],
            }
        )
        (self.root / "agent-profiles.json").write_text(
            json.dumps(catalog.to_dict()), encoding="utf-8"
        )
        registry = ensure_registry(self.root)
        self.assertEqual(len(registry["providers"]), 1)
        provider = registry["providers"][0]
        self.assertEqual(provider["base_url"], "https://api.test/v1")
        key_text = (runtime_dir / provider["key_file"]).read_text(encoding="utf-8")
        self.assertEqual(key_text, "sk-legacy")
        self.assertEqual(registry["roles"]["executor"], "executor")
        # Re-running is idempotent once the registry file exists.
        (runtime_dir / "native-api-key").unlink()
        again = ensure_registry(self.root)
        self.assertEqual(len(again["providers"]), 1)


if __name__ == "__main__":
    unittest.main()
