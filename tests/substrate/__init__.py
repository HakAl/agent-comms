"""Substrate tests.

``unittest discover -s tests`` without ``-t`` imports this package as the
top-level ``substrate`` before any test module, so pull the home isolation
guard from the top-level ``tests`` package in first.
"""

import sys as _sys
from pathlib import Path as _Path

if "tests" not in _sys.modules:
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    import tests as _tests  # noqa: F401
