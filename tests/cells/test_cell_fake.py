import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import tempfile
import unittest
from pathlib import Path

from tests.dispatch_cell_harness import RUNTIME_KILL_GRACE_SECONDS, make_fake_harness


FAKE_POSITIVE_CELL_TTL_SECONDS = 10
FAKE_POSITIVE_CELL_TERMINAL_WAIT_SECONDS = 45
assert (
    FAKE_POSITIVE_CELL_TERMINAL_WAIT_SECONDS
    > FAKE_POSITIVE_CELL_TTL_SECONDS + RUNTIME_KILL_GRACE_SECONDS
)


class FakeCellTest(unittest.TestCase):
    def test_fake_cell_dispatch_replies_and_closes_inline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            harness = make_fake_harness(Path(temp_dir), cell_delta=True)

            dispatch = harness.dispatch_and_wait(
                idempotency_key="fake-cell-inline",
                ttl_seconds=FAKE_POSITIVE_CELL_TTL_SECONDS,
                timeout_seconds=FAKE_POSITIVE_CELL_TERMINAL_WAIT_SECONDS,
                body=(
                    "Create an ordinary uncommitted file inside this project root. "
                    "Send one normal parented reply, then call close_dispatch with "
                    "result=satisfied, that reply id, and delta=true."
                ),
            )

            harness.assert_closed_with_parented_reply(dispatch)

    def test_v2_close_denials_use_real_dispatch_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            harness = make_fake_harness(root, fail_before_close=True)
            dispatch = harness.store.dispatch_agent(
                "alpha-architect",
                harness.worker_id,
                "fake-v2-semantic-denial",
                "semantic denial",
                "semantic denial",
                [],
                adapter_for_runtime=lambda _runtime: harness.adapter,
                ttl_seconds=FAKE_POSITIVE_CELL_TTL_SECONDS,
            )
            try:
                harness.assert_v2_close_denials_leave_state_unchanged(dispatch)
            finally:
                harness.adapter.halt(dispatch["spawn_handle"], dispatch.get("observed_values"))


if __name__ == "__main__":
    unittest.main()
