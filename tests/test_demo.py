from __future__ import annotations

import json
import unittest

from demo.mock_openai_server import respond


class DemoProtocolTest(unittest.TestCase):
    def test_mock_uses_v2_structured_file_changes(self) -> None:
        request = {
            "node_id": "implementation",
            "node_type": "implementation",
            "shared_context": {"inputs": {"project": {"workspace_path": ""}}},
        }
        response = respond([{"role": "user", "content": json.dumps(request)}])
        message = response["choices"][0]["message"]
        output = json.loads(message["content"])

        self.assertNotIn("tool_calls", message)
        self.assertEqual(output["file_changes"][0]["operation"], "write")
        self.assertEqual(output["file_changes"][0]["path"], "hello.py")


if __name__ == "__main__":
    unittest.main()
