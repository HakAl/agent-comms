from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_comms import codex_home, codex_refresh_driver, paths, provisioning
from agent_comms.adapters import DispatchContext
from agent_comms.adapters.codex import AuthStale, CodexAdapter
from agent_comms.cli.commands import provision_codex_home
from agent_comms.onboarding import onboard_worker
from agent_comms.schema import ValidationError
from agent_comms.spawn import render_spawn
from agent_comms.store import Store, WORKER_DISPATCH_POLICY

NOW = datetime.now(timezone.utc)
FRESH = NOW.isoformat().replace("+00:00", "Z")


def _fresh_auth(age_days: int = 1) -> str:
    return (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")


def _make_home(home: Path, *, actor_id: str = "alpha-codex-worker", age_days: int = 1,
               shared_auth: Path | None = None, extras: bool = True) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("# base config\n")
    (home / f"{actor_id}.config.toml").write_text("# profile config\n")
    if shared_auth is not None:
        if (home / "auth.json").exists() or (home / "auth.json").is_symlink():
            (home / "auth.json").unlink()
        (home / "auth.json").symlink_to(shared_auth)
    else:
        (home / "auth.json").write_text(json.dumps({"last_refresh": _fresh_auth(age_days)}))
    if extras:
        (home / "skills").mkdir(exist_ok=True)
        (home / "skills" / "keep.txt").write_text("retain me")
        (home / "cache").mkdir(exist_ok=True)
        (home / "cache" / "junk.bin").write_text("drop me")
        (home / "sessions").mkdir(exist_ok=True)
        (home / "sessions" / "s.json").write_text("ephemeral")
        (home / "logs_2026.sqlite").write_text("db")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.custody = self.root / "custody-root"
        self.store = Store(self.root / "agent-comms.sqlite")
        self.store.init()
        self.store.register_actor("01M36YTJV9XBW95S6ZWV47C4RG", "human", "alice")
        self.store.register_agent_actor("alpha-architect", "alpha", "architect", str(self.root / "arch"), [])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _register_codex(self, actor_id: str, home: Path) -> None:
        spawn = render_spawn("codex", actor_id)
        spawn["env"]["CODEX_HOME"] = str(home)
        self.store.register_agent_actor(
            actor_id, "alpha", "worker", str(self.root / actor_id), [], runtime="codex", spawn=spawn,
            owner="alpha-architect",
        )


# --- Predicate 9: adapter preflight + provisioning containment -----------------


class AdapterPreflightTest(_Base):
    """Strict preflight keys off the resolved home, never an env switch: a
    custody-managed home (the provisioned default home or any home inside the
    runtime custody root) is strict; a legacy literal home outside the custody
    root stays on the permissive auth-only check."""

    def _ctx(self, home: Path) -> DispatchContext:
        spawn = render_spawn("codex", "alpha-codex-worker")
        spawn["env"]["CODEX_HOME"] = str(home)
        return DispatchContext(
            dispatch={"dispatch_id": "dispatch_20260802_120000_abcdef12", "policy_name": WORKER_DISPATCH_POLICY},
            recipient={"id": "alpha-codex-worker", "runtime": "codex", "project_root": str(self.root), "spawn": spawn},
            message={"id": "m"},
            ttl_seconds=30,
            expected_close_by="2026-08-02T12:00:30+00:00",
            db_path=str(self.store._db.db_path),
        )

    def _preflight_raises(self, home: Path) -> str:
        # The override redirects the runtime custody root to the scratch root the
        # test homes live under (it is a test-injection point, not an enforcement
        # switch); strict preflight is turned on because the home is in that root.
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.custody)}), \
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
            with self.assertRaises(AuthStale) as raised:
                CodexAdapter()._preflight(self._ctx(home))
        return str(raised.exception)

    def _good_home(self, name: str) -> Path:
        home = self.custody / "op" / name
        _make_home(home, extras=False)
        return home

    def test_symlink_home_refused(self) -> None:
        real = self._good_home("real")
        link = self.custody / "op" / "linked"
        link.symlink_to(real)
        self.assertIn("symlink", self._preflight_raises(link))

    def test_missing_base_config_refused(self) -> None:
        home = self._good_home("nobase")
        (home / "config.toml").unlink()
        self.assertIn("base config", self._preflight_raises(home))

    def test_missing_profile_config_refused(self) -> None:
        home = self._good_home("noprofile")
        (home / "alpha-codex-worker.config.toml").unlink()
        self.assertIn("profile config", self._preflight_raises(home))

    def test_invalid_auth_refused(self) -> None:
        home = self._good_home("badauth")
        (home / "auth.json").write_text("{not json")
        self.assertIn("auth", self._preflight_raises(home))

    def test_stale_auth_refused(self) -> None:
        home = self._good_home("stale")
        (home / "auth.json").write_text(json.dumps({"last_refresh": _fresh_auth(age_days=40)}))
        self.assertIn("stale", self._preflight_raises(home))

    def test_good_managed_home_passes_strict_preflight(self) -> None:
        home = self._good_home("good")
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.custody)}), \
                mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
            CodexAdapter()._preflight(self._ctx(home))  # does not raise

    def test_legacy_actor_without_completed_operation_stays_permissive(self) -> None:
        # No completed operation -> not custody-managed -> the adapter keeps the
        # permissive auth-only check, so a legacy literal home with only fresh
        # auth is startable (a bad base/profile config is NOT strictly enforced).
        home = self.root / "legacy"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"last_refresh": _fresh_auth()}))
        adapter = CodexAdapter()
        with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
            adapter._preflight(self._ctx(home))  # does not raise

    def test_legacy_actor_stale_auth_still_refused_by_permissive_check(self) -> None:
        home = self.root / "legacy-stale"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({"last_refresh": _fresh_auth(age_days=40)}))
        with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
            with self.assertRaises(AuthStale) as raised:
                CodexAdapter()._preflight(self._ctx(home))
        self.assertIn("stale", str(raised.exception))

    def test_provisioning_containment_is_production_authoritative(self) -> None:
        # Enforcement is production-authoritative when explicitly requested; the
        # override only redirects the root to a scratch location. An out-of-root
        # home refuses; the reusable low-level primitive defaults to no
        # enforcement so scratch callers keep working.
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.custody)}):
            with self.assertRaises(ValidationError):
                provisioning.write_codex_home(
                    self.root / "outside2", "alpha-codex-worker", "/srv/x", enforce_custody_root=True
                )
            # Default (enforce_custody_root=False) materializes anywhere for scratch.
            provisioning.write_codex_home(self.root / "scratch", "alpha-codex-worker", "/srv/x")
            # A home under the (scratch) custody root is accepted with enforcement.
            provisioning.write_codex_home(
                self.custody / "default" / "alpha-codex-worker",
                "alpha-codex-worker",
                "/srv/x",
                enforce_custody_root=True,
            )

    def test_provisioned_default_home_is_strict_without_completed_op(self) -> None:
        # The checkout-independent production default home is custody-managed
        # WITHOUT any SQL row (fail closed): a missing/malformed custody table can
        # never downgrade it to the permissive check. No completed op exists here.
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(self.custody)}):
            home = paths.provisioned_codex_home("alpha-codex-worker")
            _make_home(home, extras=False)
            (home / "config.toml").unlink()  # break base config -> strict must refuse
            with mock.patch.object(CodexAdapter, "_now_utc", return_value=NOW):
                with self.assertRaises(AuthStale) as raised:
                    CodexAdapter()._preflight(self._ctx(home))
            self.assertIn("base config", str(raised.exception))

    def test_wrong_named_profile_config_refused(self) -> None:
        # A managed home with only a foreign *.config.toml (not the exact
        # actor-specific profile) is refused, proving the preflight requires the
        # exact profile, not any first *.config.toml.
        home = self._good_home("wrongprofile")
        (home / "alpha-codex-worker.config.toml").unlink()
        (home / "someone-else.config.toml").write_text("# not the actor profile\n")
        self.assertIn("profile config", self._preflight_raises(home))

    def test_stale_freshness_uses_exact_timedelta_not_truncated_days(self) -> None:
        # A 1.75-day-old auth against a 1.5-day threshold is stale; a 1.25-day-old
        # auth is fresh. A .days truncation would wrongly treat both as 1 day.
        home = self.custody / "op" / "boundary"
        _make_home(home, extras=False)
        threshold = timedelta(days=1, hours=12)
        stale = {"last_refresh": (NOW - timedelta(days=1, hours=18)).isoformat().replace("+00:00", "Z")}
        fresh = {"last_refresh": (NOW - timedelta(days=1, hours=6)).isoformat().replace("+00:00", "Z")}
        (home / "auth.json").write_text(json.dumps(stale))
        ok, why = codex_home.preflight_home(
            home, actor_id="alpha-codex-worker", stale_after=threshold, now=NOW
        )
        self.assertFalse(ok)
        self.assertIn("stale", why)
        (home / "auth.json").write_text(json.dumps(fresh))
        ok, why = codex_home.preflight_home(
            home, actor_id="alpha-codex-worker", stale_after=threshold, now=NOW
        )
        self.assertTrue(ok)


# --- Predicate 10: refresh driver lineage grouping ------------------------------


class KeepaliveTest(_Base):
    def test_groups_by_auth_identity_one_call_per_lineage(self) -> None:
        shared = self.root / "shared" / "auth.json"
        shared.parent.mkdir()
        shared.write_text(json.dumps({"last_refresh": _fresh_auth(8)}))
        h1 = self.root / "h1"
        h2 = self.root / "h2"
        priv = self.root / "priv"
        _make_home(h1, actor_id="alpha-codex-worker", shared_auth=shared, extras=False)
        _make_home(h2, actor_id="team-c-codex-worker", shared_auth=shared, extras=False)
        _make_home(priv, actor_id="ops-codex-worker", age_days=8, extras=False)
        self._register_codex("alpha-codex-worker", h1)
        self._register_codex("team-c-codex-worker", h2)
        self._register_codex("ops-codex-worker", priv)
        calls = []
        def runner(home, binary):
            calls.append(home)
            Path(home, "auth.json").write_text(json.dumps({"last_refresh": FRESH}))
            return True
        result = codex_refresh_driver.refresh(self.store, exec_runner=runner)
        # Two distinct lineages: one shared (h1/h2) and one private (priv).
        self.assertEqual(result["lineages"], 2)
        self.assertEqual(result["lineages_refreshed"], 2, result)
        self.assertEqual(len(calls), 2)


# --- Predicate 6: launchd process-group stop custody (no second authority) -----


class KeepaliveStopCustodyTest(_Base):
    def test_default_runner_does_not_detach_child_from_job_process_group(self) -> None:
        captured: dict = {}

        class FakePopen:
            returncode = 0

            def communicate(self, **kwargs):
                self.wait(**kwargs)
                return b"", b""

            pid = 123

            def __init__(self, args, **kwargs):
                captured["args"] = args
                captured.update(kwargs)

            def wait(self, **kwargs):
                captured["wait"] = kwargs
                return 0

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured.update(kwargs)
            return FakePopen(args, **kwargs)

        with mock.patch("subprocess.Popen", fake_popen):
            ok = codex_refresh_driver._default_exec_runner("/some/home", "/absolute/codex")
        self.assertTrue(ok)
        # No new session / independent process group: the child stays in launchd's
        # inherited group so bootout terminates it. subprocess.run also waits
        # synchronously (returns a completed process), so the run never outlives it.
        self.assertNotEqual(captured.get("start_new_session"), True)
        self.assertIsNone(captured.get("preexec_fn"))
        self.assertEqual(captured["args"][0], "/absolute/codex")
        self.assertEqual(captured["args"][6:12], [
            "mcp_servers={}", "-c", "features.plugins=false", "-c",
            "features.remote_plugin=false", "Reply exactly: OK",
        ])

    def test_timeout_snapshots_sweeps_and_reports_containment(self) -> None:
        killed = []
        enumerations = []

        class HangingPopen:
            returncode = -9

            def communicate(self, **kwargs):
                self.wait(**kwargs)
                return b"", b""

            pid = 100

            def __init__(self, *args, **kwargs):
                self.waits = 0

            def wait(self, **kwargs):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("codex", 1)
                return -9

            def kill(self):
                killed.append((100, "direct"))

        live = {200}

        def enumerate_processes(*, parent_pid=None, group_id=None):
            enumerations.append((parent_pid, group_id))
            if parent_pid == 100:
                return [(200, 300)]
            if group_id is not None:
                return []
            return []

        def kill(pid, sig):
            killed.append((pid, sig))
            if sig != 0:
                live.discard(abs(pid))
            elif pid not in live:
                raise ProcessLookupError

        with mock.patch("subprocess.Popen", HangingPopen):
            result = codex_refresh_driver._default_exec_runner(
                "/some/home", "/absolute/codex",
                process_enumerator=enumerate_processes, kill=kill,
                getpgid=lambda pid: 400, getpid=lambda: 50,
            )
        self.assertEqual(result, {"ok": False, "containment_failed": False})
        self.assertEqual(enumerations[0], (100, None))
        self.assertIn((100, "direct"), killed)
        self.assertIn((200, signal.SIGKILL), killed)
        self.assertIn((-300, signal.SIGKILL), killed)

    def test_timeout_persistent_survivor_fails_containment(self) -> None:
        class HangingPopen:
            returncode = -9

            def communicate(self, **kwargs):
                self.wait(**kwargs)
                return b"", b""

            pid = 100

            def __init__(self, *args, **kwargs):
                self.waits = 0

            def wait(self, **kwargs):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("codex", 1)
                return -9

            def kill(self):
                pass

        def enumerate_processes(*, parent_pid=None, group_id=None):
            if parent_pid == 100:
                return [(200, 300)]
            if group_id is not None:
                return [(200, 300)]
            return []

        with mock.patch("subprocess.Popen", HangingPopen):
            result = codex_refresh_driver._default_exec_runner(
                "/some/home", "/absolute/codex",
                process_enumerator=enumerate_processes, kill=lambda pid, sig: None,
                getpgid=lambda pid: 400, getpid=lambda: 50, sleep=lambda _seconds: None,
            )
        self.assertEqual(result, {"ok": False, "containment_failed": True})

    def test_timeout_permission_error_survivor_fails_containment(self) -> None:
        class HangingPopen:
            returncode = -9

            def communicate(self, **kwargs):
                self.wait(**kwargs)
                return b"", b""

            pid = 100

            def __init__(self, *args, **kwargs):
                self.waits = 0

            def wait(self, **kwargs):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("codex", 1)
                return -9

            def kill(self):
                pass

        def enumerate_processes(*, parent_pid=None, group_id=None):
            if parent_pid == 100:
                return [(200, 300)]
            return []

        def kill(pid, sig):
            if abs(pid) == 200:
                raise PermissionError
            raise ProcessLookupError

        with mock.patch("subprocess.Popen", HangingPopen):
            result = codex_refresh_driver._default_exec_runner(
                "/some/home", "/absolute/codex",
                process_enumerator=enumerate_processes, kill=kill,
                getpgid=lambda pid: 400, getpid=lambda: 50, sleep=lambda _seconds: None,
            )
        self.assertEqual(result, {"ok": False, "containment_failed": True})

    def test_timeout_containment_exception_fails_closed(self) -> None:
        class HangingPopen:
            returncode = -9

            def communicate(self, **kwargs):
                self.wait(**kwargs)
                return b"", b""

            pid = 100

            def __init__(self, *args, **kwargs):
                pass

            def wait(self, **kwargs):
                raise subprocess.TimeoutExpired("codex", 1)

        with mock.patch("subprocess.Popen", HangingPopen):
            result = codex_refresh_driver._default_exec_runner(
                "/some/home", "/absolute/codex",
                process_enumerator=mock.Mock(side_effect=RuntimeError("enumeration failed")),
            )
        self.assertEqual(result, {"ok": False, "containment_failed": True})

    def test_process_enumerator_skips_pid_that_exits_before_getpgid(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="10\n11\n", stderr="")
        with mock.patch("subprocess.run", return_value=completed), mock.patch(
            "os.getpgid", side_effect=[ProcessLookupError, 22]
        ):
            self.assertEqual(
                codex_refresh_driver._default_process_enumerator(parent_pid=1), [(11, 22)]
            )

    def test_pid_permission_error_means_descendant_is_present(self) -> None:
        self.assertTrue(codex_refresh_driver._pid_exists(
            123, mock.Mock(side_effect=PermissionError)
        ))

    def test_no_second_sql_or_filesystem_authority(self) -> None:
        # The governed refresh lease is a durable, additive SQL authority.
        with self.store._db.connection() as conn:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        self.assertIn("codex_refresh_claims", tables)
        for banned in (
            "record_keepalive_claim",
            "terminalize_keepalive_claim",
            "live_keepalive_claims",
        ):
            self.assertFalse(hasattr(codex_home, banned))
        src = (paths.REPO_ROOT / "agent_comms" / "codex_home.py").read_text()
        for banned in ("codex_keepalive_claims", "keepalive_claim", "advisory_lock", "flock("):
            self.assertNotIn(banned, src)

    def test_keepalive_refresh_creates_no_claim_state(self) -> None:
        priv = self.root / "priv"
        _make_home(priv, actor_id="alpha-codex-worker", extras=False)
        self._register_codex("alpha-codex-worker", priv)
        res = codex_refresh_driver.refresh(self.store, exec_runner=lambda h: True)
        self.assertTrue(res["ok"])
        with self.store._db.connection() as conn:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        self.assertIn("codex_refresh_claims", tables)


# --- Predicate 7: refresh CLI exit + credential-free opaque output --------------


class RefreshExitAndOpacityTest(_Base):
    def _rotate(self, home, binary):
        Path(home, "auth.json").write_text(json.dumps({"last_refresh": FRESH}))
        return True

    def _two_private_homes(self) -> None:
        h1 = self.root / "h1"
        h2 = self.root / "h2"
        _make_home(h1, actor_id="alpha-codex-worker", age_days=8, extras=False)
        _make_home(h2, actor_id="team-c-codex-worker", age_days=8, extras=False)
        self._register_codex("alpha-codex-worker", h1)
        self._register_codex("team-c-codex-worker", h2)

    def test_results_use_opaque_ordinals_not_auth_paths(self) -> None:
        self._two_private_homes()
        res = codex_refresh_driver.refresh(self.store, exec_runner=self._rotate)
        self.assertTrue(res["ok"])
        self.assertEqual(res["failures"], 0)
        for entry in res["results"]:
            self.assertIn("lineage_ordinal", entry)
            self.assertNotIn("lineage", entry)
        self.assertNotIn(str(self.root / "h1"), json.dumps(res))

    def test_failed_runner_marks_not_ok(self) -> None:
        self._two_private_homes()
        res = codex_refresh_driver.refresh(self.store, exec_runner=lambda h, b: False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failures"], 2)

    def test_cli_handle_prints_json_and_exits_nonzero_on_not_ok(self) -> None:
        from agent_comms.cli.commands import refresh_codex_auth

        # ok -> handle returns None (no double print) and does not exit.
        with mock.patch.object(codex_refresh_driver, "refresh", return_value={"ok": True}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertIsNone(refresh_codex_auth.handle(self.store, types.SimpleNamespace()))
        self.assertEqual(json.loads(out.getvalue())["ok"], True)
        # not ok -> handle prints the object then raises SystemExit(1) itself.
        with mock.patch.object(
            codex_refresh_driver, "refresh", return_value={"ok": False, "failures": 1}
        ):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as cm:
                    refresh_codex_auth.handle(self.store, types.SimpleNamespace())
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(json.loads(out.getvalue())["failures"], 1)

    def test_cli_run_exits_nonzero_without_leaking_exit_code_channel(self) -> None:
        from agent_comms import cli

        # The generic runner is unchanged (no exit_code key); the command exits
        # nonzero through its own SystemExit and prints one clean JSON object.
        with mock.patch.object(codex_refresh_driver, "refresh", return_value={"ok": False, "refused": "x"}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as cm:
                    cli.run(["--db", str(self.store._db.db_path), "refresh-codex-auth"])
        self.assertEqual(cm.exception.code, 1)
        self.assertNotIn("exit_code", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["refused"], "x")

    def test_cli_run_returns_zero_on_ok(self) -> None:
        from agent_comms import cli

        with mock.patch.object(
            codex_refresh_driver, "refresh", return_value={"ok": True, "failures": 0, "results": []}
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli.run(["--db", str(self.store._db.db_path), "refresh-codex-auth"])
        self.assertEqual(rc, 0)

    def test_auth_targets_report_exposes_no_auth_path(self) -> None:
        h1 = self.root / "h1"
        _make_home(h1, actor_id="alpha-codex-worker", extras=False)
        self._register_codex("alpha-codex-worker", h1)
        report = codex_refresh_driver.auth_targets_report(self.store)
        self.assertNotIn("auth_path", json.dumps(report))
        self.assertNotIn(str(h1), json.dumps(report))
        self.assertTrue(all("group" in lg and "actor_count" in lg for lg in report["lineages"]))

    def test_auth_targets_report_healthy_key_set_is_unchanged(self) -> None:
        self.assertEqual(
            set(codex_refresh_driver.auth_targets_report(self.store)),
            {"actors", "lineages", "unresolved_actors"},
        )

    def test_auth_targets_cli_docstring_declares_path_free_contract(self) -> None:
        # F2: the CLI module docstring must state the credential-free, path-free
        # contract (actor ids, opaque group ordinals, counts) and must NOT repeat
        # the old false claim that it prints resolved auth paths. Every mention of
        # an auth path in the corrected docstring is a negation.
        from agent_comms.cli.commands import codex_auth_targets

        doc = " ".join((codex_auth_targets.__doc__ or "").split()).lower()
        # Affirmative path-free contract vocabulary.
        self.assertIn("opaque", doc)
        self.assertIn("actor ids", doc)
        self.assertIn("count", doc)
        self.assertIn("never resolved auth paths or bytes", doc)
        # The reversed (false) claim that it prints resolved auth paths is gone.
        self.assertNotIn("prints only actor ids and resolved auth paths", doc)


# --- Predicate 4: contract-surface governance ----------------------------------


class ProductionScratchBoundaryTest(_Base):
    """Item 1: the existing store._db.is_default_db_open flag is the production/
    scratch boundary for onboarding and provision-codex-home."""

    def _make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        (repo / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@e.invalid", "commit", "-m", "init"],
            cwd=repo, check=True, capture_output=True,
        )
        return repo

    def test_canonical_onboarding_is_checkout_independent(self) -> None:
        repo = self._make_repo(self.root)
        worktree_root = self.root / "wt"
        worktree_root.mkdir()
        custody = self.root / "prod-custody"
        runtime_auth = self.root / "runtime-auth" / "auth.json"
        runtime_auth.parent.mkdir(parents=True)
        runtime_auth.write_text(json.dumps({"last_refresh": _fresh_auth()}))
        store = Store(self.root / "prod.sqlite")
        store._db.is_default_db_open = True  # canonical production surface
        store.init()
        store.register_agent_actor("team-x-architect", "team-x", "architect", "/srv/team-x", [])
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(custody)}), \
                mock.patch.object(paths, "runtime_codex_auth_source", return_value=runtime_auth):
            result = onboard_worker(
                store, team="team-x", runtime="codex", actor_id="team-x-codex-worker",
                owner="team-x-architect",
                project_root="/srv/team-x", worktree_root=str(worktree_root), repo_root=repo,
            )
        # The home materializes at the checkout-independent provisioned home under
        # the runtime custody root, NOT the worktree-local dir.
        expected = custody / "default" / "team-x-codex-worker"
        self.assertEqual(result["codex_home"], str(expected))
        self.assertNotIn(".agent-comms-codex-home", result["codex_home"])
        auth = expected / "auth.json"
        self.assertTrue(auth.is_symlink())
        self.assertEqual(auth.resolve(), runtime_auth.resolve())

    def test_canonical_explicit_out_of_root_provision_refuses_before_mutation(self) -> None:
        custody = self.root / "prod-custody2"
        store = Store(self.root / "prod2.sqlite")
        store._db.is_default_db_open = True
        store.init()
        outside = self.root / "explicit-outside"
        args = types.SimpleNamespace(
            actor_id="alpha-codex-worker", project_root=str(self.root),
            codex_home=str(outside), override_protected=None,
        )
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(custody)}):
            with self.assertRaises(ValidationError):
                provision_codex_home.handle(store, args)
        self.assertFalse(outside.exists())

    def test_scratch_explicit_provision_preserves_legacy_behavior(self) -> None:
        # Non-default scratch Store: an explicit --codex-home is honored as written
        # with no containment enforcement, preserving the frozen legacy behavior.
        store = Store(self.root / "scratch.sqlite")  # is_default_db_open False
        store.init()
        outside = self.root / "scratch-explicit"
        args = types.SimpleNamespace(
            actor_id="alpha-codex-worker", project_root=str(self.root),
            codex_home=str(outside), override_protected=None,
        )
        custody = self.root / "unused-custody"
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(custody)}):
            result = provision_codex_home.handle(store, args)
        self.assertIn(str(outside / "config.toml"), result["written"])
        self.assertTrue((outside / "config.toml").is_file())

    def test_scratch_default_provision_uses_legacy_home_and_auth_without_enforcement(self) -> None:
        # F1: on the scratch surface (is_default_db_open False) the DEFAULT form
        # (--codex-home omitted) must preserve the frozen legacy behavior exactly
        # like the scratch explicit form -- the legacy repo-local per-actor home and
        # legacy auth source, with NO custody-root enforcement -- and must never
        # inherit the production custody home / runtime auth just because the home
        # was left implicit. The production/scratch decision is authoritative before
        # the default/explicit selection.
        store = Store(self.root / "scratch-default.sqlite")  # is_default_db_open False
        store.init()
        self.assertFalse(store._db.is_default_db_open)
        args = types.SimpleNamespace(
            actor_id="alpha-codex-worker", project_root=str(self.root),
            codex_home=None, override_protected=None,
        )
        captured: dict = {}

        def fake_write(codex_home_path, actor_id, project_root, *, auth_source, enforce_custody_root):
            # Capture the resolved branch without materializing the legacy repo-local
            # home (which resolves under the real REPO_ROOT/config tree).
            captured.update(
                codex_home_path=codex_home_path,
                auth_source=auth_source,
                enforce_custody_root=enforce_custody_root,
            )
            return [codex_home_path / "config.toml"]

        custody = self.root / "unused-custody"
        with mock.patch.dict(os.environ, {"AGENT_COMMS_CODEX_CUSTODY_ROOT": str(custody)}), \
                mock.patch.object(provisioning, "write_codex_home", fake_write):
            provision_codex_home.handle(store, args)
        # Legacy repo-local home + legacy auth source, no enforcement.
        self.assertEqual(captured["codex_home_path"], paths.codex_home("alpha-codex-worker"))
        self.assertEqual(captured["auth_source"], paths.codex_auth_source())
        self.assertFalse(captured["enforce_custody_root"])
        # Emphatically NOT the production custody surface for the default form.
        self.assertNotEqual(
            captured["codex_home_path"], paths.provisioned_codex_home("alpha-codex-worker")
        )
        self.assertNotEqual(captured["auth_source"], paths.runtime_codex_auth_source())
        # The scratch custody root was never materialized.
        self.assertFalse(custody.exists())


class ContractSurfaceGovernanceTest(unittest.TestCase):
    def test_codex_home_is_contract_governed_not_excluded(self) -> None:
        from agent_comms import code_identity

        self.assertIn("agent_comms/codex_home.py", code_identity.CONTRACT_GOVERNING)
        self.assertIsNone(code_identity.exclusion_reason("agent_comms/codex_home.py"))
        self.assertTrue(code_identity.is_included_surface("agent_comms/codex_home.py"))

    def test_paths_is_contract_governed_not_neutral(self) -> None:
        from agent_comms import code_identity

        # paths.py now changes native home/lineage resolution, so it moves the
        # digest as a governing module and is no longer a neutral path helper.
        self.assertIn("agent_comms/paths.py", code_identity.CONTRACT_GOVERNING)
        self.assertNotIn("agent_comms/paths.py", code_identity.CONTRACT_NEUTRAL_REASONS)
        self.assertIsNone(code_identity.exclusion_reason("agent_comms/paths.py"))
        self.assertTrue(code_identity.is_included_surface("agent_comms/paths.py"))

    def test_contract_version_is_twenty_one(self) -> None:
        # 15 covered the payload transport; 16 is the payload
        # compatibility-floor and custody correction advance; 17 bound the
        # dispatch-time review intent; 18 bound file-backed message payloads;
        # 19 binds the cycle approval destination; 20 binds reply snapshots;
        # 21 refuses stale single-log adapters before structured codex output.
        from agent_comms import code_identity

        self.assertEqual(code_identity.CONTRACT_VERSION, 21)

    def test_declared_digest_matches_governed_surface(self) -> None:
        from agent_comms import code_identity

        self.assertEqual(code_identity.contract_surface_digest(), code_identity.CONTRACT_SURFACE_DIGEST)


if __name__ == "__main__":
    unittest.main()
