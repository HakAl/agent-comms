from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import base64
from datetime import datetime, timedelta, timezone
import json
import hashlib
import sys
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_comms import codex_auth_refresh
from agent_comms import codex_refresh_driver
from agent_comms.db import Database, LEDGER_SCHEMA_VERSION
from agent_comms.store import Store

NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)
HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"


def jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "x." + payload + ".x"


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite")
        self.store.init()
        self.store.register_actor(HUMAN, "human", "operator")
        self.home = self.root / "home"
        self.home.mkdir()
        self.auth = self.home / "auth.json"
        self.key = os.path.realpath(self.auth)
        self.sleeps: list[float] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def run_refresh(self, runner=lambda home, binary: True, *, sleep=None):
        with mock.patch("shutil.which", return_value="/abs/codex"):
            return codex_auth_refresh.refresh_if_due(
                self.store,
                lineage_key=self.key,
                codex_home=str(self.home),
                actor_count=1,
                lineage_ordinal=0,
                exec_runner=runner,
                now=NOW,
                sleep=sleep or self._sleep,
            )

    def _dead_row(self):
        with self.store.connection() as conn:
            return dict(
                conn.execute(
                    "select * from codex_refresh_claims where lineage_key=?",
                    (self.key,),
                ).fetchone()
            )

    def _due_auth(self):
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()))}))

    def _mark_dead(self):
        with self.store.connection() as conn:
            conn.execute(
                "insert into codex_refresh_claims(lineage_key,dead_reason,dead_at,dead_digest) values(?,?,?,?)",
                (
                    self.key,
                    "expired",
                    "old",
                    hashlib.sha256(self.auth.read_bytes()).hexdigest(),
                ),
            )

    def test_dead_record_and_page(self):
        from agent_comms.codex_auth_constants import (
            REFRESH_TOKEN_EXPIRED_MESSAGE,
            REFRESH_TOKEN_INVALIDATED_MESSAGE,
        )

        self._due_auth()
        for reason, output in (
            ("expired", REFRESH_TOKEN_EXPIRED_MESSAGE),
            ("revoked", REFRESH_TOKEN_INVALIDATED_MESSAGE),
        ):
            with self.subTest(reason=reason):
                with self.store.connection() as conn:
                    conn.execute("update codex_refresh_claims set last_page_at=NULL")
                with mock.patch.object(
                    self.store, "send_message", wraps=self.store.send_message
                ) as page:
                    result = self.run_refresh(
                        lambda h, b: {"ok": False, "output": output}
                    )
                self.assertEqual(
                    result,
                    {
                        "ok": False,
                        "outcome": "exec_failed",
                        "containment_failed": False,
                        "dead_reason": reason,
                    },
                )
                row = self._dead_row()
                self.assertEqual(
                    (row["dead_reason"], row["dead_at"], row["dead_digest"]),
                    (
                        reason,
                        NOW.isoformat(timespec="seconds"),
                        hashlib.sha256(self.auth.read_bytes()).hexdigest(),
                    ),
                )
                self.assertEqual(row["last_page_at"], NOW.isoformat(timespec="seconds"))
                self.assertIsNone(row["holder"])
                self.assertEqual(
                    page.call_args.args[3],
                    "lineage_ordinal=0\noutcome=exec_failed\nactor_count=1\nremaining_seconds=0\ndead_reason="
                    + reason,
                )
                with mock.patch.object(self.store, "send_message") as throttled:
                    self.run_refresh(lambda h, b: {"ok": False, "output": output})
                throttled.assert_not_called()

    def test_dead_unreadable_after_exec_and_unknown_output_do_not_record(self):
        from agent_comms.codex_auth_constants import REFRESH_TOKEN_EXPIRED_MESSAGE

        for deletes in (True, False):
            with self.subTest(deletes=deletes):
                self._due_auth()
                with self.store.connection() as conn:
                    conn.execute("delete from codex_refresh_claims")

                def runner(h, b):
                    if deletes:
                        self.auth.unlink()
                    return {
                        "ok": False,
                        "output": REFRESH_TOKEN_EXPIRED_MESSAGE if deletes else "some transient failure",
                    }

                with mock.patch.object(
                    self.store, "send_message", wraps=self.store.send_message
                ) as page:
                    result = self.run_refresh(runner)
                self.assertEqual(
                    result,
                    {
                        "ok": False,
                        "outcome": "exec_failed",
                        "containment_failed": False,
                    },
                )
                self.assertEqual(
                    [
                        self._dead_row()[k]
                        for k in ("dead_reason", "dead_at", "dead_digest")
                    ],
                    [None] * 3,
                )
                self.assertEqual(
                    page.call_args.args[3],
                    "lineage_ordinal=0\noutcome=exec_failed\nactor_count=1\nremaining_seconds=0",
                )

    def test_reused_refresh_token_is_a_dead_login(self):
        # Codex 0.164.0: the refresh token was exchanged by another process
        # first; Codex says to sign in again, so the lineage is dead like an
        # expired or revoked one.
        from agent_comms.codex_auth_constants import REFRESH_TOKEN_REUSED_MESSAGE

        self._due_auth()
        with mock.patch.object(self.store, "send_message", wraps=self.store.send_message) as page:
            result = self.run_refresh(lambda h, b: {"ok": False, "output": REFRESH_TOKEN_REUSED_MESSAGE})
        self.assertEqual(result, {"ok": False, "outcome": "exec_failed", "containment_failed": False, "dead_reason": "reused"})
        row = self._dead_row()
        self.assertEqual((row["dead_reason"], row["dead_at"]), ("reused", NOW.isoformat(timespec="seconds")))
        self.assertEqual(row["dead_digest"], hashlib.sha256(self.auth.read_bytes()).hexdigest())
        self.assertIn("dead_reason=reused", page.call_args.args[3])
        self.assertEqual(codex_auth_refresh.dead_credentials(self.store)[0]["dead_reason"], "reused")

    def test_reused_after_the_other_process_won_is_not_a_dead_login(self):
        # The grace window passed with the file unchanged, so we ran codex; the
        # operator's own session exchanged the refresh token while we did and
        # saved its new login before ours came back "already used". That login
        # is the live one: no death record on its digest, no page.
        from agent_comms.codex_auth_constants import REFRESH_TOKEN_REUSED_MESSAGE

        self._due_auth()
        self._mark_dead()
        fresh = json.dumps({"access_token": jwt(int(NOW.timestamp()) + 3600)})

        def other_process_wins(home, binary):
            self.auth.write_text(fresh)
            return {"ok": False, "output": REFRESH_TOKEN_REUSED_MESSAGE}

        with mock.patch.object(self.store, "send_message", wraps=self.store.send_message) as page:
            result = self.run_refresh(other_process_wins)
        self.assertEqual(result, {"outcome": "refreshed_by_other", "ok": True})
        self.assertEqual(self.sleeps, [codex_auth_refresh.REFRESH_GRACE_SECONDS])
        page.assert_not_called()
        row = self._dead_row()
        self.assertEqual([row[k] for k in ("dead_reason", "dead_at", "dead_digest", "holder")], [None] * 4)
        self.assertEqual(codex_auth_refresh.dead_credentials(self.store), [])
        # The same message against an unchanged login is the dead one.
        self._due_auth()
        result = self.run_refresh(lambda h, b: {"ok": False, "output": REFRESH_TOKEN_REUSED_MESSAGE})
        self.assertEqual(result["dead_reason"], "reused")
        self.assertEqual(self._dead_row()["dead_digest"], hashlib.sha256(self.auth.read_bytes()).hexdigest())

    def test_dead_digest_is_of_the_bytes_judged_unchanged_not_of_a_later_save(self):
        # The post-exec read still sees the expired login (ours is the dead
        # one), and the operator's session saves its new login right after
        # that read. The digest recorded as dead is of the bytes we judged,
        # so the new login never inherits the death record, and the next pass
        # clears it.
        from agent_comms.codex_auth_constants import REFRESH_TOKEN_REUSED_MESSAGE

        self._due_auth()
        old_bytes = self.auth.read_bytes()
        fresh = json.dumps({"access_token": jwt(int(NOW.timestamp()) + 3600)}).encode()
        state = {"exec_done": False, "late_save": False}
        original = codex_auth_refresh.read_auth_document

        def read_then_the_other_process_saves(auth_path, *, sleep):
            result = original(auth_path, sleep=sleep)
            if state["exec_done"] and not state["late_save"]:
                state["late_save"] = True
                self.auth.write_bytes(fresh)
            return result

        def runner(home, binary):
            state["exec_done"] = True
            return {"ok": False, "output": REFRESH_TOKEN_REUSED_MESSAGE}

        with mock.patch.object(codex_auth_refresh, "read_auth_document", read_then_the_other_process_saves), \
                mock.patch.object(self.store, "send_message", wraps=self.store.send_message) as page:
            result = self.run_refresh(runner)
        self.assertEqual(result["outcome"], "exec_failed")
        self.assertEqual(result["dead_reason"], "reused")
        self.assertTrue(state["late_save"])
        row = self._dead_row()
        self.assertEqual(row["dead_digest"], hashlib.sha256(old_bytes).hexdigest())
        self.assertNotEqual(row["dead_digest"], hashlib.sha256(fresh).hexdigest())
        self.assertIn("dead_reason=reused", page.call_args.args[3])
        # The next pass meets the new login: the death record goes, nothing is due.
        self.assertEqual(self.run_refresh(lambda h, b: self.fail("no refresh of a fresh login")), {"outcome": "not_due", "ok": True})
        self.assertIsNone(self._dead_row()["dead_digest"])
        self.assertEqual(codex_auth_refresh.dead_credentials(self.store), [])

    def test_grace_window_yields_to_the_other_refresher(self):
        # The operator's own Codex shares the login: after the grace window the
        # file has advanced, so nothing is executed and the outcome is not a
        # refresh of our own.
        self._due_auth()
        self._mark_dead()
        calls = []

        def other_process_refreshes(seconds):
            self.sleeps.append(seconds)
            self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) + 3600)}))

        result = self.run_refresh(lambda h, b: calls.append(h) or True, sleep=other_process_refreshes)
        self.assertEqual(result, {"outcome": "refreshed_by_other", "ok": True})
        self.assertEqual(self.sleeps, [codex_auth_refresh.REFRESH_GRACE_SECONDS])
        self.assertEqual(calls, [])
        self.assertNotIn("refreshed_by_other", codex_auth_refresh.FAILURE_OUTCOMES)
        row = self._dead_row()
        self.assertEqual([row[k] for k in ("dead_reason", "dead_at", "dead_digest", "holder")], [None] * 4)
        self.assertEqual(self.store.list_inbox(HUMAN), [])

    def test_grace_window_then_refreshes_an_untouched_login(self):
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) - 10)}))

        def runner(h, b):
            self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) + 3600)}))
            return True

        with mock.patch.dict(os.environ, {codex_auth_refresh.REFRESH_GRACE_ENV: "7"}):
            result = self.run_refresh(runner)
        self.assertEqual(result, {"outcome": "refreshed", "ok": True})
        self.assertEqual(self.sleeps, [7])
        self.sleeps.clear()
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) - 10)}))
        with mock.patch.dict(os.environ, {codex_auth_refresh.REFRESH_GRACE_ENV: "0"}):
            result = self.run_refresh(runner)
        self.assertEqual(result["outcome"], "refreshed")
        self.assertEqual(self.sleeps, [])

    def test_grace_window_recheck_defers_to_a_worker_and_pages_garbage(self):
        self._due_auth()
        holding = iter((False, True))
        with mock.patch.object(self.store._dispatch, "codex_lineage_holding", side_effect=lambda key: next(holding)):
            result = self.run_refresh()
        self.assertEqual(result["outcome"], "deferred_busy")
        self.assertEqual(self.sleeps, [codex_auth_refresh.REFRESH_GRACE_SECONDS])
        self.assertIsNotNone(self._dead_row()["first_deferred_at"])
        self.sleeps.clear()

        def other_process_breaks_it(seconds):
            self.sleeps.append(seconds)
            self.auth.write_text("")

        result = self.run_refresh(sleep=other_process_breaks_it)
        self.assertEqual(result["outcome"], "due_metadata_invalid")
        # The grace sleep, then the bounded re-reads of the empty file.
        self.assertEqual(self.sleeps, [codex_auth_refresh.REFRESH_GRACE_SECONDS] + [codex_auth_refresh.AUTH_READ_RETRY_SECONDS] * (codex_auth_refresh.AUTH_READ_ATTEMPTS - 1))

    def test_readers_re_read_a_partial_file_a_bounded_number_of_times(self):
        fresh = json.dumps({"access_token": jwt(int(NOW.timestamp()) + 3600), "last_refresh": NOW.isoformat()})
        for partial in ("", "{\"access_token\": \"x.", b"\xff".decode("latin-1")):
            with self.subTest(partial=partial):
                self.auth.write_text(partial, encoding="latin-1")
                sleeps = []

                def finishes_the_save(seconds):
                    sleeps.append(seconds)
                    self.auth.write_text(fresh)

                self.assertEqual(codex_auth_refresh.access_token_remaining_seconds(self.auth, now=NOW, sleep=finishes_the_save), 3600)
                self.assertEqual(sleeps, [codex_auth_refresh.AUTH_READ_RETRY_SECONDS])
                self.auth.write_text(partial, encoding="latin-1")
                snapshot = codex_auth_refresh.codex_auth_snapshot(self.auth, now=NOW, sleep=finishes_the_save)
                self.assertEqual((snapshot["read_status"], snapshot["ttl_seconds"]), ("ok", 3600))
                self.assertEqual(snapshot["auth_digest"], hashlib.sha256(fresh.encode()).hexdigest())
                self.auth.write_text(partial, encoding="latin-1")
                self.assertEqual(codex_auth_refresh._metadata(self.auth, NOW, sleep=finishes_the_save), (False, (int(NOW.timestamp()) + 3600, NOW.isoformat())))
        # Still garbage after the last attempt: invalid, with the attempts bounded.
        self.auth.write_text("")
        sleeps = []
        self.assertIsNone(codex_auth_refresh.access_token_remaining_seconds(self.auth, now=NOW, sleep=sleeps.append))
        self.assertEqual(len(sleeps), codex_auth_refresh.AUTH_READ_ATTEMPTS - 1)
        snapshot = codex_auth_refresh.codex_auth_snapshot(self.auth, now=NOW, sleep=sleeps.append)
        self.assertEqual(snapshot["read_status"], "not_json")
        self.assertEqual(snapshot["auth_digest"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(codex_auth_refresh._metadata(self.auth, NOW, sleep=sleeps.append), (None, (None, None)))
        # A missing file is not re-read: Codex saves in place, it never unlinks.
        self.auth.unlink()
        sleeps.clear()
        self.assertEqual(codex_auth_refresh.codex_auth_snapshot(self.auth, now=NOW, sleep=sleeps.append)["read_status"], "missing")
        self.assertEqual(sleeps, [])

    def test_refresher_runs_the_custody_codex_when_certified_else_the_path_one(self):
        from agent_comms import runtime_pins
        from tests.codex_custody_support import fake_codex_bundle

        versions = self.root / "custody"
        env = fake_codex_bundle(versions)
        binary = versions / runtime_pins.codex_pin().version / "bin" / "codex"
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) - 10)}))
        seen = []

        def runner(home, used):
            seen.append(used)
            self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) + 3600)}))
            return True

        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_refresh(runner)["outcome"], "refreshed")
        self.assertEqual(seen, [str(binary)])
        # Tampered custody copy: the PATH codex, as before.
        binary.write_text("#!/bin/sh\nexit 1\n")
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) - 10)}))
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.run_refresh(runner)["outcome"], "refreshed")
        self.assertEqual(seen[-1], "/abs/codex")
        # Uncertified platform: the PATH codex too, and none at all is binary_missing.
        with mock.patch.object(runtime_pins, "codex_pin", side_effect=runtime_pins.PlatformNotCertified("nope")):
            self.assertEqual(codex_auth_refresh.refresh_binary(), mock.ANY)
            with mock.patch("shutil.which", return_value=None):
                self.assertIsNone(codex_auth_refresh.refresh_binary())

    def test_dead_clear_fresh_rotated_due_busy_and_refreshed(self):
        from agent_comms.codex_auth_constants import REFRESH_TOKEN_INVALIDATED_MESSAGE

        for mode in ("fresh", "rotated_due", "busy", "refreshed", "unreadable"):
            with self.subTest(mode=mode):
                with self.store.connection() as conn:
                    conn.execute("delete from codex_refresh_claims")
                self._due_auth()
                self._mark_dead()
                original = self._dead_row()
                if mode == "fresh":
                    self.auth.write_text(
                        json.dumps({"access_token": jwt(int(NOW.timestamp()) + 100)})
                    )
                elif mode == "rotated_due":
                    self.auth.write_text(
                        json.dumps({"access_token": jwt(int(NOW.timestamp()) - 100)})
                    )
                elif mode == "unreadable":
                    self.auth.unlink()

                def runner(h, b):
                    if mode == "rotated_due":
                        row = self._dead_row()
                        self.assertEqual(
                            [row[k] for k in ("dead_reason", "dead_at", "dead_digest")],
                            [None] * 3,
                        )
                    if mode == "refreshed":
                        self.auth.write_text(
                            json.dumps(
                                {"access_token": jwt(int(NOW.timestamp()) + 100)}
                            )
                        )
                        return True
                    return {"ok": False, "output": REFRESH_TOKEN_INVALIDATED_MESSAGE}

                with mock.patch.object(
                    self.store._dispatch,
                    "codex_lineage_holding",
                    return_value=mode == "busy",
                ):
                    result = self.run_refresh(runner)
                self.assertEqual(
                    result["outcome"],
                    {
                        "fresh": "not_due",
                        "rotated_due": "exec_failed",
                        "busy": "deferred_busy",
                        "refreshed": "refreshed",
                        "unreadable": "due_metadata_invalid",
                    }[mode],
                )
                row = self._dead_row()
                if mode == "busy":
                    self.assertEqual(
                        [row[k] for k in ("dead_reason", "dead_at", "dead_digest")],
                        [
                            original[k]
                            for k in ("dead_reason", "dead_at", "dead_digest")
                        ],
                    )
                elif mode == "rotated_due":
                    self.assertEqual(row["dead_reason"], "revoked")
                    self.assertEqual(row["dead_at"], NOW.isoformat(timespec="seconds"))
                    self.assertEqual(
                        row["dead_digest"],
                        hashlib.sha256(self.auth.read_bytes()).hexdigest(),
                    )
                    self.assertNotEqual(row["dead_digest"], original["dead_digest"])
                else:
                    self.assertEqual(
                        [row[k] for k in ("dead_reason", "dead_at", "dead_digest")],
                        [None] * 3,
                    )

    def test_runner_captures_tail_and_success(self):
        from agent_comms.codex_auth_constants import REFRESH_TOKEN_EXPIRED_MESSAGE

        binary = self.root / "fake-codex"
        for noise, code in ((0, 1), (100000, 1), (0, 0)):
            with self.subTest(noise=noise, code=code):
                stderr = "x" * noise + REFRESH_TOKEN_EXPIRED_MESSAGE
                binary.write_text(
                    "#!"
                    + sys.executable
                    + "\nimport sys\nsys.stderr.buffer.write("
                    + repr(stderr.encode())
                    + ")\nsys.stdout.buffer.write(b'out\\xff')\nsys.exit("
                    + str(code)
                    + ")\n"
                )
                binary.chmod(0o700)
                result = codex_refresh_driver._default_exec_runner(
                    str(self.home), str(binary)
                )
                if code == 0:
                    self.assertIs(result, True)
                else:
                    self.assertEqual(
                        result,
                        {
                            "ok": False,
                            "containment_failed": False,
                            "output": (stderr + "out\ufffd")[-65536:],
                        },
                    )

    def test_positive_remainder_is_not_due_with_no_claim_child_or_page(self):
        calls = []
        self.auth.write_text(
            json.dumps({"access_token": jwt(int(NOW.timestamp()) + 1)})
        )
        result = self.run_refresh(lambda home, binary: calls.append(home) or True)
        self.assertEqual(result["outcome"], "not_due")
        self.assertEqual(calls, [])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("select count(*) from codex_refresh_claims").fetchone()[0], 0)
        self.assertEqual(self.store.list_inbox(HUMAN), [])

    def test_exact_and_past_expiry_are_due(self):
        for offset in (0, -1):
            with self.subTest(offset=offset):
                self.auth.write_text(
                    json.dumps({"access_token": jwt(int(NOW.timestamp()) + offset)})
                )

                def rotate(home, binary):
                    self.auth.write_text(
                        json.dumps({"access_token": jwt(int(NOW.timestamp()) + 604800)})
                    )
                    return True

                self.assertEqual(self.run_refresh(rotate)["outcome"], "refreshed")

    def test_removed_horizon_environment_cannot_make_live_jwt_due(self):
        self.auth.write_text(
            json.dumps({"access_token": jwt(int(NOW.timestamp()) + 1)})
        )
        with mock.patch.dict(
            os.environ, {"AGENT_COMMS_CODEX_REFRESH_HORIZON_SECONDS": "999999999"}
        ):
            self.assertEqual(self.run_refresh()["outcome"], "not_due")
        self.assertFalse(hasattr(codex_auth_refresh, "REFRESH_HORIZON_SECONDS"))
        self.assertFalse(hasattr(codex_auth_refresh, "refresh_horizon_seconds"))
        self.assertEqual(
            codex_auth_refresh.MINIMUM_ROTATED_TOKEN_LIFETIME_SECONDS,
            codex_auth_refresh.FALLBACK_MAX_AGE_SECONDS,
        )

    def test_fallback_age_strict_and_invalid_pages(self):
        self.auth.write_text(json.dumps({"access_token": "bad", "last_refresh": (NOW - timedelta(days=7)).isoformat()}))
        self.assertEqual(self.run_refresh()["outcome"], "not_due")
        self.auth.write_text(
            json.dumps(
                {
                    "access_token": "bad",
                    "last_refresh": (NOW - timedelta(days=7, seconds=1)).isoformat(),
                }
            )
        )

        def rotate(home, binary):
            self.auth.write_text(json.dumps({"last_refresh": NOW.isoformat()}))
            return True

        self.assertEqual(self.run_refresh(rotate)["outcome"], "refreshed")
        self.auth.write_text(json.dumps({"access_token": "bad"}))
        self.assertEqual(self.run_refresh()["outcome"], "due_metadata_invalid")
        inbox = self.store.list_inbox(HUMAN)
        self.assertEqual(len(inbox), 1)
        body = self.store.read_message(HUMAN, inbox[0]["id"])["body"]
        self.assertNotIn("remaining_seconds=", body)
        self.assertNotIn("age_seconds=", body)

    def test_wrong_shape_and_naive_fallback_timestamp_are_invalid(self):
        for value in ([], {"last_refresh": "2026-01-01T00:00:00"}):
            with self.subTest(value=value):
                self.auth.write_text(json.dumps(value))
                self.assertEqual(self.run_refresh()["outcome"], "due_metadata_invalid")

    def test_invalid_top_level_token_is_not_masked_by_valid_nested_jwt(self):
        token = jwt(int(NOW.timestamp()) + 1)
        self.auth.write_text(json.dumps({"access_token": False, "tokens": {"access_token": token}}))
        self.assertEqual(self.run_refresh()["outcome"], "due_metadata_invalid")

    def test_valid_jwt_does_not_mask_malformed_last_refresh(self):
        token = jwt(int(NOW.timestamp()) + 1)
        self.auth.write_text(json.dumps({"access_token": token, "last_refresh": "malformed"}))
        self.assertEqual(self.run_refresh()["outcome"], "due_metadata_invalid")

    def test_valid_jwt_does_not_mask_naive_last_refresh(self):
        token = jwt(int(NOW.timestamp()) + 1)
        self.auth.write_text(json.dumps({"access_token": token, "last_refresh": "2026-08-01T00:00:00"}))
        self.assertEqual(self.run_refresh()["outcome"], "due_metadata_invalid")

    def test_missing_dangling_and_directory_are_invalid(self):
        for kind in ("missing", "dangling", "directory"):
            with self.subTest(kind=kind):
                if self.auth.is_symlink():
                    self.auth.unlink()
                elif self.auth.is_dir():
                    self.auth.rmdir()
                elif self.auth.exists():
                    self.auth.unlink()
                if kind == "dangling":
                    self.auth.symlink_to(self.root / "absent")
                if kind == "directory":
                    self.auth.mkdir()
                result = self.run_refresh()
                self.assertEqual(result["outcome"], "due_metadata_invalid")

    def test_lease_contention_reclaim_release_and_row_persistence(self):
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        with self.store.connection() as conn:
            conn.execute(
                "insert into codex_refresh_claims(lineage_key,holder,claimed_at,first_deferred_at,last_page_at) values(?,?,?,?,?)",
                (self.key, "other", NOW.isoformat(), None, None),
            )
        self.assertEqual(self.run_refresh()["outcome"], "claim_contention")
        with self.store.connection() as conn:
            conn.execute("update codex_refresh_claims set claimed_at=?", ((NOW - timedelta(hours=2)).isoformat(),))
        def rotate(home, binary):
            self.auth.write_text(json.dumps({"last_refresh": NOW.isoformat()}))
            return True
        self.assertEqual(self.run_refresh(rotate)["outcome"], "refreshed")
        with self.store.connection() as conn:
            row = conn.execute("select * from codex_refresh_claims").fetchone()
            self.assertIsNone(row["holder"])
            self.assertIsNone(row["claimed_at"])

    def test_null_holder_row_does_not_contend_and_deferral_state_persists(self):
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        with self.store.connection() as conn:
            conn.execute(
                "insert into codex_refresh_claims(lineage_key,holder,claimed_at,first_deferred_at,last_page_at) values(?,?,?,?,?)",
                (self.key, None, (NOW + timedelta(hours=1)).isoformat(),
                 (NOW - timedelta(hours=2)).isoformat(), (NOW - timedelta(hours=13)).isoformat()),
            )
        with mock.patch.object(self.store._dispatch, "codex_lineage_holding", return_value=True):
            self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
        with self.store.connection() as conn:
            row = conn.execute("select * from codex_refresh_claims").fetchone()
            self.assertIsNone(row["holder"])
            self.assertIsNone(row["claimed_at"])
            self.assertEqual(row["first_deferred_at"], (NOW - timedelta(hours=2)).isoformat())
            self.assertEqual(row["last_page_at"], NOW.isoformat(timespec="seconds"))
            conn.execute(
                "update codex_refresh_claims set holder=?, claimed_at=? where lineage_key=?",
                ("expired", (NOW - timedelta(hours=2)).isoformat(), self.key),
            )
        with mock.patch.object(self.store._dispatch, "codex_lineage_holding", return_value=True):
            self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
        with self.store.connection() as conn:
            row = conn.execute("select * from codex_refresh_claims").fetchone()
            self.assertEqual(row["first_deferred_at"], (NOW - timedelta(hours=2)).isoformat())
            self.assertEqual(row["last_page_at"], NOW.isoformat(timespec="seconds"))

    def test_verify_exec_binary_and_page_failed(self):
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        self.assertEqual(self.run_refresh(lambda h, b: False)["outcome"], "exec_failed")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(codex_auth_refresh.refresh_if_due(self.store, lineage_key=self.key,
                codex_home=str(self.home), actor_count=1, lineage_ordinal=0,
                exec_runner=lambda h,b: True, now=NOW, sleep=self._sleep)["outcome"], "binary_missing")
        with mock.patch.dict(os.environ, {"AGENT_COMMS_OPERATOR_ACTOR": "unknown"}):
            with self.assertRaises(codex_auth_refresh.PageFailed):
                codex_auth_refresh._page(self.store, 0, "verify_failed", 1, "new-key", NOW)

    def test_containment_failure_holds_lease_and_pages_marker(self):
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        result = self.run_refresh(
            lambda h, b: {"ok": False, "containment_failed": True}
        )
        self.assertEqual(result, {
            "outcome": "exec_failed", "ok": False, "containment_failed": True,
        })
        with self.store.connection() as conn:
            claim = conn.execute(
                "select holder,claimed_at from codex_refresh_claims where lineage_key=?",
                (self.key,),
            ).fetchone()
        self.assertIsNotNone(claim["holder"])
        self.assertIsNotNone(claim["claimed_at"])
        message = self.store.list_inbox(HUMAN)[0]
        body = self.store.read_message(HUMAN, message["id"])["body"]
        self.assertIn("outcome=exec_failed", body)
        self.assertIn("containment_failed=true", body)

    def test_clean_containment_failure_releases_lease(self):
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        result = self.run_refresh(
            lambda h, b: {"ok": False, "containment_failed": False}
        )
        self.assertFalse(result["containment_failed"])
        with self.store.connection() as conn:
            claim = conn.execute(
                "select holder,claimed_at from codex_refresh_claims where lineage_key=?",
                (self.key,),
            ).fetchone()
        self.assertIsNone(claim["holder"])
        self.assertIsNone(claim["claimed_at"])

    def test_verify_rejects_unchanged_and_garbage(self):
        old = json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()})
        for replacement in (old, "not json"):
            with self.subTest(replacement=replacement):
                self.auth.write_text(old)
                def runner(home, binary):
                    self.auth.write_text(replacement)
                    return True
                self.assertEqual(self.run_refresh(runner)["outcome"], "verify_failed")

    def test_deferral_state_escalation_throttle_and_reset(self):
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()))}))
        with mock.patch.object(self.store._dispatch, "codex_lineage_holding", return_value=True):
            self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
            self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
        inbox = self.store.list_inbox(HUMAN)
        self.assertEqual(len(inbox), 1)
        body = self.store.read_message(HUMAN, inbox[0]["id"])["body"]
        self.assertIn("remaining_seconds=0", body)
        self.assertNotIn("age_seconds=", body)
        with self.store.connection() as conn:
            row = conn.execute("select first_deferred_at,last_page_at from codex_refresh_claims").fetchone()
            self.assertIsNotNone(row[0])
            self.assertIsNotNone(row[1])
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) + 129600)}))
        self.assertEqual(self.run_refresh()["outcome"], "not_due")
        with self.store.connection() as conn:
            self.assertIsNone(conn.execute("select first_deferred_at from codex_refresh_claims").fetchone()[0])

    def test_live_token_is_not_due_even_while_lineage_is_busy(self):
        self.auth.write_text(json.dumps({"access_token": jwt(int(NOW.timestamp()) + 100000)}))
        with mock.patch.object(self.store._dispatch, "codex_lineage_holding", return_value=True):
            self.assertEqual(self.run_refresh()["outcome"], "not_due")
        self.assertEqual(self.store.list_inbox(HUMAN), [])

    def test_fallback_age_deferral_pages_once_and_is_throttled(self):
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        with mock.patch.object(self.store._dispatch, "codex_lineage_holding", return_value=True):
            self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
            self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
        inbox = self.store.list_inbox(HUMAN)
        self.assertEqual(len(inbox), 1)
        body = self.store.read_message(HUMAN, inbox[0]["id"])["body"]
        self.assertIn("age_seconds=691200", body)
        self.assertNotIn("remaining_seconds=", body)

    def test_page_throttle_is_shared_across_connections(self):
        codex_auth_refresh._page(self.store, 0, "verify_failed", 1, self.key, NOW)
        codex_auth_refresh._page(self.store, 0, "verify_failed", 1, self.key, NOW)
        self.assertEqual(len(self.store.list_inbox(HUMAN)), 1)

    def test_operator_override_and_page_text_are_credential_free(self):
        override = "01J00000000000000000000002"
        self.store.register_actor(override, "human", "alternate")
        secret_path = "/private/CODEX_HOME/auth.json"
        with mock.patch.dict(os.environ, {"AGENT_COMMS_OPERATOR_ACTOR": override}):
            codex_auth_refresh._page(self.store, 7, "verify_failed", 2, secret_path, NOW)
        self.assertEqual(self.store.list_inbox(HUMAN), [])
        message = self.store.list_inbox(override)[0]
        rendered = message["subject"] + self.store.read_message(override, message["id"])["body"]
        self.assertNotIn(secret_path, rendered)
        self.assertNotIn("token", rendered.lower())

    def test_real_keyed_holding_row_defers_and_unrelated_does_not(self):
        values = ("producer", "recipient")
        self.store.register_agent_actor(values[0], "x", "architect", str(self.root / "producer"), [])
        self.store.register_agent_actor(values[1], "x", "worker", str(self.root), [],
                                        runtime="codex", spawn={"env": {"CODEX_HOME": str(self.home)}},
                                        owner=values[0])
        def insert(dispatch_id, key, *, status="in_flight", claimed_at=None):
            with self.store.connection() as conn:
                conn.execute("""insert into dispatch_ledger
                  (dispatch_id,idempotency_key,thread_ref,recipient_actor_id,producer_actor_id,
                   originating_actor_id,policy_name,policy_version,policy_issued_by,status,created_at,
                   auth_lineage_key,auth_lineage_claimed_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (dispatch_id,dispatch_id,dispatch_id,values[1],values[0],values[0],"p","1",values[0],
                   status,NOW.isoformat(),key,claimed_at))
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        insert("unrelated", self.key + "-other")
        def rotate(home, binary):
            self.auth.write_text(json.dumps({"last_refresh": NOW.isoformat()}))
            return True
        self.assertEqual(self.run_refresh(rotate)["outcome"], "refreshed")
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        insert("related", self.key)
        self.assertEqual(self.run_refresh()["outcome"], "deferred_busy")
        with self.store.connection() as conn:
            private = self.store._dispatch._lineage_holding(
                conn, "", "codex", self.key, exclude_dispatch_id=None)
        self.assertEqual(self.store._dispatch.codex_lineage_holding(self.key), private)
        with self.store.connection() as conn:
            conn.execute("delete from dispatch_ledger where dispatch_id='related'")
        insert("legacy", None)
        with self.store.connection() as conn:
            private_legacy = self.store._dispatch._lineage_holding(
                conn, "", "codex", self.key, exclude_dispatch_id=None)
        self.assertTrue(private_legacy)
        self.assertEqual(self.store._dispatch.codex_lineage_holding(self.key), private_legacy)

    def test_legacy_queued_claim_and_spawn_failed_claim_are_inert(self):
        producer, recipient = "producer", "recipient"
        self.store.register_agent_actor(producer, "x", "architect", str(self.root / "producer"), [])
        self.store.register_agent_actor(recipient, "x", "worker", str(self.root), [],
                                        runtime="codex", spawn={"env": {"CODEX_HOME": str(self.home)}},
                                        owner=producer)
        self.auth.write_text(json.dumps({"last_refresh": (NOW - timedelta(days=8)).isoformat()}))
        for index, status in enumerate(("queued", "spawn_failed_message_landed")):
            with self.subTest(status=status):
                dispatch_id = f"held-{index}"
                with self.store.connection() as conn:
                    conn.execute("""insert into dispatch_ledger
                      (dispatch_id,idempotency_key,thread_ref,recipient_actor_id,producer_actor_id,
                       originating_actor_id,policy_name,policy_version,policy_issued_by,status,created_at,
                       auth_lineage_key,auth_lineage_claimed_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (dispatch_id,dispatch_id,dispatch_id,recipient,producer,producer,"p","1",producer,
                       status,NOW.isoformat(),self.key,datetime.now(timezone.utc).isoformat()))
                self.assertNotEqual(self.run_refresh()["outcome"], "deferred_busy")
                with self.store.connection() as conn:
                    conn.execute("delete from dispatch_ledger where dispatch_id=?", (dispatch_id,))


class KeepaliveShapeTest(unittest.TestCase):
    def test_distinct_lineages_sharing_hardlink_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / "db.sqlite")
            store.init()
            store.register_actor(HUMAN, "human", "operator")
            store.register_agent_actor(
                "architect", "team", "architect", str(root / "architect"), [])
            homes = [root / "home-a", root / "home-b"]
            for home in homes:
                home.mkdir()
            (homes[0] / "auth.json").write_text(
                json.dumps({"last_refresh": "2000-01-01T00:00:00+00:00"}))
            os.link(homes[0] / "auth.json", homes[1] / "auth.json")
            for index, home in enumerate(homes):
                store.register_agent_actor(
                    f"worker-{index}", "team", "worker", str(root / f"worker-{index}"), [],
                    runtime="codex", spawn={"env": {"CODEX_HOME": str(home)}},
                    owner="architect")

            calls = []
            result = codex_refresh_driver.refresh(
                store, exec_runner=lambda home, binary: calls.append((home, binary)) or True)

            self.assertFalse(result["ok"])
            self.assertEqual(result["failures"], 2)
            self.assertEqual(
                [(item["outcome"], item["ok"]) for item in result["results"]],
                [("hardlink_refused", False), ("hardlink_refused", False)])
            self.assertEqual(calls, [])
            inbox = store.list_inbox(HUMAN)
            self.assertEqual(len(inbox), 2)
            self.assertTrue(all("hardlink_refused" in item["subject"] for item in inbox))

    def test_exact_shape_accounting_and_page_failed(self):
        actors = [{"lineage_key": "a", "fs_identity": None, "codex_home": "/a"},
                  {"lineage_key": "b", "fs_identity": None, "codex_home": "/b"}]
        groups = {"a": [actors[0]], "b": [actors[1]]}
        outcomes = iter(({"outcome": "deferred_busy", "ok": True},
                         {"outcome": "claim_contention", "ok": True}))
        store = mock.Mock()
        with mock.patch.object(codex_refresh_driver, "resolve_codex_actors", return_value=actors), \
             mock.patch.object(codex_refresh_driver, "group_by_lineage", return_value=groups), \
             mock.patch.object(codex_refresh_driver, "refresh_if_due", side_effect=lambda *a, **k: next(outcomes)):
            result = codex_refresh_driver.refresh(store)
        self.assertEqual(set(result), {"ok", "actors", "lineages", "lineages_refreshed",
                                      "unresolved_actors", "failures", "deferred", "results"})
        self.assertEqual(result["deferred"], 2)
        self.assertEqual(set(result["results"][0]), {"lineage_ordinal", "actor_count", "outcome", "ok"})

        with mock.patch.object(codex_refresh_driver, "resolve_codex_actors", return_value=actors[:1]), \
             mock.patch.object(codex_refresh_driver, "group_by_lineage", return_value={"a": actors[:1]}), \
             mock.patch.object(codex_refresh_driver, "refresh_if_due", side_effect=codex_auth_refresh.PageFailed()):
            failed = codex_refresh_driver.refresh(store)
        self.assertFalse(failed["ok"])
        self.assertIs(failed["page_failed"], True)


class MigrationTest(unittest.TestCase):
    def test_existing_claims_gain_nullable_dead_columns_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "create table codex_refresh_claims(lineage_key text primary key, holder text, claimed_at text, first_deferred_at text, last_page_at text)"
                )
                conn.execute(
                    "insert into codex_refresh_claims values('lineage','holder','claimed','deferred','paged')"
                )
            Database(path).init()
            Database(path).init()
            with sqlite3.connect(path) as conn:
                self.assertEqual(
                    conn.execute("select * from codex_refresh_claims").fetchone(),
                    (
                        "lineage",
                        "holder",
                        "claimed",
                        "deferred",
                        "paged",
                        None,
                        None,
                        None,
                    ),
                )
                self.assertEqual(
                    conn.execute("pragma user_version").fetchone()[0],
                    LEDGER_SCHEMA_VERSION,
                )

    def test_fresh_idempotent_and_user_version_at_declared_floor(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.sqlite"
            Database(path).init()
            Database(path).init()
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
                self.assertEqual(conn.execute("select count(*) from sqlite_master where name='codex_refresh_claims'").fetchone()[0], 1)

    def test_preexisting_floor1_database_upgrade_reaches_declared_floor(self):
        # The auth-refresh migration stays additive in shape; the floor advance
        # to 2 belongs to dispatch payload transport. Current code opening a
        # pre-existing floor-1 database stamps it forward to the declared floor.
        self.assertEqual(LEDGER_SCHEMA_VERSION, 3)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("pragma user_version=1")
                conn.execute("create table preexisting(value text)")
            Database(path).init()
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("pragma user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
                self.assertIsNotNone(conn.execute(
                    "select 1 from sqlite_master where type='table' and name='codex_refresh_claims'").fetchone())


if __name__ == "__main__":
    unittest.main()
