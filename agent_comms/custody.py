"""Fill runtime custody: copy the pinned Codex build where dispatch runs it.

A Codex worker runs ``<runtime root>/runtime-custody/codex/<version>/bin/codex``,
never the ``codex`` on PATH, which updates itself. This module puts the
certified native bundle there: it finds the bundle the PATH install runs (or
takes one named with ``--from``), checks its version against the pin, copies
it beside the destination, checks the copy's digest against the pin and
renames it into place, so dispatch never sees a partial copy.

Verification at dispatch lives in :mod:`agent_comms.runtime_pins` (governed);
this is the operator's fill step, off the dispatch spawn path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import runtime_pins
from .schema import ValidationError

SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")

# The target triple and platform package the npm wrapper (@openai/codex,
# bin/codex.js) picks for each platform key.
NATIVE_TARGETS = {
    "darwin-arm64": ("aarch64-apple-darwin", "codex-darwin-arm64"),
    "darwin-x86_64": ("x86_64-apple-darwin", "codex-darwin-x64"),
    "linux-x86_64": ("x86_64-unknown-linux-musl", "codex-linux-x64"),
    "linux-aarch64": ("aarch64-unknown-linux-musl", "codex-linux-arm64"),
    "linux-arm64": ("aarch64-unknown-linux-musl", "codex-linux-arm64"),
}

Which = Callable[[str], "str | None"]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class CustodyError(ValidationError):
    """Custody cannot be filled as asked; the message names the fix."""


def native_bundle_candidates(wrapper: Path, platform: str | None = None) -> list[Path]:
    """Where the npm wrapper at ``wrapper`` keeps the native bundle, in the
    order it looks: its own ``node_modules``, the hoisted sibling package,
    then the legacy ``vendor`` directory inside the wrapper package."""
    key = platform or runtime_pins.current_platform()
    if key not in NATIVE_TARGETS:
        return []
    triple, package = NATIVE_TARGETS[key]
    package_root = Path(os.path.realpath(wrapper)).parent.parent
    return [
        package_root / "node_modules" / "@openai" / package / "vendor" / triple,
        package_root.parent / package / "vendor" / triple,
        package_root / "vendor" / triple,
    ]


def locate_native_bundle(which: Which = shutil.which, platform: str | None = None) -> Path:
    """The native bundle behind the ``codex`` on PATH."""
    found = which("codex")
    if found is None:
        raise CustodyError(
            "codex is not on PATH; recover with: install Codex, or name its native bundle with --from DIR"
        )
    candidates = native_bundle_candidates(Path(found), platform)
    for candidate in candidates:
        if (candidate / "bin" / "codex").is_file():
            return candidate
    looked = ", ".join(str(path) for path in candidates) or "no layout known for this platform"
    raise CustodyError(
        f"no native Codex bundle found behind {found} (looked in: {looked}); "
        "recover with: name the directory holding bin/codex with --from DIR"
    )


def bundle_version(bundle: Path, run: Runner = subprocess.run) -> str:
    binary = bundle / "bin" / "codex"
    if not binary.is_file():
        raise CustodyError(f"{bundle} is not a Codex bundle: {binary} is missing")
    try:
        result = run([str(binary), "--version"], text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CustodyError(f"could not run {binary} --version: {exc}") from exc
    output = f"{result.stdout}\n{result.stderr}".strip()
    match = SEMVER_RE.search(output)
    if result.returncode != 0 or match is None:
        raise CustodyError(f"{binary} --version gave no version (exit {result.returncode}): {output!r}")
    return match.group(1)


def copy_bundle(source: Path, dest: Path) -> str:
    """Copy ``source`` to ``dest`` (which must not exist) atomically; return its digest.

    The copy is made in a sibling directory and renamed into place, so a
    reader sees no bundle or a whole one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{dest.name}.partial-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(source, staging, symlinks=True)
        digest = runtime_pins.bundle_sha256(staging)
        os.rename(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return digest


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink()


def fill_codex_custody(
    source: Path | None = None,
    *,
    replace: bool = False,
    which: Which = shutil.which,
    run: Runner = subprocess.run,
) -> dict:
    """Make the custody copy of the pinned Codex present and correct."""
    try:
        pin = runtime_pins.codex_pin()
    except runtime_pins.PlatformNotCertified as exc:
        raise CustodyError(str(exc)) from exc
    expected = runtime_pins.codex_pinned_sha256()
    if not expected:
        raise CustodyError(
            f"the codex {pin.version} pin records no bundle sha256; recover with: make certify RUNTIME=codex"
        )
    dest = runtime_pins.codex_bundle_path()
    if dest.exists() or dest.is_symlink():
        if runtime_pins.codex_bundle_digest_matches(dest, expected):
            return {"ok": True, "status": "present", "version": pin.version, "path": str(dest), "sha256": expected}
        if not replace:
            raise CustodyError(
                f"custody copy of codex {pin.version} at {dest} differs from the certified bundle; "
                "recover with: agent-comms custody codex --replace"
            )
    bundle = Path(os.path.realpath(source)) if source is not None else locate_native_bundle(which)
    installed = bundle_version(bundle, run)
    if installed != pin.version:
        raise CustodyError(
            f"{bundle} is codex {installed}, the pin is {pin.version}; recover with: install codex {pin.version} "
            f"(npm install -g @openai/codex@{pin.version}), or certify {installed} (make certify RUNTIME=codex)"
        )
    staged = dest.parent / f".{dest.name}.new-{os.getpid()}"
    shutil.rmtree(staged, ignore_errors=True)
    digest = copy_bundle(bundle, staged)
    if digest != expected:
        shutil.rmtree(staged, ignore_errors=True)
        raise CustodyError(
            f"{bundle} is codex {pin.version} but not the certified build: bundle sha256 {digest}, pinned {expected}; "
            "recover with: reinstall that version, or re-certify it (make certify RUNTIME=codex CERTIFY_ARGS=--force)"
        )
    if dest.exists() or dest.is_symlink():
        retired = dest.parent / f".{dest.name}.old-{os.getpid()}"
        _discard(retired)  # a killed run with this pid may have left it
        os.rename(dest, retired)
        os.rename(staged, dest)
        _discard(retired)
        status = "replaced"
    else:
        os.rename(staged, dest)
        status = "filled"
    return {
        "ok": True,
        "status": status,
        "version": pin.version,
        "path": str(dest),
        "source": str(bundle),
        "sha256": digest,
    }


def codex_custody_state() -> dict:
    """Whether the custody copy is present and matches the pin, for setup and doctor."""
    try:
        dest = runtime_pins.codex_bundle_path()
    except runtime_pins.PlatformNotCertified as exc:
        return {"present": False, "reason": str(exc)}
    if not (dest / "bin" / "codex").is_file():
        return {"present": False, "path": str(dest), "reason": "missing"}
    if not runtime_pins.codex_pinned_sha256():
        return {"present": False, "path": str(dest), "reason": "the pin records no bundle sha256"}
    if not runtime_pins.codex_bundle_digest_matches(dest):
        return {"present": False, "path": str(dest), "reason": "digest differs from the pin"}
    return {"present": True, "path": str(dest)}

