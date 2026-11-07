"""Import layering, checked with the AST (imports only, no runtime).

``agent_comms.cli`` is the top of the package: no domain module imports from
it, at module level or inside a function. ``agent_comms.supervising`` imports
down a declared order as module objects, ``agent_comms.supervisor`` is a
re-export facade that only the wrapper imports, and tests patch the owning
module rather than the facade.
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


# --------------------------------------------------------------------------- #
# agent_comms.supervising: dependency order, the facade, and where patches go
# --------------------------------------------------------------------------- #

SUPERVISING_ORDER = ["paths", "process", "zdotdir", "evidence", "reaper", "protocol", "server", "spawn", "janitor"]
SUPERVISING_ROOT = PACKAGE_ROOT / "supervising"


def supervising_edges(source: str) -> list[tuple[str, str]]:
    """Return ``(form, sibling)`` for every intra-package import in a supervising module.

    ``form`` is ``"module"`` for ``from . import x`` (the only form allowed: a
    patch on the owning module must bite every caller) and ``"name"`` for
    ``from .x import y``.
    """
    edges: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module is None:
                edges.extend(("module", alias.name) for alias in node.names)
            else:
                edges.append(("name", node.module.split(".")[0]))
    return edges


def facade_import_edges(source: str, *, module_relpath: str) -> list[str]:
    """Every import in ``source`` that resolves to ``agent_comms.supervisor`` (the facade)."""
    depth = len(Path(module_relpath).parent.parts)
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name == "agent_comms.supervisor")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                if module == "agent_comms.supervisor" or (module == "agent_comms" and any(a.name == "supervisor" for a in node.names)):
                    found.append("agent_comms.supervisor")
                continue
            climb = node.level - 1
            if climb > depth:
                continue
            base = Path(module_relpath).parent.parts[: depth - climb]
            parts = list(base) + (module.split(".") if module else [])
            if parts == ["supervisor"] or (not parts and any(a.name == "supervisor" for a in node.names)):
                found.append("agent_comms.supervisor")
    return found


def top_level_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def facade_patches(source: str) -> list[int]:
    """Line numbers of calls that patch a name on the facade ``agent_comms.supervisor``.

    Walks the AST, so the multiline ``patch.object(\n    supervisor,\n    "name")``
    spelling is seen too. Two forms count: ``patch.object(supervisor, ...)`` with
    the bare module as the target, and ``patch("agent_comms.supervisor.<name>")``.
    Patching ``supervisor.os`` / ``supervisor.socket`` is the stdlib module itself
    and stays allowed, as does patching ``supervising.<module>``.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_patch_object = isinstance(func, ast.Attribute) and func.attr == "object" and (
            (isinstance(func.value, ast.Attribute) and func.value.attr == "patch")
            or (isinstance(func.value, ast.Name) and func.value.id == "patch")
        )
        is_patch = (isinstance(func, ast.Attribute) and func.attr == "patch") or (
            isinstance(func, ast.Name) and func.id == "patch"
        )
        target = node.args[0]
        if is_patch_object and isinstance(target, ast.Name) and target.id == "supervisor":
            found.append(node.lineno)
        elif is_patch and isinstance(target, ast.Constant) and isinstance(target.value, str):
            parts = target.value.split(".")
            if parts[:2] == ["agent_comms", "supervisor"] and len(parts) == 3:
                found.append(node.lineno)
    return found


class SupervisingLayeringTest(unittest.TestCase):
    def test_supervising_modules_import_down_the_declared_order_as_module_objects(self) -> None:
        self.assertEqual(sorted(p.stem for p in SUPERVISING_ROOT.glob("*.py") if p.stem != "__init__"), sorted(SUPERVISING_ORDER))
        offenders: list[tuple[str, str, str]] = []
        for module in SUPERVISING_ORDER:
            for form, sibling in supervising_edges((SUPERVISING_ROOT / f"{module}.py").read_text(encoding="utf-8")):
                if form != "module":
                    offenders.append((module, "from .x import y", sibling))
                elif sibling not in SUPERVISING_ORDER or SUPERVISING_ORDER.index(sibling) >= SUPERVISING_ORDER.index(module):
                    offenders.append((module, "imports a module above it", sibling))
        self.assertEqual(offenders, [])
        # Negative controls for the detector.
        self.assertEqual(supervising_edges("from . import paths\n"), [("module", "paths")])
        self.assertEqual(supervising_edges("from .protocol import _send_json\n"), [("name", "protocol")])
        self.assertEqual(supervising_edges("from ..clock import utc_now\nfrom .. import terminal_statuses\n"), [])

    def test_facade_reexports_exactly_the_supervising_names(self) -> None:
        from agent_comms import supervisor

        expected: set[str] = {"DISPATCH_TERMINAL_STATUSES"}
        for module in SUPERVISING_ORDER:
            names = top_level_names((SUPERVISING_ROOT / f"{module}.py").read_text(encoding="utf-8"))
            expected |= names - {"logger"}
        self.assertEqual(set(supervisor.__all__), expected)
        self.assertEqual(len(supervisor.__all__), len(set(supervisor.__all__)))
        for name in supervisor.__all__:
            self.assertTrue(hasattr(supervisor, name), name)
        # The facade holds no code of its own: only imports, the two stdlib
        # modules tests patch through it, and __all__.
        self.assertEqual(top_level_names((PACKAGE_ROOT / "supervisor.py").read_text(encoding="utf-8")), {"__all__"})

    def test_only_the_wrapper_imports_the_facade(self) -> None:
        importers: list[str] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            relpath = path.relative_to(PACKAGE_ROOT).as_posix()
            if relpath == "supervisor.py":
                continue
            if facade_import_edges(path.read_text(encoding="utf-8"), module_relpath=relpath):
                importers.append(relpath)
        self.assertEqual(importers, ["timeout_wrapper.py"])
        # Negative controls: each spelling the package used before the split.
        self.assertEqual(facade_import_edges("from .. import paths, supervisor\n", module_relpath="adapters/_base.py"), ["agent_comms.supervisor"])
        self.assertEqual(facade_import_edges("def f():\n    from . import supervisor\n", module_relpath="dispatch_ledger.py"), ["agent_comms.supervisor"])
        self.assertEqual(facade_import_edges("from .supervisor import run_supervisor\n", module_relpath="timeout_wrapper.py"), ["agent_comms.supervisor"])
        self.assertEqual(facade_import_edges("from agent_comms.supervisor import run_supervisor\n", module_relpath="timeout_wrapper.py"), ["agent_comms.supervisor"])
        self.assertEqual(facade_import_edges("from .. import supervising\nfrom . import paths\n", module_relpath="supervising/evidence.py"), [])

    def test_no_test_patches_the_facade(self) -> None:
        tests_root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for path in sorted(tests_root.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            for lineno in facade_patches(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(tests_root)}:{lineno}")
        self.assertEqual(offenders, [])
        # Positive controls, including the repository's multiline spelling.
        self.assertEqual(facade_patches('mock.patch.object(supervisor, "janitor_sweep")\n'), [1])
        self.assertEqual(facade_patches('with mock.patch.object(\n    supervisor,\n    "request_halt", return_value=None\n):\n    pass\n'), [1])
        self.assertEqual(facade_patches('patch.object(supervisor, "janitor_sweep")\n'), [1])
        self.assertEqual(facade_patches('mock.patch("agent_comms.supervisor.janitor_sweep")\n'), [1])
        self.assertEqual(facade_patches('patch(\n    "agent_comms.supervisor.janitor_sweep",\n    return_value=[],\n)\n'), [1])
        # Allowed: the owning module, the stdlib modules reached through the facade, reads.
        self.assertEqual(facade_patches('mock.patch.object(supervising.janitor, "janitor_sweep")\n'), [])
        self.assertEqual(facade_patches('mock.patch.object(supervisor.os, "getpgid")\n'), [])
        self.assertEqual(facade_patches('mock.patch("agent_comms.supervisor.socket.socket")\n'), [])
        self.assertEqual(facade_patches('x = supervisor.janitor_sweep\n'), [])
