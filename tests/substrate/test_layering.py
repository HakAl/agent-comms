"""Import layering: ``agent_comms.cli`` is the top of the package.

No domain module imports from the CLI package, at module level or inside a
function. Checked with the AST (imports only, no runtime), so a lazily
imported ``from .cli import x`` inside a function body is caught as well.
"""
from __future__ import annotations

import tests.isolation  # noqa: F401

import ast
import unittest
from pathlib import Path

import agent_comms

PACKAGE_ROOT = Path(agent_comms.__file__).resolve().parent


def cli_import_edges(source: str, *, module_relpath: str) -> list[str]:
    """Return every import in ``source`` that resolves into ``agent_comms.cli``.

    ``module_relpath`` is the module's path relative to the package root; it
    decides how many package levels a relative import climbs.
    """
    depth = len(Path(module_relpath).parent.parts)  # 0 for agent_comms/x.py
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "agent_comms.cli" or alias.name.startswith("agent_comms.cli."):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                if module == "agent_comms.cli" or module.startswith("agent_comms.cli."):
                    found.append(module)
                elif module == "agent_comms" and any(a.name == "cli" for a in node.names):
                    found.append("agent_comms.cli")
                continue
            # Relative: level 1 is the module's own package, each extra level
            # climbs one package. Resolve to a package-root-relative dotted name.
            climb = node.level - 1
            if climb > depth:
                continue  # above the package root; cannot be agent_comms.cli
            base = Path(module_relpath).parent.parts[: depth - climb]
            parts = list(base) + (module.split(".") if module else [])
            if parts[:1] == ["cli"]:
                found.append(".".join(["agent_comms", *parts]))
            elif not parts and any(a.name == "cli" for a in node.names):
                found.append("agent_comms.cli")
    return found


class CliLayeringTest(unittest.TestCase):
    def test_no_module_outside_cli_imports_the_cli_package(self) -> None:
        offenders: list[tuple[str, str]] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            relpath = path.relative_to(PACKAGE_ROOT).as_posix()
            if relpath.startswith("cli/"):
                continue
            for edge in cli_import_edges(path.read_text(encoding="utf-8"), module_relpath=relpath):
                offenders.append((relpath, edge))
        self.assertEqual(offenders, [])

    def test_detector_sees_each_import_form(self) -> None:
        # Negative controls: every spelling the package has used is caught.
        cases = {
            "from .cli._helpers import x\n": "agent_comms.cli._helpers",
            "def f():\n    from .cli import settlement_plan\n": "agent_comms.cli",
            "from . import cli\n": "agent_comms.cli",
            "import agent_comms.cli\n": "agent_comms.cli",
            "from agent_comms.cli import run\n": "agent_comms.cli",
            "from agent_comms import cli\n": "agent_comms.cli",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(cli_import_edges(source, module_relpath="x.py"), [expected])
        # A subpackage module climbing two levels reaches the CLI package too.
        self.assertEqual(
            cli_import_edges("from ..cli import run\n", module_relpath="reviewing/store.py"),
            ["agent_comms.cli"],
        )
        # Not the CLI package: a sibling named like it one level down, or stdlib.
        self.assertEqual(cli_import_edges("from .clients import x\nimport click\n", module_relpath="x.py"), [])
        self.assertEqual(cli_import_edges("from . import paths\n", module_relpath="reviewing/store.py"), [])


if __name__ == "__main__":
    unittest.main()
