"""A fake Codex custody bundle for hermetic tests.

Codex dispatch runs ``<custody>/<pinned version>/bin/codex`` and checks the
bundle digest against the pin first. A test that dispatches a Codex actor
builds a small bundle here and points the adapter at it with the two
production overrides, as the Claude tests do with their custody binary.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_comms.runtime_pins import (
    CODEX_PINNED_SHA256_ENV,
    CODEX_VERSIONS_DIR_ENV,
    bundle_sha256,
    codex_pin,
)


def default_codex_script() -> str:
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f"  printf 'codex-cli {codex_pin().version}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def fake_codex_bundle(versions_dir: Path, script: str | None = None) -> dict[str, str]:
    """Write a bundle whose ``bin/codex`` runs ``script``; return the env that pins it."""
    bundle = versions_dir / codex_pin().version
    binary = bundle / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(script if script is not None else default_codex_script())
    binary.chmod(0o755)
    return codex_custody_env(versions_dir)


def codex_custody_env(versions_dir: Path) -> dict[str, str]:
    """The overrides that pin the bundle already in ``versions_dir``."""
    return {
        CODEX_VERSIONS_DIR_ENV: str(versions_dir),
        CODEX_PINNED_SHA256_ENV: bundle_sha256(versions_dir / codex_pin().version),
    }


def python_shim_script() -> str:
    """A ``bin/codex`` that hands its arguments to this interpreter, for tests
    whose stand-in worker is ``python -c ...``."""
    return f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'


def forwarding_codex_script() -> str:
    """A ``bin/codex`` that runs the ``codex`` on PATH, for tests whose
    binding preflight asks the real Codex (``codex mcp get``)."""
    real = shutil.which("codex")
    if real is None:
        return "#!/bin/sh\necho 'codex CLI unavailable' >&2\nexit 127\n"
    return f'#!/bin/sh\nexec {shlex.quote(real)} "$@"\n'


def install_fake_codex_custody(test: unittest.TestCase, script: str | None = None) -> Path:
    """Give ``test`` a fake custody bundle for its whole run; return ``bin/codex``.

    The bundle lives in a temporary directory removed at cleanup, and the
    pinning overrides are patched into the environment until then.
    """
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    versions_dir = Path(temp.name) / "codex-custody"
    patch = mock.patch.dict(os.environ, fake_codex_bundle(versions_dir, script))
    patch.start()
    test.addCleanup(patch.stop)
    return versions_dir / codex_pin().version / "bin" / "codex"
