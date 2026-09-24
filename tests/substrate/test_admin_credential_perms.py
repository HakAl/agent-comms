import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.cli import _helpers
from agent_comms.schema import ValidationError


class AdminCredentialPermsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_token = os.environ.get("AGENT_COMMS_ADMIN_TOKEN")

    def tearDown(self) -> None:
        if self._original_token is None:
            os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        else:
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = self._original_token

    def _write_token(self, root: Path, mode: int, token: str = "operator-secret") -> Path:
        token_path = root / "admin-token"
        token_path.write_text(token)
        os.chmod(token_path, mode)
        return token_path

    def test_mode_0600_matching_token_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = self._write_token(Path(temp_dir), 0o600)
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "operator-secret"

            with mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", token_path):
                _helpers.require_admin_credential()

    def test_mode_0644_matching_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = self._write_token(Path(temp_dir), 0o644)
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "operator-secret"

            with mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", token_path):
                with self.assertRaises(ValidationError):
                    _helpers.require_admin_credential()

    def test_mode_0600_wrong_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = self._write_token(Path(temp_dir), 0o600)
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "wrong-secret"

            with mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", token_path):
                with self.assertRaises(ValidationError):
                    _helpers.require_admin_credential()

    def test_mode_0600_empty_env_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = self._write_token(Path(temp_dir), 0o600)
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = ""

            with mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", token_path):
                with self.assertRaises(ValidationError):
                    _helpers.require_admin_credential()

    def test_missing_token_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "admin-token"
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "operator-secret"

            with mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", token_path):
                with self.assertRaises(ValidationError):
                    _helpers.require_admin_credential()

    def test_loose_perms_raise_before_compare_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = self._write_token(Path(temp_dir), 0o644)
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "operator-secret"

            with mock.patch.object(_helpers, "ADMIN_TOKEN_PATH", token_path):
                with mock.patch(
                    "agent_comms.cli._helpers.hmac.compare_digest",
                    side_effect=AssertionError("compare_digest called"),
                ) as compare_digest:
                    with self.assertRaises(ValidationError):
                        _helpers.require_admin_credential()

            compare_digest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
