"""xam-step1 criteria. JSON fixtures match exec_events.rs at Codex 0.153.4."""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import base64
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import codex_home, paths, supervisor
from agent_comms.adapters.codex import AuthStale, CodexAdapter
from agent_comms.adapters.claude import ClaudeAdapter
from agent_comms.adapters.fake import FakeAdapter
from agent_comms.adapters._base import SpawnFailed
from agent_comms.codex_auth_refresh import (
    access_token_remaining_seconds,
    codex_auth_snapshot,
    spawn_freshness_margin_seconds,
)
from agent_comms.policies import compile_policy
from agent_comms.schema import ValidationError
from agent_comms.spawn import render_spawn, rerender_spawns, worker_prompt_slot
from agent_comms.store import Store, WORKER_DISPATCH_POLICY, WORKER_DISPATCH_TTL_SECONDS
from agent_comms.worker_usage import parse_worker_usage
from agent_comms.monitor import enrich_worker_usage
from agent_comms.cli import run as cli_run
from agent_comms.cli.commands import dispatch_status, rerender_spawn
from tests.substrate.test_codex_auth_preflight import _codex_context, _fake_supervised
from tests.substrate import test_worker_usage as usage_fixture
from tests.substrate import test_adapter_supervised_dispatch as stream_fixture
from tests.substrate import test_spawn as spawn_fixture
from tests import dispatch_cell_harness as cell_fixture

NOW = datetime.now(timezone.utc).replace(microsecond=123456) - timedelta(seconds=1)


def token(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"synthetic.{body}.not-a-signature"


def auth_document():
    return {
        "last_refresh": NOW.isoformat(),
        "tokens": {
            "access_token": token({"exp": int(NOW.timestamp()) + 864000}),
            "refresh_token": "synthetic-refresh-secret",
            "id_token": "synthetic-id-secret",
        },
    }


class ScratchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)


class SnapshotTest(ScratchTest):
    def test_criterion_13_frozen_baseline_byte_format_admission_both_branches(self):
        # baseline codex.py and codex_home.py preflight_home_snapshot
        # both read_text then json.loads and classify every exception alike.
        def frozen(auth):
            try:
                json.loads(auth.read_text())
            except Exception:
                return "auth.json is not parseable JSON"
            return None

        for custody in (False, True):
            home = self.root / ("managed" if custody else "legacy")
            home.mkdir()
            (home / "config.toml").write_text("")
            (home / "alpha-codex-worker.config.toml").write_text("")
            auth = home / "auth.json"
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
                with self.subTest(custody=custody, encoding=encoding):
                    raw = json.dumps(auth_document()).encode(encoding)
                    auth.write_bytes(raw)
                    expected = frozen(auth)
                    self.assertEqual(expected is None, encoding == "utf-8")
                    with mock.patch.object(
                        Path, "read_bytes", return_value=raw
                    ) as read:
                        snapshot = codex_auth_snapshot(auth, now=NOW)
                    read.assert_called_once()
                    self.assertEqual(
                        snapshot["read_status"],
                        "ok" if expected is None else "not_json",
                    )
                    self.assertEqual(
                        snapshot["auth_digest"], hashlib.sha256(raw).hexdigest()
                    )
                    with (
                        mock.patch.dict(
                            os.environ,
                            {
                                "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(
                                    home if custody else self.root / "unused"
                                )
                            },
                        ),
                        mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
                    ):
                        if expected is None:
                            observed = CodexAdapter()._preflight(
                                _codex_context(self.root, home)
                            )
                            self.assertEqual(observed["codex_auth"], snapshot)
                        else:
                            with self.assertRaises(AuthStale) as raised:
                                CodexAdapter()._preflight(
                                    _codex_context(self.root, home)
                                )
                            self.assertEqual(
                                str(raised.exception),
                                str(CodexAdapter._auth_stale(home, expected)),
                            )
                    if custody:
                        ok, why, captured = codex_home.preflight_home_snapshot(
                            home, actor_id="alpha-codex-worker", now=NOW
                        )
                        self.assertEqual((ok, why), (expected is None, expected))
                        self.assertEqual(captured, snapshot)

    def test_criterion_11_snapshot_status_gaps_and_single_read(self):
        auth = self.root / "auth.json"
        self.assertEqual(codex_auth_snapshot(auth, now=NOW)["read_status"], "missing")
        self.assertEqual(
            codex_auth_snapshot(self.root, now=NOW)["read_status"], "not_regular"
        )
        auth.write_bytes(b"not JSON")
        snapshot = codex_auth_snapshot(auth, now=NOW)
        self.assertEqual(snapshot["read_status"], "not_json")
        self.assertEqual(
            snapshot["auth_digest"], hashlib.sha256(b"not JSON").hexdigest()
        )
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError("fixture")
        ) as read:
            self.assertEqual(
                codex_auth_snapshot(auth, now=NOW)["read_status"], "unreadable"
            )
            read.assert_called_once()
        values = [
            (None, "no_last_refresh"),
            ("", "no_last_refresh"),
            (0, "no_last_refresh"),
            (False, "no_last_refresh"),
            ([], "no_last_refresh"),
            ({}, "no_last_refresh"),
            (20260905, "naive_last_refresh"),
            (123, "bad_last_refresh"),
            ("2026-09-05", "naive_last_refresh"),
            (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), None),
            (NOW.isoformat(), None),
        ]
        for value, gap in values:
            with self.subTest(value=value):
                doc = auth_document()
                doc["last_refresh"] = value
                raw = json.dumps(doc).encode()
                auth.write_bytes(raw)
                with mock.patch.object(Path, "read_bytes", return_value=raw) as read:
                    snapshot = codex_auth_snapshot(auth, now=NOW)
                read.assert_called_once()
                self.assertEqual(snapshot["read_status"], "ok")
                self.assertEqual(snapshot["gaps"], [] if gap is None else [gap])
                self.assertEqual(snapshot["captured_at"], NOW.isoformat())
                self.assertEqual(
                    snapshot["auth_digest"], hashlib.sha256(raw).hexdigest()
                )
                self.assertEqual(snapshot["ttl_seconds"], 863999)
                if gap is None:
                    self.assertEqual(
                        datetime.fromisoformat(snapshot["created"]),
                        datetime.fromisoformat(str(value).replace("Z", "+00:00")),
                    )
        for doc, gaps in [
            ({}, ["no_last_refresh", "no_token"]),
            ({"last_refresh": NOW.isoformat()}, ["no_token"]),
            ({"last_refresh": NOW.isoformat(), "access_token": token({})}, ["no_exp"]),
        ]:
            auth.write_text(json.dumps(doc))
            snapshot = codex_auth_snapshot(auth, now=NOW)
            self.assertEqual(snapshot["gaps"], gaps)
            if "last_refresh" in doc:
                self.assertEqual(snapshot["created"], NOW.isoformat())

    def test_criterion_12_secrets_never_escape(self):
        auth = self.root / "auth.json"
        doc = auth_document()
        secrets = list(doc["tokens"].values())
        for value in [secrets[0], {"hidden": secrets}, NOW.isoformat()]:
            doc["last_refresh"] = value
            doc["unexpected"] = secrets
            auth.write_text(json.dumps(doc))
            observed = json.dumps(codex_auth_snapshot(auth, now=NOW))
            for secret in secrets:
                self.assertNotIn(secret, observed)

    def test_criterion_13_frozen_baseline_admission_both_branches(self):
        # Frozen classification: baseline codex.py:153-173 and
        # codex_home.py preflight_home_snapshot. Independent of snapshot helpers.
        def frozen(value, custody):
            if not value:
                return "auth.json last_refresh is missing"
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return (
                        "auth.json last_refresh is not timezone-aware"
                        if custody
                        else "auth.json last_refresh is unparseable"
                    )
            except ValueError:
                return "auth.json last_refresh is unparseable"
            if NOW - parsed.astimezone(timezone.utc) > timedelta(days=13):
                return "auth.json last_refresh is stale"
            return None

        for custody in (False, True):
            home = self.root / ("managed" if custody else "legacy")
            home.mkdir()
            (home / "config.toml").write_text("")
            (home / "alpha-codex-worker.config.toml").write_text("")
            values = [
                None,
                "",
                0,
                False,
                [],
                {},
                20260905,
                123,
                "2026-09-05",
                NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                NOW.isoformat(),
                "2020-01-01T00:00:00Z",
            ]
            for value in values:
                with self.subTest(custody=custody, value=value):
                    (home / "auth.json").write_text(json.dumps({"last_refresh": value}))
                    expected = frozen(value, custody)
                    with (
                        mock.patch.dict(
                            os.environ,
                            {
                                "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(
                                    home if custody else self.root / "unused"
                                )
                            },
                        ),
                        mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
                    ):
                        if expected is None:
                            self.assertEqual(
                                CodexAdapter()._preflight(
                                    _codex_context(self.root, home)
                                )["codex_auth"]["gaps"],
                                ["no_token"],
                            )
                        else:
                            with self.assertRaises(AuthStale) as raised:
                                CodexAdapter()._preflight(
                                    _codex_context(self.root, home)
                                )
                            self.assertEqual(
                                str(raised.exception),
                                str(CodexAdapter._auth_stale(home, expected)),
                            )
                    if custody:
                        ok, why = codex_home.preflight_home(
                            home, actor_id="alpha-codex-worker", now=NOW
                        )
                        self.assertEqual((ok, why), (expected is None, expected))
            (home / "auth.json").write_text(
                json.dumps({"last_refresh": NOW.isoformat(), "access_token": token({})})
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_COMMS_CODEX_CUSTODY_ROOT": str(
                            home if custody else self.root / "unused"
                        )
                    },
                ),
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
            ):
                self.assertEqual(
                    CodexAdapter()._preflight(_codex_context(self.root, home))[
                        "codex_auth"
                    ]["gaps"],
                    ["no_exp"],
                )

    def test_criterion_13_ledger_no_token_and_no_exp_stay_queued(self):
        home = self.root / "legacy"
        home.mkdir()
        store = Store(self.root / "ledger.sqlite")
        store.register_agent_actor(
            "architect", "alpha", "architect", str(self.root), []
        )
        for index, doc in enumerate(
            [
                {"last_refresh": NOW.isoformat()},
                {"last_refresh": NOW.isoformat(), "access_token": token({})},
            ]
        ):
            (home / "auth.json").write_text(json.dumps(doc))
            worker = f"worker-{index}"
            store.register_agent_actor(
                worker,
                "alpha",
                "worker",
                str(self.root),
                [],
                owner="architect",
                runtime="codex",
                spawn=render_spawn("codex", worker, codex_home=str(home)),
            )
            with (
                mock.patch("agent_comms.dispatch_ledger.require_fresh_module"),
                mock.patch.object(supervisor, "spawn_supervised") as spawn,
            ):
                row = store.dispatch_agent(
                    "architect",
                    worker,
                    str(index),
                    "Work",
                    "Body",
                    [],
                    adapter_for_runtime=lambda _: CodexAdapter(),
                )
            self.assertEqual(
                (row["status"], row["lineage_gate_status"]), ("queued", "token_stale")
            )
            spawn.assert_not_called()


class RerenderTest(ScratchTest):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "db.sqlite")
        self.store.register_agent_actor(
            "architect", "alpha", "architect", str(self.root), []
        )

    def actor(
        self,
        name="worker",
        *,
        baked=False,
        protected=False,
        spawn=None,
        runtime="codex",
    ):
        if spawn is None:
            spawn = render_spawn("codex", name, codex_home="/synthetic/different-home")
            spawn["args"].remove("--json")
            if baked:
                spawn["args"][-1] = (
                    f"WakePolicy={WORKER_DISPATCH_POLICY}. baked prompt stays verbatim"
                )
        self.store.register_actor(
            name,
            "agent",
            name,
            team="alpha",
            role="worker",
            project_root=str(self.root),
            owner="architect",
            capabilities=["read"],
            dispatch_cap=7,
            protected=protected,
            runtime=runtime,
            spawn=spawn,
        )
        return next(actor for actor in self.store.list_actors() if actor["id"] == name)

    def run_rerender(self, **kwargs):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return rerender_spawns(self.store, **kwargs)

    def test_criteria_1_6_8_15_two_shapes_preserve_columns_env_and_messages(self):
        for baked in (False, True):
            name = "baked" if baked else "placeholder"
            actor = self.actor(name, baked=baked, protected=True)
            with self.store.connection() as conn:
                before = dict(
                    conn.execute("select * from actors where id=?", (name,)).fetchone()
                )
                messages = list(conn.execute("select * from messages"))
            dry = self.run_rerender(actor_id=name)["actors"][0]
            changed = [
                line
                for line in dry["diff"].splitlines()
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            ]
            self.assertEqual(changed, ['+    "--json",'])
            result = self.run_rerender(
                actor_id=name, apply=True, override_protected="fixture authorization"
            )["actors"][0]
            self.assertTrue(result["verified"])
            block = result["spawn"]
            self.assertEqual(block["args"][block["args"].index("exec") + 1], "--json")
            self.assertEqual(block["args"][-1], actor["spawn"]["args"][-1])
            self.assertEqual(block["args"][-2], "workspace-write")
            self.assertEqual(block["env"], actor["spawn"]["env"])
            with self.store.connection() as conn:
                after = dict(
                    conn.execute("select * from actors where id=?", (name,)).fetchone()
                )
                self.assertEqual(messages, list(conn.execute("select * from messages")))
            before.pop("spawn_json")
            after.pop("spawn_json")
            self.assertEqual(before, after)
            with mock.patch.object(self.store, "update_actor_spawn") as write:
                again = self.run_rerender(actor_id=name, apply=True)["actors"][0]
            self.assertEqual((again["status"], again["diff"]), ("no-op", ""))
            write.assert_not_called()

    def test_criterion_7_refusals_write_nothing(self):
        base = render_spawn("codex", "worker")
        base["args"].remove("--json")
        cases = []
        for field, value, reason in [
            ("command", "custom", "command"),
            ("args", base["args"][:-1], "no worker prompt"),
            ("args", ["{worker_prompt}", *base["args"][:-1]], "not the last"),
            ("args", ["{worker_prompt}", *base["args"]], "exactly one"),
            ("args", ["custom", *base["args"]], "prefix"),
        ]:
            block = copy.deepcopy(base)
            block[field] = value
            cases.append((block, "codex", reason))
        cases.extend(
            [({}, "codex", "no stored spawn"), (base, "fake", "wrong runtime")]
        )
        for index, (block, runtime, reason) in enumerate(cases):
            name = f"worker-{index}"
            self.actor(name, spawn=block, runtime=runtime)
            with mock.patch.object(self.store, "update_actor_spawn") as write:
                result = self.run_rerender(actor_id=name, apply=True)["actors"][0]
            self.assertEqual(result["status"], "refused")
            self.assertIn(reason, result["reason"])
            write.assert_not_called()

    def test_criterion_9_protected_override(self):
        self.actor(protected=True)
        with mock.patch.object(
            self.store, "update_actor_spawn", wraps=self.store.update_actor_spawn
        ) as write:
            refused = self.run_rerender(actor_id="worker", apply=True)["actors"][0]
            self.assertEqual(refused["status"], "refused")
            write.assert_not_called()
            applied = self.run_rerender(
                actor_id="worker",
                apply=True,
                override_protected="fixture authorization",
            )["actors"][0]
        self.assertEqual(
            applied["override_protected"],
            {"actor_id": "worker", "team": "alpha", "reason": "fixture authorization"},
        )
        self.assertTrue(applied["verified"])

    def test_criterion_10_fleet_confirmation(self):
        self.actor()
        for tty, answer, yes, accepted in [
            (False, "", False, False),
            (False, "", True, True),
            (True, "yes\n", False, True),
            (True, "no\n", False, False),
        ]:
            self.actor()
            stdin = io.StringIO(answer)
            with (
                mock.patch.object(stdin, "isatty", return_value=tty),
                mock.patch("sys.stdin", stdin),
                mock.patch.object(
                    self.store,
                    "update_actor_spawn",
                    wraps=self.store.update_actor_spawn,
                ) as write,
            ):
                if accepted:
                    self.assertTrue(
                        self.run_rerender(runtime="codex", apply=True, yes=yes)[
                            "actors"
                        ][0]["verified"]
                    )
                    write.assert_called_once()
                else:
                    with self.assertRaisesRegex(
                        ValidationError, "--yes" if not tty else "confirmation refused"
                    ):
                        self.run_rerender(runtime="codex", apply=True, yes=yes)
                    write.assert_not_called()

    def test_criterion_6_cli_registration_and_admin_preflight(self):
        self.actor()
        argv = ["--db", str(self.store.db_path), "rerender-spawn", "worker"]
        with (
            mock.patch.object(rerender_spawn, "require_admin_credential") as credential,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli_run(argv), 0)
            credential.assert_not_called()
            self.assertEqual(cli_run([*argv, "--apply"]), 0)
            credential.assert_called_once()
        self.assertIn(
            "--json",
            next(
                actor for actor in self.store.list_actors() if actor["id"] == "worker"
            )["spawn"]["args"],
        )


class EventUsageTest(usage_fixture.WorkerUsageTest):
    def test_criteria_18_19_events_selection_and_last_completed_turn(self):
        first = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "cache_write_input_tokens": 3,
                "output_tokens": 10,
                "reasoning_output_tokens": 5,
            },
        }
        last = copy.deepcopy(first)
        last["usage"]["input_tokens"] = 200
        events = self.log(
            "events.jsonl",
            json.dumps(first) + "\nmalformed\n" + json.dumps(last) + "\n",
        )
        self.assertEqual(
            parse_worker_usage("codex", {"worker_events": str(events)}).fields[
                "total_tokens"
            ],
            190,
        )
        scenarios = [
            ("two", {"worker_events": str(events)}, None),
            (
                "custom",
                {"worker_events": str(self.log("custom", "human output"))},
                "no_turn_completed",
            ),
            (
                "missing",
                {"worker_events": str(self.root / "missing")},
                "events_missing",
            ),
            ("nonstring", {"worker_events": 1}, "unreadable"),
            (
                "trailer",
                {"worker_log": str(self.log("trailer", "tokens used\n9\n"))},
                None,
            ),
            (
                "no-trailer",
                {"worker_log": str(self.log("unfinished", "human"))},
                "no_trailer",
            ),
        ]
        for name, observed, reason in scenarios:
            self.row(name, observed=observed)
        self.enrich()
        for name, observed, reason in scenarios:
            self.assertEqual(self.usage(name).get("reason"), reason)
        self.assertEqual(self.usage("two")["total_tokens"], 190)
        self.assertEqual(
            self.usage("two")["usage_scope"], "through_last_completed_turn"
        )
        for key, value in last["usage"].items():
            self.assertEqual(self.usage("two")[key], value)

    def test_criterion_18_malformed_does_not_raise_or_starve(self):
        for count in (1, 25):
            with self.subTest(count=count):
                with self.store.connection() as conn:
                    conn.execute("delete from dispatch_ledger")
                for index in range(count):
                    self.row(f"bad-{count}-{index}")
                with self.store.connection() as conn:
                    conn.execute(
                        "update dispatch_ledger set observed_values_json='not JSON' where dispatch_id like 'bad-%'"
                    )
                good = self.row(
                    f"good-{count}",
                    log=self.log(f"good-{count}.log", "tokens used\n17\n"),
                    when=usage_fixture.OLD + timedelta(days=1),
                )
                rows, malformed = self.store.worker_usage_candidates(batch=25)
                self.assertIn(good, [row["dispatch_id"] for row in rows])
                counts = self.enrich(batch=25)
                self.assertEqual(counts["malformed"], malformed)
                self.assertEqual(malformed, count)
                self.assertEqual(counts["skipped"], 0)
                self.assertEqual(self.usage(good)["total_tokens"], 17)


class StreamTest(stream_fixture.SupervisedDispatchWiringTest):
    def test_criterion_14_claude_live_row_has_no_codex_snapshot_or_events(self):
        actor_id = "alpha-claude-worker"
        self.store.register_agent_actor(
            actor_id,
            "alpha",
            "worker",
            str(self.tmp / actor_id),
            [],
            runtime="claude",
            spawn=render_spawn("claude", actor_id),
            owner="alpha-architect",
        )
        queued = self.store.dispatch_agent(
            "alpha-architect", actor_id, "claude-negative", "Claude", "body", []
        )
        with self.store.connection() as conn:
            context = self.store._dispatch._dispatch_context_by_id(
                conn, queued["dispatch_id"], 30
            )
        versions_dir = self.tmp / "claude-versions"
        binary = spawn_fixture._write_claude_pin_stub(versions_dir)
        adapter = ClaudeAdapter(
            version_runner=lambda _: spawn_fixture._version_result(
                spawn_fixture.runtime_pins.CLAUDE_PINNED_VERSION
            ),
            expected_sha256=spawn_fixture._sha256(binary),
        )
        with (
            mock.patch.dict(
                os.environ, {"AGENT_COMMS_CLAUDE_VERSIONS_DIR": str(versions_dir)}
            ),
            mock.patch.object(
                supervisor, "spawn_supervised", return_value=self._ready_spawn()
            ),
            mock.patch.object(supervisor, "janitor_sweep"),
            mock.patch.object(supervisor, "reaper_registry"),
        ):
            row = self.store._dispatch._start_dispatch_context(
                lambda _: adapter, context
            )
        self.assertEqual(row["status"], "in_flight")
        self.assertNotIn("codex_auth", row["observed_values"])
        self.assertNotIn("worker_events", row["observed_values"])

    def test_criterion_17_spawn_oserror_propagates_and_cleans_both_handles(self):
        class SplitAdapter(FakeAdapter):
            def _separate_stdout(self, context, resolved_args, prompt_index):
                return True

        adapter = SplitAdapter()
        zdotdir = self.tmp / "agent-comms-zdotdir-spawn" / "empty-zdotdir"
        zdotdir.mkdir(parents=True)
        log = self.tmp / "spawn.log"
        events = self.tmp / "spawn.events"
        opened = []
        real_open = Path.open

        def open_file(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path in (log, events):
                opened.append(handle)
            return handle

        error = OSError("injected supervisor failure")
        with (
            mock.patch.object(adapter, "_worker_zdotdir", return_value=zdotdir),
            mock.patch.object(paths, "dispatch_log_path", return_value=log),
            mock.patch.object(paths, "dispatch_events_path", return_value=events),
            mock.patch.object(Path, "open", open_file),
            mock.patch.object(supervisor, "janitor_sweep"),
            mock.patch.object(supervisor, "spawn_supervised", side_effect=error),
        ):
            with self.assertRaises(OSError) as raised:
                adapter.dispatch(self._context())
        self.assertIs(raised.exception, error)
        self.assertFalse(zdotdir.exists())
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(handle.closed for handle in opened))

    def test_criteria_14_15_fake_live_row_has_no_codex_snapshot_or_extra_message(self):
        adapter = FakeAdapter()
        with self.store.connection() as conn:
            context = self.store._dispatch._dispatch_context_by_id(
                conn, self.dispatch_id, 30
            )
            before = list(conn.execute("select * from messages"))
        with (
            mock.patch.object(
                supervisor, "spawn_supervised", return_value=self._ready_spawn()
            ),
            mock.patch.object(supervisor, "janitor_sweep"),
            mock.patch.object(supervisor, "reaper_registry"),
        ):
            row = self.store._dispatch._start_dispatch_context(
                lambda _: adapter, context
            )
        self.assertEqual(row["status"], "in_flight")
        self.assertNotIn("codex_auth", row["observed_values"])
        self.assertNotIn("worker_events", row["observed_values"])
        with self.store.connection() as conn:
            self.assertEqual(before, list(conn.execute("select * from messages")))

    def test_criteria_2_3_streams_single_resolution_default_unchanged(self):
        class SplitAdapter(FakeAdapter):
            def _separate_stdout(self, context, resolved_args, prompt_index):
                self.hook_args = resolved_args
                return True

        for split in (False, True):
            adapter = SplitAdapter() if split else FakeAdapter()
            context = self._context()
            context.recipient["spawn"]["args"].insert(-1, "--stream-markers")
            Path(context.recipient["project_root"]).mkdir(exist_ok=True)
            log = self.tmp / f"{split}.log"
            events = self.tmp / f"{split}.events.jsonl"
            captured = {}

            def spawn(command, **kwargs):
                captured.update(command=command, **kwargs)
                # Execute the real fake-worker marker mode, while mocking only
                # the supervisor socket/READY boundary forbidden in this sandbox.
                subprocess.run(
                    command,
                    cwd=kwargs["cwd"],
                    env=kwargs["env"],
                    stdout=kwargs["stdout"],
                    stderr=kwargs["stderr"],
                    check=True,
                )
                return self._ready_spawn()

            original_resolve = adapter._resolved_spawn_args
            resolved_lists = []

            def resolve(*args):
                resolved = original_resolve(*args)
                resolved_lists.append(resolved)
                return resolved

            with (
                mock.patch.object(
                    adapter, "_resolved_spawn_args", side_effect=resolve
                ) as resolution,
                mock.patch.object(paths, "dispatch_log_path", return_value=log),
                mock.patch.object(paths, "dispatch_events_path", return_value=events),
                mock.patch.object(supervisor, "spawn_supervised", side_effect=spawn),
                mock.patch.object(supervisor, "janitor_sweep"),
                mock.patch.object(supervisor, "reaper_registry"),
            ):
                result = adapter.dispatch(context)
            resolution.assert_called_once()
            self.assertEqual(captured["command"][1:], resolved_lists[0])
            self.assertTrue(captured["stdout"].closed)
            self.assertTrue(captured["stderr"].closed)
            if split:
                self.assertIs(adapter.hook_args, resolved_lists[0])
                self.assertEqual(result.observed_values["worker_events"], str(events))
                self.assertNotEqual(
                    result.observed_values["worker_events"],
                    result.observed_values["worker_log"],
                )
                self.assertEqual(events.read_text(), "fake-worker-stdout\n")
                self.assertEqual(log.read_text(), "fake-worker-stderr\n")
            else:
                self.assertNotIn("worker_events", result.observed_values)
                self.assertEqual(
                    log.read_text(), "fake-worker-stdout\nfake-worker-stderr\n"
                )
                self.assertIs(captured["stdout"], captured["stderr"])

    def test_criteria_15_17_open_failures_close_handles_clean_zdotdir_and_land(self):
        class SplitAdapter(FakeAdapter):
            def _separate_stdout(self, context, resolved_args, prompt_index):
                return True

        for failing in ("log", "events"):
            with self.subTest(failing=failing):
                adapter = SplitAdapter()
                context = self._context()
                zdotdir = self.tmp / f"agent-comms-zdotdir-{failing}" / "empty-zdotdir"
                zdotdir.mkdir(parents=True)
                log = self.tmp / f"{failing}.log"
                events = self.tmp / f"{failing}.events"
                opened = []
                real_open = Path.open

                def open_file(path, *args, **kwargs):
                    if path == (log if failing == "log" else events):
                        raise PermissionError(f"injected {failing} open failure")
                    handle = real_open(path, *args, **kwargs)
                    opened.append(handle)
                    return handle

                with self.store.connection() as conn:
                    before = list(conn.execute("select * from messages"))
                with (
                    mock.patch.object(adapter, "_worker_zdotdir", return_value=zdotdir),
                    mock.patch.object(paths, "dispatch_log_path", return_value=log),
                    mock.patch.object(
                        paths, "dispatch_events_path", return_value=events
                    ),
                    mock.patch.object(Path, "open", open_file),
                    mock.patch.object(supervisor, "janitor_sweep"),
                    mock.patch.object(supervisor, "spawn_supervised") as spawn,
                ):
                    with self.assertRaisesRegex(SpawnFailed, f"injected {failing}"):
                        adapter.dispatch(context)
                self.assertFalse(zdotdir.exists())
                self.assertTrue(all(handle.closed for handle in opened))
                spawn.assert_not_called()
                with self.store.connection() as conn:
                    self.assertEqual(
                        before, list(conn.execute("select * from messages"))
                    )
                queued = self.store.dispatch_agent(
                    "alpha-architect",
                    "alpha-fake-worker",
                    f"open-{failing}",
                    "Open failure",
                    "body",
                    [],
                )
                with self.store.connection() as conn:
                    context = self.store._dispatch._dispatch_context_by_id(
                        conn, queued["dispatch_id"], 30
                    )
                    before = list(conn.execute("select * from messages"))
                zdotdir.mkdir(parents=True)
                with (
                    mock.patch.object(adapter, "_worker_zdotdir", return_value=zdotdir),
                    mock.patch.object(paths, "dispatch_log_path", return_value=log),
                    mock.patch.object(
                        paths, "dispatch_events_path", return_value=events
                    ),
                    mock.patch.object(Path, "open", open_file),
                    mock.patch.object(supervisor, "janitor_sweep"),
                ):
                    failed = self.store._dispatch._start_dispatch_context(
                        lambda _: adapter, context
                    )
                self.assertEqual(failed["status"], "spawn_failed_message_landed")
                self.assertIn(f"injected {failing}", failed["failure_reason"])
                with self.store.connection() as conn:
                    self.assertEqual(
                        before, list(conn.execute("select * from messages"))
                    )
                self.assertFalse(zdotdir.exists())
                self.assertTrue(all(handle.closed for handle in opened))


class CodexRoutingTest(ScratchTest):
    def test_fixture_token_is_fresh_under_the_real_clock(self):
        auth = self.root / "auth.json"
        auth.write_text(json.dumps(auth_document()))
        remaining = access_token_remaining_seconds(auth)
        self.assertIsNotNone(remaining)
        self.assertGreater(
            remaining, WORKER_DISPATCH_TTL_SECONDS + spawn_freshness_margin_seconds()
        )

    def _live_row(self, *, old=False):
        home = self.root / "home"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps(auth_document()))
        store = Store(self.root / "db.sqlite")
        store.register_agent_actor(
            "architect", "alpha", "architect", str(self.root), []
        )
        block = render_spawn("codex", "worker", codex_home=str(home))
        if old:
            block["args"].remove("--json")
        store.register_agent_actor(
            "worker",
            "alpha",
            "worker",
            str(self.root),
            [],
            owner="architect",
            runtime="codex",
            spawn=block,
        )
        adapter = CodexAdapter()
        original_dispatch = adapter.dispatch
        baselines = []

        def dispatch(context):
            with store.connection() as conn:
                before = list(conn.execute("select * from messages"))
            result = original_dispatch(context)
            with store.connection() as conn:
                self.assertEqual(before, list(conn.execute("select * from messages")))
            baselines.append(before)
            return result

        def spawn(command, **kwargs):
            kwargs["stderr"].write("tokens used\n71\n")
            return _fake_supervised()

        with (
            mock.patch("agent_comms.dispatch_ledger.require_fresh_module"),
            mock.patch.object(adapter, "dispatch", side_effect=dispatch),
            mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
            mock.patch.object(
                paths, "dispatch_log_path", return_value=self.root / "worker.log"
            ),
            mock.patch.object(
                paths, "dispatch_events_path", return_value=self.root / "worker.events"
            ),
            mock.patch.object(supervisor, "spawn_supervised", side_effect=spawn),
            mock.patch.object(supervisor, "janitor_sweep"),
            mock.patch.object(supervisor, "reaper_registry"),
        ):
            row = store.dispatch_agent(
                "architect",
                "worker",
                "live",
                "Work",
                "body",
                [],
                adapter_for_runtime=lambda _: adapter,
            )
        return store, row, baselines

    def test_criteria_14_15_live_row_json_status_evidence_and_no_added_message(self):
        store, row, baselines = self._live_row()
        self.assertEqual(row["status"], "in_flight")
        self.assertEqual(len(baselines), 1)
        self.assertEqual(row["observed_values"]["codex_auth"]["gaps"], [])
        out = io.StringIO()
        with (
            mock.patch.object(dispatch_status, "_codex_actor_defects", return_value=[]),
            contextlib.redirect_stdout(out),
        ):
            dispatch_status.handle(
                store, argparse.Namespace(status=None, limit=50, json=True)
            )
        self.assertEqual(
            json.loads(out.getvalue())[0]["observed_values"]["codex_auth"],
            row["observed_values"]["codex_auth"],
        )
        row["observed_values"]["worker_exit"] = {"returncode": 1}
        evidence = store._dispatch._early_dlq_evidence_lines(row)
        self.assertIn("worker_log=" + row["observed_values"]["worker_log"], evidence)
        self.assertIn(
            "worker_events=" + row["observed_values"]["worker_events"], evidence
        )
        out = io.StringIO()
        with (
            mock.patch.object(dispatch_status, "_codex_actor_defects", return_value=[]),
            contextlib.redirect_stdout(out),
        ):
            dispatch_status.handle(
                store, argparse.Namespace(status=None, limit=50, json=False)
            )
        self.assertIn(
            "worker_events=" + row["observed_values"]["worker_events"], out.getvalue()
        )
        self.assertIn(
            "worker_log=" + row["observed_values"]["worker_log"], out.getvalue()
        )

    def test_criterion_5_old_stored_argv_live_row_enriches_via_trailer(self):
        store, row, _ = self._live_row(old=True)
        self.assertEqual(row["status"], "in_flight")
        self.assertNotIn("worker_events", row["observed_values"])
        with store.connection() as conn:
            conn.execute(
                "update dispatch_ledger set status='closed', result='satisfied', closed_at=? where dispatch_id=?",
                (NOW.isoformat(), row["dispatch_id"]),
            )
        self.assertEqual(enrich_worker_usage(store.db_path, now=NOW)["enriched"], 1)
        with store.connection() as conn:
            observed = json.loads(
                conn.execute(
                    "select observed_values_json from dispatch_ledger where dispatch_id=?",
                    (row["dispatch_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(observed["worker_usage"]["total_tokens"], 71)
        self.assertEqual(
            observed["worker_usage"]["total_basis"], "runtime_reported_total"
        )

    def test_criterion_4_seven_resolved_recipient_shapes(self):
        home = self.root / "home"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps(auth_document()))
        policy = compile_policy(WORKER_DISPATCH_POLICY)
        for shape, expected in [
            ("new", True),
            ("alias", True),
            ("old", False),
            ("delimiter", False),
            ("prompt", False),
            ("placeholder", True),
            ("custom", True),
        ]:
            with self.subTest(shape=shape):
                ctx = _codex_context(self.root, home)
                block = ctx.recipient["spawn"]
                if shape in {"old", "delimiter", "prompt"}:
                    block["args"].remove("--json")
                if shape == "alias":
                    block["args"][3] = "--experimental-json"
                if shape == "delimiter":
                    block["args"][-1:-1] = ["--", "--json"]
                if shape == "placeholder":
                    block["args"][3] = "{message_id}"
                    ctx.message["id"] = "--json"
                if shape == "custom":
                    block["command"] = "/synthetic/custom-launcher"
                adapter = CodexAdapter()
                with mock.patch.object(
                    adapter,
                    "_live_worker_prompt",
                    return_value=f"WakePolicy={WORKER_DISPATCH_POLICY} --json"
                    if shape == "prompt"
                    else "WakePolicy=" + WORKER_DISPATCH_POLICY,
                ):
                    args = adapter._resolved_spawn_args(ctx, block, policy)
                slot = worker_prompt_slot(block["args"], policy.bootstrap_marker)
                self.assertEqual(adapter._separate_stdout(ctx, args, slot), expected)

    def test_criteria_5_14_old_argv_merged_log_and_new_snapshot(self):
        home = self.root / "home"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps(auth_document()))
        for old in (True, False):
            ctx = _codex_context(self.root, home)
            if old:
                ctx.recipient["spawn"]["args"].remove("--json")
            log = self.root / f"{old}.log"
            events = self.root / f"{old}.events"

            def spawn(command, **kwargs):
                kwargs["stderr"].write("tokens used\n71\n")
                kwargs["stdout"].write("human-output\n")
                return _fake_supervised()

            with (
                mock.patch.object(paths, "dispatch_log_path", return_value=log),
                mock.patch.object(paths, "dispatch_events_path", return_value=events),
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW),
                mock.patch.object(supervisor, "spawn_supervised", side_effect=spawn),
                mock.patch.object(supervisor, "janitor_sweep"),
                mock.patch.object(supervisor, "reaper_registry"),
            ):
                observed = CodexAdapter().dispatch(ctx).observed_values
            self.assertEqual(observed["codex_auth"]["gaps"], [])
            self.assertEqual("worker_events" in observed, not old)
            if old:
                self.assertIn("human-output", log.read_text())
                self.assertEqual(
                    parse_worker_usage("codex", observed).fields["total_tokens"], 71
                )


class HarnessExitTest(unittest.TestCase):
    def test_criterion_16_terminal_does_not_halt_before_natural_exit(self):
        harness = object.__new__(cell_fixture.DispatchCellHarness)
        harness.store = mock.Mock()
        harness.worker_id = "worker"
        harness.halt_on_terminal = False
        harness._spawn_handles = set()
        process = mock.Mock()
        process.wait.return_value = 0
        harness.adapter = mock.Mock()
        harness.adapter._processes = {"fake:worker:1": process}
        row = {"status": "in_flight", "spawn_handle": "fake:worker:1"}
        harness.store.dispatch_agent.return_value = row
        terminal = {**row, "status": "closed", "observed_values": {}}
        harness.store._dispatch_by_idempotency_key_fresh.return_value = terminal
        self.assertEqual(
            harness.dispatch_and_wait(
                idempotency_key="test", ttl_seconds=30, timeout_seconds=1
            ),
            terminal,
        )
        harness.adapter.halt.assert_not_called()
        process.wait.assert_not_called()
        harness.wait_for_natural_exit(1)
        process.wait.assert_called_once()
        harness.adapter.halt.assert_not_called()

    def test_criterion_16_timeout_and_nonzero_exit_are_distinct(self):
        harness = object.__new__(cell_fixture.DispatchCellHarness)
        harness.store = mock.MagicMock()
        identity = {"control_socket": "/fixture/s", "run_token": "a" * 32}
        conn = harness.store.connection.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = (json.dumps(identity),)
        harness._spawn_handles = {"handle"}
        process = mock.Mock()
        harness.adapter = mock.Mock()
        harness.adapter._processes = {"handle": process}
        process.wait.side_effect = subprocess.TimeoutExpired("fixture", 1)
        with self.assertRaisesRegex(AssertionError, "timed out"):
            harness.wait_for_natural_exit(1)
        harness.adapter.halt.assert_called_once_with("handle", identity)
        process.wait.side_effect = None
        process.wait.return_value = 7
        with self.assertRaisesRegex(AssertionError, "nonzero wrapper exit 7"):
            harness.wait_for_natural_exit(1)
