from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import unittest
from pathlib import Path

from agent_comms.spawn import DEFAULT_WORKER_PROMPT

ROOT = Path(__file__).resolve().parents[2]
SPAWN_SOURCE = (ROOT / "agent_comms" / "spawn.py").read_text()


def _rendered() -> str:
    return DEFAULT_WORKER_PROMPT.format(actor_id="alpha-claude-worker", message_id="msg-test")


class WorkerPromptCooperativeCloseTest(unittest.TestCase):
    """T1: the worker prompt tells every terminal outcome to reply+close+exit,
    while code makes clear this is cooperative, never a mechanical protocol
    boundary. Prevention only; mechanical recovery lives in the supervisor and
    monitor reconciliation paths, not in this text."""

    def test_prompt_names_every_terminal_outcome(self) -> None:
        prompt = _rendered().lower()
        # success, BLOCKED, refusal, and no-change must each be named so the
        # worker does not treat "reply + close" as success-only.
        self.assertIn("blocked", prompt)
        self.assertIn("refuse", prompt)
        for phrase in ("succeed", "nothing"):
            self.assertIn(phrase, prompt)

    def test_prompt_pins_reply_then_close_then_exit_order(self) -> None:
        prompt = _rendered()
        self.assertIn("send_message", prompt)
        self.assertIn("parent_message_id=msg-test", prompt)
        self.assertIn("close_message", prompt)
        reply_at = prompt.index("send_message")
        close_at = prompt.index("close_message")
        exit_at = prompt.rindex("exit")
        self.assertLess(reply_at, close_at, "reply must be instructed before close")
        self.assertLess(close_at, exit_at, "close must be instructed before exit")

    def test_prompt_requires_close_even_on_non_success(self) -> None:
        # The trigger must never be left open on exit, so a one-shot BLOCKED or
        # refusal worker still closes rather than dropping the dispatch to TTL.
        prompt = _rendered().lower()
        self.assertIn("never exit", prompt)
        self.assertIn("still open", prompt)

    def test_prompt_does_not_claim_close_means_work_succeeded(self) -> None:
        # Closing is a protocol-terminal signal, not a success claim. Pin the
        # disclaimer so a future edit cannot quietly turn close into "done".
        prompt = _rendered().lower()
        self.assertIn("never claims", prompt)
        self.assertIn("succeeded", prompt)

    def test_prompt_uses_only_supported_placeholders(self) -> None:
        rendered = _rendered()
        for leftover in ("{actor_id}", "{message_id}", "{worker_prompt}", "{", "}"):
            self.assertNotIn(leftover, rendered)

    def test_prompt_keeps_bootstrap_marker_and_inbox_call(self) -> None:
        prompt = _rendered()
        self.assertIn("WakePolicy=worker_dispatch_readwrite_bounded", prompt)
        self.assertIn("mcp__agent-comms__list_inbox", prompt)

    def test_code_declares_prompt_compliance_is_cooperative_not_mechanical(self) -> None:
        # T1 also requires proving code does not claim prompt compliance is
        # mechanical. The declaration lives next to the constant in spawn.py.
        source = SPAWN_SOURCE.lower()
        self.assertIn("cooperative", source)
        self.assertIn("not a protocol boundary", source)
        # Recovery for a worker that ignores the prompt is mechanical and lives
        # elsewhere; the declaration must point at it (supervisor / monitor).
        self.assertTrue(
            "supervisor" in source or "monitor" in source or "reconcil" in source,
            "cooperative-prompt note must point at the mechanical recovery path",
        )
        # And it must not assert the prompt itself mechanically enforces closure.
        self.assertNotIn("prompt compliance is mechanical", source)
        self.assertNotIn("prompt guarantees", source)


if __name__ == "__main__":
    unittest.main()
