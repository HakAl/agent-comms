"""Own the delta-manifest grammars and their shared status vocabulary.

Raw and name-status evidence must be produced by two separate git commands
over the same immutable tree pair and environment. Cross-checking those
independent serializations verifies the parse/serialization layer, not git
itself: git remains the measuring device.
"""

from __future__ import annotations

from .schema import ValidationError

RAW_GIT_STATUS_CODES = frozenset({"A", "D", "M", "T", "U", "X", "B"})
_RAW_GIT_STATUS_BYTES = frozenset(
    code.encode("ascii") for code in RAW_GIT_STATUS_CODES
)


def parse_raw_z_manifest(raw_manifest: bytes) -> tuple[int, dict[str, int]]:
    """Parse strict ``git diff-tree --raw --no-renames -z`` output."""
    if not raw_manifest or not raw_manifest.endswith(b"\0"):
        raise ValidationError("delta_manifest_malformed")
    tokens = raw_manifest[:-1].split(b"\0")
    if len(tokens) % 2:
        raise ValidationError("delta_manifest_malformed")
    statuses: dict[str, int] = {}
    for offset in range(0, len(tokens), 2):
        metadata, pathname = tokens[offset : offset + 2]
        fields = metadata.split(b" ")
        if (
            len(fields) != 5
            or not fields[0].startswith(b":")
            or len(fields[0]) != 7
            or any(byte not in b"01234567" for byte in fields[0][1:])
            or len(fields[1]) != 6
            or any(byte not in b"01234567" for byte in fields[1])
            or len(fields[2]) != 40
            or len(fields[3]) != 40
            or any(
                byte not in b"0123456789abcdef"
                for field in fields[2:4]
                for byte in field
            )
            or len(fields[4]) != 1
            or fields[4] not in _RAW_GIT_STATUS_BYTES
            or not pathname
        ):
            raise ValidationError("delta_manifest_malformed")
        status = fields[4].decode("ascii")
        statuses[status] = statuses.get(status, 0) + 1
    return len(tokens) // 2, statuses


def derive_name_status_counts(
    name_status_stream: bytes,
) -> tuple[int, dict[str, int]]:
    """Parse strict ``git diff-tree --name-status --no-renames -z`` output."""
    if not name_status_stream or not name_status_stream.endswith(b"\0"):
        raise ValidationError("delta_name_status_malformed")
    tokens = name_status_stream[:-1].split(b"\0")
    if len(tokens) % 2:
        raise ValidationError("delta_name_status_malformed")
    statuses: dict[str, int] = {}
    for offset in range(0, len(tokens), 2):
        status_bytes, pathname = tokens[offset : offset + 2]
        if (
            len(status_bytes) != 1
            or status_bytes not in _RAW_GIT_STATUS_BYTES
            or not pathname
        ):
            raise ValidationError("delta_name_status_malformed")
        status = status_bytes.decode("ascii")
        statuses[status] = statuses.get(status, 0) + 1
    return len(tokens) // 2, statuses


def parse_and_crosscheck(
    raw_manifest: bytes, name_status_stream: bytes
) -> tuple[int, dict[str, int]]:
    """Require the raw and name-status derivations to agree exactly."""
    raw = parse_raw_z_manifest(raw_manifest)
    secondary = derive_name_status_counts(name_status_stream)
    if raw != secondary:
        raise ValidationError("delta_manifest_disagreement")
    return raw
