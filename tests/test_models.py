from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.contracts import ModelProfile, ModelRequest, ModelRoutingConfig, PolicyBoundary
from app.models.backends.cli_backend import CliBackend
from app.models.backends.fake_backend import FakeBackend
from app.models.config import load_routing_config
from app.models.router import ModelRouter
from app.policy.policy_checker import PolicyChecker


def write_config(tmp: str, data: dict) -> Path:
    path = Path(tmp) / "models.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


VALID = {
    "profiles": [
        {"name": "plan-a", "provider": "cli", "model": "model-a", "command": ["a-cli", "{prompt}"]},
        {"name": "exec-b", "provider": "cli", "model": "model-b", "command": ["b-cli", "{prompt}", "--model", "{model}"]},
        {"name": "review-c", "provider": "fake", "model": "model-c"},
    ],
    "roles": {"planner": "plan-a", "executor": "exec-b", "reviewer": "review-c", "default": "exec-b"},
}


class ConfigTest(unittest.TestCase):
    def test_valid_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_routing_config(write_config(tmp, VALID))
            self.assertEqual(config.profiles["plan-a"].model, "model-a")
            self.assertEqual(config.roles["default"], "exec-b")

    def test_missing_default_role_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        del data["roles"]["default"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "default"):
                load_routing_config(write_config(tmp, data))

    def test_unknown_profile_reference_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["roles"]["reviewer"] = "no-such-profile"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "no-such-profile"):
                load_routing_config(write_config(tmp, data))

    def test_duplicate_profile_name_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["profiles"].append(dict(data["profiles"][0]))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "重复"):
                load_routing_config(write_config(tmp, data))

    def test_cli_profile_without_prompt_placeholder_raises(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["profiles"][0]["command"] = ["a-cli", "run"]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "prompt"):
                load_routing_config(write_config(tmp, data))


class RouterTest(unittest.TestCase):
    def _config(self):
        with tempfile.TemporaryDirectory() as tmp:
            return load_routing_config(write_config(tmp, VALID))

    def test_resolve_configured_role(self) -> None:
        profile, fallback = ModelRouter(self._config()).resolve("planner")
        self.assertEqual(profile.name, "plan-a")
        self.assertFalse(fallback)

    def test_resolve_unknown_role_falls_back_to_default(self) -> None:
        profile, fallback = ModelRouter(self._config()).resolve("no-such-role")
        self.assertEqual(profile.name, "exec-b")
        self.assertTrue(fallback)


FAKE_PROFILE = ModelProfile(name="f", provider="fake", model="fake-model")


class FakeBackendTest(unittest.TestCase):
    def test_default_reviewer_response_is_pass_json(self) -> None:
        response = FakeBackend().invoke(FAKE_PROFILE, ModelRequest(task_id="T", role="reviewer", prompt="p"))
        self.assertTrue(response.succeeded)
        self.assertEqual(json.loads(response.text)["verdict"], "pass")

    def test_configured_response_and_failure(self) -> None:
        backend = FakeBackend(responses={"planner": "计划内容"}, failures={"executor"})
        ok = backend.invoke(FAKE_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        bad = backend.invoke(FAKE_PROFILE, ModelRequest(task_id="T", role="executor", prompt="p"))
        self.assertEqual(ok.text, "计划内容")
        self.assertFalse(bad.succeeded)
        self.assertEqual(len(backend.requests), 2)


CLI_PROFILE = ModelProfile(
    name="c", provider="cli", model="model-x",
    command=["some-cli", "-p", "{prompt}", "--model", "{model}"], timeout_seconds=7,
)


class CliBackendTest(unittest.TestCase):
    def test_command_rendering_and_success(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="答案", stderr="")
        with mock.patch("app.models.backends.cli_backend.subprocess.run", return_value=completed) as run:
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="你好"))
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["some-cli", "-p", "你好", "--model", "model-x"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["timeout"], 7)
        self.assertTrue(response.succeeded)
        self.assertEqual(response.text, "答案")

    def test_nonzero_exit_is_failure(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="炸了")
        with mock.patch("app.models.backends.cli_backend.subprocess.run", return_value=completed):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("炸了", response.error)

    def test_timeout_is_failure(self) -> None:
        with mock.patch(
            "app.models.backends.cli_backend.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=7),
        ):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("超时", response.error)

    def test_empty_output_is_failure(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="  \n", stderr="")
        with mock.patch("app.models.backends.cli_backend.subprocess.run", return_value=completed):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("空输出", response.error)


def routing_with_models(executor_model: str, reviewer_model: str) -> ModelRoutingConfig:
    profiles = {
        "e": ModelProfile(name="e", provider="fake", model=executor_model),
        "r": ModelProfile(name="r", provider="fake", model=reviewer_model),
    }
    roles = {"executor": "e", "reviewer": "r", "default": "e"}
    return ModelRoutingConfig(profiles=profiles, roles=roles)


class ModelAssignmentPolicyTest(unittest.TestCase):
    def test_same_model_for_executor_and_reviewer_is_blocked(self) -> None:
        check = PolicyChecker().check_model_assignment(
            PolicyBoundary(), routing_with_models("m-1", "m-1")
        )
        self.assertFalse(check.passed)
        self.assertIn("m-1", check.issues[0])

    def test_distinct_models_pass(self) -> None:
        check = PolicyChecker().check_model_assignment(
            PolicyBoundary(), routing_with_models("m-1", "m-2")
        )
        self.assertTrue(check.passed)

    def test_custom_distinct_groups(self) -> None:
        policy = PolicyBoundary(distinct_model_roles=[["planner", "reviewer"]])
        routing = routing_with_models("m-1", "m-1")  # planner 未配置，回落 default=e/m-1
        check = PolicyChecker().check_model_assignment(policy, routing)
        self.assertFalse(check.passed)


if __name__ == "__main__":
    unittest.main()
