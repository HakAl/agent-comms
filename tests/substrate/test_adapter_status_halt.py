"""T3: typed adapter status and authenticated halt (no ps, no PID-parsed kill).

These pin the adapter's Spec C ``status`` (exited / running /
supervisor_unreachable) and the authenticated ``halt`` behaviour. The
authenticated path is proven not to fall back to ``os.killpg`` or shell out to
``ps``; the legacy PID-parsed path only runs for pre-supervisor handles and is
documented as the transitional residual.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import re
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.adapters import _base
from agent_comms.adapters._base import AdapterStatus, SupervisorUnreachable
from agent_comms.adapters.fake import FakeAdapter
from agent_comms import supervisor

ROOT = Path(__file__).resolve().parents[2]
BASE_SOURCE = (ROOT / "agent_comms" / "adapters" / "_base.py").read_text()

HANDLE = "fake:alpha-fake-worker:4321"
SOCKET = "/nonexistent/run/s/abc/s"
TOKEN = "a" * 32


class AdapterStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()

    def test_exited_from_worker_exit_evidence(self) -> None:
        observed = {"run_token": TOKEN, "worker_exit": {"returncode": 0, "source": "halt", "run_token": TOKEN}}
        result = self.adapter.status(HANDLE, observed)
        self.assertIsInstance(result, AdapterStatus)
        self.assertEqual(result.state, "exited")
        self.assertEqual(result.returncode, 0)

    def test_exited_from_reaper_exit_evidence(self) -> None:
        observed = {"run_token": TOKEN, "reaper_exit": {"returncode": 143, "run_token": TOKEN}}
        result = self.adapter.status(HANDLE, observed)
        self.assertEqual(result.state, "exited")
        self.assertEqual(result.returncode, 143)

    def test_exited_requires_matching_run_token_else_unreachable(self) -> None:
        # Stale exit evidence (an OLDER run's token) is not same-run: never
        # 'exited'. Without control identity it falls through to unreachable.
        observed = {"run_token": TOKEN, "worker_exit": {"returncode": 0, "run_token": "b" * 32}}
        result = self.adapter.status(HANDLE, observed)
        self.assertEqual(result.state, "supervisor_unreachable")
        self.assertNotEqual(result.state, "exited")

    def test_exited_ignores_missing_or_malformed_exit_token(self) -> None:
        for exit_obj in (
            {"returncode": 0},
            {"returncode": 0, "run_token": 42},
            {"returncode": 0, "run_token": ""},
        ):
            with self.subTest(exit_obj=exit_obj):
                observed = {"run_token": TOKEN, "worker_exit": exit_obj}
                self.assertNotEqual(self.adapter.status(HANDLE, observed).state, "exited")

    def test_exited_requires_current_run_token_present(self) -> None:
        # No current observed run_token -> the exit object cannot authenticate.
        observed = {"worker_exit": {"returncode": 0, "run_token": TOKEN}}
        self.assertEqual(self.adapter.status(HANDLE, observed).state, "supervisor_unreachable")

    def test_unreachable_when_no_control_identity(self) -> None:
        result = self.adapter.status(HANDLE, {"pid": 4321})
        self.assertEqual(result.state, "supervisor_unreachable")

    def test_unreachable_when_socket_missing_not_inferred_dead(self) -> None:
        observed = {"control_socket": SOCKET, "run_token": TOKEN}
        result = self.adapter.status(HANDLE, observed)
        self.assertEqual(result.state, "supervisor_unreachable")
        self.assertNotEqual(result.state, "exited")

    def test_running_when_status_socket_confirms(self) -> None:
        observed = {"control_socket": SOCKET, "run_token": TOKEN}
        with mock.patch.object(
            supervisor, "probe_status", return_value=supervisor.ControlResult(ok=True, state="running")
        ):
            result = self.adapter.status(HANDLE, observed)
        self.assertEqual(result.state, "running")

    def test_status_never_signals(self) -> None:
        observed = {"control_socket": SOCKET, "run_token": TOKEN}
        with mock.patch("os.killpg", side_effect=AssertionError("status must not signal")):
            with mock.patch("os.kill", side_effect=AssertionError("status must not signal")):
                self.adapter.status(HANDLE, observed)
                self.adapter.status(HANDLE, {"worker_exit": {"returncode": 0, "run_token": TOKEN}})

    def test_status_state_domain(self) -> None:
        for observed in (
            {"worker_exit": {"returncode": 0, "run_token": TOKEN}},
            {"control_socket": SOCKET, "run_token": TOKEN},
            {},
        ):
            self.assertIn(self.adapter.status(HANDLE, observed).state, {"exited", "running", "supervisor_unreachable"})


class AdapterHaltTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()

    def test_authenticated_halt_confirmed(self) -> None:
        observed = {"control_socket": SOCKET, "run_token": TOKEN}
        with mock.patch.object(
            supervisor, "request_halt", return_value=supervisor.ControlResult(ok=True, state="halted", returncode=0)
        ) as halt:
            with mock.patch("os.killpg", side_effect=AssertionError("authenticated halt must not killpg")):
                self.adapter.halt(HANDLE, observed)
        # The HALT client I/O timeout is aligned with the TERM/KILL grace so an
        # ignore-TERM child can return a real ack instead of a deterministic
        # timeout.
        halt.assert_called_once_with(SOCKET, TOKEN, io_timeout=_base.HALT_IO_TIMEOUT_SECONDS)
        self.assertGreaterEqual(_base.HALT_IO_TIMEOUT_SECONDS, 2 * _base.KILL_AFTER_SECONDS)

    def test_authenticated_halt_confirmed_via_exit_evidence(self) -> None:
        # Only the COMPLETE exact-current-token version-1 reaper proof confirms
        # a failed socket HALT; bare same-run worker_exit is child-exit evidence
        # and refuses this path (see the negatives below and the Revision 7 F2
        # bypass coverage in test_supervisor).
        observed = {
            "control_socket": SOCKET,
            "run_token": TOKEN,
            "reaper_exit": {
                "proof_version": 1,
                "run_token": TOKEN,
                "returncode": 0,
                "source": "halt_finalize",
                "reaped_at": "2026-07-27T00:00:00+00:00",
                "registered_wrapper_reaped": True,
                "native_process_group_drained": True,
                "owned_artifacts_absent": {
                    "run_dir": True,
                    "control_socket": True,
                    "zdotdir_parent": True,
                },
            },
        }
        with mock.patch.object(
            supervisor, "request_halt", return_value=supervisor.ControlResult(ok=False, error="connection refused")
        ):
            with mock.patch("os.killpg", side_effect=AssertionError("authenticated halt must not killpg")):
                self.adapter.halt(HANDLE, observed)  # no raise: complete reap proof confirms

    def test_authenticated_halt_unconfirmed_raises_without_signal(self) -> None:
        observed = {"control_socket": SOCKET, "run_token": TOKEN}
        with mock.patch.object(
            supervisor, "request_halt", return_value=supervisor.ControlResult(ok=False, error="no response")
        ):
            with mock.patch("os.killpg", side_effect=AssertionError("authenticated halt must not killpg")):
                with self.assertRaises(SupervisorUnreachable) as ctx:
                    self.adapter.halt(HANDLE, observed)
        self.assertIn("termination not confirmed", str(ctx.exception))

    def test_authenticated_halt_stale_exit_evidence_does_not_confirm(self) -> None:
        # A failed socket HALT plus STALE exit evidence (older run's token) must
        # NOT confirm: same-run predicate applies to the confirmation path too.
        observed = {
            "control_socket": SOCKET,
            "run_token": TOKEN,
            "worker_exit": {"returncode": 0, "run_token": "b" * 32},
        }
        with mock.patch.object(
            supervisor, "request_halt", return_value=supervisor.ControlResult(ok=False, error="connection refused")
        ):
            with mock.patch("os.killpg", side_effect=AssertionError("authenticated halt must not killpg")):
                with self.assertRaises(SupervisorUnreachable) as ctx:
                    self.adapter.halt(HANDLE, observed)
        self.assertIn("termination not confirmed", str(ctx.exception))

    def test_authenticated_halt_malformed_exit_evidence_does_not_confirm(self) -> None:
        observed = {
            "control_socket": SOCKET,
            "run_token": TOKEN,
            "reaper_exit": {"returncode": 0, "run_token": 42},
        }
        with mock.patch.object(
            supervisor, "request_halt", return_value=supervisor.ControlResult(ok=False, error="connection refused")
        ):
            with self.assertRaises(SupervisorUnreachable):
                self.adapter.halt(HANDLE, observed)

    def test_authenticated_halt_never_parses_pid(self) -> None:
        # A handle with no parseable PID must still work through the socket path.
        observed = {"control_socket": SOCKET, "run_token": TOKEN}
        with mock.patch.object(
            supervisor, "request_halt", return_value=supervisor.ControlResult(ok=True, state="halted")
        ):
            self.adapter.halt("fake:alpha-fake-worker:not-a-pid", observed)

    def test_no_control_identity_raises_unreachable_without_signal(self) -> None:
        # Missing supervisor identity is unreachable/unconfirmed and
        # stage-2/manual: there is NO handle-parsed PID / killpg fallback.
        with mock.patch.object(supervisor, "request_halt", side_effect=AssertionError("no socket, no signal")):
            with mock.patch("os.killpg", side_effect=AssertionError("no control identity must not killpg")):
                with self.assertRaises(SupervisorUnreachable) as ctx:
                    self.adapter.halt(HANDLE, None)
        self.assertIn("termination not confirmed", str(ctx.exception))


class NoPsUsageTest(unittest.TestCase):
    def test_base_adapter_never_shells_out_to_ps(self) -> None:
        self.assertIsNone(re.search(r"['\"]ps['\"]", BASE_SOURCE), "adapter must not invoke ps")
        self.assertNotIn("/bin/ps", BASE_SOURCE)

    def test_base_adapter_has_no_pid_signalling_at_all(self) -> None:
        # Categorical Spec C: the legacy PID-parsed killpg path is gone entirely;
        # NO signalling primitive remains in the adapter base.
        self.assertNotIn("killpg(", BASE_SOURCE)
        self.assertNotIn("os.kill(", BASE_SOURCE)
        self.assertNotIn("_legacy_halt", BASE_SOURCE)

    def test_authenticated_halt_path_uses_socket_request(self) -> None:
        auth = BASE_SOURCE.split("def _authenticated_halt", 1)[1].split("\n    def ", 1)[0]
        self.assertNotIn("killpg(", auth)
        self.assertNotIn("os.kill(", auth)
        self.assertIn("request_halt", auth)


if __name__ == "__main__":
    unittest.main()
