import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import importlib.util
import unittest
from unittest.mock import Mock, patch as P
from tests import dispatch_cell_harness as h


class CodexCellPreflightTest(unittest.TestCase):
    def test_preflight_gates(self):
        with h.contextlib.ExitStack() as stack:
            enter = stack.enter_context
            home = h.Path(enter(h.tempfile.TemporaryDirectory()))
            enter(P.object(h, "codex_cert_home", return_value=home))
            which = enter(P.object(h.shutil, "which", return_value=None))
            run = enter(P.object(h.subprocess, "run"))
            self.assertFalse(h.codex_cell_preflight()[0] or run.called)
            which.return_value = "/fake/codex"
            errors = (h.subprocess.TimeoutExpired("secret", 10), OSError("secret"))
            for value in (1, 0, *errors):
                run.side_effect = value if isinstance(value, Exception) else None
                run.return_value = Mock(returncode=value, stdout="", stderr="")
                ready, reason = h.codex_cell_preflight()
                self.assertEqual((ready, "secret" in reason), (False, False))
                self.assertTrue(reason.endswith(f"CODEX_HOME={home} codex login"))
            run.side_effect = None
            run.return_value = Mock(returncode=0, stdout="Logged in", stderr="")
            bad = '{"tokens":{"access_token":123}}'
            for doc in (None, "secret", "{}", '{"tokens":[]}', bad):
                if doc is not None:
                    (home / "auth.json").write_text(doc)
                ready, reason = h.codex_cell_preflight()
                self.assertFalse(ready or "secret" in reason)
                self.assertIn("expiry", reason)
            expiry = enter(P.object(h, "access_token_remaining_seconds"))
            for margin in (h.spawn_freshness_margin_seconds(), 321):
                key = "AGENT_COMMS_CODEX_SPAWN_FRESHNESS_MARGIN_SECONDS"
                enter(P.dict(h.os.environ, {key: str(margin)}))
                for extra in (0, 1):
                    life = h.POSITIVE_CELL_TTL_SECONDS + margin + extra
                    expiry.return_value = life
                    self.assertEqual(h.codex_cell_preflight()[0], bool(extra))

    def test_import_skips_all_bodies(self):
        spec = importlib.util.find_spec("tests.cells.test_cell_codex")
        module = importlib.util.module_from_spec(spec)
        with P.object(h, "codex_cell_preflight", return_value=(False, "stale")) as p:
            spec.loader.exec_module(module)
        p.assert_called_once_with()
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        for case in next(iter(suite)):
            setattr(case, case._testMethodName, Mock(side_effect=AssertionError()))
        result = suite.run(unittest.TestResult())
        self.assertEqual((result.testsRun, result.errors, result.failures), (8, [], []))
        self.assertEqual([reason for _, reason in result.skipped], ["stale"] * 8)
