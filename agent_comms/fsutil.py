"""Durable file primitives shared across the package: one atomic write and one
file digest, so every caller gets the same guarantees.

``atomic_write_text`` and ``atomic_write_json`` write to a uniquely named
temporary file in the target's directory, flush and fsync it, then ``os.replace``
it over the target; a reader never sees a partial file and a failure leaves the
target untouched and the temporary file removed. The caller owns the parent
directory (no implicit ``mkdir``). ``file_sha256`` streams the file in 1 MiB
chunks and raises ``OSError`` as the filesystem does.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
