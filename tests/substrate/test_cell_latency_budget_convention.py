"""Convention test: runtime cell positive-path latency budgets.

Cycle runtime-cell-latency-budget-002. The positive-path runtime cells
dispatch a real model-driven worker that must discover MCP tools, perform
the probe, reply, and close. Those paths must use the named budgets from
tests/dispatch_cell_harness.py rather than short TTL literals; only the
two dedicated hard-TTL termination cells may use their short literal
TTLs. Enforced hermetically by parsing the cell modules with ast and
classifying every dispatch call by its enclosing test function, so the
check needs no runtime, no auth, and no line-number pinning.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import unittest
from pathlib import Path

import tests.dispatch_cell_harness as harness_module

TESTS_DIR = Path(__file__).resolve().parents[1]
HARNESS_PATH = TESTS_DIR / "dispatch_cell_harness.py"
CELL_MODULES = {
    "codex": TESTS_DIR / "cells" / "test_cell_codex.py",
    "claude": TESTS_DIR / "cells" / "test_cell_claude.py",
}

TTL_NAME = "POSITIVE_CELL_TTL_SECONDS"
WAIT_NAME = "POSITIVE_CELL_TERMINAL_WAIT_SECONDS"
KILL_GRACE_NAME = "RUNTIME_KILL_GRACE_SECONDS"
CLAUDE_T8_TTL_NAME = "CLAUDE_SUPERVISOR_ROOT_T8_TTL_SECONDS"
CLAUDE_T8_WAIT_NAME = "CLAUDE_SUPERVISOR_ROOT_T8_TERMINAL_WAIT_SECONDS"

EXPECTED_TTL = 90
EXPECTED_KILL_GRACE = 30
EXPECTED_WAIT = 150
EXPECTED_CLAUDE_T8_TTL = 180
EXPECTED_CLAUDE_T8_WAIT = 240

# The only cells allowed to dispatch with a short literal TTL: they prove
# forced termination of a real runtime at that boundary.
HARD_TTL_TESTS = {
    "codex": ("test_hard_ttl_kills_real_codex_task", 3),
    "claude": ("test_claude_hard_ttl_kills_real_task", 1),
}

CLAUDE_T8_TEST = "test_claude_cannot_touch_protected_supervisor_root"

# Every model-driven positive dispatch path, per module, whether it goes
# through dispatch_and_wait or calls store.dispatch_agent directly. The
# dead-worker-terminalization T8 protected-supervisor-root negative cell is
# also model-driven (the worker runs one probe, replies, and closes), so it
# uses the same positive-cell budgets and is counted here (codex +1, claude +1).
EXPECTED_POSITIVE_DISPATCHES = {"codex": 6, "claude": 10}

DISPATCH_CALL_NAMES = {"dispatch_and_wait", "dispatch_agent"}


def iter_test_functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            yield node


def call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def wait_until_timeout(call: ast.Call) -> ast.expr | None:
    value = keyword_value(call, "timeout_seconds")
    if value is None and len(call.args) >= 2:
        value = call.args[1]
    return value


def is_named_constant(node: ast.expr | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def describe(node: ast.expr | None) -> str:
    return ast.unparse(node) if node is not None else "<missing>"


class LatencyBudgetConstantsTest(unittest.TestCase):
    def test_named_budgets_exist_with_expected_values_and_ordering(self) -> None:
        ttl = getattr(harness_module, TTL_NAME, None)
        kill_grace = getattr(harness_module, KILL_GRACE_NAME, None)
        wait = getattr(harness_module, WAIT_NAME, None)
        self.assertEqual(ttl, EXPECTED_TTL, f"{TTL_NAME} must be {EXPECTED_TTL}")
        self.assertEqual(
            kill_grace, EXPECTED_KILL_GRACE, f"{KILL_GRACE_NAME} must be {EXPECTED_KILL_GRACE}"
        )
        self.assertEqual(wait, EXPECTED_WAIT, f"{WAIT_NAME} must be {EXPECTED_WAIT}")
        self.assertGreater(
            wait,
            ttl + kill_grace,
            "terminal wait must exceed TTL plus kill grace so the harness "
            "observes the settled row instead of racing forced termination",
        )

    def test_harness_has_no_legacy_timeout_wrapper_kill_grace_argv(self) -> None:
        # The temp adapters now route through the real per-dispatch supervisor;
        # the removed legacy timeout-wrapper mode (and its --kill-after-seconds
        # argv) must not reappear in the harness. The supervisor's own kill grace
        # (adapters._base.KILL_AFTER_SECONDS) enforces TERM/KILL now.
        source = HARNESS_PATH.read_text()
        self.assertNotIn("--kill-after-seconds", source)
        self.assertNotIn("--ttl-seconds", source)


class PositiveCellBudgetConventionTest(unittest.TestCase):
    def check_module(self, runtime: str) -> None:
        module_path = CELL_MODULES[runtime]
        hard_ttl_name, hard_ttl_value = HARD_TTL_TESTS[runtime]
        tree = ast.parse(module_path.read_text())
        module_constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance((target := node.targets[0]), ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        violations: list[str] = []
        positive_dispatches = 0
        hard_ttl_dispatches = 0
        seen_functions: set[str] = set()

        for function in iter_test_functions(tree):
            seen_functions.add(function.name)
            exempt = function.name == hard_ttl_name
            dedicated_claude_t8 = runtime == "claude" and function.name == CLAUDE_T8_TEST
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                name = call_name(node)
                if name in DISPATCH_CALL_NAMES:
                    ttl = keyword_value(node, "ttl_seconds")
                    if exempt:
                        hard_ttl_dispatches += 1
                        if not (isinstance(ttl, ast.Constant) and ttl.value == hard_ttl_value):
                            violations.append(
                                f"{function.name} line {node.lineno}: hard-TTL dispatch "
                                f"must keep literal ttl_seconds={hard_ttl_value}, "
                                f"got {describe(ttl)}"
                            )
                        continue
                    positive_dispatches += 1
                    expected_ttl_name = CLAUDE_T8_TTL_NAME if dedicated_claude_t8 else TTL_NAME
                    expected_wait_name = CLAUDE_T8_WAIT_NAME if dedicated_claude_t8 else WAIT_NAME
                    if not is_named_constant(ttl, expected_ttl_name):
                        violations.append(
                            f"{function.name} line {node.lineno}: positive dispatch "
                            f"ttl_seconds must be {expected_ttl_name}, got {describe(ttl)}"
                        )
                    if name == "dispatch_and_wait":
                        timeout = keyword_value(node, "timeout_seconds")
                        if not is_named_constant(timeout, expected_wait_name):
                            violations.append(
                                f"{function.name} line {node.lineno}: dispatch_and_wait "
                                f"timeout_seconds must be {expected_wait_name}, got {describe(timeout)}"
                            )
                elif name == "wait_until" and not exempt:
                    timeout = wait_until_timeout(node)
                    if not is_named_constant(timeout, WAIT_NAME):
                        violations.append(
                            f"{function.name} line {node.lineno}: positive-path "
                            f"wait_until timeout must be {WAIT_NAME}, got {describe(timeout)}"
                        )

        self.assertIn(
            hard_ttl_name,
            seen_functions,
            f"hard-TTL cell {hard_ttl_name} must remain in {module_path.name}",
        )
        self.assertEqual(
            hard_ttl_dispatches,
            1,
            f"{hard_ttl_name} must contain exactly one dispatch call",
        )
        self.assertEqual(
            positive_dispatches,
            EXPECTED_POSITIVE_DISPATCHES[runtime],
            f"{module_path.name}: expected {EXPECTED_POSITIVE_DISPATCHES[runtime]} "
            f"positive dispatch paths, found {positive_dispatches}",
        )
        if runtime == "claude":
            self.assertEqual(module_constants.get(CLAUDE_T8_TTL_NAME), EXPECTED_CLAUDE_T8_TTL)
            self.assertEqual(module_constants.get(CLAUDE_T8_WAIT_NAME), EXPECTED_CLAUDE_T8_WAIT)
            self.assertGreater(
                EXPECTED_CLAUDE_T8_WAIT,
                EXPECTED_CLAUDE_T8_TTL + EXPECTED_KILL_GRACE,
            )
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_codex_positive_cells_use_named_budgets(self) -> None:
        self.check_module("codex")

    def test_claude_positive_cells_use_named_budgets(self) -> None:
        self.check_module("claude")


if __name__ == "__main__":
    unittest.main()
