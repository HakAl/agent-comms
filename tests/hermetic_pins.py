"""A certified-pin table for hermetic tests on a host without one.

The hermetic helpers build stub custody binaries named after the pinned
version (``versions_dir / claude_pin().version``, ``codex_pin().version /
"bin" / "codex"``), so on a platform the shipped ``runtime_pins.json`` does
not list they cannot even start. Those tests exercise package logic that
has nothing to do with the host platform, so this module gives them a pin
table entry for the host, for one test class at a time and for the children
it spawns, through ``AGENT_COMMS_RUNTIME_PINS``.

What it never does: it is a no-op on a certified host (macOS today), under
``AGENT_COMMS_TEST_CERT=1`` (certification mode must never see a synthetic
pin; certify.py has just written the candidate pin for one runtime), and
in live-home mode (``tests.isolation.LIVE_HOME_ENV``: the real runtime
decides). It is never applied process-wide: the live cells and
``test_cell_version_drift`` decide their skip by catching
``PlatformNotCertified`` and must keep that honest skip, and the
manifest-shape tests read the shipped file unpatched.

Usage: decorate a test class with :func:`with_hermetic_pins`, or call
:func:`use_hermetic_pins` first thing in its ``setUpClass``. A child
started with a copy of ``os.environ`` while the class runs inherits the
override; a child built from a stripped dict needs
:func:`hermetic_pins_env` merged in.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from agent_comms import runtime_pins
from tests import isolation

# The platform whose entries stand in for the host's. The shipped manifest
# always has it (ManifestDataTest asserts so).
SOURCE_PLATFORM = "darwin-arm64"
SCRATCH_RELPATH = "hermetic-runtime-pins"

_cache: dict[tuple[str, Path], Path | None] = {}


def write_hermetic_manifest(platform: str, source: Path, directory: Path | None = None) -> Path | None:
    """A copy of ``source`` with ``platform`` certified for every runtime.

    Returns ``None`` when every runtime in ``source`` already has an entry
    under ``platform``: there is nothing to synthesize. Otherwise each
    runtime that lacks one gets its :data:`SOURCE_PLATFORM` entry copied
    under ``platform`` (``setdefault``: a runtime certified for the host
    keeps its real entry, so certifying one runtime does not get the other
    faked), and the copy is written under ``directory`` (default: a
    directory under the scratch home) and returned. Not memoized; see
    :func:`hermetic_pins_env`.
    """
    manifest = json.loads(Path(source).read_text(encoding="utf-8"))
    runtimes = manifest["runtimes"]
    if all(platform in entry["platforms"] for entry in runtimes.values()):
        return None
    for entry in runtimes.values():
        platforms = entry["platforms"]
        if SOURCE_PLATFORM in platforms:
            platforms.setdefault(platform, dict(platforms[SOURCE_PLATFORM]))
    target_dir = Path(os.environ["HOME"]) / SCRATCH_RELPATH if directory is None else Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"runtime_pins-{platform}.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def hermetic_pins_env() -> dict[str, str]:
    """The environment that gives this host a pin table entry, or ``{}``.

    Empty on a certified host, in certification mode and in live-home mode.
    Otherwise ``{"AGENT_COMMS_RUNTIME_PINS": <scratch manifest>}``, written
    once per ``(current_platform(), RUNTIME_PINS_PATH)`` in this process.
    """
    if os.environ.get(isolation.CERT_ENV) == "1" or os.environ.get(isolation.LIVE_HOME_ENV) == "1":
        return {}
    key = (runtime_pins.current_platform(), runtime_pins.RUNTIME_PINS_PATH)
    if key not in _cache:
        _cache[key] = write_hermetic_manifest(*key)
    path = _cache[key]
    if path is None:
        return {}
    return {runtime_pins.RUNTIME_PINS_ENV: str(path)}


def reset_cache() -> None:
    """Forget the written manifests (for this module's own tests)."""
    _cache.clear()


def use_hermetic_pins(testcase_class: type[unittest.TestCase]) -> None:
    """Hold the override for the rest of ``testcase_class``'s run.

    Call first in ``setUpClass`` so it precedes every helper that resolves a
    pin; the patch is removed by a class cleanup, after ``tearDownClass``.
    """
    patch = mock.patch.dict(os.environ, hermetic_pins_env())
    patch.start()
    testcase_class.addClassCleanup(patch.stop)


def with_hermetic_pins(testcase_class: type[unittest.TestCase]) -> type[unittest.TestCase]:
    """Class decorator: :func:`use_hermetic_pins` before the class's own ``setUpClass``."""
    original = testcase_class.setUpClass

    @classmethod
    def setUpClass(cls) -> None:  # type: ignore[no-untyped-def]
        use_hermetic_pins(cls)
        original.__func__(cls)

    testcase_class.setUpClass = setUpClass  # type: ignore[method-assign]
    return testcase_class
