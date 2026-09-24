"""Public-boundary waterfall acceptance for the dead-worker terminalization
aggregate.

This module is a test-only acceptance layer whose job is to raise confidence for
an operator-directed waterfall merge of the accumulated stage-2 sanctioned
cancellation work. It exercises the HIGHEST-VALUE cross-component wiring that the
fragmented per-aspect unit tests can miss: it drives real public surfaces (the
scoped ``cancel_dispatch`` MCP stdio server, the credentialed admin CLI as a real
subprocess, and the public ``request_cancellation`` / ``reconcile_dispatches``
store seams) and JOINS their durable SQLite mutations to the single canonical
``project_dispatch_transport`` outcome and to the janitor evidence gate. It also
proves durability across a real Store reopen, and binds every reached public
outcome to the declared T1-T10 contract vocabulary.

It deliberately does NOT re-clone the granular per-surface assertions those
modules already own; each class instead adds the joining, cross-component
assertion and names the existing module that supplies the remaining leg:

* Item 1 (producer MCP stdio -> SQLite -> projection): the exact identity-free
  schema, worker/operator-mailbox denial, and notice-free detail are owned by
  ``tests/substrate/test_cancellation_mcp_surface.py``. This module adds the
  projection join (a real stdio cancellation projects ``confirmed_cancel``) and
  the no-spawn/no-socket terminalization evidence.
* Item 2 (credentialed admin CLI subprocess -> cancel + settlement): the exact
  refusal/idempotency/replay/forgery matrix is owned by
  ``tests/substrate/test_cancellation_cli_admin.py`` and
  ``tests/substrate/test_settlement_cli.py`` (both drive ``cli.run`` in process).
  This module adds the REAL subprocess boundary with real ``$HOME`` admin-token
  plumbing, joined to the projection, the single producer notice, and the
  proof that settlement deletes no supervisor artifact and claims no death.
* Item 3 (monitor/supervisor terminalization): the confirm/hold/escalate/TTL/
  promotion legs are owned by ``tests/substrate/test_cancellation_monitor.py``,
  the same-run-exit proof by ``tests/substrate/test_cancellation_inflight.py``,
  and the cleanup ordering by ``tests/substrate/test_supervisor.py`` /
  ``tests/substrate/test_cancellation_janitor_gate.py``. This module joins
  confirmation -> projection -> FIFO promotion -> cleanup-evidence gate in
  cohesive scenarios and proves foreign/incomplete proof is fail-closed.
* Item 4 (restart durability): the residue-batch restart is owned by
  ``tests/substrate/test_cancellation_monitor.py``. This module proves a PENDING
  cancellation survives a real Store reopen and converges only on complete exact
  proof, while incomplete/foreign proof stays fail-closed.
* Item 5 (contract matrix): the static contract/doc pins are owned by
  ``tests/substrate/test_dead_worker_contract.py``. This module ties the LIVE
  public outcomes reached above to the declared T1-T10 invariants.

The authenticated live-worker legs of T9 (a real Codex/Claude cell cancelling a
live pre-TTL worker) are runtime cells the architect runs separately
(``tests.cells.test_cell_codex`` / ``tests.cells.test_cell_claude``); this
module asserts only their deterministic, runtime-free public analog (a bounded
worker cannot discover or call ``cancel_dispatch``).

Everything here is deterministic and bounded: no fixed success sleeps, no live
runtime, and no production mutation. Mocks are not used to replace any
mutation/projection/authorization path under test; the two runtime doubles below
only stand in for a runtime adapter's HALT outcome, exactly as the existing
cancellation unit tests do.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_comms import schema
from agent_comms.dispatch_ledger import (
    CANCELLATION_AUTHORITIES,
    CANCELLATION_REASON_MAX,
    DISPATCH_TERMINAL_STATUSES,
    SETTLEMENT_FAILURE_REASON,
    SETTLEMENT_KEY,
    SETTLEMENT_TERMINATION_RESULT,
    project_dispatch_transport,
)
from agent_comms.hooks import pre_tool_use
from agent_comms.policies import OPERATOR_MAILBOX_POLICY, compile_policy
from agent_comms.store import WORKER_DISPATCH_POLICY, Store
from agent_comms.supervisor import complete_reaper_proof, confirmed_termination_evidence

ROOT = Path(__file__).resolve().parents[2]
HUMAN_ID = "01M36YTJV9XBW95S6ZWV47C4RG"
OTHER_HUMAN_ID = "01J00000000000000000000002"
REAPED_AT = "2026-07-26T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Runtime adapter doubles (identical in spirit to the cancellation unit tests):
# they only control a HALT outcome, never the mutation/projection/auth path.
# --------------------------------------------------------------------------- #
class StubAdapter:
    """A stub runtime adapter whose authenticated HALT confirms termination."""

    def __init__(self, run_token: str = "run-token-acceptance-0001") -> None:
        self.control_socket = "/nonexistent/agent-comms/run/s/control.sock"
        self.run_token = run_token
        self.halt_calls: list = []

    def dispatch(self, context):  # noqa: ANN001 - test double
        from agent_comms.adapters import DispatchStart

        return DispatchStart(
            spawn_handle=f"stub:{context.recipient['id']}:1",
            observed_values={
                "adapter": "stub",
                "control_socket": self.control_socket,
                "run_token": self.run_token,
                "protocol_version": 1,
                "worker_log": "/tmp/worker.log",
            },
        )

    def status(self, spawn_handle, observed_values=None):  # noqa: ANN001 - test double
        from collections import namedtuple

        return namedtuple("S", ["state", "detail"])("running", "d")

    def halt(self, spawn_handle, observed_values=None) -> None:  # noqa: ANN001 - test double
        self.halt_calls.append((spawn_handle, observed_values))


class UnconfirmedAdapter(StubAdapter):
    """A HALT that raises: termination is never confirmed, so the row is held."""

    def halt(self, spawn_handle, observed_values=None) -> None:  # noqa: ANN001 - test double
        self.halt_calls.append((spawn_handle, observed_values))
        raise RuntimeError("authenticated halt did not confirm termination")


class ExplodingHaltAdapter(StubAdapter):
    """A HALT that must never run: proves the same-run exit path skips HALT."""

    def halt(self, spawn_handle, observed_values=None) -> None:  # noqa: ANN001 - test double
        raise AssertionError("HALT must not be called when same-run exit already confirms")


def _complete_proof(run_token: str, *, returncode: int = 0, source: str = "halt_finalize") -> dict:
    """The exact version-1 COMPLETE ``reaper_exit`` proof for ``run_token``.

    Byte-shaped to satisfy :func:`complete_reaper_proof`; the helpers below mutate
    a copy of it to produce the fail-closed negatives.
    """
    return {
        "proof_version": 1,
        "run_token": run_token,
        "returncode": returncode,
        "source": source,
        "reaped_at": REAPED_AT,
        "registered_wrapper_reaped": True,
        "native_process_group_drained": True,
        "owned_artifacts_absent": {
            "run_dir": True,
            "control_socket": True,
            "zdotdir_parent": True,
        },
    }


def _seed(store: Store, root: Path) -> None:
    store.register_actor(HUMAN_ID, "human", "alice")
    store.register_agent_actor("arch", "alpha", "architect", str(root / "arch"), [])
    store.register_agent_actor(
        "wrk", "alpha", "worker", str(root / "wrk"), [], runtime="stub", spawn={"command": "stub"},
        owner="arch",
    )


def _ledger_row(store: Store, dispatch_id: str):
    with store._db.connection() as conn:
        return conn.execute(
            "select * from dispatch_ledger where dispatch_id = ?", (dispatch_id,)
        ).fetchone()


def _observed(store: Store, dispatch_id: str) -> dict:
    return json.loads(_ledger_row(store, dispatch_id)["observed_values_json"] or "{}")


def _transport_status(store: Store, message_id: str, recipient: str) -> str | None:
    with store._db.connection() as conn:
        row = conn.execute(
            "select status from message_recipients where message_id = ? and to_agent = ?",
            (message_id, recipient),
        ).fetchone()
    return None if row is None else row["status"]


def _joined_outcome(store: Store, dispatch_id: str) -> dict:
    """The canonical joined projection over the REAL committed ledger+transport."""
    row = _ledger_row(store, dispatch_id)
    transport = _transport_status(store, row["message_id"], row["recipient_actor_id"])
    return project_dispatch_transport(row["status"], transport)


def _merge_observed(store: Store, dispatch_id: str, extra: dict) -> None:
    with store._db.connection() as conn:
        row = conn.execute(
            "select observed_values_json from dispatch_ledger where dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        observed = json.loads(row["observed_values_json"] or "{}")
        observed.update(extra)
        conn.execute(
            "update dispatch_ledger set observed_values_json = ? where dispatch_id = ?",
            (json.dumps(observed, sort_keys=True), dispatch_id),
        )


def _stamp_lineage(store: Store, dispatch_id: str) -> None:
    # The stub runtime does not engage the codex-only auth-lineage claim; an
    # in_flight row's single-lineage HOLD semantics assume the marker is stamped.
    with store._db.connection() as conn:
        conn.execute(
            "update dispatch_ledger set auth_lineage_claimed_at = ? where dispatch_id = ?",
            (REAPED_AT, dispatch_id),
        )


class _StoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        _seed(self.store, self.tmp)
        self.addCleanup(self._tmp.cleanup)

    def _queued(self, key: str, producer: str = "arch") -> dict:
        return self.store.dispatch_agent(producer, "wrk", key, f"S {key}", f"B {key}", [])

    def _in_flight(self, adapter, key: str = "live") -> dict:
        self.store.dispatch_agent("arch", "wrk", key, f"S {key}", f"B {key}", [])
        started = self.store.start_queued_dispatches(lambda _r: adapter, ttl_seconds=3600)[0]
        assert started["status"] == "in_flight", started
        _stamp_lineage(self.store, started["dispatch_id"])
        return started


# --------------------------------------------------------------------------- #
# Item 1: producer public MCP stdio path -> real temp SQLite -> joined projection
# --------------------------------------------------------------------------- #
class ProducerMcpStdioProjectionJoinTest(_StoreBase):
    """A real ``python -m agent_comms.mcp_server`` producer cancellation, joined
    to the canonical projection.

    ``tests/substrate/test_cancellation_mcp_surface.py`` owns the exact
    identity-free ``cancel_dispatch`` schema, the worker/operator-mailbox denial,
    the idempotent-repeat result fields, and the notice-free detail. This test
    adds the cross-component leg those assertions do not reach: the durable
    SQLite mutation a stdio producer cancellation commits projects, through the
    single canonical ``project_dispatch_transport``, to ``confirmed_cancel``, and
    the row terminalizes with NO spawn or socket activity.
    """

    def _base_env(self) -> dict:
        env = os.environ.copy()
        env.pop("WAKE_POLICY", None)
        env.pop("WAKE_POLICY_VERSION", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _mcp_cancel(self, dispatch_id: str, reason: str, call_id: int = 3) -> dict:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "acceptance", "version": "0.1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {
                    "name": "cancel_dispatch",
                    "arguments": {"dispatch_id": dispatch_id, "reason": reason},
                },
            },
        ]
        payload = "\n".join(json.dumps(message) for message in messages) + "\n"
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.mcp_server", "--db", str(self.db_path), "--actor-id", "arch"],
            cwd=ROOT,
            env=self._base_env(),
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        response = next(r for r in responses if r.get("id") == call_id)
        self.assertFalse(response["result"].get("isError", False), response)
        text = "\n".join(item.get("text", "") for item in response["result"]["content"])
        return json.loads(text)

    def _message_count(self) -> int:
        with self.store._db.connection() as conn:
            return conn.execute("select count(*) as c from messages").fetchone()["c"]

    def test_real_stdio_producer_cancel_projects_confirmed_cancel_without_spawn(self) -> None:
        d = self._queued("mcp-accept")
        before_msgs = self._message_count()

        result = self._mcp_cancel(d["dispatch_id"], "obsolete; corrected brief ready")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancellation_state"], "confirmed")
        self.assertEqual(result["authority"], "producer")
        self.assertEqual(result["termination_result"], "not_started")

        # JOIN: the durable ledger+transport mutation projects to the single
        # sanctioned confirmed-cancel outcome (not folded into an ordinary close).
        row = _ledger_row(self.store, d["dispatch_id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(
            _transport_status(self.store, row["message_id"], row["recipient_actor_id"]),
            "cancelled",
        )
        self.assertEqual(_joined_outcome(self.store, d["dispatch_id"])["outcome"], "confirmed_cancel")

        # Terminalized with NO spawn/socket activity: an unclaimed queued row was
        # never selected for spawn, so no lineage claim, no spawn handle, and no
        # adapter-owned run-token / control-socket observed values exist.
        self.assertIsNone(row["auth_lineage_claimed_at"])
        self.assertIsNone(row["spawn_handle"])
        observed = _observed(self.store, d["dispatch_id"])
        self.assertNotIn("run_token", observed)
        self.assertNotIn("control_socket", observed)
        # No control socket or run directory artifact was materialized anywhere.
        self.assertEqual(list(self.tmp.rglob("*.sock")), [])

        # Producer self-cancellation is notice-free: no message row was added.
        self.assertEqual(self._message_count(), before_msgs)
        self.assertEqual(self.store.list_inbox("arch", unread_only=False), [])

    def test_real_stdio_replay_is_idempotent_at_the_projection(self) -> None:
        d = self._queued("mcp-accept-idem")
        first = self._mcp_cancel(d["dispatch_id"], "drop", call_id=3)
        cancelled_at = _ledger_row(self.store, d["dispatch_id"])["cancelled_at"]
        self.assertEqual(_joined_outcome(self.store, d["dispatch_id"])["outcome"], "confirmed_cancel")

        second = self._mcp_cancel(d["dispatch_id"], "drop", call_id=4)
        # Same terminal winner; the projection is unchanged and nothing churned.
        self.assertEqual(first["status"], second["status"], "cancelled")
        self.assertEqual(second["cancellation_state"], "confirmed")
        self.assertEqual(second["termination_result"], "not_started")
        self.assertEqual(_ledger_row(self.store, d["dispatch_id"])["cancelled_at"], cancelled_at)
        self.assertEqual(_joined_outcome(self.store, d["dispatch_id"])["outcome"], "confirmed_cancel")


# --------------------------------------------------------------------------- #
# Item 2: credentialed admin public CLI subprocess path -> cancel + settlement
# --------------------------------------------------------------------------- #
class AdminCliSubprocessSettlementJoinTest(_StoreBase):
    """The credentialed admin CLI driven as a REAL subprocess against real temp
    DB + ``$HOME`` admin-token plumbing.

    ``tests/substrate/test_cancellation_cli_admin.py`` and
    ``tests/substrate/test_settlement_cli.py`` own the full in-process
    ``cli.run`` refusal / idempotency / tamper / forgery / drift / replay matrix.
    This test adds the actual installed-CLI subprocess boundary: the credential
    is resolved from a real mode-600 ``$HOME/.agent-comms/admin-token`` file, and
    the executed outcomes are joined to the canonical projection, the single
    producer notice, and the proof that settlement deletes no supervisor artifact
    and never claims native-process death.
    """

    VALID_TOKEN = "operator-secret-acceptance"

    def setUp(self) -> None:
        super().setUp()
        self.store.register_actor(OTHER_HUMAN_ID, "human", "pat")
        # A private temp $HOME so the CLI subprocess resolves ITS admin-token file
        # (never the operator's real ~/.agent-comms/admin-token).
        self.home = self.tmp / "home"
        (self.home / ".agent-comms").mkdir(parents=True)
        self.token_path = self.home / ".agent-comms" / "admin-token"
        self.token_path.write_text(self.VALID_TOKEN)
        os.chmod(self.token_path, 0o600)

    def _cli(self, argv: list[str], *, token: str | None = "valid") -> tuple[int, dict, str]:
        env = os.environ.copy()
        env.pop("WAKE_POLICY", None)
        env.pop("WAKE_POLICY_VERSION", None)
        env["HOME"] = str(self.home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if token == "valid":
            env["AGENT_COMMS_ADMIN_TOKEN"] = self.VALID_TOKEN
        elif token is None:
            env.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        else:
            env["AGENT_COMMS_ADMIN_TOKEN"] = token
        result = subprocess.run(
            [sys.executable, "-m", "agent_comms.cli", "--db", str(self.db_path), *argv],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        text = result.stdout.strip()
        payload = json.loads(text) if text.startswith("{") else {}
        return result.returncode, payload, text

    def _admin_cancel(self, dispatch_id: str, reason: str, *, actor: str = HUMAN_ID, token="valid"):
        return self._cli(
            [
                "admin", "cancel-dispatch",
                "--from-actor-id", actor,
                "--dispatch-id", dispatch_id,
                "--reason", reason,
            ],
            token=token,
        )

    def _settle_dry_run(self, dispatch_id: str, reason: str, *, actor: str = HUMAN_ID, token="valid"):
        return self._cli(
            [
                "admin", "settle-dispatch",
                "--from-actor-id", actor,
                "--dispatch-id", dispatch_id,
                "--reason", reason,
                "--dry-run",
            ],
            token=token,
        )

    def _settle_execute(self, dispatch_id: str, plan: str, *, actor: str = HUMAN_ID, token="valid"):
        return self._cli(
            [
                "admin", "settle-dispatch",
                "--from-actor-id", actor,
                "--dispatch-id", dispatch_id,
                "--execute-plan", plan,
                "--release-with-termination-unconfirmed",
            ],
            token=token,
        )

    def _producer_notices(self):
        with self.store._db.connection() as conn:
            rows = conn.execute(
                """
                select m.id, m.subject, m.body, m.priority, m.from_agent
                from messages m
                join message_recipients r on r.message_id = m.id
                where r.to_agent = 'arch'
                order by m.created_at, m.id
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def _make_pending_inflight(self, key: str) -> str:
        """An in_flight row carrying a pending (unconfirmed) admin cancellation."""
        started = self._in_flight(StubAdapter(run_token=f"rt-{key}"), key=key)
        result = self.store.request_cancellation(
            started["dispatch_id"], requesting_actor_id=HUMAN_ID, reason=f"cancel {key}", authority="admin"
        )
        self.assertEqual(result["cancellation_state"], "requested")
        return started["dispatch_id"]

    def test_subprocess_admin_cancel_queued_projects_confirmed_cancel_one_notice(self) -> None:
        d = self._queued("cli-cancel")
        rc, payload, text = self._admin_cancel(d["dispatch_id"], "operator withdrew obsolete dispatch")
        self.assertEqual(rc, 0, text)
        self.assertEqual(payload["status"], "cancelled")
        self.assertEqual(payload["authority"], "admin")
        self.assertNotIn(self.VALID_TOKEN, text)

        # JOIN: real subprocess mutation projects to confirmed_cancel.
        self.assertEqual(_joined_outcome(self.store, d["dispatch_id"])["outcome"], "confirmed_cancel")
        # Exactly one admin notice to the producer; a confirmed cancel is never a DLQ.
        notices = self._producer_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["from_agent"], HUMAN_ID)
        self.assertNotIn("dlq", notices[0]["body"].lower())
        self.assertNotIn("ledger released; termination not confirmed", notices[0]["body"])

    def test_subprocess_missing_credential_refuses_before_mutation(self) -> None:
        d = self._queued("cli-nocred")
        rc, payload, _ = self._admin_cancel(d["dispatch_id"], "withdraw", token=None)
        self.assertEqual(rc, 2)
        self.assertFalse(payload.get("ok", True))
        # The ledger is untouched by a credential-refused subprocess request.
        self.assertEqual(_ledger_row(self.store, d["dispatch_id"])["status"], "queued")
        self.assertNotIn("cancellation", _observed(self.store, d["dispatch_id"]))

    def test_subprocess_dry_run_is_pure_then_execute_settles_to_dlq_cancelled(self) -> None:
        did = self._make_pending_inflight("cli-settle")

        # An unrelated supervisor artifact: settlement must NOT delete it.
        artifact_dir = self.tmp / "run" / did
        artifact_dir.mkdir(parents=True)
        artifact = artifact_dir / "control.sock"
        artifact.write_text("owned-by-supervisor")

        before_row = dict(_ledger_row(self.store, did))
        before_observed = _observed(self.store, did)

        # DRY RUN is pure: it commits nothing to the application ledger.
        rc, preview, text = self._settle_dry_run(did, "stuck cancellation; release lineage")
        self.assertEqual(rc, 0, text)
        self.assertIn("plan", preview)
        self.assertNotIn(self.VALID_TOKEN, text)
        after_row = dict(_ledger_row(self.store, did))
        self.assertEqual(after_row["status"], before_row["status"])
        self.assertEqual(after_row["status"], "in_flight")
        self.assertEqual(_observed(self.store, did), before_observed)

        # EXECUTE: the exceptional winner is ledger dlq + transport cancelled with
        # the exact normalized outcome.
        rc, result, text = self._settle_execute(did, preview["plan"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(result["status"], "dlq")
        self.assertEqual(result["outcome"], "operator_settled_termination_unconfirmed")
        self.assertEqual(result["failure_reason"], SETTLEMENT_FAILURE_REASON)
        self.assertEqual(result["termination_result"], SETTLEMENT_TERMINATION_RESULT)
        self.assertTrue(result["lineage_released"])
        self.assertNotIn(self.VALID_TOKEN, text)

        row = _ledger_row(self.store, did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(row["auth_lineage_claimed_at"], REAPED_AT)
        # JOIN: the joined projection is the distinct operator-settlement outcome,
        # never a plain dlq and never confirmed_cancel.
        self.assertEqual(
            _joined_outcome(self.store, did)["outcome"], "operator_settled_termination_unconfirmed"
        )
        # The cancellation itself is NEVER marked confirmed by a settlement.
        self.assertEqual(_observed(self.store, did)["cancellation"]["state"], "requested")

        # Exactly one producer settlement notice with the truthful unconfirmed
        # phrase, blocker priority, and NO native-death claim. (The earlier
        # admin-cancel pending notice never carries the reserved phrase.)
        notices = [
            n for n in self._producer_notices()
            if "ledger released; termination not confirmed" in n["body"]
        ]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["priority"], "blocker")
        body = notices[0]["body"]
        for death_claim in ("process killed", "native child died", "pid ", "sigkill", "was terminated"):
            self.assertNotIn(death_claim, body.lower())

        # Settlement deleted NO supervisor artifact.
        self.assertTrue(artifact.exists())
        self.assertEqual(artifact.read_text(), "owned-by-supervisor")


# --------------------------------------------------------------------------- #
# Item 3: public monitor/supervisor terminalization integration
# --------------------------------------------------------------------------- #
class MonitorSupervisorTerminalizationJoinTest(_StoreBase):
    """The public ``request_cancellation`` / ``reconcile_dispatches`` seams, joined
    to the projection, the janitor evidence gate, and FIFO promotion.

    ``tests/substrate/test_cancellation_monitor.py`` owns the retry/hold/escalate/
    TTL/promotion legs; ``tests/substrate/test_cancellation_inflight.py`` owns the
    same-run-exit proof; ``tests/substrate/test_cancellation_janitor_gate.py`` and
    ``tests/substrate/test_supervisor.py`` own the cleanup-ordering barriers. This
    test joins them: a valid exact same-run proof yields exactly one correctly
    bound terminal cancellation whose cleanup evidence is a precondition (so the
    janitor gate then authorizes cleanup), foreign/incomplete proof is
    fail-closed with cap/lineage retained, and FIFO promotion happens only after
    a confirmed cancel.
    """

    def test_valid_exact_same_run_proof_terminalizes_once_and_authorizes_cleanup(self) -> None:
        adapter = ExplodingHaltAdapter(run_token="run-token-item3-valid")
        started = self._in_flight(adapter, key="valid")
        did = started["dispatch_id"]
        # The complete same-run proof (its owned_artifacts_absent booleans ARE the
        # durable cleanup evidence) is present before the terminal request.
        proof = _complete_proof(adapter.run_token)
        _merge_observed(self.store, did, {"reaper_exit": proof})

        result = self.store.request_cancellation(
            did, requesting_actor_id="arch", reason="already exited", authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        # Exactly one correctly bound terminal cancellation, via same-run exit (the
        # ExplodingHaltAdapter proves NO HALT was attempted).
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["termination_result"], "same_run_exit_confirmed")
        row = _ledger_row(self.store, did)
        # Terminalization preserves the legacy inert lineage timestamp column.
        self.assertEqual(row["auth_lineage_claimed_at"], REAPED_AT)
        self.assertEqual(_joined_outcome(self.store, did)["outcome"], "confirmed_cancel")

        # The cleanup evidence gate now authorizes janitor cleanup for the EXACT
        # run token; terminal status alone never does (verified below).
        self.assertIsNotNone(
            confirmed_termination_evidence(_observed(self.store, did), adapter.run_token)
        )

        # Idempotent replay is a stable winner: same terminal, no churn.
        cancelled_at = row["cancelled_at"]
        replay = self.store.request_cancellation(
            did, requesting_actor_id="arch", reason="already exited", authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        self.assertEqual(replay["status"], "cancelled")
        self.assertEqual(_ledger_row(self.store, did)["cancelled_at"], cancelled_at)

    def test_terminal_status_alone_is_not_janitor_authority(self) -> None:
        # A cancelled row whose proof carries a FALSE cleanup boolean is terminal
        # but NOT cleanup-authorized: the gate reads positive evidence, not status.
        run_token = "run-token-item3-nocleanup"
        incomplete = _complete_proof(run_token)
        incomplete["owned_artifacts_absent"]["run_dir"] = False
        observed = {"run_token": run_token, "reaper_exit": incomplete, "termination_result": "supervised_halt_confirmed"}
        self.assertIsNone(complete_reaper_proof(incomplete, run_token))
        self.assertIsNone(confirmed_termination_evidence(observed, run_token))
        # A wrong-token complete proof likewise never authorizes deletion.
        cross = {"run_token": run_token, "reaper_exit": _complete_proof("run-token-OTHER")}
        self.assertIsNone(confirmed_termination_evidence(cross, run_token))

    def test_foreign_or_incomplete_proof_is_fail_closed_with_cap_and_lineage_held(self) -> None:
        # Every case below leaves a HELD in_flight row (fail-closed), so raise the
        # producer cap enough to hold all of them concurrently.
        with self.store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = 20 where id = 'arch'")
        run_token = "run-token-item3-failclosed"
        base = _complete_proof(run_token)
        wrong_token = _complete_proof("run-token-STALE")
        missing_key = {k: v for k, v in base.items() if k != "native_process_group_drained"}
        false_bool = _complete_proof(run_token)
        false_bool["native_process_group_drained"] = False
        bool_version = _complete_proof(run_token)
        bool_version["proof_version"] = True
        worker_exit_only = None  # no reaper_exit at all; a bare worker_exit is insufficient

        cases = {
            "wrong_token": {"reaper_exit": wrong_token},
            "missing_key": {"reaper_exit": missing_key},
            "false_cleanup_bool": {"reaper_exit": false_bool},
            "bool_proof_version": {"reaper_exit": bool_version},
            # A bare worker_exit is child-exit evidence only; it never qualifies.
            "worker_exit_only": {"worker_exit": {"returncode": 0, "run_token": run_token}},
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                adapter = UnconfirmedAdapter(run_token=run_token)
                started = self._in_flight(adapter, key=f"fc-{name}")
                did = started["dispatch_id"]
                _merge_observed(self.store, did, extra)
                claimed_before = _ledger_row(self.store, did)["auth_lineage_claimed_at"]

                result = self.store.request_cancellation(
                    did, requesting_actor_id="arch", reason="withdraw", authority="producer",
                    adapter_for_runtime=lambda _r: adapter,
                )
                # Zero terminal cancellations: the row is held nonterminal with
                # cap/lineage retained, and it never projects confirmed_cancel.
                self.assertEqual(result["status"], "in_flight")
                self.assertEqual(result["cancellation_state"], "requested")
                row = _ledger_row(self.store, did)
                self.assertEqual(row["status"], "in_flight")
                self.assertEqual(row["auth_lineage_claimed_at"], claimed_before)
                self.assertIsNotNone(row["auth_lineage_claimed_at"])
                self.assertEqual(_joined_outcome(self.store, did)["outcome"], "in_flight")
                self.assertTrue(adapter.halt_calls)  # HALT was attempted and refused

    def test_pending_cancel_does_not_promote_a_queued_successor(self) -> None:
        # cap=1: while the pending cancel is UNCONFIRMED, the cap stays held and
        # the FIFO successor is NOT promoted.
        with self.store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = 1 where id = 'arch'")
        held_adapter = UnconfirmedAdapter(run_token="run-token-item3-held")
        first = self._in_flight(held_adapter, key="held-a")
        self.store.dispatch_agent("arch", "wrk", "held-b", "S", "B", [])
        successor = self.store._dispatch_by_idempotency_key_fresh("arch", "held-b")
        self.store.request_cancellation(
            first["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.store.reconcile_dispatches(lambda _r: held_adapter, human_actor_id=HUMAN_ID)
        self.assertEqual(_ledger_row(self.store, first["dispatch_id"])["status"], "in_flight")
        self.assertEqual(_ledger_row(self.store, successor["dispatch_id"])["status"], "queued")

    def test_confirmed_cancel_releases_cap_and_promotes_successor_same_pass(self) -> None:
        # cap=1: a confirmed cancel releases the cap and promotes the queued
        # successor in the SAME reconcile pass -- promotion strictly AFTER the
        # terminal cancel.
        with self.store._db.connection() as conn:
            conn.execute("update actors set dispatch_cap = 1 where id = 'arch'")
        confirm_adapter = StubAdapter(run_token="run-token-item3-promote")
        first = self._in_flight(confirm_adapter, key="promote-a")
        self.store.dispatch_agent("arch", "wrk", "promote-b", "S", "B", [])
        successor = self.store._dispatch_by_idempotency_key_fresh("arch", "promote-b")
        self.store.request_cancellation(
            first["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.store.reconcile_dispatches(lambda _r: confirm_adapter, human_actor_id=HUMAN_ID)
        self.assertEqual(_ledger_row(self.store, first["dispatch_id"])["status"], "cancelled")
        self.assertEqual(
            _joined_outcome(self.store, first["dispatch_id"])["outcome"], "confirmed_cancel"
        )
        self.assertEqual(_ledger_row(self.store, successor["dispatch_id"])["status"], "in_flight")


# --------------------------------------------------------------------------- #
# Item 4: restart durability and convergence
# --------------------------------------------------------------------------- #
class RestartDurabilityConvergenceTest(_StoreBase):
    """A pending cancellation survives a real Store reopen and converges only on
    complete exact proof.

    ``tests/substrate/test_cancellation_monitor.py`` owns the residue-batch
    restart fairness. This test proves the durability + convergence join for a
    PENDING cancellation request across a fresh Store process on the same sqlite
    file: it survives restart, converges under the public reconcile path once a
    same-run HALT confirms, and stays fail-closed under foreign/incomplete proof.
    """

    def _reopen(self) -> Store:
        return Store(self.db_path)

    def test_pending_request_survives_restart_and_converges_via_reconcile(self) -> None:
        started = self._in_flight(StubAdapter(run_token="run-token-item4-a"), key="restart")
        did = started["dispatch_id"]
        # Pending: recorded without an adapter so the monitor owns termination.
        self.store.request_cancellation(
            did, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        claimed = _ledger_row(self.store, did)["auth_lineage_claimed_at"]
        self.assertIsNotNone(claimed)

        # RESTART: a brand-new Store on the same file continues from durable SQL.
        restarted = self._reopen()
        restarted.reconcile_dispatches(lambda _r: UnconfirmedAdapter(), human_actor_id=HUMAN_ID)
        # The durable pending request survived and is still held (nonterminal,
        # cap/lineage retained); it did not silently release or terminalize.
        row = _ledger_row(restarted, did)
        self.assertEqual(row["status"], "in_flight")
        self.assertEqual(row["auth_lineage_claimed_at"], claimed)
        self.assertEqual(_observed(restarted, did)["cancellation"]["state"], "requested")

        # A second restart converges once a same-run HALT confirms termination.
        restarted2 = self._reopen()
        restarted2.reconcile_dispatches(
            lambda _r: StubAdapter(run_token="run-token-item4-a"), human_actor_id=HUMAN_ID
        )
        self.assertEqual(_ledger_row(restarted2, did)["status"], "cancelled")
        self.assertEqual(_joined_outcome(restarted2, did)["outcome"], "confirmed_cancel")

    def test_incomplete_or_foreign_proof_stays_fail_closed_across_restart(self) -> None:
        # Convergence-by-proof after restart requires the COMPLETE exact proof.
        good = self._in_flight(ExplodingHaltAdapter(run_token="run-token-item4-good"), key="good")
        good_id = good["dispatch_id"]
        self.store.request_cancellation(
            good_id, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        _merge_observed(self.store, good_id, {"reaper_exit": _complete_proof("run-token-item4-good")})

        foreign = self._in_flight(UnconfirmedAdapter(run_token="run-token-item4-foreign"), key="foreign")
        foreign_id = foreign["dispatch_id"]
        self.store.request_cancellation(
            foreign_id, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        _merge_observed(self.store, foreign_id, {"reaper_exit": _complete_proof("run-token-WRONG")})

        restarted = self._reopen()
        # The complete-proof row converges (same-run exit; no HALT needed).
        good_result = restarted.request_cancellation(
            good_id, requesting_actor_id="arch", reason="withdraw", authority="producer",
            adapter_for_runtime=lambda _r: ExplodingHaltAdapter(run_token="run-token-item4-good"),
        )
        self.assertEqual(good_result["status"], "cancelled")
        self.assertEqual(good_result["termination_result"], "same_run_exit_confirmed")
        self.assertEqual(_joined_outcome(restarted, good_id)["outcome"], "confirmed_cancel")

        # The foreign-token proof stays fail-closed: HALT is attempted and refused,
        # and the row is held nonterminal with cap/lineage retained.
        foreign_result = restarted.request_cancellation(
            foreign_id, requesting_actor_id="arch", reason="withdraw", authority="producer",
            adapter_for_runtime=lambda _r: UnconfirmedAdapter(run_token="run-token-item4-foreign"),
        )
        self.assertEqual(foreign_result["status"], "in_flight")
        self.assertEqual(foreign_result["cancellation_state"], "requested")
        self.assertIsNotNone(_ledger_row(restarted, foreign_id)["auth_lineage_claimed_at"])
        self.assertEqual(_joined_outcome(restarted, foreign_id)["outcome"], "in_flight")


# --------------------------------------------------------------------------- #
# Item 5: contract-level acceptance matrix tying public outcomes to T1-T10
# --------------------------------------------------------------------------- #
class TerminalizationContractAcceptanceMatrixTest(_StoreBase):
    """Bind the LIVE public outcomes reached by items 1-4 to the declared T1-T10
    invariants.

    The static contract/doc pins (contract 10 -> 11, digest, frozen floor,
    literal interactions, mixed-reader statement) are owned by
    ``tests/substrate/test_dead_worker_contract.py`` and are not re-cloned here.
    This matrix instead asserts that each public outcome this aggregate produces
    is a member of the declared contract vocabulary and projects to its named
    sanctioned outcome, and it does not assert implementation-private call counts
    unless the invariant itself requires the count (T8's single producer notice).
    """

    VALID_TOKEN = "operator-secret-matrix"

    def setUp(self) -> None:
        super().setUp()

    # --- live public flows the matrix binds against ---------------------- #
    def _confirmed_producer_cancel(self) -> str:
        d = self._queued("m-producer")
        result = self.store.request_cancellation(
            d["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.assertEqual(result["termination_result"], "not_started")
        return d["dispatch_id"]

    def _same_run_exit_cancel(self) -> str:
        adapter = ExplodingHaltAdapter(run_token="run-token-matrix-exit")
        started = self._in_flight(adapter, key="m-exit")
        _merge_observed(self.store, started["dispatch_id"], {"reaper_exit": _complete_proof(adapter.run_token)})
        self.store.request_cancellation(
            started["dispatch_id"], requesting_actor_id="arch", reason="exited", authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        return started["dispatch_id"]

    def _admin_settlement(self) -> str:
        adapter = StubAdapter(run_token="run-token-matrix-settle")
        started = self._in_flight(adapter, key="m-settle")
        did = started["dispatch_id"]
        self.store.request_cancellation(
            did, requesting_actor_id=HUMAN_ID, reason="stuck", authority="admin"
        )
        preview = self.store.settle_dispatch_preview(
            did, actor_id=HUMAN_ID, reason="release lineage", secret=self.VALID_TOKEN
        )
        result = self.store.settle_dispatch_execute(
            did, actor_id=HUMAN_ID, plan=preview["plan"], secret=self.VALID_TOKEN, release_ack=True
        )
        self.assertEqual(result["status"], "dlq")
        return did

    def _held_pending(self) -> str:
        adapter = UnconfirmedAdapter(run_token="run-token-matrix-held")
        started = self._in_flight(adapter, key="m-held")
        self.store.request_cancellation(
            started["dispatch_id"], requesting_actor_id="arch", reason="withdraw", authority="producer",
            adapter_for_runtime=lambda _r: adapter,
        )
        return started["dispatch_id"]

    def test_t1_authority_boundary_producer_admin_and_worker_denial(self) -> None:
        # T1: the only cancellation authorities are producer and admin, and the
        # live producer + admin flows below exercise exactly those.
        self.assertEqual(set(CANCELLATION_AUTHORITIES), {"producer", "admin"})
        producer_id = self._confirmed_producer_cancel()
        admin_id = self._admin_settlement()
        self.assertEqual(_observed(self.store, producer_id)["cancellation"]["authority"], "producer")
        self.assertEqual(_observed(self.store, admin_id)["cancellation"]["authority"], "admin")
        # A bounded worker is denied cancel_dispatch at BOTH the MCP allowlist and
        # the runtime hook (the deterministic public analog of T9's worker denial).
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        self.assertIn("cancel_dispatch", policy.mcp_denied_tools)
        self.assertNotIn("cancel_dispatch", policy.mcp_allowed_tools)
        self.assertIn("cancel_dispatch", policy.hook_denied_tools)
        self.assertIn("cancel_dispatch", pre_tool_use.MCP_DENIED_TOOLS)
        # The operator-mailbox seat exposes no cancel_dispatch surface either.
        self.assertNotIn("cancel_dispatch", compile_policy(OPERATOR_MAILBOX_POLICY).mcp_allowed_tools)

    def test_t2_terminal_vocabulary_and_projection_distinctness(self) -> None:
        # T2: cancelled is a truthful terminal in BOTH state machines, and the
        # sanctioned cluster projects distinctly (never folded into ordinary
        # close/dlq).
        self.assertIn("cancelled", schema.STATUSES)
        self.assertIn("cancelled", DISPATCH_TERMINAL_STATUSES)
        self.assertEqual(
            project_dispatch_transport("cancelled", "cancelled")["outcome"], "confirmed_cancel"
        )
        self.assertEqual(
            project_dispatch_transport("dlq", "cancelled")["outcome"],
            "operator_settled_termination_unconfirmed",
        )
        self.assertEqual(
            project_dispatch_transport("cancelled", "closed")["outcome"],
            "confirmed_cancel_transport_closed_first",
        )
        # An impossible cancelled pair is a loud unknown, never inferred equality.
        self.assertEqual(project_dispatch_transport("cancelled", "sent")["outcome"], "unknown")

    def test_t3_unclaimed_queued_cancel_is_not_started_and_confirmed(self) -> None:
        # T3: an unclaimed queued cancel commits confirmed cancelled with
        # not_started (no false in-flight publication).
        did = self._confirmed_producer_cancel()
        self.assertEqual(_observed(self.store, did)["cancellation"]["termination_result"], "not_started")
        self.assertEqual(_joined_outcome(self.store, did)["outcome"], "confirmed_cancel")

    def test_t4_same_run_exit_requires_complete_proof(self) -> None:
        # T4: terminal cancellation from a same-run exit rests on the exact
        # version-1 complete reaper proof (a bare worker_exit never qualifies).
        did = self._same_run_exit_cancel()
        self.assertEqual(
            _observed(self.store, did)["cancellation"]["termination_result"], "same_run_exit_confirmed"
        )
        self.assertEqual(_joined_outcome(self.store, did)["outcome"], "confirmed_cancel")
        self.assertIsNone(complete_reaper_proof({"returncode": 0, "run_token": "x"}, "x"))

    def test_t5_pending_request_is_nonterminal_and_holds_lineage(self) -> None:
        # T5: a pending request is nonterminal and releases neither cap nor lineage
        # (the monitor retry/escalation legs themselves live in the monitor module).
        did = self._held_pending()
        row = _ledger_row(self.store, did)
        self.assertEqual(row["status"], "in_flight")
        self.assertIsNotNone(row["auth_lineage_claimed_at"])
        self.assertEqual(_observed(self.store, did)["cancellation"]["state"], "requested")

    def test_t6_terminal_winner_is_stable_under_replay(self) -> None:
        # T6: a committed terminal cancel is a stable winner; an identical replay
        # returns it without churn or a terminal-to-live transition.
        did = self._confirmed_producer_cancel()
        cancelled_at = _ledger_row(self.store, did)["cancelled_at"]
        replay = self.store.request_cancellation(
            did, requesting_actor_id="arch", reason="withdraw", authority="producer"
        )
        self.assertEqual(replay["status"], "cancelled")
        self.assertEqual(_ledger_row(self.store, did)["cancelled_at"], cancelled_at)
        self.assertEqual(_joined_outcome(self.store, did)["outcome"], "confirmed_cancel")

    def test_t7_settlement_is_dlq_cancelled_unconfirmed_with_one_notice(self) -> None:
        # T7: emergency settlement writes ledger dlq + transport cancelled under
        # the exact unconfirmed outcome; the single producer notice is the one
        # count the invariant itself requires.
        did = self._admin_settlement()
        row = _ledger_row(self.store, did)
        self.assertEqual(row["status"], "dlq")
        self.assertEqual(_transport_status(self.store, row["message_id"], "wrk"), "cancelled")
        self.assertEqual(
            _joined_outcome(self.store, did)["outcome"], "operator_settled_termination_unconfirmed"
        )
        self.assertEqual(_observed(self.store, did)[SETTLEMENT_KEY]["failure_reason"], SETTLEMENT_FAILURE_REASON)
        with self.store._db.connection() as conn:
            notice_count = conn.execute(
                "select count(*) c from messages m join message_recipients r on r.message_id = m.id "
                "where r.to_agent = 'arch' and m.body like '%ledger released; termination not confirmed%'"
            ).fetchone()["c"]
        self.assertEqual(notice_count, 1)

    def test_t8_producer_self_cancel_is_notice_free_settlement_pages_once(self) -> None:
        # T8: a producer self-cancel pages nobody; the exceptional settlement is
        # the DLQ blocker page. The two live flows demonstrate the distinction.
        producer_id = self._confirmed_producer_cancel()
        self.assertEqual(self.store.list_inbox("arch", unread_only=False), [])
        settle_id = self._admin_settlement()
        pages = [
            m for m in self.store.list_inbox("arch", unread_only=False)
            if "ledger released; termination not confirmed"
            in self.store.read_message("arch", m["id"])["body"]
        ]
        self.assertEqual(len(pages), 1)
        self.assertNotEqual(producer_id, settle_id)

    def test_t9_bounded_worker_cannot_reach_cancel_dispatch_runtime_free(self) -> None:
        # T9 (runtime-free analog): a bounded worker policy hides and denies
        # cancel_dispatch. The authenticated live-cancel cells are architect-run.
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        self.assertNotIn("cancel_dispatch", policy.mcp_allowed_tools)
        self.assertIn("cancel_dispatch", policy.mcp_denied_tools)
        for name in (
            "cancel_dispatch",
            "mcp__agent_comms__cancel_dispatch",
            "mcp__agent-comms__cancel_dispatch",
        ):
            self.assertIn(name, policy.hook_denied_tools)

    def test_t10_every_reached_public_outcome_is_declared_and_named(self) -> None:
        # T10: every terminal outcome the public surfaces in this aggregate reach
        # is a declared terminal status that projects to a NAMED (non-unknown)
        # sanctioned outcome, binding the live outcomes to the contract vocabulary.
        reached = {
            self._confirmed_producer_cancel(): ("cancelled", "confirmed_cancel"),
            self._same_run_exit_cancel(): ("cancelled", "confirmed_cancel"),
            self._admin_settlement(): ("dlq", "operator_settled_termination_unconfirmed"),
        }
        for did, (expected_status, expected_outcome) in reached.items():
            with self.subTest(dispatch_id=did):
                row = _ledger_row(self.store, did)
                self.assertEqual(row["status"], expected_status)
                self.assertIn(row["status"], DISPATCH_TERMINAL_STATUSES)
                outcome = _joined_outcome(self.store, did)["outcome"]
                self.assertEqual(outcome, expected_outcome)
                self.assertNotEqual(outcome, "unknown")


if __name__ == "__main__":
    unittest.main()
