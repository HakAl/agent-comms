from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

TAIL_BYTES = 64 * 1024
PARSER_VERSION = 1


@dataclass(frozen=True)
class UsageParseResult:
    completeness: str
    fields: dict[str, object]
    reason: str | None = None


def _tail(path: Path) -> bytes:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - TAIL_BYTES))
        return handle.read(TAIL_BYTES)


def _codex_events(path: str) -> UsageParseResult:
    usage = None
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("type") == "turn.completed"
                    and isinstance(event.get("usage"), dict)
                ):
                    usage = event["usage"]
    except FileNotFoundError:
        return UsageParseResult("unavailable", {}, "events_missing")
    except (OSError, ValueError):
        return UsageParseResult("unavailable", {}, "unreadable")
    if usage is None:
        return UsageParseResult("unavailable", {}, "no_turn_completed")
    names = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    try:
        fields = {name: int(usage.get(name, 0)) for name in names}
    except (ValueError, TypeError, OverflowError):
        return UsageParseResult("unavailable", {}, "no_turn_completed")
    fields["total_tokens"] = max(
        fields["input_tokens"] - fields["cached_input_tokens"], 0
    ) + max(fields["output_tokens"], 0)
    return UsageParseResult(
        "complete",
        {
            **fields,
            "total_basis": "blended_total",
            "usage_scope": "through_last_completed_turn",
        },
    )


def parse_worker_usage(
    runtime: str | None, observed_values: Mapping[str, object]
) -> UsageParseResult:
    if runtime == "codex" and "worker_events" in observed_values:
        path = observed_values["worker_events"]
        if not isinstance(path, str):
            return UsageParseResult("unavailable", {}, "unreadable")
        return _codex_events(path)
    if "worker_log" not in observed_values:
        return UsageParseResult("unavailable", {}, "no_worker_log")
    path = observed_values["worker_log"]
    if not isinstance(path, str):
        return UsageParseResult("unavailable", {}, "unreadable")
    log_path = Path(path)
    try:
        tail = _tail(log_path)
    except FileNotFoundError:
        return UsageParseResult("unavailable", {}, "log_missing")
    except (OSError, ValueError):
        return UsageParseResult("unavailable", {}, "unreadable")
    text = tail.decode("utf-8", errors="replace")
    if runtime == "codex":
        lines = text.splitlines()
        for index in range(len(lines) - 2, -1, -1):
            if lines[index].strip() != "tokens used":
                continue
            try:
                total = int(lines[index + 1].strip().replace(",", ""))
            except (ValueError, IndexError):
                continue
            return UsageParseResult(
                "complete", {"total_tokens": total, "total_basis": "runtime_reported_total"}
            )
        return UsageParseResult("unavailable", {}, "no_trailer")
    if runtime == "claude":
        result_event: dict | None = None
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("usage"), dict):
                result_event = event
        if result_event is None:
            return UsageParseResult("unavailable", {}, "no_result_event")
        usage = result_event["usage"]
        names = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        try:
            fields = {name: int(usage.get(name, 0)) for name in names}
        except (TypeError, ValueError):
            return UsageParseResult("unavailable", {}, "no_result_event")
        fields["total_tokens"] = fields["input_tokens"] + fields["cache_creation_input_tokens"] + fields["output_tokens"]
        fields["total_basis"] = "input+cache_creation+output"
        return UsageParseResult("complete", fields)
    return UsageParseResult("unavailable", {}, "unsupported_runtime")
