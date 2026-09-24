import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import unittest


class ConstantsTest(unittest.TestCase):
    def test_classify_only_unambiguous_terminal_messages(self):
        from agent_comms.codex_auth_constants import (
            REFRESH_TOKEN_EXPIRED_MESSAGE as expired,
            REFRESH_TOKEN_INVALIDATED_MESSAGE as revoked,
        )
        from agent_comms.codex_auth_refresh import classify_refresh_failure

        reused = "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again."
        for output, reason in (
            (expired, "expired"),
            (revoked, "revoked"),
            (expired + revoked, None),
            ("noise", None),
            (reused, None),
            (None, None),
        ):
            with self.subTest(output=output):
                self.assertEqual(classify_refresh_failure(output), reason)
