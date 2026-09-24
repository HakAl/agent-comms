import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import unittest

from agent_comms.schema import ValidationError, identity_to_path_segment


class IdentityPathSegmentTest(unittest.TestCase):
    def test_identity_to_path_segment_passthrough_canonical(self) -> None:
        self.assertEqual(
            identity_to_path_segment("alpha-codex-worker"),
            "alpha-codex-worker",
        )

    def test_identity_to_path_segment_rejects_noncanonical(self) -> None:
        bad_identities = [
            "a/b",
            "..",
            ".",
            "a\\b",
            "a\x00b",
            "",
            "   ",
            " alpha-worker ",
            "Alpha-Worker",
            "NUL",
            "Con",
            "CONIN$",
            "clock$",
            "conout$",
            "a_b",
            "nul",
            "com1",
            "lpt1",
        ]

        for identity in bad_identities:
            with self.subTest(identity=identity):
                with self.assertRaises(ValidationError):
                    identity_to_path_segment(identity)


if __name__ == "__main__":
    unittest.main()
