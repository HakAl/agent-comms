"""The dispatch_agent tool description states where payload_origin belongs.

ac-434: live architects read the old description as "payload_origin goes with
the body" and sent it with an inline body, which the ledger rejects
(``dispatch_payload_origin_invalid``), so every first dispatch failed and was
retried. The description is the only thing a model sees before its first
call, so this pins its wording: payload_origin is required with ``body_file``
and forbidden with ``body``, each stated in its own sentence.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import re
import unittest
from pathlib import Path

from agent_comms import mcp_server

SERVER_SOURCE = Path(mcp_server.__file__)


def _dispatch_agent_docstring() -> str:
    module = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_agent":
            doc = ast.get_docstring(node)
            assert doc, "dispatch_agent has no docstring"
            return doc
    raise AssertionError("dispatch_agent is not defined in mcp_server.py")


class DispatchAgentDescriptionTest(unittest.TestCase):
    def test_description_binds_payload_origin_to_body_file_only(self) -> None:
        doc = " ".join(_dispatch_agent_docstring().split())

        self.assertRegex(
            doc,
            r"Do not send ``payload_origin`` with an inline body; the call is rejected\.",
        )
        self.assertRegex(
            doc,
            r"``body_file``.*together with ``payload_origin`` \(required with ``body_file``",
        )
        # The two forms are separate sentences: no sentence names both ``body``
        # as the inline form and ``payload_origin`` as required.
        for sentence in re.split(r"(?<=\.)\s+", doc):
            if "inline" in sentence.lower() and "required" in sentence.lower():
                self.fail(f"inline body and a required payload_origin share a sentence: {sentence!r}")

    def test_description_names_every_accepted_origin(self) -> None:
        doc = _dispatch_agent_docstring()

        for origin in ("authored_brief", "generated_artifact", "verbatim_source"):
            self.assertIn(origin, doc)


if __name__ == "__main__":
    unittest.main()
