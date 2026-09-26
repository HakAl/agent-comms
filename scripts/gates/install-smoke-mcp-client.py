#!/usr/bin/env python3
"""Drive an installed ``agent-comms-mcp`` over stdio for the install-smoke gate.

Standard library only, so it runs on whatever python3 is on PATH rather than
on the environment under test. One MCP server process is started per actor,
exactly as an agent CLI would start it: ``initialize``, ``initialized``, then
the tool calls, all as JSON-RPC lines on stdin. The exchange is: the sender
posts a message; the recipient lists its inbox, reads the message, sends a
threaded reply and acknowledges; the sender sees the reply in its inbox. Any
refused call, non-zero exit or mismatch fails the run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading


def tool_call(call_id: int, name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def call_mcp(mcp: str, actor_id: str, calls: list[dict], timeout: float) -> list:
    """Run one server for ``actor_id`` and return the parsed result of each call.

    Stdin stays open until every call has answered: a stdio server may treat
    end of input as shutdown and drop a request it has not started yet.
    """
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "install-smoke", "version": "0.1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        *calls,
    ]
    process = subprocess.Popen(
        [mcp, "--actor-id", actor_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_lines: list[str] = []
    drain = threading.Thread(target=lambda: stderr_lines.extend(process.stderr), daemon=True)
    drain.start()
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    responses: dict = {}
    pending = {call["id"] for call in calls}
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        while pending:
            line = process.stdout.readline()
            if not line:
                break
            if line.startswith("{"):
                response = json.loads(line)
                if "id" in response:
                    responses[response["id"]] = response
                    pending.discard(response["id"])
        process.stdin.close()
        process.wait()
    finally:
        timer.cancel()
        drain.join(timeout=5)
    stderr = "".join(stderr_lines)
    if process.returncode != 0:
        sys.exit(f"FAIL install-smoke: agent-comms-mcp --actor-id {actor_id} exited {process.returncode}:\n{stderr}")
    if "agent-comms startup:" not in stderr:
        sys.exit(f"FAIL install-smoke: no startup report on stderr for {actor_id}:\n{stderr}")
    results = []
    for call in calls:
        response = responses.get(call["id"])
        if response is None:
            sys.exit(f"FAIL install-smoke: no response to {call['params']['name']} for {actor_id} within {timeout}s")
        if "error" in response or response["result"].get("isError"):
            sys.exit(f"FAIL install-smoke: {call['params']['name']} refused for {actor_id}: {json.dumps(response)}")
        content = [json.loads(item["text"]) for item in response["result"]["content"] if item.get("type") == "text"]
        results.append(content[0] if len(content) == 1 else content)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mcp", required=True, help="Path to the installed agent-comms-mcp command")
    parser.add_argument("--sender", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--subject", default="install-smoke ping")
    parser.add_argument("--body", default="Reply with PONG.")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    (sent,) = call_mcp(
        args.mcp,
        args.sender,
        [tool_call(2, "send_message", {"to_agents": [args.recipient], "subject": args.subject, "body": args.body})],
        args.timeout,
    )
    message_id = sent["id"]
    inbox, read, reply, acked = call_mcp(
        args.mcp,
        args.recipient,
        [
            tool_call(2, "list_inbox", {}),
            tool_call(3, "read_message", {"message_id": message_id}),
            tool_call(
                4,
                "send_message",
                {"to_agents": [args.sender], "subject": f"Re: {args.subject}", "body": "PONG", "parent_message_id": message_id},
            ),
            # A response text on the ack is refused (it would be invisible to
            # the sender); the reply above carries it and the ack is a receipt.
            tool_call(5, "ack_message", {"message_id": message_id}),
        ],
        args.timeout,
    )
    inbox_rows = inbox if isinstance(inbox, list) else [inbox]
    if not any(isinstance(row, dict) and row.get("id") == message_id for row in inbox_rows):
        sys.exit(f"FAIL install-smoke: {message_id} not in {args.recipient}'s inbox: {json.dumps(inbox)}")
    if read.get("body") != args.body or read.get("from") != args.sender:
        sys.exit(f"FAIL install-smoke: read_message returned {json.dumps(read)}")
    (sender_inbox,) = call_mcp(args.mcp, args.sender, [tool_call(2, "list_inbox", {})], args.timeout)
    sender_rows = sender_inbox if isinstance(sender_inbox, list) else [sender_inbox]
    if not any(isinstance(row, dict) and row.get("id") == reply["id"] for row in sender_rows):
        sys.exit(f"FAIL install-smoke: reply {reply['id']} not in {args.sender}'s inbox: {json.dumps(sender_inbox)}")
    print(json.dumps({"message_id": message_id, "reply_id": reply["id"], "acked": acked}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
