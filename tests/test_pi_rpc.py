from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from app.agents.contracts import AgentAccess, AgentBudget, AgentPolicy, AgentRequest
from app.agents.pi_rpc import PiRpcProfile, PiRpcRuntime


_FAKE_PI = r'''
import json
import sys

payload = {
    "completed_steps": ["fake step"],
    "modified_files": [],
    "tests": [],
    "deviations": [],
    "remaining_risks": [],
    "next_steps": [],
}
for line in sys.stdin:
    request = json.loads(line)
    command = request.get("type")
    if command == "prompt":
        print(json.dumps({"id": request.get("id"), "type": "response", "command": "prompt", "success": True}), flush=True)
        print(json.dumps({"type": "agent_start"}), flush=True)
        text = json.dumps(payload)
        print(json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
    elif command == "get_state":
        print(json.dumps({"id": request.get("id"), "type": "response", "command": "get_state", "success": True, "data": {"sessionFile": "fake-session", "model": {"id": "fake-model"}}}), flush=True)
    elif command == "get_session_stats":
        # Real Pi 0.83 returns `cost` as an aggregated scalar number.
        print(json.dumps({"id": request.get("id"), "type": "response", "command": "get_session_stats", "success": True, "data": {"tokens": {"input": 2, "output": 1, "total": 3}, "cost": 0.01}}), flush=True)
'''



class PiRpcRuntimeTest(unittest.TestCase):
    def test_rpc_session_normalizes_events_and_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake_pi.py"
            fake.write_text(_FAKE_PI, encoding="utf-8")
            runtime = PiRpcRuntime(
                PiRpcProfile(
                    command=[sys.executable, str(fake)],
                    model="fake-model",
                    read_only_tools=(),
                    workspace_write_tools=(),
                )
            )
            result = runtime.invoke(
                AgentRequest(
                    task_id="TASK-test",
                    role="executor",
                    instructions="return the result",
                    workspace=root,
                    access=AgentAccess.WORKSPACE_WRITE,
                    # This exercises the RPC protocol, not the sandbox gate. Pi
                    # cannot enforce a network denial, so a write request under
                    # the default deny-all policy is refused; say explicitly
                    # that this request does not ask for that guarantee.
                    policy=AgentPolicy(network_allowed=True),
                    budget=AgentBudget(total_timeout_seconds=10, idle_timeout_seconds=3),
                )
            )

            self.assertTrue(result.succeeded, result.error)
            self.assertEqual(result.output["completed_steps"], ["fake step"])
            self.assertEqual(result.session_id, "fake-session")
            self.assertEqual(result.usage["total_cost_usd"], 0.01)
            self.assertEqual(result.runtime, "pi-rpc")
            self.assertTrue(any(event.event_type.value == "message_delta" for event in result.events))

    def test_write_access_refused_when_policy_denies_network_without_opt_in(self) -> None:
        """Pi is launched with a cwd and a tool allow-list — no OS sandbox — so a
        policy that denies network cannot be honored. Refuse rather than run and
        report success, which is what made an unenforced denial invisible."""
        # An unresolvable command on purpose: past the gate this must fail on the
        # missing binary, never spawn a real agent session from the test suite.
        runtime = PiRpcRuntime(
            PiRpcProfile(command=["workloop-absent-pi-binary"], model="fake-model")
        )
        request = AgentRequest(
            task_id="TASK-test",
            role="executor",
            instructions="write a file",
            workspace=Path("."),
            access=AgentAccess.WORKSPACE_WRITE,
            policy=AgentPolicy(network_allowed=False),
            budget=AgentBudget(total_timeout_seconds=5, idle_timeout_seconds=1),
        )
        previous = os.environ.pop("WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR", None)
        try:
            refused = runtime.invoke(request)
            self.assertFalse(refused.succeeded)
            self.assertEqual(refused.error_type, "sandbox_unavailable")

            os.environ["WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR"] = "1"
            # With the opt-in the gate is out of the way; the call now fails on
            # the missing binary instead, proving the gate is what blocked it.
            allowed = runtime.invoke(request)
            self.assertEqual(allowed.error_type, "environment_missing")
        finally:
            os.environ.pop("WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR", None)
            if previous is not None:
                os.environ["WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR"] = previous

    def test_read_only_access_is_not_gated_by_the_sandbox_check(self) -> None:
        """Planner/reviewer requests get no write or bash tool, so the missing
        sandbox does not change what they can reach: the gate must not fire."""
        runtime = PiRpcRuntime(
            PiRpcProfile(command=["workloop-absent-pi-binary"], model="fake-model")
        )
        result = runtime.invoke(
            AgentRequest(
                task_id="TASK-test",
                role="reviewer",
                instructions="review",
                workspace=Path("."),
                access=AgentAccess.READ_ONLY,
                policy=AgentPolicy(network_allowed=False),
                budget=AgentBudget(total_timeout_seconds=5, idle_timeout_seconds=1),
            )
        )
        self.assertEqual(result.error_type, "environment_missing")

    def test_model_can_be_overridden_per_request(self) -> None:
        runtime = PiRpcRuntime(PiRpcProfile(command=["pi"], model="default"))
        request = AgentRequest(
            task_id="TASK-test",
            role="planner",
            instructions="",
            workspace=Path("."),
            access=AgentAccess.READ_ONLY,
            model="opus-5",
            provider="anthropic",
            thinking="high",
        )

        command = runtime._command(request)

        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "opus-5")
        self.assertIn("--provider", command)
        self.assertEqual(command[command.index("--provider") + 1], "anthropic")
        self.assertEqual(command[command.index("--thinking") + 1], "high")


class PiUsageFromStatsTest(unittest.TestCase):
    """Locks the get_session_stats cost/tokens contract against Pi 0.83.

    Real Pi returns ``tokens`` as an object and ``cost`` as an aggregated
    scalar number (verified against the installed binary). The dict-with-total
    branch is a defensive fallback.
    """

    def test_scalar_cost_is_coerced_to_total_cost_usd(self) -> None:
        usage = PiRpcRuntime._usage_from_stats(
            {"tokens": {"input": 2, "output": 1, "total": 3}, "cost": 0.0123}
        )
        self.assertEqual(usage["total_cost_usd"], 0.0123)
        self.assertEqual(usage["input"], 2)
        self.assertEqual(usage["total"], 3)

    def test_zero_scalar_cost_is_recorded(self) -> None:
        usage = PiRpcRuntime._usage_from_stats({"tokens": {}, "cost": 0})
        self.assertEqual(usage["total_cost_usd"], 0.0)

    def test_dict_cost_with_total_falls_back(self) -> None:
        usage = PiRpcRuntime._usage_from_stats({"cost": {"total": 0.5}})
        self.assertEqual(usage["total_cost_usd"], 0.5)

    def test_missing_cost_is_omitted(self) -> None:
        usage = PiRpcRuntime._usage_from_stats({"tokens": {"total": 1}})
        self.assertNotIn("total_cost_usd", usage)
        self.assertEqual(usage["total"], 1)

    def test_non_dict_stats_returns_empty(self) -> None:
        self.assertEqual(PiRpcRuntime._usage_from_stats(None), {})
        self.assertEqual(PiRpcRuntime._usage_from_stats({}), {})


@unittest.skipUnless(shutil.which("pi"), "pi binary not installed on PATH")
class RealPiRpcHandshakeTest(unittest.TestCase):
    """Live protocol test against the installed `pi` binary.

    Exercises the get_state / get_session_stats query path through the real
    PiRpcRuntime machinery. These commands never call the model, so no API key
    or network is required. This is the test that catches protocol drift
    (e.g. the cost field changing shape).
    """

    def _spawn(self, runtime: PiRpcRuntime, request: AgentRequest) -> subprocess.Popen:
        return subprocess.Popen(
            runtime._command(request),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )

    def test_get_state_and_session_stats_match_pi_rpc_assumptions(self) -> None:
        pi_bin = shutil.which("pi")
        self.assertIsNotNone(pi_bin)  # guarded by the class skip decorator
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = PiRpcRuntime(
                PiRpcProfile(
                    command=[pi_bin],
                    model="claude-3-5-haiku-latest",
                    provider="anthropic",
                    thinking="medium",
                    read_only_tools=(),
                    workspace_write_tools=(),
                )
            )
            request = AgentRequest(
                task_id="TASK-probe",
                role="planner",
                instructions="",
                workspace=root,
                access=AgentAccess.READ_ONLY,
                budget=AgentBudget(total_timeout_seconds=20, idle_timeout_seconds=10),
            )
            process = self._spawn(runtime, request)
            try:
                records: "queue.Queue[tuple[str, bytes | None]]" = queue.Queue()
                PiRpcRuntime._start_reader(process.stdout, "stdout", records)
                PiRpcRuntime._start_reader(process.stderr, "stderr", records)
                deadline = time.monotonic() + 20
                raw_events: list[dict[str, Any]] = []

                state = runtime._query(process, records, "get_state", deadline, raw_events)
                stats = runtime._query(process, records, "get_session_stats", deadline, raw_events)

                self.assertIsInstance(state, dict)
                self.assertTrue(state.get("sessionFile") or state.get("sessionId"))
                self.assertIsInstance(state.get("model"), dict)
                self.assertTrue(state["model"].get("id"))

                self.assertIsInstance(stats, dict)
                self.assertIsInstance(stats.get("tokens"), dict)
                # Real Pi 0.83 returns cost as a scalar number.
                self.assertIsInstance(stats.get("cost"), (int, float))

                usage = PiRpcRuntime._usage_from_stats(stats)
                self.assertIn("total_cost_usd", usage)
                self.assertIsInstance(usage["total_cost_usd"], float)
            finally:
                PiRpcRuntime._stop_process(process, None)


if __name__ == "__main__":
    unittest.main()
