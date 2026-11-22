"""Runtime-free checks for the shared message-only cell evidence contract."""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import copy
import errno
import json
import subprocess
import sys
import tempfile
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


def _command(command: str, *, kind: str = "completed", output: str = "", exit_code=0, status="completed") -> dict:
    return _item(
        kind, "command_execution", command=command, aggregated_output=output,
        exit_code=exit_code, status=status,
    )


CLOSE_CALL = _item("completed", "mcp_tool_call", server="agent-comms", tool="close_dispatch", status="completed")


class ProbeAttemptClassificationTest(unittest.TestCase):
    """A blocked dispatch is retried only when it is shown to be a refusal
    made before the probe ran; everything unclear stops the cell."""

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

    def classify(self) -> str:
        attempt = cell_harness.ProbeAttempt(
            body="b", matches=cell_harness.command_matcher(INVOCATION), host_check=self.host_check
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

    def test_unclear_evidence_is_inconclusive(self):
        def missing_events():
            self.events_path.unlink()

        def truncated_events():
            self.write_events([CLOSE_CALL], tail='{"type":"item.started","item":{"id"')

        def no_close_call():
            self.write_events([_command("ls")])

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
            missing_events, truncated_events, no_close_call, unbound_reply, two_replies,
            reply_from_another_actor, open_trigger, host_touched, dlq, no_events_path,
        ):
            with self.subTest(case=case.__name__):
                self.setUp()
                case()
                self.assertEqual(self.classify(), cell_harness.INCONCLUSIVE)


class CommandMatcherTest(unittest.TestCase):
    def test_the_exact_invocation_matches_bare_or_under_a_shell(self):
        matches = cell_harness.command_matcher(INVOCATION)
        for command in (
            INVOCATION,
            f"/bin/zsh -c '{INVOCATION}'",
            f"bash -lc '{INVOCATION}'",
            f"/bin/bash -c 'cd /work && {INVOCATION}'",
        ):
            with self.subTest(command=command):
                self.assertTrue(matches(_command(command)["item"]))

    def test_naming_the_script_is_not_running_it(self):
        matches = cell_harness.command_matcher(INVOCATION)
        for item in (
            _command("/bin/zsh -c 'ls -l probe_1.py && cat /work/probe_1.py'")["item"],
            _command("cat /work/probe_1.py")["item"],
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
        self.harness.dispatch_and_wait = lambda **kwargs: (
            self.calls.append(kwargs) or {"dispatch_id": f"d{len(self.calls)}", "status": "closed"}
        )
        self.harness.settle_dispatch = lambda dispatch: None
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

    def test_env_probe_reports_presence_only(self):
        names = ("TOKEN_A", "TOKEN_B")
        source = cell_harness.env_probe_source(Path("RESULT"), names, nonce="n1")
        printed, written = self.run_source(source, TOKEN_A="the-value", AGENT_COMMS_DISPATCH_ID="d1")
        self.assertEqual(printed, written)
        self.assertEqual(printed, {"nonce": "n1", "dispatch_id": "d1", "present": {"TOKEN_A": True, "TOKEN_B": False}})
        absent, _ = self.run_source(source)
        self.assertEqual(absent["present"], {"TOKEN_A": False, "TOKEN_B": False})

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
