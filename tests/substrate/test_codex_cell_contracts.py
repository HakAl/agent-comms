"""Runtime-free checks for the shared message-only cell evidence contract."""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import copy
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

import tests.dispatch_cell_harness as cell_harness
from tests.dispatch_cell_harness import ARCHITECT_ID, DispatchCellHarness


class FileBackedCellContractTest(unittest.TestCase):
    def setUp(self):
        self.harness = object.__new__(DispatchCellHarness)
        self.harness.worker_id = "worker"
        self.harness.store = mock.Mock()
        self.fixture = b"exact\r\nartifact bytes\n"
        self.trigger = {"id": "trigger", "status": "closed"}
        self.reply = {
            "id": "reply",
            "from": "worker",
            "parent_message_id": "trigger",
            "body_storage": "artifact",
        }
        self.triggers = [self.trigger]
        self.replies = [self.reply]
        self.harness.store.list_inbox.side_effect = lambda actor, **kw: (
            self.replies if actor == ARCHITECT_ID else self.triggers
        )
        self.harness.store.read_message.return_value = {"body": self.fixture.decode()}
        self.dispatch = {
            "message_id": "trigger",
            "status": "closed",
            "policy_version": "v2",
            "result": "satisfied",
            "observed_values": {
                "closeout": {
                    "protocol": 1,
                    "reply_message_id": "reply",
                    "delta": None,
                }
            },
        }

    def check(self):
        return self.harness.assert_closed_with_file_backed_reply(
            self.dispatch, self.fixture
        )

    def test_accepts_exact_artifact_without_delta(self):
        self.assertEqual(self.check(), (self.reply, []))
        self.harness.store.read_message.assert_called_once_with(ARCHITECT_ID, "reply")

    def test_rejects_altered_bytes(self):
        self.harness.store.read_message.return_value = {
            "body": "exact\nartifact bytes\n"
        }
        with self.assertRaises(AssertionError):
            self.check()

    def test_rejects_invalid_closeout(self):
        original = copy.deepcopy(self.dispatch)
        for key, value in (
            ("protocol", 2),
            ("reply_message_id", "other"),
            ("delta", {}),
            ("delta", {"entries": ["file"]}),
        ):
            with self.subTest(key=key, value=value):
                self.dispatch = copy.deepcopy(original)
                self.dispatch["observed_values"]["closeout"][key] = value
                with self.assertRaises(AssertionError):
                    self.check()
        for key, value in (
            ("status", "in_flight"),
            ("result", "blocked"),
            ("policy_version", "v1"),
        ):
            with self.subTest(key=key):
                self.dispatch = copy.deepcopy(original)
                self.dispatch[key] = value
                with self.assertRaises(AssertionError):
                    self.check()

    def test_rejects_invalid_message_bindings(self):
        for key, value in (
            ("from", "other"),
            ("parent_message_id", "other"),
            ("body_storage", "inline"),
        ):
            with self.subTest(key=key):
                original = self.reply[key]
                self.reply[key] = value
                with self.assertRaises(AssertionError):
                    self.check()
                self.reply[key] = original
        self.replies.append(dict(self.reply, id="duplicate"))
        with self.assertRaises(AssertionError):
            self.check()

    def test_rejects_missing_duplicate_or_open_trigger(self):
        for triggers in (
            [],
            [self.trigger, self.trigger],
            [dict(self.trigger, status="read")],
        ):
            with self.subTest(triggers=triggers):
                self.triggers = triggers
                with self.assertRaises(AssertionError):
                    self.check()

    def test_returns_other_parented_replies_for_callers_to_reject(self):
        other = dict(self.reply, id="inline", body_storage="inline")
        self.replies.append(other)
        self.assertEqual(self.check(), (self.reply, [other]))

    def test_generic_edit_assertion_still_requires_delta(self):
        with self.assertRaisesRegex(AssertionError, "no delta snapshot"):
            self.harness.assert_closed_with_parented_reply(self.dispatch)


INVOCATION = "/usr/bin/python3 /work/probe_1.py"


def _item(kind: str, item_type: str, **fields) -> dict:
    return {"type": f"item.{kind}", "item": {"id": "item_1", "type": item_type, **fields}}


def _started(command: str, item_id: str = "item_0") -> dict:
    event = _command(command, kind="started", exit_code=None, status="in_progress")
    event["item"]["id"] = item_id
    return event


def _command(command: str, *, kind: str = "completed", output: str = "", exit_code=0, status="completed") -> dict:
    return _item(
        kind, "command_execution", command=command, aggregated_output=output,
        exit_code=exit_code, status=status,
    )


CLOSE_CALL = _item(
    "completed", "mcp_tool_call", server="agent-comms", tool="close_dispatch", status="completed",
    arguments={"message_id": "trigger", "reply_message_id": "reply", "result": "blocked"},
)


class BlockedDispatchFixture(unittest.TestCase):
    """A closed, blocked dispatch with one bound reply and an event log that
    holds only the close call."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.events_path = Path(temp.name) / "events.jsonl"
        self.harness = object.__new__(DispatchCellHarness)
        self.harness.worker_id = "worker"
        self.harness.store = mock.Mock()
        self.trigger = {"id": "trigger", "status": "closed"}
        self.reply = {"id": "reply", "from": "worker", "parent_message_id": "trigger"}
        self.triggers = [self.trigger]
        self.replies = [self.reply]
        self.harness.store.list_inbox.side_effect = lambda actor, **kw: (
            self.replies if actor == ARCHITECT_ID else self.triggers
        )
        self.dispatch = {
            "dispatch_id": "d1",
            "message_id": "trigger",
            "status": "closed",
            "result": "blocked",
            "policy_version": "v2",
            "observed_values": {
                "worker_events": str(self.events_path),
                "closeout": {"protocol": 1, "reply_message_id": "reply", "result": "blocked"},
            },
        }
        self.host_ok = True
        self.write_events([CLOSE_CALL])

    def write_events(self, events, tail: str = "") -> None:
        self.events_path.write_text("".join(json.dumps(event) + "\n" for event in events) + tail)

    def host_check(self) -> None:
        if not self.host_ok:
            raise AssertionError("the probe left something on the host")


class ProbeAttemptClassificationTest(BlockedDispatchFixture):
    """A blocked dispatch is retried only when it is shown to be a refusal
    made before the probe ran; everything unclear stops the cell."""

    def classify(self, log_matches=None) -> str:
        attempt = cell_harness.ProbeAttempt(
            body="b", matches=cell_harness.command_matcher(INVOCATION), host_check=self.host_check,
            log_matches=log_matches,
        )
        return self.harness.classify_probe_attempt(self.dispatch, attempt)[0]

    def test_a_bound_blocked_close_with_no_probe_command_is_a_decline(self):
        self.assertEqual(self.classify(), cell_harness.DECLINED)

    def test_other_shell_activity_does_not_make_it_an_attempt(self):
        self.write_events([_command("/bin/zsh -c 'ls -l probe_1.py && cat /work/probe_1.py'"), CLOSE_CALL])
        self.assertEqual(self.classify(), cell_harness.DECLINED)

    def test_a_blocked_close_after_the_probe_ran_is_an_attempt(self):
        self.write_events([_command(f"/bin/zsh -c '{INVOCATION}'"), CLOSE_CALL])
        self.assertEqual(self.classify(), cell_harness.ATTEMPTED)

    def test_a_probe_that_only_started_is_an_attempt(self):
        self.write_events([_command(INVOCATION, kind="started", exit_code=None, status="in_progress"), CLOSE_CALL])
        self.assertEqual(self.classify(), cell_harness.ATTEMPTED)

    def test_a_satisfied_close_goes_to_the_cell_checks(self):
        self.dispatch["result"] = "satisfied"
        self.assertEqual(self.classify(), cell_harness.ATTEMPTED)

    def test_a_blocked_close_after_a_refusal_in_the_worker_log_is_an_attempt(self):
        # Codex emits no command item for a command a hook blocks; the
        # runtime's own log is where the attempt shows.
        log = self.events_path.with_name("worker.log")
        log.write_text(HookRefusalLogTest.LINE + "\n")
        self.dispatch["observed_values"]["worker_log"] = str(log)
        matcher = cell_harness.hook_refusal_matcher
        self.assertEqual(self.classify(), cell_harness.DECLINED)
        self.assertEqual(self.classify(matcher(HookRefusalLogTest.PUSH)), cell_harness.ATTEMPTED)
        self.assertEqual(self.classify(matcher("git push other HEAD")), cell_harness.DECLINED)

    def test_unclear_evidence_is_inconclusive(self):
        def missing_events():
            self.events_path.unlink()

        def truncated_events():
            self.write_events([CLOSE_CALL], tail='{"type":"item.started","item":{"id"')

        def no_close_call():
            self.write_events([_command("ls")])

        def parseable_prefix():
            # Every line parses, but a command that started never completed.
            self.write_events([_started("ls"), CLOSE_CALL])

        def close_call_for_another_trigger():
            call = copy.deepcopy(CLOSE_CALL)
            call["item"]["arguments"]["message_id"] = "another"
            self.write_events([call])

        def close_call_for_another_reply():
            call = copy.deepcopy(CLOSE_CALL)
            call["item"]["arguments"]["reply_message_id"] = "another"
            self.write_events([call])

        def unbound_reply():
            self.dispatch["observed_values"]["closeout"]["reply_message_id"] = "other"

        def two_replies():
            self.replies.append(dict(self.reply, id="second"))

        def reply_from_another_actor():
            self.reply["from"] = "someone"

        def open_trigger():
            self.trigger["status"] = "read"

        def host_touched():
            self.host_ok = False

        def dlq():
            self.dispatch["status"] = "dlq"

        def no_events_path():
            del self.dispatch["observed_values"]["worker_events"]

        for case in (
            missing_events, truncated_events, parseable_prefix, close_call_for_another_trigger,
            close_call_for_another_reply, no_close_call, unbound_reply, two_replies,
            reply_from_another_actor, open_trigger, host_touched, dlq, no_events_path,
        ):
            with self.subTest(case=case.__name__):
                self.setUp()
                case()
                self.assertEqual(self.classify(), cell_harness.INCONCLUSIVE)


class TimedOutAfterProbeTest(BlockedDispatchFixture):
    """A cell whose measurement the host takes while the probe runs may be
    judged on a dispatch that the TTL ended after the probe completed, and
    only then: the close is not what such a cell shows."""

    def setUp(self):
        super().setUp()
        self.dispatch.update(status="dlq", result=None, failure_reason="timeout")
        self.dispatch["observed_values"].pop("closeout")
        self.write_events([_command(f"/bin/zsh -c '{INVOCATION}'"), _started("send", item_id="item_9")])

    def classify(self, *, complete_without_close: bool = True) -> tuple[str, str]:
        attempt = cell_harness.ProbeAttempt(
            body="b", matches=cell_harness.command_matcher(INVOCATION), host_check=self.host_check,
            complete_without_close=complete_without_close,
        )
        return self.harness.classify_probe_attempt(self.dispatch, attempt)

    def test_a_timeout_after_the_probe_completed_is_an_attempt(self):
        outcome, why = self.classify()
        self.assertEqual(outcome, cell_harness.ATTEMPTED)
        self.assertIn("timed out after the probe completed", why)

    def test_a_cell_that_did_not_ask_for_it_stays_inconclusive(self):
        self.assertEqual(self.classify(complete_without_close=False)[0], cell_harness.INCONCLUSIVE)

    def test_anything_short_of_a_completed_probe_is_inconclusive(self):
        def probe_only_started():
            self.write_events([_command(INVOCATION, kind="started", exit_code=None, status="in_progress")])

        def probe_absent():
            self.write_events([_command("ls")])

        def another_failure():
            self.dispatch["failure_reason"] = "worker_exited_before_close"

        def no_failure_reason():
            del self.dispatch["failure_reason"]

        def another_status():
            self.dispatch["status"] = "spawn_failed_message_landed"

        def missing_events():
            self.events_path.unlink()

        def truncated_events():
            self.write_events([_command(INVOCATION)], tail='{"type":"item.started","item":{"id"')

        for case in (
            probe_only_started, probe_absent, another_failure, no_failure_reason, another_status,
            missing_events, truncated_events,
        ):
            with self.subTest(case=case.__name__):
                self.setUp()
                case()
                self.assertEqual(self.classify()[0], cell_harness.INCONCLUSIVE)

    def test_the_close_check_passes_a_timeout_and_nothing_else_unclosed(self):
        self.harness.evidence_secrets = ()
        with mock.patch("builtins.print") as printed:
            self.harness.assert_closed_or_timed_out_after_probe(self.dispatch)
        self.assertIn("timeout", printed.call_args.args[0])
        self.dispatch["failure_reason"] = "worker_exited_before_close"
        with self.assertRaisesRegex(AssertionError, "neither closed nor timed out"):
            self.harness.assert_closed_or_timed_out_after_probe(self.dispatch)

    def test_the_close_check_holds_a_closed_dispatch_to_the_positive_close(self):
        self.harness.evidence_secrets = ()
        self.dispatch.update(status="closed", result="blocked")
        self.dispatch["observed_values"]["closeout"] = {
            "protocol": 1, "reply_message_id": "reply", "result": "blocked",
        }
        with self.assertRaisesRegex(AssertionError, "did not record satisfied"):
            self.harness.assert_closed_or_timed_out_after_probe(self.dispatch)


class CommandShellsTest(unittest.TestCase):
    """The shells a worker's commands ran under are read from every command
    that started, so one the TTL ended before its completion still counts."""

    HOLD = _command(f"/bin/bash -c '{INVOCATION}'")

    def flags(self, events) -> list:
        return [flags for _item, flags in cell_harness.command_shells(events)]

    def test_a_login_shell_that_only_started_is_seen(self):
        late = _started("/bin/bash -lc 'sleep 60'", item_id="item_7")
        self.assertEqual(self.flags([self.HOLD, late]), [("bash", "c"), ("bash", "lc")])

    def test_a_command_started_and_completed_is_one_command(self):
        started = _started("/bin/bash -c 'ls'", item_id="item_1")
        self.assertEqual(self.flags([started, _command("/bin/bash -c 'ls'")]), [("bash", "c")])

    def test_an_unfinished_call_that_is_not_a_command_is_not_a_shell(self):
        call = _item("started", "mcp_tool_call", server="agent-comms", tool="close_dispatch", status="in_progress")
        self.assertEqual(self.flags([self.HOLD, call]), [("bash", "c")])

    def test_a_command_under_no_shell_has_no_flags(self):
        self.assertEqual(self.flags([_command(INVOCATION)]), [None])


class DenialCloseTest(BlockedDispatchFixture):
    """A denial cell's dispatch may close satisfied or blocked, but always
    with one reply bound by a protocol 1 closeout to the closed trigger."""

    def check(self) -> None:
        DispatchCellHarness.assert_denial_closed_with_parented_reply.__wrapped__(self.harness, self.dispatch)

    def test_a_blocked_or_satisfied_close_without_a_delta_is_accepted(self):
        for result in ("blocked", "satisfied"):
            with self.subTest(result=result):
                self.dispatch["result"] = result
                self.check()

    def test_a_close_that_is_not_bound_is_refused(self):
        def unbound_reply():
            self.dispatch["observed_values"]["closeout"]["reply_message_id"] = "other"

        def two_replies():
            self.replies.append(dict(self.reply, id="second"))

        def open_trigger():
            self.trigger["status"] = "read"

        def dlq():
            self.dispatch["status"] = "dlq"

        def no_closeout():
            del self.dispatch["observed_values"]["closeout"]

        def failed_result():
            self.dispatch["result"] = "failed"

        for case in (unbound_reply, two_replies, open_trigger, dlq, no_closeout, failed_result):
            with self.subTest(case=case.__name__):
                self.setUp()
                case()
                with self.assertRaises(AssertionError):
                    self.check()


class DenialProbeTest(unittest.TestCase):
    """A denial cell dispatches once: the probe was attempted, or the worker
    verifiably declined it and left the host as it was."""

    def setUp(self):
        self.harness = object.__new__(DispatchCellHarness)
        self.harness.evidence_secrets = ()
        self.calls: list[dict] = []
        self.outcome = cell_harness.ATTEMPTED
        self.settled = True
        self.harness.dispatch_and_wait = lambda **kwargs: (
            self.calls.append(kwargs) or {"dispatch_id": "d1", "status": "closed"}
        )
        self.harness.settle_dispatch = lambda dispatch: self.settled
        self.harness.classify_probe_attempt = lambda dispatch, attempt: (self.outcome, "detail")

    def run_cell(self):
        return self.harness.dispatch_denial_probe(
            idempotency_key="k", ttl_seconds=1, timeout_seconds=2,
            attempt=cell_harness.ProbeAttempt(body="body", matches=lambda item: False, host_check=lambda: None),
        )

    def test_an_attempted_probe_goes_to_the_cell_checks(self):
        dispatch, outcome = self.run_cell()
        self.assertEqual((dispatch["dispatch_id"], outcome), ("d1", cell_harness.ATTEMPTED))
        self.assertEqual([(call["idempotency_key"], call["body"]) for call in self.calls], [("k", "body")])

    def test_a_verified_decline_is_returned_and_not_retried(self):
        self.outcome = cell_harness.DECLINED
        with mock.patch("builtins.print") as printed:
            dispatch, outcome = self.run_cell()
        self.assertEqual(outcome, cell_harness.DECLINED)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("DECLINED k: detail", printed.call_args.args[0])

    def test_a_decline_from_a_worker_not_confirmed_gone_fails(self):
        self.outcome = cell_harness.DECLINED
        self.settled = False
        with self.assertRaisesRegex(AssertionError, "exit was not confirmed"):
            self.run_cell()

    def test_inconclusive_evidence_fails(self):
        self.outcome = cell_harness.INCONCLUSIVE
        with self.assertRaisesRegex(AssertionError, "inconclusive"):
            self.run_cell()


class HookRefusalLogTest(unittest.TestCase):
    PUSH = "git push hook-probe-remote.git HEAD:refs/heads/hook-probe"
    LINE = (
        "2026-11-22T12:28:34.544472Z ERROR codex_core::tools::router: error=Command blocked by "
        "PreToolUse hook: worker_dispatch_readwrite_bounded forbids network/external write commands. "
        "Command: " + PUSH
    )

    def lines(self, text: str | None, command: str = PUSH) -> list[str]:
        with tempfile.TemporaryDirectory() as temp:
            observed = {}
            if text is not None:
                log = Path(temp) / "worker.log"
                log.write_text(text)
                observed["worker_log"] = str(log)
            return cell_harness.hook_refusal_lines({"observed_values": observed}, command)

    def test_the_hook_s_refusal_of_the_command_is_found(self):
        self.assertEqual(self.lines("Reading additional input from stdin...\n" + self.LINE + "\n"), [self.LINE])

    def test_anything_else_is_not_a_refusal_of_the_command(self):
        for text in (
            None,
            "",
            self.LINE.replace(self.PUSH, "git push elsewhere.git HEAD:refs/heads/hook-probe"),
            self.LINE + "-2",
            self.LINE.replace("Command blocked by PreToolUse hook", "Command failed"),
            self.LINE.replace("forbids network/external write commands", "some other reason"),
            "I will not run " + self.PUSH,
            # Other entries that hold the expected text are not the router's record.
            self.LINE.replace("ERROR codex_core::tools::router: error=", "INFO codex_core::exec: output="),
            "note: " + self.LINE,
            self.LINE.replace("Z ERROR", " ERROR"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.lines(text), [])


class EvidenceLocationTest(unittest.TestCase):
    def test_logs_must_exist_outside_the_worker_s_writable_root(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            logs = Path(temp) / "home" / "logs"
            project.mkdir()
            logs.mkdir(parents=True)
            for name in ("w.log", "w.events.jsonl"):
                (logs / name).write_text("")
                (project / name).write_text("")
            outside = {"worker_log": str(logs / "w.log"), "worker_events": str(logs / "w.events.jsonl")}
            cell_harness.assert_evidence_outside({"observed_values": outside}, project)
            for bad in (
                dict(outside, worker_log=str(project / "w.log")),
                dict(outside, worker_events=str(project / "w.events.jsonl")),
                dict(outside, worker_log=str(logs / "missing.log")),
                {"worker_events": outside["worker_events"]},
            ):
                with self.subTest(bad=bad), self.assertRaises(AssertionError):
                    cell_harness.assert_evidence_outside({"observed_values": bad}, project)

    def test_unfinished_items_are_named(self):
        started = _started("ls")
        completed = _command("ls")
        completed["item"]["id"] = "item_0"
        self.assertEqual(cell_harness.unfinished_items([started, CLOSE_CALL]), ["item_0"])
        self.assertEqual(cell_harness.unfinished_items([started, completed, CLOSE_CALL]), [])


class SupervisorListenerTest(unittest.TestCase):
    """The check the supervisor root cell makes on the fixture's listener,
    on a listener set up the way the fixture sets its own up."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "s"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.path))
        listener.listen(8)
        listener.setblocking(False)
        self.probe = {
            "listener": listener,
            "listener_socket": self.path,
            "listener_identity": cell_harness._path_identity(self.path),
        }

    def test_an_idle_listener_passes_and_stays_checkable(self):
        cell_harness.assert_supervisor_listener_unreached(self.probe)
        cell_harness.assert_supervisor_listener_unreached(self.probe)

    def test_a_connection_that_reached_the_listener_fails(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(self.path))
        with self.assertRaisesRegex(AssertionError, "reached the protected listener"):
            cell_harness.assert_supervisor_listener_unreached(self.probe)

    def test_a_removed_or_replaced_socket_fails(self):
        self.path.unlink()
        with self.assertRaisesRegex(AssertionError, "gone"):
            cell_harness.assert_supervisor_listener_unreached(self.probe)
        self.path.write_text("")
        with self.assertRaisesRegex(AssertionError, "replaced"):
            cell_harness.assert_supervisor_listener_unreached(self.probe)


class CommandEnvironmentObserverTest(unittest.TestCase):
    """The environment of a worker command is read from its process by the
    host, so the answer does not pass through the worker."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.script = Path(temp.name) / "hold.py"
        self.receipt = Path(temp.name) / "receipt.json"
        self.script.write_text(cell_harness.hold_probe_source(self.receipt, nonce="n1", seconds=3))
        self.invocation = f"{sys.executable} {self.script}"

    def hold(self, **env: str) -> subprocess.Popen:
        full = {"PATH": "/usr/bin:/bin", "AGENT_COMMS_DISPATCH_ID": "d1", **env}
        child = subprocess.Popen([sys.executable, str(self.script)], env=full, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def observe(self, **env: str) -> dict:
        observer = self.observer = cell_harness.CommandEnvironmentObserver(self.invocation, timeout_seconds=20)
        observer.start()
        self.addCleanup(observer.stop)
        child = self.child = self.hold(**env)
        self.assertEqual(json.loads(child.stdout.readline()), {"nonce": "n1"})
        snapshot = observer.wait(10)
        if snapshot is None:
            self.skipTest("this host does not show a child process's environment")
        self.assertEqual(snapshot["pid"], child.pid)
        return snapshot

    def test_the_hold_script_leaves_a_receipt_and_holds(self):
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(self.script)], capture_output=True, text=True, check=True)
        self.assertGreaterEqual(time.monotonic() - started, 3)
        self.assertEqual(json.loads(done.stdout), {"nonce": "n1"})
        self.assertEqual(json.loads(self.receipt.read_text()), {"nonce": "n1"})

    def test_a_second_launch_of_the_hold_script_fails_before_it_prints(self):
        # The receipt is created exclusively: of several launches only the
        # first can print the nonce and end with exit 0.
        first = self.hold()
        self.assertEqual(json.loads(first.stdout.readline()), {"nonce": "n1"})
        second = subprocess.run([sys.executable, str(self.script)], capture_output=True, text=True, check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        self.assertIn("FileExistsError", second.stderr)

    def test_the_sole_launch_seen_is_the_one_sampled(self):
        snapshot = self.observe()
        observer = self.observer
        self.assertEqual(observer.launches(), [snapshot["pid"]])
        self.assertIs(cell_harness.sole_observed_launch(observer), snapshot)

    def test_a_wrapped_launch_before_the_standalone_one_is_refused(self):
        # The events of this pair pass single_successful_run: the wrapped
        # launch is no launch to the event matcher. The host saw two
        # processes with the command's argv, so the sample is not evidence.
        snapshot = self.observe()
        self.child.kill()
        self.assertNotEqual(self.child.wait(), 0)
        self.receipt.unlink()
        later = self.hold()
        self.assertEqual(json.loads(later.stdout.readline()), {"nonce": "n1"})
        cell_harness.wait_until(lambda: len(self.observer.launches()) == 2, timeout_seconds=10)
        self.assertEqual(self.observer.launches(), sorted([snapshot["pid"], later.pid]))
        with self.assertRaisesRegex(AssertionError, "2 processes ran the command"):
            cell_harness.sole_observed_launch(self.observer)

    def test_no_launch_seen_is_refused(self):
        observer = cell_harness.CommandEnvironmentObserver(self.invocation, timeout_seconds=20)
        with self.assertRaisesRegex(AssertionError, "never seen"):
            cell_harness.sole_observed_launch(observer)

    def test_the_command_s_environment_and_ancestry_are_seen(self):
        snapshot = self.observe(FIXTURE_PLACEHOLDER="set")
        self.assertEqual(snapshot["environment"].get("FIXTURE_PLACEHOLDER"), "set")
        self.assertNotIn("FIXTURE_ABSENT", snapshot["environment"])
        self.assertIn(os.getpid(), snapshot["ancestors"])

    def test_a_command_that_only_names_the_script_is_not_observed(self):
        observer = cell_harness.CommandEnvironmentObserver(self.invocation, timeout_seconds=20)
        observer.start()
        self.addCleanup(observer.stop)
        other = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)", self.invocation], stdout=subprocess.DEVNULL
        )
        self.addCleanup(other.wait)
        self.assertIsNone(observer.wait(1.5))

    def test_the_observation_is_bound_to_the_dispatch(self):
        snapshot = self.observe()
        no_such_pid = 2**31 - 1
        dispatch = {"dispatch_id": "d1", "observed_values": {"pid": no_such_pid, "child_pid": os.getpid()}}
        environment = cell_harness.observed_command_environment(snapshot, dispatch)
        self.assertEqual(environment["AGENT_COMMS_DISPATCH_ID"], "d1")
        for bad, why in (
            (dict(dispatch, dispatch_id="d2"), "another dispatch"),
            (dict(dispatch, observed_values={"pid": no_such_pid, "child_pid": no_such_pid - 1}), "not a descendant"),
            (dict(dispatch, observed_values={}), "not a descendant"),
        ):
            with self.subTest(why=why), self.assertRaisesRegex(AssertionError, why):
                cell_harness.observed_command_environment(snapshot, bad)
        with self.assertRaisesRegex(AssertionError, "never seen"):
            cell_harness.observed_command_environment(None, dispatch)

    def test_an_environment_without_the_dispatch_id_is_not_evidence(self):
        # A host that hides environments would show nothing, and nothing
        # must not read as "the credentials are absent".
        snapshot = {"pid": 5, "ancestors": [os.getpid()], "environment": {}}
        dispatch = {"dispatch_id": "d1", "observed_values": {"child_pid": os.getpid()}}
        with self.assertRaisesRegex(AssertionError, "another dispatch"):
            cell_harness.observed_command_environment(snapshot, dispatch)


class SingleRunTest(unittest.TestCase):
    """The host's sample of a command's process counts only when the
    dispatch launched that command once and that launch succeeded."""

    def run_events(self, item_id: str, *, exit_code=0, output='{"nonce": "n1"}', command=None) -> list[dict]:
        command = command or f"/bin/zsh -c '{INVOCATION}'"
        completed = _command(command, exit_code=exit_code, output=output)
        completed["item"]["id"] = item_id
        return [_started(command, item_id), completed]

    def check(self, events) -> dict:
        return cell_harness.single_successful_run(events, INVOCATION, nonce="n1")

    def test_one_successful_launch_is_returned(self):
        events = [_started("ls", "item_0"), *self.run_events("item_3"), CLOSE_CALL]
        self.assertEqual(self.check(events)["id"], "item_3")

    def test_a_failed_launch_and_a_successful_retry_do_not_add_up(self):
        events = [*self.run_events("item_3", exit_code=1, output=""), *self.run_events("item_4"), CLOSE_CALL]
        with self.assertRaisesRegex(AssertionError, "not launched exactly once: 2 started, 2 completed"):
            self.check(events)

    def test_what_is_not_one_successful_launch_is_refused(self):
        for name, events in (
            ("never launched", [CLOSE_CALL]),
            ("started only", [_started(INVOCATION, "item_3")]),
            ("failed", self.run_events("item_3", exit_code=1)),
            ("another nonce", self.run_events("item_3", output='{"nonce": "n2"}')),
            ("two starts", [_started(INVOCATION, "item_2"), *self.run_events("item_3")]),
            ("mismatched ids", [_started(INVOCATION, "item_2"), self.run_events("item_3")[1]]),
        ):
            with self.subTest(name=name), self.assertRaises(AssertionError):
                self.check(events)

    def test_the_bare_command_and_one_shell_wrapper_are_standalone(self):
        for command in (INVOCATION, f"/bin/zsh -c '{INVOCATION}'", f"bash -lc '{INVOCATION}'"):
            with self.subTest(command=command):
                self.assertEqual(self.check(self.run_events("item_3", command=command))["id"], "item_3")

    def test_a_wrapped_launch_is_not_counted_by_the_events(self):
        # Why the cells also require sole_observed_launch: a launch through
        # a wrapper is not a launch here, so a wrapped attempt followed by a
        # standalone success reads as one launch.
        wrapped = self.run_events("item_2", exit_code=124, output="", command=f"/bin/zsh -c 'timeout 1 {INVOCATION}'")
        events = [*wrapped, *self.run_events("item_3"), CLOSE_CALL]
        self.assertEqual(self.check(events)["id"], "item_3")

    def test_one_item_that_covers_more_than_the_command_is_refused(self):
        # One native item, exit 0 and the nonce printed, but the item's
        # status is another command's, or the probe ran more than once.
        for command in (
            f"/bin/zsh -c '{INVOCATION}; true'",
            f"/bin/zsh -c '{INVOCATION} || true'",
            f"/bin/zsh -c '{INVOCATION} && {INVOCATION}'",
            f"/bin/zsh -c 'cd /work && {INVOCATION}'",
            f"/bin/zsh -c '{INVOCATION} &'",
            f"/bin/zsh -c 'timeout 1 {INVOCATION}'",
            f"/bin/bash -lc \"zsh -c '{INVOCATION}'\"",
            f"/bin/zsh -c '{INVOCATION} extra'",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(AssertionError, "not launched exactly once|not run on its own"):
                    self.check(self.run_events("item_3", command=command))


class ValueAbsentTest(unittest.TestCase):
    def test_a_found_value_fails_without_showing_the_text(self):
        text = "HOME=/h OTHER_PRIVATE=second-synthetic-value LABEL=prefix sample-marker"
        cell_harness.assert_value_absent(text, "not-there", "the fixture value")
        with self.assertRaises(AssertionError) as caught:
            cell_harness.assert_value_absent(text, "sample-marker", "the fixture value")
        shown = "".join(traceback.format_exception(caught.exception))
        self.assertIn("the fixture value", shown)
        for private in ("second-synthetic-value", "OTHER_PRIVATE", "sample-marker", "/h"):
            self.assertNotIn(private, shown)


class PsEnvironmentTest(unittest.TestCase):
    def test_names_are_parsed_and_a_value_after_a_space_stays_in_the_text(self):
        text = "/usr/bin/python3 /work/hold.py LABEL=prefix sample-marker AGENT_COMMS_DISPATCH_ID=d1 EMPTY=\n"
        environment = cell_harness.parse_ps_environment(text)
        self.assertEqual(environment, {"LABEL": "prefix", "AGENT_COMMS_DISPATCH_ID": "d1", "EMPTY": ""})
        # The parsed value lost what follows the space; the text did not,
        # and the text is what a cell searches for a value.
        self.assertFalse(any("sample-marker" in value for value in environment.values()))
        self.assertIn("sample-marker", text)

    def test_the_observer_keeps_the_text_with_whole_values(self):
        # Registered first, so it runs last: the child writes its receipt into
        # this directory when it starts, and must be reaped before the
        # directory is removed (ac-03f).
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        temp = temp_dir.name
        script = Path(temp) / "hold.py"
        script.write_text(cell_harness.hold_probe_source(Path(temp) / "r.json", nonce="n1", seconds=3))
        observer = cell_harness.CommandEnvironmentObserver(f"{sys.executable} {script}", timeout_seconds=20)
        observer.start()
        self.addCleanup(observer.stop)
        child = subprocess.Popen(
            [sys.executable, str(script)], stdout=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LABEL": "prefix sample-marker", "AGENT_COMMS_DISPATCH_ID": "d1"},
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        snapshot = observer.wait(10)
        if snapshot is None:
            self.skipTest("this host does not show a child process's environment")
        self.assertIn("sample-marker", snapshot["environment_text"])
        self.assertEqual(snapshot["environment"]["AGENT_COMMS_DISPATCH_ID"], "d1")


class CommandMatcherTest(unittest.TestCase):
    def test_the_exact_invocation_matches_bare_or_under_a_shell(self):
        matches = cell_harness.command_matcher(INVOCATION)
        for command in (
            INVOCATION,
            f"/bin/zsh -c '{INVOCATION}'",
            f"bash -lc '{INVOCATION}'",
            f"/bin/bash -c 'cd /work && {INVOCATION}'",
            f"/bin/bash -c 'cd /work; {INVOCATION} && ls'",
            f"/bin/bash -lc \"zsh -c '{INVOCATION}'\"",
        ):
            with self.subTest(command=command):
                self.assertTrue(matches(_command(command)["item"]))

    def test_naming_the_script_is_not_running_it(self):
        matches = cell_harness.command_matcher(INVOCATION)
        for item in (
            _command("/bin/zsh -c 'ls -l probe_1.py && cat /work/probe_1.py'")["item"],
            _command("cat /work/probe_1.py")["item"],
            _command(f"/bin/zsh -c 'echo \"{INVOCATION}\"'")["item"],
            _command(f"/bin/zsh -c 'echo {INVOCATION}'")["item"],
            _command(f"grep -n '{INVOCATION}' notes.txt")["item"],
            _command("/usr/bin/python3 /work/probe_2.py")["item"],
            _command("/bin/zsh -c 'unterminated")["item"],
            _item("completed", "agent_message", text=f"I will not run {INVOCATION}")["item"],
        ):
            with self.subTest(item=item):
                self.assertFalse(matches(item))


class DispatchUntilAttemptedTest(unittest.TestCase):
    def setUp(self):
        self.harness = object.__new__(DispatchCellHarness)
        self.harness.evidence_secrets = ()
        self.calls: list[dict] = []
        self.classes: list[str] = []
        self.status = "closed"
        self.harness.dispatch_and_wait = lambda **kwargs: (
            self.calls.append(kwargs) or {"dispatch_id": f"d{len(self.calls)}", "status": self.status}
        )
        self.settled = True
        self.harness.settle_dispatch = lambda dispatch: self.settled
        self.harness.classify_probe_attempt = lambda dispatch, attempt: (
            self.classes[len(self.calls) - 1], "detail"
        )

    def run_cell(self):
        return self.harness.dispatch_until_attempted(
            idempotency_key="k",
            ttl_seconds=1,
            timeout_seconds=2,
            attempt_for=lambda number: cell_harness.ProbeAttempt(
                body=f"body {number}", matches=lambda item: False, host_check=lambda: None
            ),
        )

    def test_an_attempt_on_the_first_try_dispatches_once(self):
        self.classes = [cell_harness.ATTEMPTED]
        dispatch, attempt, declines = self.run_cell()
        self.assertEqual((dispatch["dispatch_id"], attempt.body, declines), ("d1", "body 1", []))
        self.assertEqual([call["idempotency_key"] for call in self.calls], ["k"])

    def test_declines_are_retried_with_fresh_keys_and_bodies(self):
        self.classes = [cell_harness.DECLINED, cell_harness.DECLINED, cell_harness.ATTEMPTED]
        dispatch, attempt, declines = self.run_cell()
        self.assertEqual(dispatch["dispatch_id"], "d3")
        self.assertEqual(attempt.body, "body 3")
        self.assertEqual([row["dispatch_id"] for row in declines], ["d1", "d2"])
        self.assertEqual([call["idempotency_key"] for call in self.calls], ["k", "k-retry-2", "k-retry-3"])
        self.assertEqual([call["body"] for call in self.calls], ["body 1", "body 2", "body 3"])
        self.assertEqual({(call["ttl_seconds"], call["timeout_seconds"]) for call in self.calls}, {(1, 2)})

    def test_a_probe_that_is_never_attempted_fails_the_cell(self):
        self.classes = [cell_harness.DECLINED] * 3
        with self.assertRaisesRegex(AssertionError, "declined the probe in all 3 attempts"):
            self.run_cell()
        self.assertEqual(len(self.calls), cell_harness.PROBE_ATTEMPTS)

    def test_a_refusal_from_a_worker_not_confirmed_gone_is_inconclusive(self):
        self.classes = [cell_harness.DECLINED]
        self.settled = False
        with self.assertRaisesRegex(AssertionError, "exit was not confirmed"):
            self.run_cell()
        self.assertEqual(len(self.calls), 1)

    def test_an_attempt_on_an_unclosed_dispatch_needs_the_worker_confirmed_gone(self):
        # The events of a dispatch the TTL ended are final only once its
        # worker is gone.
        self.classes = [cell_harness.ATTEMPTED]
        self.status = "dlq"
        self.assertEqual(self.run_cell()[0]["dispatch_id"], "d1")
        self.calls.clear()
        self.settled = False
        with self.assertRaisesRegex(AssertionError, "exit was not confirmed"):
            self.run_cell()
        self.assertEqual(len(self.calls), 1)

    def test_inconclusive_evidence_is_not_retried(self):
        self.classes = [cell_harness.INCONCLUSIVE]
        with self.assertRaisesRegex(AssertionError, "inconclusive"):
            self.run_cell()
        self.assertEqual(len(self.calls), 1)


class ProbeOutputBindingTest(unittest.TestCase):
    """A measurement counts only as the output of the probe's own successful run."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.events_path = Path(temp.name) / "events.jsonl"
        self.dispatch = {"dispatch_id": "d1", "observed_values": {"worker_events": str(self.events_path)}}
        self.payload = {"nonce": "n1", "dispatch_id": "d1", "present": {"A": False, "B": False}}

    def read(self, events):
        self.events_path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return cell_harness.probe_output(self.dispatch, INVOCATION, nonce="n1")

    def test_the_output_of_the_successful_invocation_is_returned(self):
        events = [_command(f"/bin/zsh -c '{INVOCATION}'", output=json.dumps(self.payload) + "\n")]
        self.assertEqual(self.read(events), self.payload)

    def test_other_output_around_the_result_line_is_tolerated(self):
        events = [_command(f"/bin/zsh -c 'cd /work && {INVOCATION} && ls'", output="x\n" + json.dumps(self.payload) + "\nprobe_1.py\n")]
        self.assertEqual(self.read(events), self.payload)

    def test_what_is_not_the_probe_s_own_successful_output_is_refused(self):
        good = json.dumps(self.payload)
        cases = {
            "only names the script": [_command("cat /work/probe_1.py", output=good)],
            "failed": [_command(INVOCATION, output=good, exit_code=1, status="failed")],
            "still running": [_command(INVOCATION, kind="started", output=good, exit_code=None, status="in_progress")],
            "not JSON": [_command(INVOCATION, output="PONG")],
            "not an object": [_command(INVOCATION, output="[1]")],
            "two result lines": [_command(INVOCATION, output=good + "\n" + good)],
            "wrong nonce": [_command(INVOCATION, output=json.dumps(dict(self.payload, nonce="old")))],
            "another dispatch": [_command(INVOCATION, output=json.dumps(dict(self.payload, dispatch_id="d0")))],
            "one good run and one bad": [
                _command(INVOCATION, output=good),
                _command(INVOCATION, output=json.dumps(dict(self.payload, nonce="old"))),
            ],
            "no events": [],
        }
        for name, events in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(AssertionError):
                    self.read(events)


class ProbeSourceTest(unittest.TestCase):
    def run_source(self, source: str, **env: str) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "probe.py"
            script.write_text(source.replace("RESULT", str(Path(temp) / "result.json")))
            done = subprocess.run(
                [sys.executable, str(script)], env={"PATH": "/usr/bin:/bin", **env},
                capture_output=True, text=True, check=True, timeout=30,
            )
            return json.loads(done.stdout), json.loads((Path(temp) / "result.json").read_text())

    def test_forbidden_write_probe_reports_a_refused_and_an_allowed_write(self):
        with tempfile.TemporaryDirectory() as temp:
            missing_parent = Path(temp) / "no-such-directory" / "x.txt"
            source = cell_harness.forbidden_write_probe_source(missing_parent, Path("RESULT"), nonce="n1")
            printed, written = self.run_source(source, AGENT_COMMS_DISPATCH_ID="d1")
            self.assertEqual(printed, written)
            self.assertEqual(printed["nonce"], "n1")
            self.assertEqual(printed["dispatch_id"], "d1")
            self.assertIs(printed["forbidden_write_succeeded"], False)
            self.assertTrue(printed["error"])
            self.assertEqual(printed["errno"], errno.ENOENT)
            allowed = Path(temp) / "x.txt"
            source = cell_harness.forbidden_write_probe_source(allowed, Path("RESULT"), nonce="n1")
            printed, _ = self.run_source(source)
            self.assertIs(printed["forbidden_write_succeeded"], True)
            self.assertIsNone(printed["error"])
            self.assertIsNone(printed["errno"])
            self.assertTrue(allowed.exists())
