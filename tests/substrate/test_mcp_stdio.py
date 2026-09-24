import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from agent_comms.mcp_server import MISSING_ACTOR_ID_MESSAGE, create_server
from agent_comms.schema import ValidationError
from agent_comms.policies import OPERATOR_MAILBOX_POLICY
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[2]


class McpStdioTest(unittest.TestCase):
    def test_file_backed_send_roundtrips_marker_collision(self) -> None:
        """spec proof 4"""
        env = self.base_env()
        fixture = ROOT / "tests/fixtures/send-message-marker-collision-claude-v1.txt"
        expected = fixture.read_bytes()
        self.assertEqual(
            __import__("hashlib").sha256(expected).hexdigest(),
            "91e6b17ce61330c74c5f02510a15e8ceb667d36830d9c9c4bcaf8ab630992355",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(
                ["--db", db_path, "bootstrap", "--config", str(config)], env
            )
            sender_root = root / "team-b"
            sender_root.mkdir(parents=True)
            (sender_root / "collision.txt").write_bytes(expected)
            parent = Store(Path(db_path)).send_message(
                "alpha-architect", ["team-b-architect"], "parent", "parent body", []
            )

            def send(arguments: dict, call_id: int) -> dict:
                responses = self.call_mcp(
                    ["--db", db_path, "--actor-id", "team-b-architect"],
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": call_id,
                            "method": "tools/call",
                            "params": {"name": "send_message", "arguments": arguments},
                        }
                    ],
                    env,
                )
                return next(
                    response for response in responses if response.get("id") == call_id
                )

            inline = send(
                {
                    "to_agents": ["alpha-architect"],
                    "subject": "inline",
                    "body": expected.decode(),
                    "parent_message_id": parent["id"],
                },
                2,
            )
            self.assertTrue(inline["result"]["isError"])
            self.assertIn("parameter framing leaked", json.dumps(inline))
            backed = send(
                {
                    "to_agents": ["alpha-architect"],
                    "subject": "backed",
                    "body_file": "collision.txt",
                    "parent_message_id": parent["id"],
                },
                3,
            )
            self.assertFalse(backed["result"].get("isError", False), backed)
            sent = json.loads(backed["result"]["content"][0]["text"])
            read = self.call_mcp(
                ["--db", db_path, "--actor-id", "alpha-architect"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "read_message",
                            "arguments": {"message_id": sent["id"]},
                        },
                    }
                ],
                env,
            )
            result = next(response for response in read if response.get("id") == 4)
            body = json.loads(result["result"]["content"][0]["text"])["body"]
            self.assertEqual(body.encode(), expected)
            with Store(Path(db_path)).connection() as conn:
                row = conn.execute(
                    "select parent_message_id from message_threads where message_id=?",
                    (sent["id"],),
                ).fetchone()
            self.assertEqual(row["parent_message_id"], parent["id"])

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("WAKE_POLICY", None)
        env.pop("WAKE_POLICY_VERSION", None)
        env["PYTHONPYCACHEPREFIX"] = "/private/tmp/agent-comms-pycache"
        return env

    def write_actor_config(self, root: Path) -> Path:
        config = root / "actors.json"
        config.write_text(
            json.dumps(
                {
                    "actors": {
                        "team-b-architect": {
                            "kind": "agent",
                            "display_name": "team-b-architect",
                            "team": "team-b",
                            "role": "architect",
                            "project_root": str(root / "team-b"),
                            "capabilities": [],
                        },
                        "alpha-architect": {
                            "kind": "agent",
                            "display_name": "alpha-architect",
                            "team": "alpha",
                            "role": "architect",
                            "project_root": str(root / "alpha"),
                            "capabilities": [],
                        },
                        "team-b-worker": {
                            "kind": "agent",
                            "display_name": "team-b-worker",
                            "team": "team-b",
                            "role": "worker",
                            "project_root": str(root / "team-b-worker"),
                            "capabilities": [],
                            "owner": "team-b-architect",
                        },
                        "01M36YTJV9XBW95S6ZWV47C4RG": {
                            "kind": "human",
                            "display_name": "alice",
                        },
                        "cron-sweep": {
                            "kind": "system",
                            "display_name": "cron:daily_inbox_sweep",
                            "system_class": "cron",
                            "system_instance": "daily_inbox_sweep",
                        },
                    }
                }
            )
        )
        return config

    def run_agent_comms(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agent_comms.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def call_mcp(self, args: list[str], calls: list[dict], env: dict[str, str]) -> list[dict]:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0.1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            *calls,
        ]
        payload = "\n".join(json.dumps(message) for message in messages) + "\n"
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.mcp_server", *args],
            cwd=ROOT,
            env=env,
            input=payload,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]

    def test_create_server_none_raises(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            create_server(actor_id=None)

        message = str(raised.exception)
        self.assertIn("--actor-id", message)
        self.assertIn("docs/mcp-setup.md", message)

    def test_create_server_empty_and_whitespace_raise(self) -> None:
        for actor_id in ("", "   "):
            with self.subTest(actor_id=repr(actor_id)):
                with self.assertRaises(ValidationError) as raised:
                    create_server(actor_id=actor_id)

                message = str(raised.exception)
                self.assertIn("--actor-id", message)
                self.assertIn("docs/mcp-setup.md", message)

    def test_create_server_padded_registered_actor_refuses_unknown_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "agent-comms.sqlite"
            Store(db_path).register_agent(
                "team-b-architect",
                "team-b",
                "architect",
                str(root / "team-b-architect"),
                [],
            )

            with patch("agent_comms.mcp_server.require_mcp", return_value=lambda _name: object()):
                with self.assertRaises(ValidationError) as raised:
                    create_server(db_path=str(db_path), actor_id=" team-b-architect ")

        self.assertEqual(str(raised.exception), "unknown actor:  team-b-architect ")

    def test_launch_without_actor_id_refuses_before_jsonrpc(self) -> None:
        env = self.base_env()

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "agent-comms.sqlite")
            result = subprocess.run(
                [sys.executable, "-m", "agent_comms.mcp_server", "--db", db_path],
                cwd=ROOT,
                env=env,
                input=json.dumps(
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
                )
                + "\n",
                text=True,
                capture_output=True,
                timeout=15,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(MISSING_ACTOR_ID_MESSAGE, result.stderr)
        self.assertNotIn('"jsonrpc"', result.stdout)

    def test_launch_with_actor_id_serves_initialize(self) -> None:
        env = self.base_env()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(["--db", db_path, "bootstrap", "--config", str(config)], env)

            responses = self.call_mcp(["--db", db_path, "--actor-id", "team-b-architect"], [], env)

        initialize_response = next(response for response in responses if response.get("id") == 1)
        self.assertIn("protocolVersion", initialize_response["result"])

    def test_list_agents_tool_call_over_stdio(self) -> None:
        env = self.base_env()
        # The example roster declares its project root via an env var.
        env["PROJECT_A_ROOT"] = "/srv/project-a"

        # Bootstrap the shipped example roster into a throwaway DB, never the
        # default production DB.
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        db_path = str(Path(temp_dir) / "agent-comms.sqlite")
        example_config = str(ROOT / "config" / "actors.example.json")

        bootstrap = self.run_agent_comms(["--db", db_path, "bootstrap", "--config", example_config], env)
        self.assertIn("team-a-architect", bootstrap.stdout)

        responses = self.call_mcp(
            ["--db", db_path, "--actor-id", "team-a-architect"],
            [
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_agents", "arguments": {}},
                }
            ],
            env,
        )
        tool_response = next(response for response in responses if response.get("id") == 2)
        content_text = "\n".join(item.get("text", "") for item in tool_response["result"]["content"])
        self.assertIn("team-a-architect", content_text)
        self.assertIn("team-a-codex-worker", content_text)

    def test_scoped_mcp_binds_actor_identity(self) -> None:
        env = self.base_env()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(["--db", db_path, "bootstrap", "--config", str(config)], env)

            list_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "team-b-architect"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                ],
                env,
            )
            send_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "team-b-architect"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "send_message",
                            "arguments": {
                                "to_agents": ["alpha-architect"],
                                "subject": "Scoped send",
                                "body": "Identity should come from --actor-id.",
                            },
                        },
                    },
                ],
                env,
            )

            tools_response = next(response for response in list_responses if response.get("id") == 2)
            tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}
            self.assertNotIn("register_agent", tools)
            self.assertNotIn("from_agent", json.dumps(tools["send_message"]["inputSchema"]))
            self.assertNotIn("agent_id", json.dumps(tools["list_inbox"]["inputSchema"]))
            self.assertIn("dispatch_agent", tools)
            dispatch_schema = json.dumps(tools["dispatch_agent"]["inputSchema"])
            self.assertNotIn("producer_actor_id", dispatch_schema)
            self.assertIn("idempotency_key", dispatch_schema)
            self.assertIn("post_handoff", tools)
            self.assertIn("read_handoff", tools)
            self.assertNotIn("actor_id", json.dumps(tools["post_handoff"]["inputSchema"]))

            send_response = next(response for response in send_responses if response.get("id") == 3)
            send_text = "\n".join(item.get("text", "") for item in send_response["result"]["content"])
            sent = json.loads(send_text)
            self.assertEqual(sent["from"], "team-b-architect")

            message_body = "Identity should come from --actor-id."
            message_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "alpha-architect"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "list_inbox",
                            "arguments": {"unread_only": True},
                        },
                    },
                ],
                env,
            )
            read_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "alpha-architect"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "read_message",
                            "arguments": {"message_id": sent["id"]},
                        },
                    },
                ],
                env,
            )
            list_response = next(response for response in message_responses if response.get("id") == 5)
            listed = list_response["result"]["structuredContent"]["result"]
            self.assertEqual(listed[0]["body_snippet"], message_body)
            self.assertEqual(listed[0]["body_chars"], len(message_body))
            self.assertNotIn("body", listed[0])

            read_response = next(response for response in read_responses if response.get("id") == 6)
            read_text = "\n".join(item.get("text", "") for item in read_response["result"]["content"])
            read = json.loads(read_text)
            self.assertEqual(read["body"], message_body)
            self.assertEqual(read["body_chars"], len(message_body))
            self.assertNotIn("body_snippet", read)

            inbox = self.run_agent_comms(["--db", db_path, "inbox", "alpha-architect", "--all"], env)
            self.assertIn("Scoped send", inbox.stdout)

            dispatch_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "team-b-architect"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "dispatch_agent",
                            "arguments": {
                                "target_actor_id": "team-b-worker",
                                "idempotency_key": "mcp-dispatch",
                                "subject": "Scoped dispatch",
                                "body": "Identity should come from --actor-id.",
                            },
                        },
                    }
                ],
                env,
            )
            dispatch_response = next(response for response in dispatch_responses if response.get("id") == 4)
            dispatch_text = "\n".join(item.get("text", "") for item in dispatch_response["result"]["content"])
            dispatch = json.loads(dispatch_text)
            self.assertEqual(dispatch["producer_actor_id"], "team-b-architect")
            self.assertEqual(dispatch["recipient_actor_id"], "team-b-worker")

    def test_worker_policy_mcp_surface_hides_dispatch_agent(self) -> None:
        env = self.base_env()
        env["WAKE_POLICY"] = WORKER_DISPATCH_POLICY

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(["--db", db_path, "bootstrap", "--config", str(config)], env)

            responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "team-b-worker"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                ],
                env,
            )

            tools_response = next(response for response in responses if response.get("id") == 2)
            tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}
            self.assertIn("send_message", tools)
            self.assertIn("list_inbox", tools)
            self.assertNotIn("list_agents", tools)
            self.assertNotIn("list_actors", tools)
            self.assertNotIn("list_status", tools)
            self.assertNotIn("dispatch_agent", tools)
            self.assertNotIn("post_handoff", tools)
            self.assertNotIn("read_handoff", tools)
            self.assertIn("close_dispatch", tools)

            send_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", "team-b-worker"],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "send_message",
                            "arguments": {
                                "to_agents": ["team-b-architect"],
                                "subject": "Blocked initiate",
                                "body": "Worker policy must require a parent.",
                            },
                        },
                    },
                ],
                env,
            )

            send_response = next(response for response in send_responses if response.get("id") == 3)
            self.assertTrue(send_response["result"]["isError"])
            send_text = "\n".join(item.get("text", "") for item in send_response["result"]["content"])
            self.assertIn("parent_message_id", send_text)

    def test_operator_mailbox_policy_launches_human_and_allows_initiate_and_reply(self) -> None:
        env = self.base_env()
        env["WAKE_POLICY"] = OPERATOR_MAILBOX_POLICY
        human_id = "01M36YTJV9XBW95S6ZWV47C4RG"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(["--db", db_path, "bootstrap", "--config", str(config)], env)

            store = Store(Path(db_path))
            incoming = store.send_message(
                from_agent="team-b-architect",
                to_agents=[human_id],
                subject="Needs operator",
                body="Please reply from the operator mailbox.",
                refs=[],
            )

            responses = self.call_mcp(
                ["--db", db_path, "--actor-id", human_id],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                ],
                env,
            )
            initiate_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", human_id],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "send_message",
                            "arguments": {
                                "to_agents": ["team-b-architect"],
                                "subject": "Operator initiate",
                                "body": "This message has no parent.",
                            },
                        },
                    },
                ],
                env,
            )
            reply_responses = self.call_mcp(
                ["--db", db_path, "--actor-id", human_id],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "send_message",
                            "arguments": {
                                "to_agents": ["team-b-architect"],
                                "subject": "Operator reply",
                                "body": "This message is parented.",
                                "parent_message_id": incoming["id"],
                            },
                        },
                    },
                ],
                env,
            )

            tools_response = next(response for response in responses if response.get("id") == 2)
            tools = {tool["name"] for tool in tools_response["result"]["tools"]}
            self.assertEqual(
                {
                    "list_inbox",
                    "list_actors",
                    "read_message",
                    "ack_message",
                    "close_message",
                    "send_message",
                    "post_status",
                    "wait_for_reply",
                    "read_handoff",
                },
                tools,
            )
            self.assertNotIn("post_handoff", tools)
            self.assertNotIn("dispatch_agent", tools)
            self.assertNotIn("close_dispatch", tools)
            self.assertNotIn("register_actor", tools)
            self.assertNotIn("register_agent", tools)

            initiate_response = next(response for response in initiate_responses if response.get("id") == 3)
            self.assertFalse(initiate_response["result"].get("isError", False))
            initiate_text = "\n".join(item.get("text", "") for item in initiate_response["result"]["content"])
            initiated = json.loads(initiate_text)
            self.assertEqual(initiated["from"], human_id)
            self.assertEqual(initiated["to"], ["team-b-architect"])

            reply_response = next(response for response in reply_responses if response.get("id") == 4)
            self.assertFalse(reply_response["result"].get("isError", False))
            reply_text = "\n".join(item.get("text", "") for item in reply_response["result"]["content"])
            reply = json.loads(reply_text)
            self.assertEqual(reply["from"], human_id)

    def test_scoped_mcp_rejects_non_launchable_system_actor(self) -> None:
        env = self.base_env()
        env["WAKE_POLICY"] = OPERATOR_MAILBOX_POLICY

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(["--db", db_path, "bootstrap", "--config", str(config)], env)

            result = subprocess.run(
                [sys.executable, "-m", "agent_comms.mcp_server", "--db", db_path, "--actor-id", "cron-sweep"],
                cwd=ROOT,
                env=env,
                input="",
                text=True,
                capture_output=True,
                timeout=15,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("actor is not launchable", result.stderr)

    def test_scoped_mcp_rejects_unknown_actor(self) -> None:
        env = self.base_env()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "agent-comms.sqlite")
            config = self.write_actor_config(root)
            self.run_agent_comms(["--db", db_path, "bootstrap", "--config", str(config)], env)

            result = subprocess.run(
                [sys.executable, "-m", "agent_comms.mcp_server", "--db", db_path, "--actor-id", "unknown-agent"],
                cwd=ROOT,
                env=env,
                input="",
                text=True,
                capture_output=True,
                timeout=15,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown actor", result.stderr)


if __name__ == "__main__":
    unittest.main()
