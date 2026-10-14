import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import json
import sys
import unittest
from pathlib import Path

from agent_comms import doctor, paths, runtimes
from agent_comms.adapters.registry import ADAPTERS_BY_RUNTIME, adapter_for
from agent_comms.spawn import ALLOWED_RUNTIMES, render_spawn
from tests.substrate.test_setup import HUMAN, _Scratch

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = Path(__file__).resolve().parents[2] / "agent_comms"
NAMES = ("claude", "codex", "fake")
# Modules that look runtimes up instead of naming them (ac-4ao.10 slice 4).
RUNTIME_NEUTRAL = ("spawn.py", "setup.py", "doctor.py", "adapters/registry.py")


def runtime_name_uses(path: Path) -> list[str]:
    """Each place ``path`` uses a runtime name as a value: a comparison
    operand, a dict key, a set/list/tuple element, a call argument or a
    subscript. Docstrings and message text are free."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def named(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Constant) and node.value in NAMES

    for node in ast.walk(tree):
        candidates: list[ast.AST | None] = []
        if isinstance(node, ast.Compare):
            candidates = [node.left, *node.comparators]
        elif isinstance(node, ast.Dict):
            candidates = [*node.keys, *node.values]
        elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            candidates = list(node.elts)
        elif isinstance(node, ast.Call):
            candidates = [*node.args, *(keyword.value for keyword in node.keywords)]
        elif isinstance(node, ast.Subscript):
            candidates = [node.slice]
        found.extend(f"{path.name}:{item.lineno} {item.value!r}" for item in candidates if named(item))
    return found


class RuntimeRegistryTest(unittest.TestCase):
    def test_every_runtime_implements_the_protocol(self) -> None:
        self.assertEqual(runtimes.NAMES, NAMES)
        self.assertEqual(tuple(runtime.name for runtime in runtimes.registered()), NAMES)
        for runtime in runtimes.registered():
            with self.subTest(runtime=runtime.name):
                self.assertIsInstance(runtime, runtimes.Runtime)
                self.assertIs(runtimes.get(runtime.name), runtime)
                self.assertIs(ADAPTERS_BY_RUNTIME[runtime.name], runtime.adapter_class)
                self.assertIsInstance(adapter_for(runtime.name), runtime.adapter_class)

    def test_an_unknown_runtime_is_refused_with_the_allowed_names(self) -> None:
        with self.assertRaisesRegex(Exception, "unsupported runtime 'gemini'; allowed: claude, codex, fake"):
            runtimes.get("gemini")
        self.assertEqual(ALLOWED_RUNTIMES, NAMES)

    def test_spawn_setup_doctor_and_registry_do_not_name_a_runtime(self) -> None:
        uses = [use for module in RUNTIME_NEUTRAL for use in runtime_name_uses(SOURCE / module)]
        self.assertEqual(uses, [])


class RenderIdentityTest(unittest.TestCase):
    """The spawn blocks are byte-identical to what 1d3577f rendered."""

    def test_render_spawn_matches_the_snapshot(self) -> None:
        expected = json.loads((FIXTURES / "runtime_render_snapshot.json").read_text(encoding="utf-8"))
        for name in NAMES:
            for key, kwargs in ((name, {}), (f"{name}+codex_home", {"codex_home": "/h/codex-home"})):
                with self.subTest(key=key):
                    rendered = render_spawn(name, "team-x-worker", **kwargs)
                    self.assertEqual(json.dumps(rendered, sort_keys=True), json.dumps(expected[key], sort_keys=True))


class SetupDoctorIdentityTest(_Scratch):
    """Setup and doctor report what 1d3577f reported, with every runtime registered."""

    def capture(self, runtimes: str) -> dict:
        rc, result = self.setup("--runtimes", runtimes, "--clients", "claude,codex", "--human", "alice", "--human-id", HUMAN)
        report = doctor.run_doctor(db_path=self.db, db_explicit=True, which=self.clients.which, run=self.clients.run)
        text = json.dumps({"rc": rc, "setup": result, "doctor": report})
        for path, label in (
            (paths.runtime_root(), "<RUNTIME_ROOT>"),
            (self.root.resolve(), "<ROOT>"),
            (self.root, "<ROOT>"),
            (Path(sys.executable).parent, "<BIN>"),
        ):
            text = text.replace(json.dumps(str(path))[1:-1], label)
        return json.loads(text)

    def test_setup_and_doctor_match_the_snapshot_with_every_runtime(self) -> None:
        expected = json.loads((FIXTURES / "runtime_setup_doctor_snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(json.dumps(self.capture("claude,codex,fake")), json.dumps(expected))

    def test_setup_and_doctor_match_the_snapshot_with_only_fake(self) -> None:
        """The absent-runtime rows: provision_absent and doctor_skip."""
        expected = json.loads((FIXTURES / "runtime_setup_doctor_fake_snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(json.dumps(self.capture("fake")), json.dumps(expected))


if __name__ == "__main__":
    unittest.main()
