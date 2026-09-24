"""Refuse one known tool-parameter framing leak before stored-state mutation.

This is a narrow mechanical guard, not a security boundary.  Framing-shaped
text that does not have the exact adjacency checked here may still be stored.
"""

from __future__ import annotations

import re

from .schema import ValidationError


LEAK_CHECK_EXEMPT_PARAMETERS = frozenset()


def assert_no_parameter_leak(field: str, value: str) -> None:
    pattern = rf"</\s*{re.escape(field)}\s*>\s*<parameter\s+name\s*="
    if re.search(pattern, value, flags=re.IGNORECASE):
        raise ValidationError(f"parameter framing leaked into field {field!r}")
