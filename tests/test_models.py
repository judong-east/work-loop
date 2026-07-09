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

    def test_missing_config_file_has_friendly_error(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "模型配置文件"):
            load_routing_config(Path("no-such-models.json"))

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
    def setUp(self) -> None:
        from app.models.backends.cli_backend import clear_task_cancel
        clear_task_cancel("T")

    def fake_process(self, returncode: int = 0, stdout: str = "答案", stderr: str = ""):
        process = mock.Mock()
        process.returncode = returncode
        process.poll.return_value = returncode
        process.communicate.return_value = (stdout, stderr)
        return process

    def test_command_rendering_and_success(self) -> None:
        process = self.fake_process(stdout="答案")
        with mock.patch("app.models.backends.cli_backend.subprocess.Popen", return_value=process) as popen:
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="你好"))
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["some-cli", "-p", "你好", "--model", "model-x"])
        self.assertFalse(kwargs.get("shell", False))
        process.communicate.assert_called_once_with(timeout=7)
        self.assertTrue(response.succeeded)
        self.assertEqual(response.text, "答案")

    def test_windows_cmd_shim_is_resolved_via_which(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "node_modules" / "pkg" / "tool.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("", encoding="utf-8")
            cmd = root / "some-cli.cmd"
            cmd.write_text('@ECHO OFF\n"%dp0%\\node_modules\\pkg\\tool.exe" %*\n', encoding="utf-8")
            process = self.fake_process(stdout="答案")
            with mock.patch("app.models.backends.cli_backend.shutil.which", return_value=str(cmd)) as which, mock.patch(
                "app.models.backends.cli_backend.subprocess.Popen", return_value=process
            ) as popen:
                response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        which.assert_called_once_with("some-cli")
        self.assertEqual(Path(popen.call_args[0][0][0]), exe)
        self.assertTrue(response.succeeded)

    def test_nonzero_exit_is_failure(self) -> None:
        process = self.fake_process(returncode=1, stdout="", stderr="炸了")
        with mock.patch("app.models.backends.cli_backend.subprocess.Popen", return_value=process):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("炸了", response.error)

    def test_timeout_is_failure(self) -> None:
        process = self.fake_process()
        process.communicate.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=7), ("", "")]
        with mock.patch("app.models.backends.cli_backend.subprocess.Popen", return_value=process):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("超时", response.error)
        process.kill.assert_called_once()

    def test_empty_output_is_failure(self) -> None:
        process = self.fake_process(stdout="  \n")
        with mock.patch("app.models.backends.cli_backend.subprocess.Popen", return_value=process):
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        self.assertFalse(response.succeeded)
        self.assertIn("空输出", response.error)

    def test_cancel_task_processes_marks_invocation_interrupted(self) -> None:
        from app.models.backends.cli_backend import cancel_task_processes, clear_task_cancel

        clear_task_cancel("T")
        cancel_task_processes("T")
        with mock.patch("app.models.backends.cli_backend.subprocess.Popen") as popen:
            response = CliBackend().invoke(CLI_PROFILE, ModelRequest(task_id="T", role="planner", prompt="p"))
        popen.assert_not_called()
        self.assertFalse(response.succeeded)
        self.assertIn("中断", response.error)
        clear_task_cancel("T")


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
