from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.dispatch_cell_harness import (
    ROOT,
    codex_cert_auth_copy_back,
    codex_cert_auth_copy_in,
    codex_cert_home,
    make_codex_harness,
)


T0 = "2026-07-08T00:00:00+00:00"
T1 = "2026-07-08T00:01:00+00:00"
T2 = "2026-07-08T00:02:00+00:00"
T3 = "2026-07-08T00:03:00+00:00"


@contextlib.contextmanager
def patched_cert_home(path: Path | None):
    old = os.environ.get("AGENT_COMMS_CODEX_CERT_HOME")
    if path is None:
        os.environ.pop("AGENT_COMMS_CODEX_CERT_HOME", None)
    else:
        os.environ["AGENT_COMMS_CODEX_CERT_HOME"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("AGENT_COMMS_CODEX_CERT_HOME", None)
        else:
            os.environ["AGENT_COMMS_CODEX_CERT_HOME"] = old


def write_auth(path: Path, *, token: str, last_refresh: str | None = T0) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"access_token": token}
    if last_refresh is not None:
        payload["last_refresh"] = last_refresh
    content = json.dumps(payload, sort_keys=True)
    path.write_text(content)
    return content


class CertHarnessAuthTest(unittest.TestCase):
    def test_t1_rotation_survives_copy_back_to_cert_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cert_auth = root / "cert" / "auth.json"
            temp_home = root / "temp-home"
            temp_home.mkdir()
            write_auth(cert_auth, token="original", last_refresh=T0)

            snapshot = codex_cert_auth_copy_in(cert_auth.parent, temp_home)
            rotated = write_auth(temp_home / "auth.json", token="rotated", last_refresh=T1)

            codex_cert_auth_copy_back(cert_auth.parent, temp_home, snapshot)

            self.assertEqual(cert_auth.read_text(), rotated)

    def test_t2_conflict_resolution_uses_newer_last_refresh_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cert_home = root / "cert"
            temp_home = root / "temp-home"
            temp_home.mkdir()
            cert_auth = cert_home / "auth.json"

            cases = [
                ("external_newer_wins", T2, T1, "external", False),
                ("temp_newer_wins", T1, T2, "temp", True),
                ("current_missing_loses", None, T2, "temp", True),
                ("temp_missing_loses", T2, None, "external", False),
                ("both_missing_keeps_current", None, None, "external", False),
            ]
            for name, current_refresh, temp_refresh, expected_token, temp_should_win in cases:
                with self.subTest(name=name):
                    write_auth(cert_auth, token="snapshot", last_refresh=T0)
                    snapshot = codex_cert_auth_copy_in(cert_home, temp_home)
                    external = write_auth(cert_auth, token="external", last_refresh=current_refresh)
                    temp = write_auth(temp_home / "auth.json", token="temp", last_refresh=temp_refresh)

                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        codex_cert_auth_copy_back(cert_home, temp_home, snapshot)

                    warning = stderr.getvalue()
                    self.assertIn("WARNING:", warning)
                    self.assertIn(str(cert_auth), warning)
                    self.assertIn(repr(current_refresh), warning)
                    self.assertIn(repr(temp_refresh), warning)
                    self.assertEqual(json.loads(cert_auth.read_text())["access_token"], expected_token)
                    self.assertEqual(cert_auth.read_text(), temp if temp_should_win else external)

    def test_t3_torn_temp_auth_is_rejected_without_touching_cert_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cert_home = root / "cert"
            temp_home = root / "temp-home"
            temp_home.mkdir()
            cert_auth = cert_home / "auth.json"
            original = write_auth(cert_auth, token="original", last_refresh=T0)
            snapshot = codex_cert_auth_copy_in(cert_home, temp_home)
            (temp_home / "auth.json").write_text("{not-json")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                codex_cert_auth_copy_back(cert_home, temp_home, snapshot)

            self.assertEqual(cert_auth.read_text(), original)
            self.assertIn("WARNING:", stderr.getvalue())
            self.assertIn("invalid JSON", stderr.getvalue())

    def test_t4_harness_close_runs_before_tempdir_cleanup_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            cert_home = Path(outer) / "cert"
            cert_auth = cert_home / "auth.json"
            write_auth(cert_auth, token="original", last_refresh=T0)

            with patched_cert_home(cert_home):
                with self.assertRaises(RuntimeError):
                    with tempfile.TemporaryDirectory() as temp_dir, contextlib.ExitStack() as stack:
                        harness = make_codex_harness(Path(temp_dir))
                        stack.callback(harness.close)
                        write_auth(Path(temp_dir) / "codex-home" / "auth.json", token="rotated", last_refresh=T1)
                        raise RuntimeError("body failed")

            self.assertEqual(json.loads(cert_auth.read_text())["access_token"], "rotated")

    def test_t5_codex_cert_home_source_is_dedicated_not_production_auth(self) -> None:
        production_auth = ROOT / "config" / "codex-home" / "auth.json"
        with patched_cert_home(None):
            default_home = Path("~/.agent-comms/codex-cert-home").expanduser()
            self.assertEqual(codex_cert_home(), default_home)
            self.assertNotEqual(codex_cert_home() / "auth.json", production_auth)

        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "cert-home"
            with patched_cert_home(override):
                self.assertEqual(codex_cert_home(), override)
                self.assertNotEqual(codex_cert_home() / "auth.json", production_auth)


if __name__ == "__main__":
    unittest.main()
