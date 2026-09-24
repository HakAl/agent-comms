from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tempfile
import json
import os
import shlex
import subprocess
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path

from tests.dispatch_cell_harness import (
    CODEX_WORKER_ID,
    POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
    POSITIVE_CELL_TTL_SECONDS,
    codex_cell_preflight,
    long_running_worker_spawn,
    make_codex_harness,
    supervisor_root_negative_fixture,
    wait_until,
)
from agent_comms import supervisor
from agent_comms.adapters._base import SupervisorUnreachable


ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def patched_env(values: dict[str, str]):
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def process_is_live(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and "Z" not in result.stdout.strip()


def env_present(value: object) -> bool:
    if isinstance(value, dict):
        return bool(value.get("present"))
    return bool(value)


CODEX_READY, CODEX_SKIP_REASON = codex_cell_preflight()


@unittest.skipUnless(CODEX_READY, CODEX_SKIP_REASON)
class CodexCellTest(unittest.TestCase):
    def test_codex_cell_file_backed_reply_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            harness = make_codex_harness(root)
            harness.halt_on_terminal = False
            stack.callback(harness.close)
            dispatch, fixture = harness.file_backed_reply_roundtrip(runtime="codex")
            _, other_replies = harness.assert_closed_with_file_backed_reply(
                dispatch, fixture
            )
            self.assertEqual(other_replies, [])
            # Criterion 16: terminal SQL precedes the final JSON event.
            harness.wait_for_natural_exit(POSITIVE_CELL_TERMINAL_WAIT_SECONDS)
            worker_events = Path(dispatch["observed_values"]["worker_events"])
            events = [
                json.loads(line)
                for line in worker_events.read_text().splitlines()
                if line.strip()
            ]
            self.assertEqual(events[0]["type"], "thread.started")
            calls = [
                event["item"]
                for event in events
                if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "mcp_tool_call"
            ]
            self.assertTrue(
                any(
                    item.get("server") == "agent-comms"
                    and item.get("tool") == "send_message"
                    and item.get("status") == "completed"
                    for item in calls
                )
            )
            self.assertTrue(
                any(
                    event.get("type") == "turn.completed"
                    and isinstance(event.get("usage"), dict)
                    for event in events
                ),
                "missing turn.completed after clean wrapper exit",
            )
            with harness.store.connection() as conn:
                delivered = conn.execute(
                    "select 1 from messages where from_agent=? and body like ? limit 1",
                    (harness.worker_id, "%</subject>%"),
                ).fetchone()
            if delivered is not None:
                outcome = "delivered"
            elif any(item.get("status") == "failed" for item in calls) or any(
                event.get("type") == "error" for event in events
            ):
                outcome = "refused"
            else:
                outcome = "absent"
            print(f"CODEX_INLINE_OUTCOME={outcome}")

    def test_two_same_lineage_dispatches_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            harness = make_codex_harness(Path(temp_dir))
            stack.callback(harness.close)
            rows = [harness.store.dispatch_agent(
                "alpha-architect", harness.worker_id, f"codex-concurrent-{index}",
                "concurrency", (
                    "Create a real git delta in this project root by adding one uncommitted "
                    f"test file named codex-concurrent-{index}.txt. Reply with PONG using "
                    "the parented reply, then call close_dispatch with result=satisfied, "
                    "the parented reply id, and delta=true."
                ), [],
                adapter_for_runtime=lambda _runtime: harness.adapter,
                ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
            ) for index in range(2)]
            self.assertEqual([row["status"] for row in rows], ["in_flight", "in_flight"])
            self.assertNotEqual(rows[0]["spawn_handle"], rows[1]["spawn_handle"])
            harness._spawn_handles.update(row["spawn_handle"] for row in rows)

            def both_terminal():
                harness.store.reconcile_dispatches(lambda _runtime: harness.adapter, human_actor_id="01M36YTJV9XBW95S6ZWV47C4RG")
                current = [harness.store._dispatch_by_idempotency_key_fresh(
                    "alpha-architect", f"codex-concurrent-{index}") for index in range(2)]
                return current if all(row["status"] in {"closed", "dlq"} for row in current) else None

            terminal = wait_until(both_terminal, POSITIVE_CELL_TERMINAL_WAIT_SECONDS)
            for row in terminal:
                harness.assert_closed_with_parented_reply(row)

    def test_codex_cell_dispatch_replies_and_closes_inline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            harness = make_codex_harness(Path(temp_dir))
            stack.callback(harness.close)

            dispatch = harness.dispatch_and_wait(
                idempotency_key="codex-cell-inline",
                ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
                timeout_seconds=POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
                body=(
                    "Create a real git delta in this project root by adding one uncommitted "
                    "test file. Reply with PONG, then call close_dispatch with result=satisfied, "
                    "the parented reply id, and delta=true."
                ),
                in_flight_assertion=harness.assert_v2_close_denials_leave_state_unchanged,
            )

            harness.assert_closed_with_parented_reply(dispatch)

    def test_codex_sandbox_blocks_forbidden_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            harness = make_codex_harness(root)
            stack.callback(harness.close)
            result_path = root / "alpha-codex-worker" / "sandbox_result.json"
            forbidden_path = ROOT / ".agent-comms" / "codex-forbidden-write-probe.txt"
            forbidden_path.unlink(missing_ok=True)

            dispatch = harness.dispatch_and_wait(
                idempotency_key="codex-sandbox-forbidden-write",
                ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
                timeout_seconds=POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
                body=(
                    "Attempt exactly one forbidden filesystem write to "
                    f"{forbidden_path}. Capture the outcome by writing JSON to "
                    f"{result_path} with keys forbidden_write_succeeded and error. "
                    "Then send the normal parented reply and call close_dispatch with "
                    "result=satisfied, that reply id, and delta=true."
                ),
            )

            harness.assert_closed_with_parented_reply(dispatch)
            self.assertTrue(result_path.exists(), f"missing sandbox result at {result_path}")
            payload = __import__("json").loads(result_path.read_text())
            self.assertFalse(payload["forbidden_write_succeeded"], payload)
            self.assertFalse(forbidden_path.exists())

    def test_codex_cannot_touch_protected_supervisor_root(self) -> None:
        # T8 negative runtime cell: a bounded Codex worker must be OS-sandbox
        # denied from creating, connecting to, or writing the protected
        # supervisor control root (~/.agent-comms/run/s). The run token is a
        # same-run correlation nonce, NOT a same-uid secret and NOT cross-user
        # privilege separation; this OS denial is the capability boundary.
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            harness = make_codex_harness(root)
            stack.callback(harness.close)
            # Authenticated certification retains the FIXED protected root
            # explicitly: only a real bounded Codex worker denied under the live
            # default control root proves the OS capability boundary.
            probe = stack.enter_context(
                supervisor_root_negative_fixture(
                    root / CODEX_WORKER_ID, protected_root=supervisor._DEFAULT_CONTROL_ROOT
                )
            )

            dispatch = harness.dispatch_and_wait(
                idempotency_key="codex-supervisor-root-negative",
                ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
                timeout_seconds=POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
                subject="supervisor root boundary",
                body=(
                    "This is an expected-to-be-denied sandbox boundary self-test against "
                    "the protected agent-comms supervisor control root, not a real task. "
                    "Run exactly this one Bash command and nothing else, then send the "
                    "normal parented reply and close the triggering message: "
                    f"`{probe['command']}` from your established worker cwd. "
                    "The script captures the result in your "
                    "working directory. Call close_dispatch with result=satisfied, the "
                    "parented reply id, and delta=true."
                ),
            )

            # Auth cell: the dispatch MUST end closed with the normal parented
            # reply; there is no DLQ conditional pass.
            harness.assert_closed_with_parented_reply(dispatch)
            sentinel = probe["sentinel"]
            self.assertTrue(sentinel.exists(), f"missing probe sentinel at {sentinel}")
            result = sentinel.read_text(errors="replace")
            for token in probe["success_tokens"]:
                self.assertNotIn(token, result, result)
            self.assertTrue(
                any(signal in result.lower() for signal in probe["denial_tokens"]),
                result,
            )
            self.assertFalse(probe["probe_socket"].exists(), result)
            # The architect's live listener stays intact: the bounded worker
            # could neither connect to nor remove it.
            self.assertTrue(probe["listener_socket"].exists(), result)

    def test_codex_spawn_env_strips_write_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            harness = make_codex_harness(root)
            stack.callback(harness.close)
            result_path = root / "alpha-codex-worker" / "env_result.json"

            with patched_env(
                {
                    "AGENT_COMMS_ADMIN_TOKEN": "operator-secret",
                    "GITHUB_TOKEN": "github-secret",
                    "AWS_SECRET_ACCESS_KEY": "aws-secret",
                }
            ):
                dispatch = harness.dispatch_and_wait(
                    idempotency_key="codex-env-backstop",
                    ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
                    timeout_seconds=POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
                    body=(
                        "Inspect your process environment and write JSON to "
                        f"{result_path} showing whether AGENT_COMMS_ADMIN_TOKEN, "
                        "GITHUB_TOKEN, and AWS_SECRET_ACCESS_KEY are present. "
                        "Then send the normal parented reply and call close_dispatch with "
                        "result=satisfied, that reply id, and delta=true."
                    ),
                )

            harness.assert_closed_with_parented_reply(dispatch)
            self.assertTrue(result_path.exists(), f"missing env result at {result_path}")
            payload = __import__("json").loads(result_path.read_text())
            self.assertFalse(env_present(payload["AGENT_COMMS_ADMIN_TOKEN"]), payload)
            self.assertFalse(env_present(payload["GITHUB_TOKEN"]), payload)
            self.assertFalse(env_present(payload["AWS_SECRET_ACCESS_KEY"]), payload)

    def test_codex_spawn_zdotdir_isolates_operator_rc_secret_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            operator_home = root / "operator-home"
            operator_home.mkdir()
            secret_path = operator_home / "admin-token"
            secret_value = "rc-derived-admin-secret"
            secret_path.write_text(secret_value)
            (operator_home / ".zshenv").write_text(
                f"export RC_DERIVED_SECRET=\"$(cat {shlex.quote(str(secret_path))})\"\n"
            )
            (operator_home / ".zshrc").write_text(
                f"export RC_DERIVED_SECRET=\"$(cat {shlex.quote(str(secret_path))})\"\n"
            )
            harness = make_codex_harness(root)
            stack.callback(harness.close)
            result_path = root / "alpha-codex-worker" / "rc_env_result.json"

            with patched_env({"HOME": str(operator_home), "ZDOTDIR": str(operator_home)}):
                dispatch = harness.dispatch_and_wait(
                    idempotency_key="codex-rc-secret-isolation",
                    ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
                    timeout_seconds=POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
                    body=(
                        "Run a fresh zsh login-style environment check and write JSON to "
                        f"{result_path} with keys rc_secret_present and rc_secret_value. "
                        "The value should come only from os.environ.get('RC_DERIVED_SECRET', '') "
                        "inside that zsh-launched process. Then send the normal parented reply "
                        "and call close_dispatch with result=satisfied, that reply id, and "
                        "delta=true."
                    ),
                )

            harness.assert_closed_with_parented_reply(dispatch)
            self.assertTrue(result_path.exists(), f"missing rc env result at {result_path}")
            payload = __import__("json").loads(result_path.read_text())
            self.assertFalse(env_present(payload["rc_secret_present"]), payload)
            self.assertNotEqual(payload["rc_secret_value"], secret_value, payload)

    def test_hard_ttl_kills_real_codex_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            pid_file = root / "codex-child.pid"
            spawn = long_running_worker_spawn(pid_file)
            spawn["env"] = {"CODEX_HOME": str(root / "codex-home")}
            harness = make_codex_harness(root, child_pid_file=pid_file, spawn=spawn)
            stack.callback(harness.close)
            dispatch = {}
            try:
                dispatch = harness.store.dispatch_agent(
                    "alpha-architect",
                    "alpha-codex-worker",
                    "codex-hard-ttl",
                    "ttl",
                    "Start the long-running task.",
                    [],
                    adapter_for_runtime=lambda _runtime: harness.adapter,
                    ttl_seconds=3,
                )
                self.assertEqual(dispatch["status"], "in_flight")

                from tests.dispatch_cell_harness import wait_until

                wait_until(lambda: pid_file.exists(), timeout_seconds=30)
                child_pid = int(pid_file.read_text().strip())
                self.assertTrue(process_is_live(child_pid))

                def dlq_row() -> dict | None:
                    harness.store.reconcile_dispatches(lambda _runtime: harness.adapter)
                    row = harness.store._dispatch_by_idempotency_key_fresh("alpha-architect", "codex-hard-ttl")
                    return row if row["status"] == "dlq" else None

                row = wait_until(dlq_row, timeout_seconds=60)
                self.assertEqual(row["failure_reason"], "timeout")
                self.assertFalse(process_is_live(child_pid))
            finally:
                if dispatch.get("spawn_handle"):
                    # Authenticated teardown via the settled row's control
                    # identity. A terminal/released row's wrapper is already
                    # gone, so tolerate only that typed already-gone condition;
                    # any other error still surfaces the measured outcome.
                    try:
                        harness.adapter.halt(
                            dispatch["spawn_handle"], dispatch.get("observed_values")
                        )
                    except SupervisorUnreachable:
                        pass


if __name__ == "__main__":
    unittest.main()
