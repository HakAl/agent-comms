"""Drive ``agent_comms.mcp_server`` over stdio the way a real client does.

The MCP SDK cancels in-flight request handlers as soon as stdin reaches EOF,
so a client that writes its requests and closes stdin at once can lose any
response whose handler has not started yet. ``call_mcp`` keeps stdin open
until every request has been answered (or the server closes stdout), then
closes it and waits for a clean exit.
"""

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def call_mcp(
    args: list[str],
    calls: list[dict],
    env: dict[str, str],
    *,
    timeout: float = 15,
) -> list[dict]:
    """Initialize a server, send ``calls`` and return every JSON line it wrote.

    Raises ``subprocess.CalledProcessError`` on a nonzero exit and
    ``subprocess.TimeoutExpired`` when the answers or the exit take longer than
    ``timeout`` seconds in total.
    """
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
    pending = {message["id"] for message in messages if "id" in message}
    command = [sys.executable, "-m", "agent_comms.mcp_server", *args]
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()
    stdout: list[str] = []
    stderr: list[str] = []

    def read_stdout() -> None:
        for line in proc.stdout:
            stdout.append(line)
            lines.put(line)
        lines.put(None)

    def close_stdin() -> None:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True),
    ]
    for reader in readers:
        reader.start()
    responses: list[dict] = []
    try:
        try:
            proc.stdin.write("".join(json.dumps(message) + "\n" for message in messages))
            proc.stdin.flush()
        except BrokenPipeError:
            pass  # the server already exited; its exit status says why
        while pending:
            try:
                line = lines.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                raise subprocess.TimeoutExpired(command, timeout) from None
            if line is None:
                break
            if line.startswith("{"):
                response = json.loads(line)
                responses.append(response)
                pending.discard(response.get("id"))
        close_stdin()
        proc.wait(timeout=max(deadline - time.monotonic(), 0))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        close_stdin()
        for reader in readers:
            reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    while not lines.empty():
        line = lines.get_nowait()
        if line is not None and line.startswith("{"):
            responses.append(json.loads(line))
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, command, output="".join(stdout), stderr="".join(stderr)
        )
    return responses
