from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agents.profiles import load_agent_profiles, migrate_legacy_profiles


class AgentProfileMigrationTest(unittest.TestCase):
    def test_migration_discards_commands_and_freezes_role_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "models.json"
            destination = root / "agent-profiles.json"
            source.write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "name": "legacy",
                                "provider": "cli",
                                "model": "legacy-model",
                                "command": ["arbitrary", "{prompt}"],
                            }
                        ],
                        "roles": {"default": "legacy"},
                    }
                ),
                "utf-8",
            )

            migrate_legacy_profiles(source, destination)
            profiles = load_agent_profiles(destination)

            self.assertEqual(profiles["planner"].access, "read_only")
            self.assertEqual(profiles["executor"].runtime, "codex_cli")
            self.assertEqual(profiles["executor"].access, "workspace_write")
            self.assertNotIn("command", destination.read_text("utf-8"))

    def test_loader_rejects_commands_and_permission_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent-profiles.json"
            roles = {
                "planner": {
                    "runtime": "claude_code",
                    "model": "m",
                    "access": "read_only",
                },
                "executor": {
                    "runtime": "codex_cli",
                    "model": "m",
                    "access": "workspace_write",
                },
                "reviewer": {
                    "runtime": "claude_code",
                    "model": "m",
                    "access": "read_only",
                },
            }
            roles["executor"]["command"] = ["arbitrary"]
            path.write_text(json.dumps({"roles": roles}), "utf-8")
            with self.assertRaisesRegex(ValueError, "command"):
                load_agent_profiles(path)

            del roles["executor"]["command"]
            roles["reviewer"]["access"] = "workspace_write"
            path.write_text(json.dumps({"roles": roles}), "utf-8")
            with self.assertRaisesRegex(ValueError, "reviewer"):
                load_agent_profiles(path)


if __name__ == "__main__":
    unittest.main()
