from __future__ import annotations

import contextlib
import json
import hashlib
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from agent_comms.codex_auth_refresh import access_token_remaining_seconds
from agent_comms.codex_auth_refresh import spawn_freshness_margin_seconds
from agent_comms import paths, supervisor
from agent_comms.adapters import DispatchContext, DispatchStart, RuntimeAdapter
from agent_comms.adapters._base import AdapterStatus
from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.adapters.codex import CodexAdapter
from agent_comms.policies import WORKER_DISPATCH_POLICY_VERSION
from agent_comms.runtime_pins import (
    CLAUDE_PINNED_SHA256_ENV,
    CLAUDE_PINNED_VERSION,
    CLAUDE_VERSIONS_DIR_ENV,
    claude_binary_path,
    custody_binary_digest_matches,
)
from agent_comms.spawn import render_spawn
from agent_comms.schema import ValidationError
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

ROOT = Path(__file__).resolve().parents[1]

# Positive-path runtime cell budgets (cycle runtime-cell-latency-budget-002).
# A model-driven worker must discover MCP tools, perform the probe, reply,
# and close within the TTL; 90 seconds tolerates bounded tool discovery and
# one ordinary correction. The terminal wait must exceed TTL plus kill grace
# so the harness observes the settled ledger row instead of racing forced
# termination. The dedicated hard-TTL cells keep their short literal TTLs.
POSITIVE_CELL_TTL_SECONDS = 90
RUNTIME_KILL_GRACE_SECONDS = 30
POSITIVE_CELL_TERMINAL_WAIT_SECONDS = 150
assert (
    POSITIVE_CELL_TERMINAL_WAIT_SECONDS
    > POSITIVE_CELL_TTL_SECONDS + RUNTIME_KILL_GRACE_SECONDS
)

HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
ARCHITECT_ID = "alpha-architect"
FAKE_WORKER_ID = "alpha-fake-worker"
CLAUDE_WORKER_ID = "alpha-claude-worker"
CODEX_WORKER_ID = "alpha-codex-worker"


def provision_worker_git_root(project_root: Path) -> None:
    git_env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Agent Comms Cell Fixture",
        "GIT_AUTHOR_EMAIL": "cell-fixture@agent-comms.invalid",
        "GIT_COMMITTER_NAME": "Agent Comms Cell Fixture",
        "GIT_COMMITTER_EMAIL": "cell-fixture@agent-comms.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    project_root.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=project_root, check=True, env=git_env)
    (project_root / ".cell-fixture-base").write_text("agent-comms cell fixture base\n")
    subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "add", ".cell-fixture-base"],
        cwd=project_root,
        check=True,
        env=git_env,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Agent Comms Cell Fixture",
            "-c",
            "user.email=cell-fixture@agent-comms.invalid",
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "--quiet",
            "--message=Initialize cell fixture",
        ],
        cwd=project_root,
        check=True,
        env=git_env,
    )


def codex_cert_home() -> Path:
    return Path(os.environ.get("AGENT_COMMS_CODEX_CERT_HOME") or "~/.agent-comms/codex-cert-home").expanduser()


def codex_cert_auth_copy_in(cert_home: Path, temp_home: Path) -> str:
    source_auth = cert_home / "auth.json"
    snapshot = source_auth.read_text()
    shutil.copy2(source_auth, temp_home / "auth.json")
    return snapshot


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _auth_last_refresh(content: str) -> tuple[datetime | None, str | None]:
    try:
        value = json.loads(content).get("last_refresh")
    except (AttributeError, json.JSONDecodeError):
        return None, None
    if not value:
        return None, None
    try:
        return datetime.fromisoformat(str(value)), str(value)
    except ValueError:
        return None, str(value)


def codex_cert_auth_copy_back(cert_home: Path, temp_home: Path, snapshot: str) -> None:
    cert_auth = cert_home / "auth.json"
    temp_auth = temp_home / "auth.json"
    try:
        temp_content = temp_auth.read_text()
    except FileNotFoundError:
        print(f"WARNING: codex cert auth copy-back source missing; leaving {cert_auth} untouched: {temp_auth}", file=sys.stderr)
        return
    try:
        json.loads(temp_content)
    except json.JSONDecodeError:
        print(f"WARNING: codex cert auth copy-back rejected invalid JSON from {temp_auth}; leaving {cert_auth} untouched", file=sys.stderr)
        return

    try:
        current_content = cert_auth.read_text()
    except FileNotFoundError:
        print(f"WARNING: codex cert auth file was deleted since copy-in; restoring {cert_auth} from {temp_auth}", file=sys.stderr)
        _atomic_write_text(cert_auth, temp_content)
        return

    if current_content == snapshot:
        if temp_content != current_content:
            _atomic_write_text(cert_auth, temp_content)
        return

    current_refresh, current_raw = _auth_last_refresh(current_content)
    temp_refresh, temp_raw = _auth_last_refresh(temp_content)
    print(
        "WARNING: codex cert auth changed since copy-in; resolving by last_refresh "
        f"for cert={cert_auth} temp={temp_auth} (current={current_raw!r}, temp={temp_raw!r})",
        file=sys.stderr,
    )
    if current_refresh is None and temp_refresh is None:
        return
    if current_refresh is None or (temp_refresh is not None and temp_refresh > current_refresh):
        _atomic_write_text(cert_auth, temp_content)


def wait_until(predicate: Callable[[], object], timeout_seconds: float) -> object:
    deadline = time.monotonic() + timeout_seconds
    last_value: object = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for condition; last value: {last_value!r}")


def fake_worker_args(
    db_path: Path, *, cell_delta: bool = False, fail_before_close: bool = False
) -> list[str]:
    args = [
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT)!r}); "
            "from agent_comms.adapters.fake_worker import main; "
            "raise SystemExit(main())"
        ),
        "--actor-id",
        "{actor_id}",
        "--message-id",
        "{message_id}",
        "--db",
        str(db_path),
        f"WakePolicy={WORKER_DISPATCH_POLICY}",
    ]
    if cell_delta:
        args.append("--cell-delta")
    if fail_before_close:
        args.append("--fail-before-close")
    return args


def long_running_worker_spawn(pid_file: Path, *, claude_versions_dir: Path | None = None) -> dict:
    if claude_versions_dir is not None:
        binary = claude_versions_dir / CLAUDE_PINNED_VERSION
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            f"  printf 'Claude Code {CLAUDE_PINNED_VERSION}\\n'\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s' \"$$\" > \"$AGENT_COMMS_TIMEOUT_CHILD_PID_FILE\"\n"
            "sleep 60\n"
        )
        binary.chmod(0o755)
        return {"command": "{claude_binary}", "args": ["{worker_prompt}"]}

    script = (
        "import os, pathlib, time; "
        "pathlib.Path(os.environ['AGENT_COMMS_TIMEOUT_CHILD_PID_FILE']).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    return {"command": sys.executable, "args": ["-c", script, "{worker_prompt}"]}


def claude_pin_env_for_versions_dir(versions_dir: Path) -> dict[str, str]:
    binary = versions_dir / CLAUDE_PINNED_VERSION
    return {
        CLAUDE_VERSIONS_DIR_ENV: str(versions_dir),
        CLAUDE_PINNED_SHA256_ENV: hashlib.sha256(binary.read_bytes()).hexdigest(),
    }


def claude_tool_events(output_file: Path) -> list[dict]:
    """Return ordered Claude tool-use/result items from a stream-json log."""
    events: list[dict] = []
    if not output_file.exists():
        return events
    for line in output_file.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") in {
                "tool_use",
                "tool_result",
            }:
                events.append(item)
    return events


class TempClaudeAdapter(ClaudeAdapter):
    """Claude adapter routed through the real supervised path.

    Output capture and child-pid capture ride the supervisor's own log and child
    environment (via the ``_worker_log_path`` / ``_extra_child_env`` hooks) rather
    than the removed legacy timeout-wrapper mode, so cell tests exercise the same
    authenticated supervisor that production uses.
    """

    def __init__(self, child_pid_file: Path | None = None, output_file: Path | None = None) -> None:
        super().__init__()
        self.child_pid_file = child_pid_file
        self.output_file = output_file

    def _worker_log_path(self, context):  # type: ignore[no-untyped-def]
        if self.output_file is not None:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            return self.output_file
        return super()._worker_log_path(context)

    def _extra_child_env(self, context):  # type: ignore[no-untyped-def]
        env = dict(super()._extra_child_env(context))
        if self.child_pid_file is not None:
            env["AGENT_COMMS_TIMEOUT_CHILD_PID_FILE"] = str(self.child_pid_file)
        return env


class TempCodexAdapter(CodexAdapter):
    """Codex adapter routed through the real supervised path (see TempClaudeAdapter)."""

    def __init__(self, child_pid_file: Path | None = None) -> None:
        super().__init__()
        self.child_pid_file = child_pid_file

    def _extra_child_env(self, context):  # type: ignore[no-untyped-def]
        env = dict(super()._extra_child_env(context))
        if self.child_pid_file is not None:
            env["AGENT_COMMS_TIMEOUT_CHILD_PID_FILE"] = str(self.child_pid_file)
        return env


class DeterministicFakeCellAdapter:
    """Test adapter that exercises Store start without the unavailable supervisor."""

    def __init__(self, *, cell_delta: bool = False, fail_before_close: bool = False) -> None:
        self.cell_delta = cell_delta
        self.fail_before_close = fail_before_close
        self._processes: dict[str, subprocess.Popen] = {}

    def dispatch(self, context: DispatchContext) -> DispatchStart:
        project_root = Path(str(context.recipient["project_root"]))
        project_root.mkdir(parents=True, exist_ok=True)
        handle = f"cell-fake-{context.dispatch['dispatch_id']}"
        command = [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "time.sleep(0.15); "
                f"sys.path.insert(0, {str(ROOT)!r}); "
                "from agent_comms.adapters.fake_worker import main; "
                "raise SystemExit(main())"
            ),
            "--actor-id",
            str(context.recipient["id"]),
            "--message-id",
            str(context.message["id"]),
            "--db",
            context.db_path,
        ]
        if self.cell_delta:
            command.append("--cell-delta")
        if self.fail_before_close:
            command.append("--fail-before-close")
        self._processes[handle] = subprocess.Popen(command, cwd=project_root)
        return DispatchStart(spawn_handle=handle, observed_values={"cell_fixture": True})

    def status(self, spawn_handle: str, observed_values=None) -> AdapterStatus:  # type: ignore[no-untyped-def]
        returncode = self._processes[spawn_handle].poll()
        if returncode is None:
            return AdapterStatus("running")
        return AdapterStatus("exited", returncode=returncode)

    def halt(self, spawn_handle: str, observed_values=None) -> None:  # type: ignore[no-untyped-def]
        process = self._processes.get(spawn_handle)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


class DispatchCellHarness:
    def __init__(
        self,
        root: Path,
        worker_id: str,
        runtime: str,
        spawn: dict,
        adapter: RuntimeAdapter,
        codex_cert_auth_state: tuple[Path, Path, str] | None = None,
        halt_on_terminal: bool = True,
    ) -> None:
        self.root = root
        self.db_path = root / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.worker_id = worker_id
        self.adapter = adapter
        self._codex_cert_auth_state = codex_cert_auth_state
        self._spawn_handles: set[str] = set()
        self._closed = False
        self.halt_on_terminal = halt_on_terminal
        worker_project_root = root / worker_id
        provision_worker_git_root(worker_project_root)
        self.store.register_actor(HUMAN_ID, "human", "alice")
        self.store.register_agent_actor(ARCHITECT_ID, "alpha", "architect", str(root / ARCHITECT_ID), [])
        self.store.register_agent_actor(
            worker_id,
            "alpha",
            "worker",
            str(worker_project_root),
            [],
            runtime=runtime,
            spawn=spawn,
            owner=ARCHITECT_ID,
        )

    def _halt_wrapper(self, handle: str) -> None:
        # HALT authenticates with the persisted identity, never the handle's PID.
        with self.store.connection() as conn:
            row = conn.execute(
                "select observed_values_json from dispatch_ledger where spawn_handle = ?",
                (handle,),
            ).fetchone()
        observed = json.loads(row[0] or "{}") if row is not None else {}
        self.adapter.halt(handle, observed)

    def wait_for_natural_exit(self, timeout: float) -> None:
        """Wait for native reap and wrapper exit before consuming final events."""
        deadline = time.monotonic() + timeout
        processes = getattr(self.adapter, "_processes", {})
        for handle in self._spawn_handles:
            process = processes.get(handle)
            if process is None:
                raise AssertionError(f"natural exit: missing wrapper for {handle}")
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                try:
                    self._halt_wrapper(handle)
                except Exception as halt_error:
                    raise AssertionError(
                        f"natural exit timed out after {timeout}s; HALT failed: {halt_error}"
                    ) from exc
                raise AssertionError(
                    f"natural exit timed out after {timeout}s"
                ) from exc
            if code != 0:
                raise AssertionError(f"natural exit: nonzero wrapper exit {code}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A terminal ledger row can precede the supervisor wrapper's finally
        # cleanup and the parent reaper's last SQLite merge.  Join every wrapper
        # this fixture spawned, then synchronously reap it, before the enclosing
        # TemporaryDirectory is allowed to remove the DB/root.
        processes = getattr(self.adapter, "_processes", {})
        for spawn_handle in self._spawn_handles:
            process = processes.get(spawn_handle)
            if process is not None:
                if process.poll() is None:
                    self._halt_wrapper(spawn_handle)
                process.wait(timeout=RUNTIME_KILL_GRACE_SECONDS + 5)
        supervisor.reaper_registry().reap_ready()
        if self._codex_cert_auth_state is None:
            return
        cert_home, temp_home, snapshot = self._codex_cert_auth_state
        codex_cert_auth_copy_back(cert_home, temp_home, snapshot)

    def dispatch_and_wait(
        self,
        *,
        idempotency_key: str,
        ttl_seconds: int,
        timeout_seconds: float,
        subject: str = "ping",
        body: str = "Reply with PONG.",
        in_flight_assertion: Callable[[dict], None] | None = None,
    ) -> dict:
        dispatch = self.store.dispatch_agent(
            ARCHITECT_ID,
            self.worker_id,
            idempotency_key,
            subject,
            body,
            [],
            adapter_for_runtime=lambda _runtime: self.adapter,
            ttl_seconds=ttl_seconds,
        )
        if dispatch.get("spawn_handle"):
            self._spawn_handles.add(dispatch["spawn_handle"])
        if dispatch["status"] != "in_flight":
            return dispatch
        if in_flight_assertion is not None:
            in_flight_assertion(dispatch)

        def terminal_row() -> dict | None:
            self.store.reconcile_dispatches(lambda _runtime: self.adapter, human_actor_id=HUMAN_ID)
            row = self.store._dispatch_by_idempotency_key_fresh(ARCHITECT_ID, idempotency_key)
            return row if row["status"] in {"closed", "dlq", "spawn_failed_message_landed"} else None

        terminal = wait_until(terminal_row, timeout_seconds)
        if self.halt_on_terminal and terminal.get("spawn_handle"):
            # Best-effort teardown of any residual wrapper. Pass observed values
            # so it is the authenticated socket HALT; a terminal row's wrapper is
            # usually already gone, so tolerate an unconfirmed/unreachable result.
            try:
                self.adapter.halt(terminal["spawn_handle"], terminal.get("observed_values"))
            except Exception:
                pass
        return terminal

    def file_backed_reply_roundtrip(self, *, runtime: str) -> tuple[dict, bytes]:
        fixture = (
            ROOT / "tests/fixtures/send-message-marker-collision-claude-v1.txt"
        ).read_bytes()
        (self.root / self.worker_id / "collision.txt").write_bytes(fixture)
        dispatch = self.dispatch_and_wait(
            idempotency_key=f"{runtime}-cell-file-backed",
            ttl_seconds=POSITIVE_CELL_TTL_SECONDS,
            timeout_seconds=POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
            body=(
                "First attempt send_message inline with the exact text from collision.txt "
                "as body and this dispatch message as parent_message_id; print whether it "
                "was delivered, refused, or absent. Then send exactly one reply with "
                "body_file='collision.txt' and this dispatch message as parent_message_id, "
                "and close_dispatch satisfied with that file-backed reply id and delta=false."
            ),
        )
        return dispatch, fixture

    def assert_closed_with_parented_reply(self, dispatch: dict) -> None:
        self.assert_dispatch_closed(dispatch)
        triggers = [
            message
            for message in self.store.list_inbox(self.worker_id, unread_only=False, include_closed=True)
            if message["id"] == dispatch["message_id"]
        ]
        replies = [
            message
            for message in self.store.list_inbox(ARCHITECT_ID, unread_only=False, include_closed=True)
            if message["parent_message_id"] == dispatch["message_id"]
        ]
        if len(triggers) != 1:
            raise AssertionError(f"expected one trigger for {dispatch['message_id']}, got {triggers!r}")
        if len(replies) != 1:
            raise AssertionError(f"expected one reply parented to {dispatch['message_id']}, got {replies!r}")
        if triggers[0]["status"] != "closed":
            raise AssertionError(f"trigger was not closed: {triggers[0]!r}")
        if replies[0]["from"] != self.worker_id:
            raise AssertionError(f"reply came from wrong actor: {replies[0]!r}")
        closeout = dispatch.get("observed_values", {}).get("closeout")
        if not isinstance(closeout, dict):
            raise AssertionError(f"v2 dispatch has no closeout evidence: {dispatch!r}")
        if dispatch.get("policy_version") != "v2":
            raise AssertionError(f"positive cell did not exercise policy v2: {dispatch!r}")
        if dispatch.get("result") != "satisfied":
            raise AssertionError(f"positive cell did not record satisfied: {dispatch!r}")
        if closeout.get("protocol") != 1:
            raise AssertionError(f"unexpected closeout protocol: {closeout!r}")
        if closeout.get("reply_message_id") != replies[0]["id"]:
            raise AssertionError(f"closeout did not bind observed reply: {closeout!r}")
        delta = closeout.get("delta")
        if not isinstance(delta, dict):
            raise AssertionError(f"positive cell has no delta snapshot: {closeout!r}")
        if not delta.get("entries"):
            raise AssertionError(f"positive cell delta is empty: {delta!r}")
        if not __import__("re").fullmatch(r"[0-9a-f]{40}", str(delta.get("snapshot_tree", ""))):
            raise AssertionError(f"positive cell has invalid snapshot tree: {delta!r}")
        if not __import__("re").fullmatch(r"[0-9a-f]{64}", str(delta.get("manifest_sha256", ""))):
            raise AssertionError(f"positive cell has invalid manifest digest: {delta!r}")

    def assert_closed_with_file_backed_reply(
        self, dispatch: dict, expected_bytes: bytes
    ) -> tuple[dict, list[dict]]:
        self.assert_dispatch_closed(dispatch)
        triggers = [
            message
            for message in self.store.list_inbox(
                self.worker_id, unread_only=False, include_closed=True
            )
            if message["id"] == dispatch["message_id"]
        ]
        replies = [
            message
            for message in self.store.list_inbox(
                ARCHITECT_ID, unread_only=False, include_closed=True
            )
            if message["parent_message_id"] == dispatch["message_id"]
        ]
        artifact_replies = [
            message for message in replies if message["body_storage"] == "artifact"
        ]
        if len(triggers) != 1:
            raise AssertionError(
                f"expected one trigger for {dispatch['message_id']}, got {triggers!r}"
            )
        if len(artifact_replies) != 1:
            raise AssertionError(
                f"expected one artifact reply parented to {dispatch['message_id']}, got {artifact_replies!r}"
            )
        if triggers[0]["status"] != "closed":
            raise AssertionError(f"trigger was not closed: {triggers[0]!r}")
        if any(reply["from"] != self.worker_id for reply in replies):
            raise AssertionError(f"parented reply came from wrong actor: {replies!r}")
        artifact_reply = artifact_replies[0]
        closeout = dispatch.get("observed_values", {}).get("closeout")
        if not isinstance(closeout, dict):
            raise AssertionError(f"v2 dispatch has no closeout evidence: {dispatch!r}")
        if dispatch.get("policy_version") != "v2":
            raise AssertionError(
                f"positive cell did not exercise policy v2: {dispatch!r}"
            )
        if dispatch.get("result") != "satisfied":
            raise AssertionError(
                f"positive cell did not record satisfied: {dispatch!r}"
            )
        if closeout.get("protocol") != 1:
            raise AssertionError(f"unexpected closeout protocol: {closeout!r}")
        if closeout.get("reply_message_id") != artifact_reply["id"]:
            raise AssertionError(f"closeout did not bind artifact reply: {closeout!r}")
        if closeout.get("delta") is not None:
            raise AssertionError(
                f"file-backed roundtrip recorded a worktree delta: {closeout!r}"
            )
        actual_bytes = self.store.read_message(ARCHITECT_ID, artifact_reply["id"])[
            "body"
        ].encode("utf-8")
        if actual_bytes != expected_bytes:
            raise AssertionError("artifact-backed reply bytes differ from fixture")
        return artifact_reply, [
            reply for reply in replies if reply["id"] != artifact_reply["id"]
        ]

    def assert_v2_close_denials_leave_state_unchanged(self, dispatch: dict) -> None:
        if dispatch.get("status") != "in_flight" or dispatch.get("policy_version") != "v2":
            raise AssertionError(f"denial assertion requires writer-created v2 in-flight row: {dispatch!r}")
        other_actor = f"{self.worker_id}-other"
        self.store.register_agent_actor(
            other_actor, "alpha", "worker", str(self.root / other_actor), [],
            owner=ARCHITECT_ID,
        )

        def state() -> tuple[dict, dict]:
            with self.store.connection() as conn:
                ledger = dict(
                    conn.execute(
                        "select * from dispatch_ledger where dispatch_id=?",
                        (dispatch["dispatch_id"],),
                    ).fetchone()
                )
                recipient = dict(
                    conn.execute(
                        "select * from message_recipients where message_id=? and to_agent=?",
                        (dispatch["message_id"], self.worker_id),
                    ).fetchone()
                )
            return ledger, recipient

        before = state()
        try:
            self.store.close_dispatch(
                other_actor,
                message_id=dispatch["message_id"],
                result="satisfied",
                reply_message_id="missing",
                summary="forged",
            )
        except ValidationError as exc:
            if "wrong_actor" not in str(exc):
                raise
        else:
            raise AssertionError("wrong actor close_dispatch unexpectedly succeeded")
        if state() != before:
            raise AssertionError("wrong actor close_dispatch mutated ledger or recipient copy")

        for legacy_close in (
            lambda: self.store.close_message(self.worker_id, dispatch["message_id"]),
            lambda: self.store.ack_message(self.worker_id, dispatch["message_id"], ""),
        ):
            before = state()
            try:
                legacy_close()
            except ValidationError as exc:
                if "close_dispatch" not in str(exc):
                    raise
            else:
                raise AssertionError("legacy v2 trigger close unexpectedly succeeded")
            if state() != before:
                raise AssertionError("legacy v2 trigger close mutated ledger or recipient copy")

    @staticmethod
    def assert_dispatch_closed(dispatch: dict) -> None:
        if dispatch["status"] != "closed":
            raise AssertionError(
                "dispatch did not close: "
                + json.dumps(
                    {
                        "dispatch_id": dispatch.get("dispatch_id"),
                        "status": dispatch.get("status"),
                        "failure_reason": dispatch.get("failure_reason"),
                        "observed_values": dispatch.get("observed_values"),
                    },
                    sort_keys=True,
                )
            )


def make_fake_harness(
    root: Path, *, cell_delta: bool = False, fail_before_close: bool = False
) -> DispatchCellHarness:
    adapter = DeterministicFakeCellAdapter(
        cell_delta=cell_delta, fail_before_close=fail_before_close
    )
    return DispatchCellHarness(
        root,
        FAKE_WORKER_ID,
        "fake",
        {
            "command": sys.executable,
            "args": fake_worker_args(
                root / "agent-comms.sqlite",
                cell_delta=cell_delta,
                fail_before_close=fail_before_close,
            ),
        },
        adapter,
    )


def claude_logged_in() -> bool:
    claude_binary = claude_binary_path()
    if not custody_binary_digest_matches(claude_binary):
        return False
    result = subprocess.run(
        [str(claude_binary), "auth", "status"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        return False
    try:
        return bool(json.loads(result.stdout).get("loggedIn"))
    except json.JSONDecodeError:
        return False


def make_claude_harness(
    root: Path,
    child_pid_file: Path | None = None,
    output_file: Path | None = None,
    spawn: dict | None = None,
) -> DispatchCellHarness:
    return DispatchCellHarness(
        root,
        CLAUDE_WORKER_ID,
        "claude",
        spawn or render_spawn("claude", CLAUDE_WORKER_ID),
        TempClaudeAdapter(child_pid_file, output_file),
    )


def codex_cell_preflight() -> tuple[bool, str]:
    if shutil.which("codex") is None:
        return False, "codex CLI unavailable"
    codex_home = codex_cert_home()
    recovery = f"; run CODEX_HOME={codex_home} codex login"
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(
            ["codex", "login", "status"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, "codex login-status unavailable" + recovery
    if result.returncode != 0 or "Logged in" not in f"{result.stdout}\n{result.stderr}":
        return False, "codex login-status not logged in" + recovery
    remaining = access_token_remaining_seconds(codex_home / "auth.json")
    if remaining is None:
        return False, "codex access-token expiry missing or malformed" + recovery
    if remaining <= POSITIVE_CELL_TTL_SECONDS + spawn_freshness_margin_seconds():
        return False, "codex access-token life insufficient/stale" + recovery
    return True, "codex certification auth ready"


def make_codex_harness(
    root: Path,
    child_pid_file: Path | None = None,
    spawn: dict | None = None,
) -> DispatchCellHarness:
    codex_home = root / "codex-home"
    codex_home.mkdir(parents=True)
    cert_home = codex_cert_home()
    cert_snapshot = codex_cert_auth_copy_in(cert_home, codex_home)
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                "[mcp_servers.agent-comms]",
                f'command = "{paths.mcp_command()}"',
                f'args = ["--db", "{root / "agent-comms.sqlite"}", "--actor-id", "{CODEX_WORKER_ID}"]',
                'default_tools_approval_mode = "approve"',
                "",
                "[mcp_servers.agent-comms.env]",
                'WAKE_POLICY = "worker_dispatch_readwrite_bounded"',
                f'WAKE_POLICY_VERSION = "{WORKER_DISPATCH_POLICY_VERSION}"',
                f'AGENT_COMMS_ACTOR_ID = "{CODEX_WORKER_ID}"',
                f'AGENT_COMMS_PROJECT_ROOT = "{root / CODEX_WORKER_ID}"',
                "",
            ]
        )
    )
    (codex_home / f"{CODEX_WORKER_ID}.config.toml").write_text(
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "never"',
                f'[projects."{root / CODEX_WORKER_ID}"]',
                'trust_level = "trusted"',
                "",
            ]
        )
    )
    return DispatchCellHarness(
        root,
        CODEX_WORKER_ID,
        "codex",
        spawn or render_spawn("codex", CODEX_WORKER_ID, codex_home=str(codex_home)),
        TempCodexAdapter(child_pid_file),
        codex_cert_auth_state=(cert_home, codex_home, cert_snapshot),
    )


class TempRoot:
    def __enter__(self) -> Path:
        self._temp = tempfile.TemporaryDirectory()
        return Path(self._temp.__enter__())

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self._temp.__exit__(exc_type, exc, tb)


# Reserved strict run-dir names (32 hex chars). A live run token is 16 random
# bytes, so these can never collide with a real supervised run; the spawn-time
# janitor also ignores them because they carry no terminal-owned marker. One is
# the worker's own create/write probe target (must NOT pre-exist so an
# unrestricted create/write would succeed); the other is the architect-owned
# LIVE listener fixture the worker's connect probe targets (must pre-exist and be
# connectable so an unrestricted connect would succeed).
SUPERVISOR_PROBE_RUN_DIR_NAME = "0" * 32
SUPERVISOR_PROBE_LISTENER_DIR_NAME = "1" * 32

# Denial signals a bounded runtime worker's probe should surface instead of a
# success token. Kept broad because the exact OS/sandbox wording differs across
# Seatbelt (macOS), Codex sandbox, and Linux bwrap.
SUPERVISOR_PROBE_DENIAL_TOKENS = (
    "operation not permitted",
    "sandbox",
    "denied",
    "permission",
    "read-only",
    "create_denied",
    "write_denied",
    "connect_denied",
)
SUPERVISOR_PROBE_SUCCESS_TOKENS = ("CREATE_OK", "WRITE_OK", "CONNECT_OK")


def supervisor_root_negative_probe(worker_cwd: Path, protected_root: Path | None = None) -> dict:
    """Build the negative probe against the protected supervisor control root.

    A bounded runtime worker (Codex or Claude) must be OS-sandbox-denied from
    creating, connecting to, or writing anything under the FIXED protected
    control root (``supervisor._DEFAULT_CONTROL_ROOT`` ==
    ``~/.agent-comms/run/s``). That is the real capability boundary.

    ``protected_root`` defaults to that fixed control root, which the pure-shape
    check and the authenticated Codex/Claude certification callers keep passing
    (only the fixed root proves the real OS denial). A runtime-free caller that
    must not touch live production state passes an explicit TEST-OWNED root
    instead; an ambient ``AGENT_COMMS_SUPERVISOR_ROOT`` would not suffice because
    this helper consumes ``_DEFAULT_CONTROL_ROOT`` directly.

    The ``run_token`` the supervisor persists into observed values is a same-run
    correlation / authentication nonce, NOT a same-uid secret and NOT cross-user
    privilege separation: it prevents stale/cross-run confusion, nothing more.
    This probe therefore targets the fixed default root on purpose (that is the
    path the sandbox denies), never a test override, and it does not depend on
    keeping any token secret from the same-uid worker.

    Keeps create, write, and connect on SEPARATE paths so each would succeed if
    unrestricted and each must be denied bounded: create/write target a run dir
    the worker creates, while connect targets an EXISTING architect-owned live
    listener socket (built by ``supervisor_root_negative_fixture``) rather than a
    nonexistent reserved socket -- otherwise an unrestricted connect would also
    fail and the connect arm would be vacuous.

    Returns fixture script bytes and a fixed Bash invocation from worker cwd,
    the in-cwd sentinel the script writes (asserted deterministically, not from
    the prompt echo), and the protected paths checked for disk mutations.
    """
    protected_root = protected_root if protected_root is not None else supervisor._DEFAULT_CONTROL_ROOT
    probe_dir = protected_root / SUPERVISOR_PROBE_RUN_DIR_NAME
    probe_socket = probe_dir / supervisor._SOCKET_NAME
    listener_dir = protected_root / SUPERVISOR_PROBE_LISTENER_DIR_NAME
    listener_socket = listener_dir / supervisor._SOCKET_NAME
    sentinel = worker_cwd / "supervisor-root-probe.txt"
    connect = (
        "python3 -c 'import os,socket; "
        "socket.socket(socket.AF_UNIX).connect(os.environ[\"L\"])' 2>&1 "
        "&& echo CONNECT_OK || echo CONNECT_DENIED"
    )
    script = (
        f"D={shlex.quote(str(probe_dir))}; "
        f"W={shlex.quote(str(probe_socket))}; "
        f"L={shlex.quote(str(listener_socket))}; "
        f"OUT={shlex.quote(str(sentinel))}; export L; "
        "{ "
        "if mkdir -p \"$D\" 2>&1; then echo CREATE_OK; else echo CREATE_DENIED; fi; "
        "if printf SHOULD_NOT_EXIST > \"$W\" 2>&1; then echo WRITE_OK; else echo WRITE_DENIED; fi; "
        f"{connect}; "
        "} > \"$OUT\" 2>&1 || true"
    )
    return {
        "command": "/bin/bash supervisor-root-probe.sh",
        "script_path": worker_cwd / "supervisor-root-probe.sh",
        "script_bytes": script.encode("utf-8"),
        "sentinel": sentinel,
        "protected_root": protected_root,
        "probe_dir": probe_dir,
        "probe_socket": probe_socket,
        "listener_dir": listener_dir,
        "listener_socket": listener_socket,
        "denial_tokens": SUPERVISOR_PROBE_DENIAL_TOKENS,
        "success_tokens": SUPERVISOR_PROBE_SUCCESS_TOKENS,
    }


@contextlib.contextmanager
def supervisor_root_negative_fixture(worker_cwd: Path, protected_root: Path | None = None) -> Iterator[dict]:
    """Architect-owned LIVE AF_UNIX listener under the protected root.

    Builds a real, connectable listener socket under ``protected_root`` (default
    ``supervisor._DEFAULT_CONTROL_ROOT``) BEFORE the bounded worker probes it, so
    the worker's CONNECT probe targets an EXISTING live socket. From the
    architect (unsandboxed) context the connect succeeds; the OS sandbox denies
    it for a bounded runtime worker -- that denial is the capability boundary.
    The authenticated Codex/Claude certification callers pass the fixed default
    root explicitly because only that separately authorized capability test
    proves the real denial. A runtime-free liveness caller passes a TEST-OWNED
    root so it binds/creates nothing under live production state. Cleans ONLY the
    paths it owns (script, sentinel, listener dir, and create/write probe dir),
    never the shared protected root.
    """
    probe = supervisor_root_negative_probe(worker_cwd, protected_root)
    protected_root = probe["protected_root"]
    listener_dir = probe["listener_dir"]
    listener_socket = probe["listener_socket"]
    protected_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    listener_dir.mkdir(mode=0o700, exist_ok=True)
    # Prove the fixed-root socket path fits the AF_UNIX byte ceiling before bind.
    supervisor.assert_sun_path_ok(listener_socket)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe["script_path"].write_bytes(probe["script_bytes"])
        if listener_socket.exists():
            listener_socket.unlink()
        listener.bind(str(listener_socket))
        listener.listen(8)
        yield probe
    finally:
        listener.close()
        probe["script_path"].unlink(missing_ok=True)
        probe["sentinel"].unlink(missing_ok=True)
        shutil.rmtree(listener_dir, ignore_errors=True)
        shutil.rmtree(probe["probe_dir"], ignore_errors=True)


def operator_env(root: Path, token: str = "operator-secret") -> dict[str, str]:
    home = root / "home"
    secret_dir = home / ".agent-comms"
    secret_dir.mkdir(parents=True, exist_ok=True)
    token_file = secret_dir / "admin-token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["AGENT_COMMS_ADMIN_TOKEN"] = token
    env["PYTHONPYCACHEPREFIX"] = "/private/tmp/agent-comms-pycache"
    return env
