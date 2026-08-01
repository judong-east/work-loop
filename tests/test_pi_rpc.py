from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from app.agents.contracts import AgentAccess, AgentBudget, AgentRequest
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
        print(json.dumps({"id": request.get("id"), "type": "response", "command": "get_session_stats", "success": True, "data": {"tokens": {"total": 3}, "cost": {"total": 0.01}}}), flush=True)
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
                    budget=AgentBudget(total_timeout_seconds=10, idle_timeout_seconds=3),
                )
            )

            self.assertTrue(result.succeeded, result.error)
            self.assertEqual(result.output["completed_steps"], ["fake step"])
            self.assertEqual(result.session_id, "fake-session")
            self.assertEqual(result.usage["total_cost_usd"], 0.01)
            self.assertEqual(result.runtime, "pi-rpc")
            self.assertTrue(any(event.event_type.value == "message_delta" for event in result.events))

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


if __name__ == "__main__":
    unittest.main()
