from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.composition import ModelCatalog
from app.agents.runtime_factory import default_model_catalog, native_catalog
from app.web.model_registry import ensure_registry, registry_status
from app.web.native_setup import native_status, save_native_config, test_native_connection


class NativeCatalogTest(unittest.TestCase):
    def test_native_catalog_maps_every_role(self) -> None:
        catalog = native_catalog(
            "https://api.deepseek.com/v1",
            "deepseek-chat",
            role_models={"planner": "deepseek-reasoner"},
        )
        entries = {option.profile_id: option for option in catalog.list_all()}
        self.assertEqual(set(entries), {"planner", "executor", "reviewer"})
        self.assertEqual(entries["planner"].model, "deepseek-reasoner")
        self.assertEqual(entries["executor"].model, "deepseek-chat")
        self.assertTrue(
            all(option.runtime == "native" for option in entries.values())
        )

    def test_default_catalog_env_overrides(self) -> None:
        environment = {
            "WORKLOOP_NATIVE_BASE_URL": "https://example.test/v1",
            "WORKLOOP_NATIVE_MODEL": "model-x",
            "WORKLOOP_NATIVE_PLANNER_MODEL": "model-plan",
        }
        with patch.dict(os.environ, environment, clear=False):
            catalog = default_model_catalog()
        entries = {option.profile_id: option for option in catalog.list_all()}
        self.assertEqual(entries["planner"].model, "model-plan")
        self.assertEqual(entries["executor"].model, "model-x")


class QuickSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_quick_setup_configures_registry_and_roles(self) -> None:
        save_native_config(
            self.root,
            {
                "base_url": "https://api.deepseek.com/v1/",
                "model": "deepseek-chat",
                "api_key": "sk-test",
                "proxy": "127.0.0.1:7897",
            },
        )
        status = native_status(self.root)
        self.assertTrue(status["configured"])
        self.assertEqual(status["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(status["model"], "deepseek-chat")
        self.assertTrue(status["api_key_set"])
        registry = registry_status(self.root)
        self.assertEqual(len(registry["providers"]), 1)
        self.assertEqual(
            registry["role_bindings"],
            {"planner": "native-readonly", "executor": "native-writer", "reviewer": "native-readonly"},
        )
        catalog = ModelCatalog.from_dict(
            json.loads((self.root / "agent-profiles.json").read_text(encoding="utf-8"))
        )
        entries = {option.profile_id: option for option in catalog.list_all()}
        self.assertEqual(entries["native-writer"].api_key_file, "keys/p-quick001.key")
        self.assertEqual(entries["native-writer"].proxy, "http://127.0.0.1:7897")
        # Key lives only in the key file, never in registry/catalog JSON.
        self.assertEqual(
            (self.root / "agent-runtime/keys/p-quick001.key").read_text(encoding="utf-8"),
            "sk-test",
        )
        registry_text = (
            self.root / "agent-runtime/model-providers.json"
        ).read_text(encoding="utf-8")
        catalog_text = (self.root / "agent-profiles.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-test", registry_text)
        self.assertNotIn("sk-test", catalog_text)

    def test_quick_setup_without_key_reuses_saved_key(self) -> None:
        save_native_config(
            self.root,
            {"base_url": "https://api.test/v1", "model": "m", "api_key": "sk-1"},
        )
        save_native_config(
            self.root,
            {"base_url": "https://api.test/v1", "model": "m2", "api_key": ""},
        )
        self.assertTrue(native_status(self.root)["configured"])
        self.assertEqual(
            (self.root / "agent-runtime/keys/p-quick001.key").read_text(encoding="utf-8"),
            "sk-1",
        )

    def test_quick_setup_validates_input(self) -> None:
        with self.assertRaises(ValueError):
            save_native_config(self.root, {"base_url": "ftp://x", "model": "m", "api_key": "k"})
        with self.assertRaises(ValueError):
            save_native_config(self.root, {"base_url": "https://api.test/v1", "model": "", "api_key": "k"})
        with self.assertRaises(ValueError):
            save_native_config(self.root, {"base_url": "https://api.test/v1", "model": "m", "api_key": ""})

    def test_connection_test_reports_missing_fields(self) -> None:
        ok, detail = test_native_connection(self.root, {"base_url": "", "model": "", "api_key": ""})
        self.assertFalse(ok)
        self.assertIn("Base URL", detail)


if __name__ == "__main__":
    unittest.main()
