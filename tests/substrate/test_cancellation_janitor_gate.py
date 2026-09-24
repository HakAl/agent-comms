"""Stage-2 T2 (Required outcome 5 coupling): the janitor's status-only deletion
authority is replaced by a positive same-run termination-evidence gate.

Adding the ``cancelled`` terminal status must never, by itself, authorize the
spawn-time janitor to delete a run directory. Cleanup now requires the SQL owner
to be terminal AND to carry positive same-run termination evidence. There are
exactly two positive authorities, and a bare confirmed-looking result string is
not one of them:

- the matching-token ``termination_result=not_started`` string (cancellation of
  a never-spawned dispatch) is separately safe on its own, but only when the SQL
  run token is present and equals the run directory token; or
- the exact version-1 COMPLETE ``$.reaper_exit`` proof whose ``run_token``
  exactly matches the run directory's token.

Revision 7 F2: the ``supervised_halt_confirmed``, ``same_run_exit_confirmed``,
and ``worker_exited_before_close`` result strings are INSUFFICIENT on their own;
each requires the complete exact-token version-1 ``$.reaper_exit`` proof above
(which the authenticated HALT persists before it returns success). A bare
same-run ``$.worker_exit`` is child-exit evidence only and is likewise
insufficient: it is PRESERVED and never grants janitor deletion authority; only
the complete registered-wrapper reap proof does. When that complete proof exists
a downstream confirmed ``termination_result`` may coexist with it but is never an
independent deletion authority. A wrong-token exit object never authorizes
deletion in either direction.

Every ``termination_not_confirmed`` residue (including pre-feature hard-TTL
``dlq`` rows that predate the feature and carry no evidence) is preserved loudly
even though its ledger owner is terminal: a dead supervisor can still leave a
real zombie that only an operator may reclaim.

These tests exercise the gate directly through ``supervisor.janitor_sweep`` with
an explicit isolated control root; no AF_UNIX bind or child process is involved.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_comms.dispatch_ledger as dispatch_ledger
from agent_comms import supervisor
from agent_comms.store import Store


def _seed_dispatch(store: Store, root: Path, key: str) -> str:
    store.register_agent_actor("arch", "alpha", "architect", str(root / "arch"), [])
    store.register_agent_actor("wrk", "alpha", "worker", str(root / "wrk"), [], owner="arch")
    dispatch = store.dispatch_agent("arch", "wrk", key, "subject", "body", [])
    return dispatch["dispatch_id"]


def _set_status(store: Store, dispatch_id: str, status: str) -> None:
    # Forcing a v2 row to ``closed`` must seed the checked result the ledger
    # CHECK requires; the janitor gate reads evidence, never the result.
    with store.connection() as conn:
        conn.execute(
            "update dispatch_ledger set status = ?, result = case when ? = 'closed' "
            "then coalesce(result, 'satisfied') else result end where dispatch_id = ?",
            (status, status, dispatch_id),
        )


def _set_observed(store: Store, dispatch_id: str, path: str, value: str) -> None:
    with store.connection() as conn:
        conn.execute(
            "update dispatch_ledger set observed_values_json = json_set("
            "coalesce(nullif(observed_values_json, ''), '{}'), ?, ?) where dispatch_id = ?",
            (path, value, dispatch_id),
        )


def _set_transport(store: Store, dispatch_id: str, status: str) -> None:
    """Advance the recipient transport copy for a dispatch's message."""
    with store.connection() as conn:
        conn.execute(
            "update message_recipients set status = ? where message_id = "
            "(select message_id from dispatch_ledger where dispatch_id = ?)",
            (status, dispatch_id),
        )


class JanitorPositiveEvidenceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.control_root = self.tmp / "s"
        self.control_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _owned_dir(self, token: str, dispatch_id: str) -> Path:
        run_dir = supervisor.create_run_dir(self.control_root, token)
        marker = {
            "marker_version": supervisor.MARKER_VERSION,
            "dispatch_id": dispatch_id,
            "run_token": token,
            "wrapper_pid": 1234,
            "created_at": "2026-07-15T00:00:00+00:00",
        }
        (run_dir / "owner.json").write_text(json.dumps(marker, sort_keys=True))
        return run_dir

    def _identify(self, dispatch_id: str, token: str) -> None:
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )

    def _sweep_action(self, run_dir: Path) -> str:
        outcomes = supervisor.janitor_sweep(str(self.db_path), self.control_root)
        by_path = {outcome["path"]: outcome for outcome in outcomes}
        return by_path[str(run_dir)]["action"]

    # ----- removed: positive same-run evidence present ---------------------- #

    def test_cancelled_not_started_is_removed(self) -> None:
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "not-started")
        self._identify(dispatch_id, token)
        _set_observed(self.store, dispatch_id, "$.termination_result", "not_started")
        _set_status(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "removed")
        self.assertFalse(run_dir.exists())

    def test_cancelled_supervised_halt_confirmed_is_removed(self) -> None:
        # Revision 7 F2: a truthful ``supervised_halt_confirmed`` cancellation
        # carries the complete exact-token version-1 ``$.reaper_exit`` proof that
        # the normal authenticated HALT persists before it returns success. The
        # result string alone never grants janitor deletion; the complete same-run
        # registered-wrapper reap proof is what authorizes removal.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "halt-confirmed")
        self._identify(dispatch_id, token)
        self.assertTrue(
            supervisor.record_reaper_exit(str(self.db_path), dispatch_id, token, returncode=0)
        )
        _set_observed(self.store, dispatch_id, "$.termination_result", "supervised_halt_confirmed")
        _set_status(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "removed")

    def test_terminal_with_matching_worker_exit_alone_is_preserved(self) -> None:
        # Revision 7 F2: a bare same-run ``$.worker_exit`` (child-exit evidence
        # only) with a terminal owner must NOT grant janitor deletion authority.
        # The exact-token worker_exit is preserved, not removed; only the
        # complete registered-wrapper reap proof authorizes cleanup.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "worker-exit")
        self._identify(dispatch_id, token)
        self.assertTrue(
            supervisor.record_worker_exit(str(self.db_path), dispatch_id, token, returncode=0, source="child")
        )
        _set_status(self.store, dispatch_id, "closed")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())

    def test_terminal_with_matching_reaper_exit_is_removed(self) -> None:
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "reaper-exit")
        self._identify(dispatch_id, token)
        self.assertTrue(
            supervisor.record_reaper_exit(str(self.db_path), dispatch_id, token, returncode=143)
        )
        _set_status(self.store, dispatch_id, "dlq")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "removed")

    # ----- preserved: terminal status but NO confirmed evidence ------------- #

    def test_terminal_closed_without_any_evidence_is_preserved(self) -> None:
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "no-evidence")
        self._identify(dispatch_id, token)  # identity only, no exit / result
        _set_status(self.store, dispatch_id, "closed")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())

    def test_hard_ttl_dlq_termination_not_confirmed_is_preserved(self) -> None:
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "unconfirmed-residue")
        self._identify(dispatch_id, token)
        _set_observed(self.store, dispatch_id, "$.termination_result", "termination_not_confirmed")
        _set_status(self.store, dispatch_id, "dlq")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())

    def test_pre_feature_dlq_without_termination_result_is_preserved(self) -> None:
        # A hard-TTL dlq row written before this feature carries an identity but
        # no termination_result and no exit object; it must be preserved loudly.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "pre-feature")
        self._identify(dispatch_id, token)
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set failure_reason = ? where dispatch_id = ?",
                ("timeout; ledger released; termination not confirmed", dispatch_id),
            )
        _set_status(self.store, dispatch_id, "dlq")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")

    def test_missing_sql_run_token_with_positive_result_is_preserved(self) -> None:
        # SQL run-token identity is MISSING (no spawn identity recorded), yet a
        # positive termination_result string is present. The gate must NOT remove
        # on the string alone: exact same-run identity is required. A stray/forged
        # run directory whose owner has no SQL run token is preserved for the
        # operator, even when it carries a confirmed-looking termination_result.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "missing-identity")
        # NOTE: no self._identify(...) -> observed has no run_token.
        _set_observed(self.store, dispatch_id, "$.termination_result", "not_started")
        _set_status(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())

    def test_missing_sql_run_token_with_halt_confirmed_is_preserved(self) -> None:
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "missing-identity-halt")
        _set_observed(self.store, dispatch_id, "$.termination_result", "supervised_halt_confirmed")
        _set_status(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())

    def test_mismatched_sql_run_token_with_positive_result_is_preserved(self) -> None:
        # SQL run-token identity is present but points at a DIFFERENT run than the
        # directory; a positive termination_result string must not override the
        # mismatch. This is an ABA / cross-run artifact and is preserved.
        token = supervisor.new_run_token()
        other_token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "mismatch-identity")
        self._identify(dispatch_id, other_token)  # SQL identity != run dir token
        _set_observed(self.store, dispatch_id, "$.termination_result", "same_run_exit_confirmed")
        _set_status(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())

    def test_matching_sql_run_token_with_positive_result_still_removes(self) -> None:
        # The strengthening does not break the legitimate path: a spawned dispatch
        # whose SQL run-token identity matches the directory AND carries a positive
        # termination_result is still removed.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "matching-identity")
        self._identify(dispatch_id, token)
        _set_observed(self.store, dispatch_id, "$.termination_result", "not_started")
        _set_status(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "removed")
        self.assertFalse(run_dir.exists())

    def test_exit_object_with_wrong_run_token_is_preserved(self) -> None:
        # Object existence is never enough: an exit whose run_token does not match
        # the run directory (a cross-run / stale artifact) does not authorize
        # deletion.
        token = supervisor.new_run_token()
        other_token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "wrong-token")
        self._identify(dispatch_id, token)
        # Force a worker_exit OBJECT whose run_token is a DIFFERENT run (stored as
        # real JSON via json(), not a JSON string), so the gate sees a dict but
        # rejects it on the run-token mismatch.
        with self.store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set observed_values_json = json_set("
                "coalesce(nullif(observed_values_json, ''), '{}'), '$.worker_exit', json(?)) "
                "where dispatch_id = ?",
                (json.dumps({"returncode": 0, "source": "child", "run_token": other_token}), dispatch_id),
            )
        _set_status(self.store, dispatch_id, "closed")
        run_dir = self._owned_dir(token, dispatch_id)
        self.assertEqual(self._sweep_action(run_dir), "preserved")
        self.assertTrue(run_dir.exists())


class JanitorConsumesJoinedProjectionTest(unittest.TestCase):
    """The janitor cleanup DECISION consumes the single canonical ledger/transport
    projection, not the raw ledger status alone.

    Terminal-ness still follows the execution (ledger) machine, but the janitor
    now joins the recipient transport copy and classifies the pair through the
    shared ``project_dispatch_transport`` authority, recording the normalized
    ``outcome`` on every cleanup record. These tests exercise the real
    ``supervisor.janitor_sweep`` production path (not the classifier helper
    directly) and would FAIL if cleanup bypassed the joined projection: two owners
    with the SAME terminal ledger status but different transport copies must carry
    DISTINCT cleanup outcomes.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "agent-comms.sqlite"
        self.store = Store(self.db_path)
        self.control_root = self.tmp / "s"
        self.control_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _owned_dir(self, token: str, dispatch_id: str) -> Path:
        run_dir = supervisor.create_run_dir(self.control_root, token)
        marker = {
            "marker_version": supervisor.MARKER_VERSION,
            "dispatch_id": dispatch_id,
            "run_token": token,
            "wrapper_pid": 1234,
            "created_at": "2026-07-15T00:00:00+00:00",
        }
        (run_dir / "owner.json").write_text(json.dumps(marker, sort_keys=True))
        return run_dir

    def _identify(self, dispatch_id: str, token: str) -> None:
        supervisor.merge_supervisor_observed(
            str(self.db_path), dispatch_id, run_token=token, wrapper_pid=1, child_pid=2, control_socket="x"
        )

    def _sweep_record(self, run_dir: Path) -> dict:
        outcomes = supervisor.janitor_sweep(str(self.db_path), self.control_root)
        by_path = {outcome["path"]: outcome for outcome in outcomes}
        return by_path[str(run_dir)]

    def test_removed_reason_carries_joined_projection_outcome(self) -> None:
        # A confirmed cancellation (cancelled ledger + cancelled transport) with
        # positive same-run evidence is removed, and the record carries the joined
        # projection outcome ``confirmed_cancel`` -- never a bare ``cancelled``.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "confirmed-cancel")
        self._identify(dispatch_id, token)
        _set_observed(self.store, dispatch_id, "$.termination_result", "not_started")
        _set_status(self.store, dispatch_id, "cancelled")
        _set_transport(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)

        record = self._sweep_record(run_dir)
        self.assertEqual(record["action"], "removed")
        self.assertIn("confirmed_cancel", record["reason"])

    def test_same_ledger_distinct_transport_yield_distinct_cleanup_outcomes(self) -> None:
        # Two owners share the SAME terminal ledger status (dlq) and the same
        # positive same-run cleanup evidence -- the exact complete reaper proof,
        # NOT a bare worker_exit (Revision 7 F2) -- differing ONLY in their
        # recipient transport copy. If cleanup consulted the ledger status alone
        # the two records would be identical; because it joins the transport
        # through the shared projection they carry DISTINCT outcomes.
        ordinary_token = supervisor.new_run_token()
        ordinary_id = _seed_dispatch(self.store, self.tmp, "dlq-ordinary")
        self._identify(ordinary_id, ordinary_token)
        self.assertTrue(
            supervisor.record_reaper_exit(
                str(self.db_path), ordinary_id, ordinary_token, returncode=1
            )
        )
        _set_status(self.store, ordinary_id, "dlq")
        _set_transport(self.store, ordinary_id, "sent")
        ordinary_dir = self._owned_dir(ordinary_token, ordinary_id)

        settled_token = supervisor.new_run_token()
        settled_id = _seed_dispatch(self.store, self.tmp, "dlq-operator-settled")
        self._identify(settled_id, settled_token)
        self.assertTrue(
            supervisor.record_reaper_exit(
                str(self.db_path), settled_id, settled_token, returncode=1
            )
        )
        _set_status(self.store, settled_id, "dlq")
        _set_transport(self.store, settled_id, "cancelled")
        settled_dir = self._owned_dir(settled_token, settled_id)

        # A single sweep classifies both owners; capture all records at once
        # (each removed directory is gone after this one pass).
        outcomes = supervisor.janitor_sweep(str(self.db_path), self.control_root)
        by_path = {outcome["path"]: outcome for outcome in outcomes}
        ordinary = by_path[str(ordinary_dir)]
        settled = by_path[str(settled_dir)]
        self.assertEqual(ordinary["action"], "removed")
        self.assertEqual(settled["action"], "removed")
        # Distinct joined-projection outcomes despite the identical ledger status.
        self.assertIn("dlq/dlq", ordinary["reason"])
        self.assertIn("operator_settled_termination_unconfirmed", settled["reason"])
        self.assertNotEqual(ordinary["reason"], settled["reason"])

    def test_preserved_reason_carries_joined_projection_outcome(self) -> None:
        # Even the preserve path consumes the join: a dlq owner whose recipient
        # copy was operator-settled but which carries NO confirmed same-run
        # evidence is preserved, and the loud record names the settlement outcome.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "settled-no-evidence")
        self._identify(dispatch_id, token)  # identity only, no exit / result
        _set_status(self.store, dispatch_id, "dlq")
        _set_transport(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)

        record = self._sweep_record(run_dir)
        self.assertEqual(record["action"], "preserved")
        self.assertIn("operator_settled_termination_unconfirmed", record["reason"])
        self.assertTrue(run_dir.exists())

    # ----- bypass-proof: the classifier's return drives the decision -------- #

    def test_cleanup_supplies_actual_joined_pair_and_records_classifier_outcome(self) -> None:
        # BYPASS-PROOF (Required outcome 3): cleanup feeds the shared classifier
        # the ACTUAL joined pair -- the real ledger execution status and the real
        # recipient transport copy -- and records the classifier's returned
        # ``outcome`` on the cleanup decision. A spy captures every pair supplied.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "spy-pair")
        self._identify(dispatch_id, token)
        _set_observed(self.store, dispatch_id, "$.termination_result", "not_started")
        _set_status(self.store, dispatch_id, "dlq")
        _set_transport(self.store, dispatch_id, "cancelled")
        run_dir = self._owned_dir(token, dispatch_id)

        calls: list[tuple] = []
        real = dispatch_ledger.project_dispatch_transport

        def spy(dispatch_status, transport_status):
            calls.append((dispatch_status, transport_status))
            return real(dispatch_status, transport_status)

        with mock.patch.object(dispatch_ledger, "project_dispatch_transport", spy):
            record = self._sweep_record(run_dir)

        # The exact raw joined pair was supplied from the live ledger/transport.
        self.assertIn(("dlq", "cancelled"), calls)
        # The recorded decision carries the classifier's returned outcome.
        self.assertEqual(record["action"], "removed")
        self.assertIn("operator_settled_termination_unconfirmed", record["reason"])

    @staticmethod
    def _fixed_projection(dispatch_status, outcome):
        def classifier(_dispatch_status, transport_status):
            return {
                "dispatch_status": dispatch_status,
                "transport_status": transport_status,
                "outcome": outcome,
            }

        return classifier

    def test_cleanup_terminal_status_from_classifier_overrides_raw_terminal(self) -> None:
        # BYPASS-PROOF (Required outcome 3 + 4): terminal-ness is read from the
        # classifier's RETURNED ``dispatch_status``, never re-derived from a raw
        # ledger read. Raw ledger is terminal (dlq) with the complete reaper
        # proof (Revision 7 F2 confirmed same-run evidence), but the classifier
        # returns a NON-terminal status -> preserved as nonterminal. A consumer
        # that re-queried the raw status would remove it.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "raw-terminal")
        self._identify(dispatch_id, token)
        self.assertTrue(
            supervisor.record_reaper_exit(
                str(self.db_path), dispatch_id, token, returncode=0
            )
        )
        _set_status(self.store, dispatch_id, "dlq")
        run_dir = self._owned_dir(token, dispatch_id)
        with mock.patch.object(
            dispatch_ledger, "project_dispatch_transport", self._fixed_projection("queued", "queued")
        ):
            record = self._sweep_record(run_dir)
        self.assertEqual(record["action"], "preserved")
        self.assertIn("nonterminal: queued", record["reason"])
        self.assertTrue(run_dir.exists())

    def test_cleanup_terminal_status_from_classifier_overrides_raw_nonterminal(self) -> None:
        # BYPASS-PROOF (Required outcome 3 + 4), inverse direction: raw ledger is
        # NON-terminal (queued), but the classifier returns a terminal status;
        # with the complete reaper proof as confirmed same-run evidence
        # (Revision 7 F2) the owner is removed. A consumer that re-queried the raw
        # status would preserve it as nonterminal.
        token = supervisor.new_run_token()
        dispatch_id = _seed_dispatch(self.store, self.tmp, "raw-nonterminal")
        self._identify(dispatch_id, token)
        self.assertTrue(
            supervisor.record_reaper_exit(
                str(self.db_path), dispatch_id, token, returncode=0
            )
        )
        # ledger status stays the seeded "queued".
        run_dir = self._owned_dir(token, dispatch_id)
        with mock.patch.object(
            dispatch_ledger, "project_dispatch_transport", self._fixed_projection("closed", "closed")
        ):
            record = self._sweep_record(run_dir)
        self.assertEqual(record["action"], "removed")
        self.assertIn("terminal closed/closed", record["reason"])
        self.assertFalse(run_dir.exists())


if __name__ == "__main__":
    unittest.main()
