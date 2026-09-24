"""AST architecture guards for the review decomposition.

Constrain the behavior-preserving extraction of ``agent_comms/review.py`` into an
``agent_comms/reviewing`` package, run against the pinned baseline (no extraction
module yet). Each guard is a pure function raising ``GuardError``; green tests feed
the real tree through it and red controls feed a violating fixture through the SAME
function, so a raw measurement is never mistaken for the guard. The allowed import
DAG is an explicit edge set, so an undeclared edge a total order would tolerate is
still rejected.
"""

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import ast
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_comms.paths import REPO_ROOT
from tests.substrate.review_decomposition_support import require_schema_v1

# Ordered steps, the allowed DAG edges, the rank order, milestone tiers, and the Git/ownership
# vocabularies are frozen in review_decomposition_contract.json and rebuilt here as the exact
# tuples/frozensets the guards compare against.
_CONTRACT = json.loads(
    (Path(__file__).parent / "review_decomposition_contract.json").read_text(
        encoding="utf-8"
    )
)
require_schema_v1(_CONTRACT)
EXTRACTION_STEPS = [tuple(step) for step in _CONTRACT["architecture_extraction_steps"]]
ALLOWED_EDGES = frozenset(
    tuple(edge) for edge in _CONTRACT["architecture_allowed_edges"]
)
_RANK_ORDER = _CONTRACT["architecture_rank_order"]
MILESTONE_TIERS = [
    (frozenset(mods), cap) for mods, cap in _CONTRACT["architecture_milestone_tiers"]
]
GIT_MUTATIONS = frozenset(_CONTRACT["architecture_git_mutations"])
WORKTREE_MUTATIONS = frozenset(_CONTRACT["architecture_worktree_mutations"])
_SUBPROCESS_FUNCS = frozenset(_CONTRACT["architecture_subprocess_funcs"])
_GIT_WRAPPERS = dict(_CONTRACT["architecture_git_wrappers"])
_GIT_GLOBAL_VALUE_OPTS = frozenset(_CONTRACT["architecture_git_global_value_opts"])
RECORD_WRITE_FUNCS = frozenset(_CONTRACT["architecture_record_write_funcs"])
BINDING_FUNCS = frozenset(_CONTRACT["architecture_binding_funcs"])

REVIEW_PY = REPO_ROOT / "agent_comms" / "review.py"
REVIEWING_ROOT = REPO_ROOT / "agent_comms" / "reviewing"
CONSUMER_FILES = [
    REPO_ROOT / "agent_comms" / "push_approval.py",
]

# Re-baselined for cycle approval destination binding (contract 18): the v2 payload,
# destination derivation/recheck, and legacy replacement land within measured headroom
# (core 5860, extended 7183). Per-module ceilings and churn limits did not move.
# Re-baselined for review evidence lifecycle Landing 2 (contract 20): the cohesive
# reply-snapshot owner reviewing/reply_snapshots.py joins the reviewing package
# (isolated-index capture plus the legacy single-capture delta extracted from
# mailbox), and reviewing/git_evidence.py gains the reusable F1/F2 custody
# primitives (retained no-follow directory custody, the fchdir/exec Git child, and
# the bounded no-follow index digest). Revision 11 restores readable Ruff 0.16.2
# formatting under the measured core 6449 / extended 7772, so the caps move to
# 6500 / 7800. The shared 700-line per-module ceiling did not move.
CORE_CAP = 6500
EXTENDED_CAP = 7800
PER_MODULE_CAP = 700
# contracts.py owns the schema/validation contract concern and carries a measured
# 1000-line exception under the formatted Extraction 1 re-baseline; every other
# reviewing module stays at the shared per-module ceiling.
PER_MODULE_CAP_EXCEPTIONS = {"contracts.py": 1000}

# EXTRACTION_STEPS (foundational-first), ALLOWED_EDGES (the plan's importer->imported edge
# set; any absent edge is undeclared and the acyclic set makes every cycle undeclared), and
# _RANK (a total order used ONLY to demonstrate the gap) are frozen in the contract.
_STEP_OF = {name: i for i, step in enumerate(EXTRACTION_STEPS) for name in step}
ALL_EXTRACTION_MODULES = frozenset(_STEP_OF)
_RANK = {name: i for i, name in enumerate(_RANK_ORDER)}


def _rank_allows(importer: str, imported: str) -> bool:
    return importer in _RANK and imported in _RANK and _RANK[imported] < _RANK[importer]


# MILESTONE_TIERS (cumulative module sets -> review.py ceiling; a partial/out-of-order set
# is an error), the Git mutation/subprocess/wrapper vocabularies, and the ownership sets
# RECORD_WRITE_FUNCS/BINDING_FUNCS are frozen in the contract. Git wrappers map to their
# subcommand-argument index (run_git/git_proc=1, git_bytes=2; global value-opts skip WITH
# their value). Ownership is AST-bound by enclosing definition name: store.py owns record
# read/write and persistence, ledger_evidence.py owns claim publication and recover-binding.


class GuardError(AssertionError):
    """A modeled architecture violation, raised by a pure guard function."""


def count_loc(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def reviewing_modules(root: Path = REVIEWING_ROOT) -> list[Path]:
    return sorted(root.glob("**/*.py")) if root.exists() else []


def core_manifest() -> list[Path]:
    return [REVIEW_PY, *reviewing_modules()]


def extended_manifest() -> list[Path]:
    return [*core_manifest(), *CONSUMER_FILES]


def present_extraction_modules(root: Path = REVIEWING_ROOT) -> frozenset:
    return frozenset(p.stem for p in reviewing_modules(root)) & ALL_EXTRACTION_MODULES


def milestone_cap(present: frozenset) -> int:
    """review.py ceiling for a completed milestone; raises on a partial/out-of-order set."""
    for module_set, cap in MILESTONE_TIERS:
        if present == module_set:
            return cap
    raise GuardError(f"partial/out-of-order milestone module set: {sorted(present)}")


def _manifest_rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return Path(path).name


def require_within_cap(paths, cap: int, label: str) -> int:
    """Measure the manifest; on breach enumerate every matched path with its line count."""
    measured = [(Path(p), count_loc(p)) for p in paths]
    total = sum(n for _, n in measured)
    if total > cap:
        listing = "; ".join(f"{_manifest_rel(p)}={n}" for p, n in measured)
        raise GuardError(f"{label}: {total} > {cap}; matched: {listing}")
    return total


def require_milestone_within_cap(review_loc: int, present: frozenset) -> int:
    cap = milestone_cap(present)  # raises on partial/out-of-order
    if review_loc > cap:
        raise GuardError(
            f"review.py {review_loc} > milestone cap {cap} for {sorted(present)}"
        )
    return cap


def require_sole_owner(actual: set, expected: str) -> None:
    if actual != {expected}:
        raise GuardError(f"expected sole owner {{{expected}}}, got {sorted(actual)}")


def require_clean_imports(edges, forbidden) -> None:
    problems = list(dag_violations(edges))
    problems += [f"forbidden import {stem}->{target}" for stem, target in forbidden]
    if problems:
        raise GuardError("; ".join(problems))


def require_no_git_mutations(path: Path) -> None:
    violations = git_call_violations(path)
    if violations:
        raise GuardError("; ".join(violations))


def _import_targets(node: ast.AST, importer: str, known: set):
    """Return (edges, forbidden) for one import across all absolute/relative forms
    (relative level>0 resolves as sibling reviewing modules). Monolith/CLI imports
    are forbidden, whether absolute, ``from agent_comms import review/cli``, or upward."""
    raw_edges: list = []
    forbidden: list = []

    def classify_absolute(dotted: str, names) -> None:
        parts = dotted.split(".")
        if parts[:2] == ["agent_comms", "reviewing"]:
            if len(parts) >= 3:
                raw_edges.append(parts[2])
            else:
                raw_edges.extend(n.name for n in names)
        elif dotted == "agent_comms.review" or dotted.startswith("agent_comms.review."):
            forbidden.append((importer, dotted))
        elif dotted == "agent_comms.cli" or dotted.startswith("agent_comms.cli."):
            forbidden.append((importer, dotted))
        elif dotted == "agent_comms":  # from agent_comms import review/cli
            for n in names:
                if n.name == "review":
                    forbidden.append((importer, "agent_comms.review"))
                elif n.name == "cli":
                    forbidden.append((importer, "agent_comms.cli"))

    if isinstance(node, ast.Import):
        for alias in node.names:
            classify_absolute(alias.name, [])
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            classify_absolute(node.module or "", node.names)
        elif node.level == 1:
            if node.module:
                raw_edges.append(node.module.split(".")[0])
            else:
                raw_edges.extend(alias.name for alias in node.names)
        else:  # level >= 2: reaches the agent_comms package or higher
            parts = (node.module or "").split(".") if node.module else []
            top = parts[0] if parts else ""
            if top == "reviewing":  # from ..reviewing[.x] import y: sibling edge
                if len(parts) >= 2:
                    raw_edges.append(parts[1])
                else:
                    raw_edges.extend(alias.name for alias in node.names)
            elif top == "review" or (
                not node.module and any(a.name == "review" for a in node.names)
            ):
                forbidden.append((importer, "agent_comms.review"))
            elif top == "cli" or (
                not node.module and any(a.name == "cli" for a in node.names)
            ):
                forbidden.append((importer, "agent_comms.cli"))
    # Retain self-edges (store: ``from . import store``): undeclared and cyclic, so
    # require_clean_imports must reject them rather than silently drop them.
    edges = {(importer, target) for target in raw_edges if target in known}
    return edges, forbidden


def analyze_reviewing_imports(files):
    """Return (internal_edges, forbidden) for a set of reviewing modules."""
    files = list(files)
    known = {Path(f).stem for f in files}
    edges: set = set()
    forbidden: list = []
    for f in files:
        stem = Path(f).stem
        tree = ast.parse(Path(f).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                e, fb = _import_targets(node, stem, known)
                edges.update(e)
                forbidden.extend(fb)
    return edges, forbidden


def dag_violations(edges) -> list:
    """Undeclared edges (not in ALLOWED_EDGES) plus any cycle among modules."""
    problems = [
        f"undeclared edge {a}->{b}"
        for a, b in sorted(edges)
        if (a, b) not in ALLOWED_EDGES
    ]
    adjacency: dict = {}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict = {}

    def visit(node) -> bool:
        color[node] = GREY
        for nxt in adjacency.get(node, ()):  # noqa: SIM118
            if color.get(nxt, WHITE) == GREY:
                return True
            if color.get(nxt, WHITE) == WHITE and visit(nxt):
                return True
        color[node] = BLACK
        return False

    for node in {a for a, _ in edges} | {b for _, b in edges}:
        if color.get(node, WHITE) == WHITE and visit(node):
            problems.append(f"cycle through {node}")
            break
    return problems


def _parse(path: Path) -> ast.AST:
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _defines_any(path: Path, names: frozenset) -> bool:
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
        for n in ast.walk(_parse(path))
    )


def _calls_attr(path: Path, obj: str, attr: str) -> bool:
    for n in ast.walk(_parse(path)):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == obj
        ):
            return True
    return False


def sqlite_owners(files) -> set:
    return {Path(f).name for f in files if _calls_attr(f, "sqlite3", "connect")}


def record_write_owners(files) -> set:
    return {Path(f).name for f in files if _defines_any(f, RECORD_WRITE_FUNCS)}


def binding_owners(files) -> set:
    return {Path(f).name for f in files if _defines_any(f, BINDING_FUNCS)}


def _phase_owner(successor_stem: str, present) -> str:
    """Pure phase decision: review.py owns the mechanic until its successor is present."""
    return f"{successor_stem}.py" if successor_stem in present else "review.py"


def expected_owner(successor_stem: str) -> str:
    return _phase_owner(successor_stem, {p.stem for p in reviewing_modules()})


def _argv_elements(node: ast.AST):
    """Constant elements of a list OR tuple argv; non-constants become None."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
    return None


def _scan_git_argv(tokens, lineno: int) -> list:
    """Scan git tokens, skipping global options and value-taking globals' values, so
    the first real non-flag token is inspected as the subcommand."""
    rest = [t for t in tokens if isinstance(t, str)]
    violations: list = []
    i, n = 0, len(rest)
    while i < n:
        tok = rest[i]
        if tok in _GIT_GLOBAL_VALUE_OPTS:
            i += 2  # skip global option and its separate value
            continue
        if (
            tok.startswith("--")
            and "=" in tok
            and tok.split("=", 1)[0] in _GIT_GLOBAL_VALUE_OPTS
        ):
            i += 1  # --git-dir=/path carries its value inline
            continue
        if tok.startswith("-"):
            i += 1  # value-less global flag (e.g. --no-pager)
            continue
        if tok in GIT_MUTATIONS:
            violations.append(f"git {tok} at line {lineno}")
        if tok == "worktree" and i + 1 < n and rest[i + 1] in WORKTREE_MUTATIONS:
            violations.append(f"git worktree {rest[i + 1]} at line {lineno}")
        break  # first real non-flag token is the subcommand
    return violations


def _wrapper_name(func: ast.AST):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _subprocess_bindings(tree) -> tuple:
    """Local names bound to the subprocess module and to imported subprocess functions,
    resolving aliases (``import subprocess as sp``; ``from subprocess import run as r``)."""
    module_names, func_aliases = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(
                a.asname or a.name for a in node.names if a.name == "subprocess"
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "subprocess"
        ):
            func_aliases.update(
                a.asname or a.name for a in node.names if a.name in _SUBPROCESS_FUNCS
            )
    return module_names, func_aliases


def _is_subprocess_call(func: ast.AST, module_names: set, func_aliases: set) -> bool:
    """Recognize subprocess.<fn>, alias.<fn>, imported <fn>, or aliased <fn>."""
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _SUBPROCESS_FUNCS
        and isinstance(func.value, ast.Name)
        and func.value.id in module_names
    ):
        return True
    return isinstance(func, ast.Name) and func.id in func_aliases


def git_call_violations(path: Path) -> list:
    """Reject Git mutations as a direct subprocess argv (list or tuple) OR through a
    known wrapper (run_git/git_proc/git_bytes), plus shell=True and string-form git.
    Subprocess is recognized via the module, an import alias, an imported function, or
    a function alias. Read-only argument-vector calls are preserved."""
    tree = _parse(path)
    module_names, func_aliases = _subprocess_bindings(tree)
    violations: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if _is_subprocess_call(func, module_names, func_aliases):
            if any(
                kw.arg == "shell"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                violations.append(f"shell=True at line {node.lineno}")
            argv = node.args[0] if node.args else None
            if (
                isinstance(argv, ast.Constant)
                and isinstance(argv.value, str)
                and "git" in argv.value.split()
            ):
                violations.append(f"string git argv at line {node.lineno}")
                continue
            elements = _argv_elements(argv)
            if elements and elements[0] == "git":
                violations.extend(_scan_git_argv(elements[1:], node.lineno))
            continue
        name = _wrapper_name(func)
        if name in _GIT_WRAPPERS:
            idx = _GIT_WRAPPERS[name]
            tokens = [
                a.value if isinstance(a, ast.Constant) else None
                for a in node.args[idx:]
            ]
            violations.extend(_scan_git_argv(tokens, node.lineno))
    return violations


class _Fixture:
    """A throwaway directory of named python sources for red-control tests."""

    def __init__(self, test: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, name: str, source: str) -> Path:
        path = self.root / name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path


class LocCeilingGuardTest(unittest.TestCase):
    def test_core_manifest_within_cap(self) -> None:
        require_within_cap(core_manifest(), CORE_CAP, "core manifest")

    def test_extended_manifest_within_cap(self) -> None:
        require_within_cap(extended_manifest(), EXTENDED_CAP, "extended manifest")

    def test_every_reviewing_module_within_per_module_cap(self) -> None:
        for module in reviewing_modules():
            cap = PER_MODULE_CAP_EXCEPTIONS.get(module.name, PER_MODULE_CAP)
            with self.subTest(module=module.name):
                require_within_cap([module], cap, module.name)

    def test_glob_captures_new_modules(self) -> None:
        fix = _Fixture(self)
        fix.write("newmod.py", "x = 1\n")
        self.assertEqual([p.name for p in reviewing_modules(fix.root)], ["newmod.py"])

    def test_red_control_cap_breach_enumerates_matched_paths(self) -> None:
        fix = _Fixture(self)
        base = fix.write("small_manifest_probe.py", "x = 1\n")
        big = fix.write(
            "oversized_manifest_probe.py",
            "\n".join("x = 1" for _ in range(CORE_CAP)) + "\n",
        )
        with self.assertRaises(GuardError) as ctx:
            require_within_cap([base, big], CORE_CAP, "core manifest")
        message = str(ctx.exception)
        self.assertIn("oversized_manifest_probe.py", message)
        self.assertIn("small_manifest_probe.py", message)

    def test_red_control_per_module_cap_breach(self) -> None:
        fix = _Fixture(self)
        big = fix.write(
            "huge.py", "\n".join("x = 1" for _ in range(PER_MODULE_CAP + 1)) + "\n"
        )
        with self.assertRaises(GuardError) as ctx:
            require_within_cap([big], PER_MODULE_CAP, "huge.py")
        self.assertIn("huge.py", str(ctx.exception))

    def test_contract_rejects_non_v1_schema_version(self) -> None:
        for bad in (2, "1", 1.0, None):
            with self.assertRaises(ValueError):
                require_schema_v1({"schema_version": bad})


class MilestoneCeilingGuardTest(unittest.TestCase):
    def test_current_phase_review_within_milestone_cap(self) -> None:
        present = present_extraction_modules()
        cap = require_milestone_within_cap(count_loc(REVIEW_PY), present)
        self.assertEqual(cap, milestone_cap(present))

    def test_each_cumulative_tier_has_a_cap(self) -> None:
        for module_set, cap in MILESTONE_TIERS:
            self.assertEqual(milestone_cap(module_set), cap)

    def test_red_control_partial_milestone_is_error(self) -> None:
        with self.assertRaises(GuardError):
            require_milestone_within_cap(
                1000, frozenset({"contracts"})
            )  # briefs missing

    def test_red_control_full_milestone_over_ceiling(self) -> None:
        with self.assertRaises(GuardError):
            require_milestone_within_cap(
                900, frozenset(ALL_EXTRACTION_MODULES)
            )  # cap 800


class ImportDagGuardTest(unittest.TestCase):
    def test_real_reviewing_package_absent_or_clean(self) -> None:
        edges, forbidden = analyze_reviewing_imports(reviewing_modules())
        require_clean_imports(edges, forbidden)  # must not raise

    def test_all_five_import_forms_parse(self) -> None:
        fix = _Fixture(self)
        fix.write("store.py", "from . import contracts\nfrom .briefs import canon\n")
        fix.write(
            "execution.py",
            "from agent_comms.reviewing import store\n"
            "from agent_comms.reviewing.ledger_evidence import bind\n"
            "import agent_comms.reviewing.git_evidence\n",
        )
        for stem in ("contracts", "briefs", "ledger_evidence", "git_evidence"):
            fix.write(f"{stem}.py", "x = 1\n")
        files = [
            fix.root / f"{s}.py"
            for s in (
                "store",
                "execution",
                "contracts",
                "briefs",
                "ledger_evidence",
                "git_evidence",
            )
        ]
        edges, forbidden = analyze_reviewing_imports(files)
        self.assertEqual(
            edges,
            {
                ("store", "contracts"),
                ("store", "briefs"),
                ("execution", "store"),
                ("execution", "ledger_evidence"),
                ("execution", "git_evidence"),
            },
        )
        self.assertEqual(forbidden, [])
        require_clean_imports(edges, forbidden)  # all declared edges

    def test_red_control_forbidden_monolith_and_cli_imports(self) -> None:
        fix = _Fixture(self)
        fix.write(
            "contracts.py",
            "import agent_comms.review\nfrom agent_comms.cli import landing\nfrom .. import review as _r\n",
        )
        edges, forbidden = analyze_reviewing_imports([fix.root / "contracts.py"])
        targets = {t for _stem, t in forbidden}
        self.assertIn("agent_comms.review", targets)
        self.assertTrue(any(t.startswith("agent_comms.cli") for t in targets))
        with self.assertRaises(GuardError):
            require_clean_imports(edges, forbidden)

    def test_red_control_from_agent_comms_import_review_and_cli(self) -> None:
        fix = _Fixture(self)
        fix.write(
            "checks.py", "from agent_comms import review\nfrom agent_comms import cli\n"
        )
        edges, forbidden = analyze_reviewing_imports([fix.root / "checks.py"])
        targets = {t for _stem, t in forbidden}
        self.assertIn("agent_comms.review", targets)
        self.assertIn("agent_comms.cli", targets)
        with self.assertRaises(GuardError):
            require_clean_imports(edges, forbidden)

    def test_red_control_upward_relative_reviewing_sibling_undeclared(self) -> None:
        # from ..reviewing import store re-enters as contracts->store (undeclared).
        fix = _Fixture(self)
        fix.write("contracts.py", "from ..reviewing import store\n")
        fix.write("store.py", "x = 1\n")
        edges, forbidden = analyze_reviewing_imports(
            [fix.root / "contracts.py", fix.root / "store.py"]
        )
        self.assertIn(("contracts", "store"), edges)
        self.assertEqual(forbidden, [])
        self.assertNotIn(("contracts", "store"), ALLOWED_EDGES)
        with self.assertRaises(GuardError):
            require_clean_imports(edges, forbidden)

    def test_red_control_self_import_edge(self) -> None:
        # store.py: ``from . import store`` is a self-edge; retained and rejected as an
        # undeclared edge / cycle, not silently dropped.
        fix = _Fixture(self)
        fix.write("store.py", "from . import store\n")
        edges, forbidden = analyze_reviewing_imports([fix.root / "store.py"])
        self.assertIn(("store", "store"), edges)
        self.assertNotIn(("store", "store"), ALLOWED_EDGES)
        self.assertTrue(
            any("cycle" in v or "undeclared" in v for v in dag_violations(edges))
        )
        with self.assertRaises(GuardError):
            require_clean_imports(edges, forbidden)

    def test_red_control_edge_a_total_order_would_allow(self) -> None:
        # rank(checks)>rank(ledger_evidence) so a total order accepts it, but the DAG
        # has no such edge: an undeclared edge still rejected (subsumes plain).
        self.assertTrue(_rank_allows("checks", "ledger_evidence"))
        self.assertNotIn(("checks", "ledger_evidence"), ALLOWED_EDGES)
        fix = _Fixture(self)
        fix.write("checks.py", "from agent_comms.reviewing import ledger_evidence\n")
        fix.write("ledger_evidence.py", "x = 1\n")
        edges, forbidden = analyze_reviewing_imports(
            [fix.root / "checks.py", fix.root / "ledger_evidence.py"]
        )
        self.assertIn(("checks", "ledger_evidence"), edges)
        with self.assertRaises(GuardError):
            require_clean_imports(edges, forbidden)

    def test_red_control_cycle(self) -> None:
        fix = _Fixture(self)
        fix.write("store.py", "from agent_comms.reviewing import execution\n")
        fix.write("execution.py", "from agent_comms.reviewing import store\n")
        edges, forbidden = analyze_reviewing_imports(
            [fix.root / "store.py", fix.root / "execution.py"]
        )
        self.assertTrue(any("cycle" in v for v in dag_violations(edges)))
        with self.assertRaises(GuardError):
            require_clean_imports(edges, forbidden)


class OwnershipMigrationGuardTest(unittest.TestCase):
    def test_sqlite_connect_sole_owner(self) -> None:
        require_sole_owner(
            sqlite_owners(core_manifest()), expected_owner("ledger_evidence")
        )

    def test_record_write_sole_owner(self) -> None:
        require_sole_owner(
            record_write_owners(core_manifest()), expected_owner("store")
        )

    def test_binding_registry_sole_owner(self) -> None:
        require_sole_owner(
            binding_owners(core_manifest()), expected_owner("ledger_evidence")
        )

    def test_record_write_and_binding_publication_are_distinct(self) -> None:
        # Phase-aware sole owner per mechanic (same decision as expected_owner), so this
        # stays green after record write moves to store.py and binding to ledger_evidence.py.
        self.assertEqual(
            record_write_owners(core_manifest()), {expected_owner("store")}
        )
        self.assertEqual(
            binding_owners(core_manifest()), {expected_owner("ledger_evidence")}
        )
        self.assertFalse(RECORD_WRITE_FUNCS & BINDING_FUNCS)  # mechanics stay disjoint

    def test_phase_owner_pre_and_post_extraction(self) -> None:
        # Pure controls: review.py owns both mechanics pre-extraction; each successor owns
        # its mechanic post-extraction, and the owner detectors attribute the migrated funcs.
        self.assertEqual(_phase_owner("store", frozenset()), "review.py")
        self.assertEqual(_phase_owner("ledger_evidence", frozenset()), "review.py")
        post = frozenset({"store", "ledger_evidence"})
        self.assertEqual(_phase_owner("store", post), "store.py")
        self.assertEqual(_phase_owner("ledger_evidence", post), "ledger_evidence.py")
        fix = _Fixture(self)
        review = fix.write("review.py", "x = 1\n")
        store = fix.write(
            "store.py", "import os\ndef persist(p, r):\n    os.replace('a', 'b')\n"
        )
        led = fix.write(
            "ledger_evidence.py",
            "import os\ndef _atomic_claim_write(p, r):\n    os.replace('c', 'd')\n",
        )
        self.assertEqual(record_write_owners([review, store, led]), {"store.py"})
        self.assertEqual(binding_owners([review, store, led]), {"ledger_evidence.py"})

    def test_red_control_duplicate_sql_owner(self) -> None:
        fix = _Fixture(self)
        a = fix.write("review.py", "import sqlite3\nsqlite3.connect('x')\n")
        b = fix.write("ledger_evidence.py", "import sqlite3\nsqlite3.connect('y')\n")
        with self.assertRaises(GuardError):
            require_sole_owner(sqlite_owners([a, b]), "review.py")

    def test_red_control_duplicate_record_write_owner(self) -> None:
        fix = _Fixture(self)
        a = fix.write(
            "review.py",
            "import os\ndef atomic_write_json(p, r):\n    os.replace('a', 'b')\n",
        )
        b = fix.write(
            "store.py",
            "import os\ndef atomic_write_json(p, r):\n    os.replace('c', 'd')\n",
        )
        with self.assertRaises(GuardError):
            require_sole_owner(record_write_owners([a, b]), "store.py")

    def test_red_control_duplicate_binding_registry_owner(self) -> None:
        fix = _Fixture(self)
        a = fix.write(
            "review.py",
            "import os\ndef _atomic_claim_write(p, r):\n    os.replace('a', 'b')\n",
        )
        b = fix.write(
            "ledger_evidence.py",
            "import os\ndef _atomic_claim_write(p, r):\n    os.replace('c', 'd')\n",
        )
        with self.assertRaises(GuardError):
            require_sole_owner(binding_owners([a, b]), "ledger_evidence.py")

    def test_red_control_duplicate_persistence_owner(self) -> None:
        self.assertIn("locked_update", RECORD_WRITE_FUNCS)
        fix = _Fixture(self)
        a = fix.write("review.py", "def locked_update(d, fn):\n    return fn({})\n")
        b = fix.write("store.py", "def locked_update(d, fn):\n    return fn({})\n")
        self.assertEqual(record_write_owners([a, b]), {"review.py", "store.py"})
        with self.assertRaises(GuardError):
            require_sole_owner(record_write_owners([a, b]), "store.py")

    def test_red_control_duplicate_recover_binding_owner(self) -> None:
        self.assertIn("command_recover_binding", BINDING_FUNCS)
        fix = _Fixture(self)
        a = fix.write(
            "review.py", "def command_recover_binding(args):\n    return None\n"
        )
        b = fix.write(
            "ledger_evidence.py",
            "def command_recover_binding(args):\n    return None\n",
        )
        self.assertEqual(binding_owners([a, b]), {"review.py", "ledger_evidence.py"})
        with self.assertRaises(GuardError):
            require_sole_owner(binding_owners([a, b]), "ledger_evidence.py")


class GitSubprocessGuardTest(unittest.TestCase):
    def _assert_flags(self, violations, *fragments) -> None:
        blob = " | ".join(violations)
        for fragment in fragments:
            self.assertIn(fragment, blob)

    def test_review_git_calls_are_safe(self) -> None:
        for path in core_manifest():
            with self.subTest(module=path.name):
                require_no_git_mutations(path)  # must not raise

    def test_red_control_direct_subprocess_mutation(self) -> None:
        fix = _Fixture(self)
        src = fix.write(
            "m.py",
            "import subprocess\n"
            'subprocess.run(["git", "commit", "-m", "x"])\n'
            'subprocess.run(["git", "worktree", "add", "wt"])\n',
        )
        self._assert_flags(git_call_violations(src), "git commit", "git worktree add")
        with self.assertRaises(GuardError):
            require_no_git_mutations(src)

    def test_red_control_tuple_and_alias_mutation_forms(self) -> None:
        # Tuple argv, module alias, direct imported run, and imported-run alias all reach
        # the same guard and must be rejected, never silently accepted.
        forms = {
            "tuple argv": 'import subprocess\nsubprocess.run(("git", "commit", "-m", "x"))\n',
            "module alias": 'import subprocess as sp\nsp.run(["git", "commit", "-m", "x"])\n',
            "imported run": 'from subprocess import run\nrun(["git", "commit", "-m", "x"])\n',
            "run alias": 'from subprocess import run as sprun\nsprun(["git", "commit", "-m", "x"])\n',
        }
        for label, source in forms.items():
            with self.subTest(form=label):
                src = _Fixture(self).write("m.py", source)
                self._assert_flags(git_call_violations(src), "git commit")
                with self.assertRaises(GuardError):
                    require_no_git_mutations(src)

    def test_red_control_wrapper_mediated_mutation(self) -> None:
        fix = _Fixture(self)
        src = fix.write(
            "m.py",
            "def f(repo, context):\n"
            '    run_git(repo, "commit", "-m", "x")\n'
            '    git_proc(repo, "reset", "--hard")\n'
            '    git_bytes(context, repo, "push", "origin", "main")\n'
            '    run_git(repo, "worktree", "remove", "wt")\n',
        )
        self._assert_flags(
            git_call_violations(src),
            "git commit",
            "git reset",
            "git push",
            "git worktree remove",
        )
        with self.assertRaises(GuardError):
            require_no_git_mutations(src)

    def test_red_control_global_option_shielded_mutation(self) -> None:
        self.assertEqual(
            _scan_git_argv(["-c", "core.quotePath=true", "commit"], 1),
            ["git commit at line 1"],
        )
        self.assertEqual(
            _scan_git_argv(["--git-dir", "/tmp/repo", "reset"], 2),
            ["git reset at line 2"],
        )
        fix = _Fixture(self)
        src = fix.write(
            "m.py",
            "import subprocess\n"
            'subprocess.run(["git", "-c", "core.quotePath=true", "commit", "-m", "x"])\n'
            'subprocess.run(["git", "--git-dir", "/tmp/repo", "reset", "--hard"])\n'
            'subprocess.run(["git", "--work-tree=/tmp/wt", "checkout", "main"])\n'
            'subprocess.run(["git", "-C", "/tmp/repo", "worktree", "add", "wt"])\n',
        )
        self._assert_flags(
            git_call_violations(src),
            "git commit",
            "git reset",
            "git checkout",
            "git worktree add",
        )
        with self.assertRaises(GuardError):
            require_no_git_mutations(src)

    def test_red_control_shell_true_and_string_argv(self) -> None:
        fix = _Fixture(self)
        src = fix.write(
            "m.py", 'import subprocess\nsubprocess.run("git status", shell=True)\n'
        )
        self._assert_flags(git_call_violations(src), "shell=True", "string git argv")

    def test_read_only_argv_and_wrapper_calls_pass(self) -> None:
        fix = _Fixture(self)
        src = fix.write(
            "m.py",
            "import subprocess\n"
            "def f(repo, context):\n"
            '    subprocess.run(["git", "show", "HEAD:x"])\n'
            '    subprocess.run(["git", "-c", "patchid.verbatim=false", "patch-id"])\n'
            '    run_git(repo, "rev-parse", "HEAD")\n'
            '    git_proc(repo, "status", "--porcelain")\n'
            '    git_bytes(context, repo, "--no-pager", "diff-tree", "-r", "-p")\n',
        )
        self.assertEqual(git_call_violations(src), [])
        require_no_git_mutations(src)  # must not raise


if __name__ == "__main__":
    unittest.main()
