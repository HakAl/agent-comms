"""Move the test process into a scratch home before ``agent_comms`` loads.

``tests/__init__.py`` calls :func:`activate` when the package is imported, and
every test module starts with ``import tests.isolation`` before any
``agent_comms`` import (``tests/substrate/test_isolation.py`` enforces this).
The guard therefore runs before ``agent_comms.paths``, ``agent_comms.supervisor``
or ``agent_comms.cli._helpers`` can compute a path from the real
``Path.home()``, whichever directory unittest discovery treats as top level.
Any invocation is isolated, including a bare
``python -m unittest discover -s tests/substrate`` typed without the
maintainer's wrapper script. Running a test file directly as a script fails
loudly instead (``tests`` is not importable from inside the package directory).

What it does, once per process tree:

- creates a scratch ``HOME`` and a short scratch supervisor control root
  (unix socket paths are limited to 103 bytes, and a control root under a
  system temp home would exceed that);
- strips every ambient ``AGENT_COMMS_*`` override except the guard's own
  ``AGENT_COMMS_TEST_*`` switches, plus ``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``,
  the XDG base directories and ``ZDOTDIR``;
- verifies that every home-derived path in ``agent_comms`` now resolves
  outside the real ``~/.agent-comms``, ``~/.codex`` and ``~/.claude``;
- removes the scratch directories at exit.

Switches (environment variables):

``AGENT_COMMS_TEST_LIVE_HOME=1``
    Skip isolation. Only for operator certification runs of ``tests/cells``
    that need real runtime logins and probe the real protected root. The
    guard refuses it unless every test target on the command line is under
    ``tests/cells``, so the substrate suite can never run against a real home.
``AGENT_COMMS_TEST_KEEP_SCRATCH=1``
    Keep the scratch directories and print their paths to stderr, so a run
    can be audited for what it would have written under a real home.
``AGENT_COMMS_TEST_SCRATCH_HOME``
    Set by the guard, never by hand. A child process that inherits it is
    inside an isolated parent and keeps the environment the test built for
    it, including a HOME of the test's choosing. It is ignored, and isolation
    runs afresh, when HOME is the account's home directory, so a stale value
    in a shell cannot bypass the guard.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

LIVE_HOME_ENV = "AGENT_COMMS_TEST_LIVE_HOME"
KEEP_SCRATCH_ENV = "AGENT_COMMS_TEST_KEEP_SCRATCH"
SCRATCH_HOME_ENV = "AGENT_COMMS_TEST_SCRATCH_HOME"
SUPERVISOR_ROOT_ENV = "AGENT_COMMS_SUPERVISOR_ROOT"

GUARD_PREFIX = "AGENT_COMMS_TEST_"
STRIPPED_PREFIX = "AGENT_COMMS_"
STRIPPED_NAMES = frozenset(
    {
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "ZDOTDIR",
    }
)
PROTECTED_SUBDIRS = (".agent-comms", ".codex", ".claude")

SUPPORTED_INVOCATIONS = (
    "python -m unittest discover -s tests -t . "
    "(or -s tests/substrate -t ., or python -m unittest tests.substrate.test_x)"
)

# Run token (32 hex chars) + "/" + socket name "s", plus the joining "/".
_CONTROL_SOCKET_SUFFIX_BYTES = 1 + 32 + 1 + 1

_real_homes: tuple[Path, ...] = ()
_scratch: tuple[Path, Path] | None = None


def real_homes() -> tuple[Path, ...]:
    """The home directories this process was protected from (empty if opted out)."""
    return _real_homes


def protected_roots() -> tuple[Path, ...]:
    """The real runtime directories no test may resolve into."""
    return tuple(home / name for home in _real_homes for name in PROTECTED_SUBDIRS)


def is_within(path: Path, root: Path) -> bool:
    try:
        resolved = Path(path).resolve()
        root_resolved = Path(root).resolve()
    except OSError:
        return False
    return resolved == root_resolved or resolved.is_relative_to(root_resolved)


def _passwd_home() -> Path | None:
    try:
        import pwd
    except ImportError:
        return None
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return None


_CELLS_DIR = Path(__file__).resolve().parent / "cells"
_CELLS_MODULE = "tests.cells"
# unittest options whose value is not a test target.
_SKIPPED_OPTION_VALUES = frozenset({"-t", "--top-level-directory", "-p", "--pattern", "-k"})


def _is_cells_target(arg: str) -> bool:
    """True when a command-line test target resolves inside ``tests/cells``.

    Paths are resolved against the working directory (so ``tests/cells/..``
    tricks and look-alike directory names do not pass); dotted names must be
    the ``tests.cells`` package or a module inside it.
    """
    if "/" in arg or arg.endswith(".py") or arg == "tests":
        try:
            resolved = Path(arg).resolve()
        except OSError:
            return False
        return resolved == _CELLS_DIR or resolved.is_relative_to(_CELLS_DIR)
    return arg == _CELLS_MODULE or arg.startswith(_CELLS_MODULE + ".")


def _argv_targets_cells() -> bool:
    """True when every test target on the command line is in ``tests/cells``."""
    targets: list[str] = []
    args = sys.argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg in _SKIPPED_OPTION_VALUES:
            index += 1
            continue
        if arg.startswith("-") or arg == "discover":
            continue
        targets.append(arg)
    if not targets:
        return False
    return all(_is_cells_target(arg) for arg in targets)


def _inherited_isolation() -> bool:
    """True when this is a child of an isolated process and may keep its env.

    The marker variable alone is not enough: a stale one left in a shell must
    not bypass isolation. It is honored only when ``HOME`` is set and is not
    the account's home directory (a test may give a child its own HOME).
    """
    marker = os.environ.get(SCRATCH_HOME_ENV)
    home = os.environ.get("HOME")
    if not marker or not home:
        return False
    account_home = _passwd_home()
    if account_home is None:
        # Cannot tell a stale marker from a real one: trust only an exact match.
        return home == marker
    try:
        if Path(home).resolve() == account_home.resolve():
            return False
    except OSError:
        return False
    return True


def _loaded_agent_comms_modules() -> list[str]:
    return sorted(
        name for name in sys.modules if name == "agent_comms" or name.startswith("agent_comms.")
    )


def _cleanup(home: Path, supervisor_root: Path) -> None:
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(supervisor_root, ignore_errors=True)


def _verify(home: Path, supervisor_root: Path) -> None:
    from agent_comms import paths, supervisor

    candidates = {
        "Path.home()": Path.home(),
        "agent_comms.paths.DEFAULT_DB": paths.DEFAULT_DB,
        "agent_comms.paths.runtime_root()": paths.runtime_root(),
        "agent_comms.paths.db_path()": paths.db_path(),
        "agent_comms.paths.codex_custody_root()": paths.codex_custody_root(),
        "agent_comms.supervisor._DEFAULT_CONTROL_ROOT": supervisor._DEFAULT_CONTROL_ROOT,
        "agent_comms.supervisor.control_root()": supervisor.control_root(),
    }
    leaks = [
        f"{name} -> {path}"
        for name, path in candidates.items()
        if any(is_within(path, root) for root in protected_roots())
    ]
    if Path.home() != home:
        leaks.append(f"Path.home() -> {Path.home()} (expected {home})")
    if leaks:
        raise RuntimeError(
            "test isolation failed; these paths still resolve into the real home:\n  "
            + "\n  ".join(leaks)
        )
    budget = len(os.fsencode(str(supervisor_root))) + _CONTROL_SOCKET_SUFFIX_BYTES
    if budget > supervisor.SUN_PATH_MAX_BYTES:
        print(
            f"agent-comms tests: scratch supervisor root {supervisor_root} is too long for a "
            f"control socket ({budget} > {supervisor.SUN_PATH_MAX_BYTES} bytes); set TMPDIR "
            "to a short directory. Tests that bind the control socket will fail loudly.",
            file=sys.stderr,
        )


def activate() -> None:
    """Isolate this process from the real home. Idempotent per process tree."""
    global _real_homes, _scratch

    if os.environ.get(LIVE_HOME_ENV) == "1":
        if not _argv_targets_cells():
            raise RuntimeError(
                f"{LIVE_HOME_ENV}=1 runs tests against the real home and is only for the "
                "login-backed cell suite; invoke it with tests/cells (or a tests.cells module) "
                "as the only target, from your own shell."
            )
        return
    if _inherited_isolation():
        # A child of an isolated process: keep whatever environment the test
        # built for it and never strip or re-create scratch directories.
        return
    if _scratch is not None:
        return

    loaded = _loaded_agent_comms_modules()
    if loaded:
        raise RuntimeError(
            "the tests package must be imported before agent_comms so it can redirect HOME "
            f"first, but these modules are already loaded: {', '.join(loaded)}. "
            f"Run the suite as {SUPPORTED_INVOCATIONS}."
        )

    homes: list[Path] = []
    for candidate in (os.environ.get("HOME"), _passwd_home()):
        if candidate and Path(candidate) not in homes:
            homes.append(Path(candidate))
    _real_homes = tuple(homes)

    keep = os.environ.get(KEEP_SCRATCH_ENV) == "1"
    home = Path(tempfile.mkdtemp(prefix="ach"))
    # Shortest possible name: this root plus "/<32 hex>/s" must fit a socket path.
    supervisor_root = Path(tempfile.mkdtemp(prefix="s"))
    _scratch = (home, supervisor_root)
    if keep:
        print(
            f"agent-comms tests: keeping scratch home {home} and supervisor root {supervisor_root}",
            file=sys.stderr,
        )
    else:
        atexit.register(_cleanup, home, supervisor_root)

    for name in list(os.environ):
        if name in STRIPPED_NAMES or (
            name.startswith(STRIPPED_PREFIX) and not name.startswith(GUARD_PREFIX)
        ):
            del os.environ[name]
    os.environ["HOME"] = str(home)
    os.environ[SUPERVISOR_ROOT_ENV] = str(supervisor_root)
    os.environ[SCRATCH_HOME_ENV] = str(home)

    _verify(home, supervisor_root)
