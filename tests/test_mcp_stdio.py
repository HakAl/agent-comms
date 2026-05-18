import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class McpStdioTest(unittest.TestCase):
    def test_list_agents_tool_call_over_stdio(self) -> None:
        env = os.environ.copy()
        env["UV_CACHE_DIR"] = "/tmp/uv-cache"
        env["PYTHONPYCACHEPREFIX"] = "/tmp/agent-comms-pycache"

        bootstrap = subprocess.run(
            [
                str(ROOT / "scripts" / "agent-comms"),
                "bootstrap",
                "--config",
                str(ROOT / "config" / "agents.example.json"),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("team-a-architect", bootstrap.stdout)

        payload = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "smoke", "version": "0.1"},
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list_agents", "arguments": {}},
                    }
                ),
                "",
            ]
        )
        result = subprocess.run(
            [str(ROOT / "scripts" / "agent-comms-mcp")],
            cwd=ROOT,
            env=env,
            input=payload,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        responses = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        tool_response = next(response for response in responses if response.get("id") == 2)
        content_text = "\n".join(item.get("text", "") for item in tool_response["result"]["content"])
        self.assertIn("team-a-architect", content_text)
        self.assertIn("team-b-architect", content_text)


if __name__ == "__main__":
    unittest.main()
