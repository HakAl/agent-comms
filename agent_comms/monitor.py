from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, TextIO

from .adapters.registry import adapter_for
from . import __version__
from . import paths
from .store import Store
from .worker_usage import PARSER_VERSION, parse_worker_usage

STALE_HEARTBEAT_SECONDS = 300
STALE_PAGE_INTERVAL_SECONDS = 3600
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
MONITOR_VERSION = __version__
WATCHDOG_FALLBACK_MESSAGE_ID = "monitor-watchdog-fallback"
WORKER_USAGE_BATCH = 25
WORKER_USAGE_AGE_OUT = timedelta(hours=24)
REFRESH_COMMAND = "refresh-codex-auth"
# The one refresh pass this monitor process started, if any (single-flight).
_refresh_process: subprocess.Popen | None = None


def _default_kick_runner(argv: list[str]):
    """Start one detached refresh pass unless the previous one is still running.

    The pass runs as its own process-group leader so its containment sweep
    covers every ``codex exec`` child. Failures are paged to the operator by the
    refresh driver itself.
    """
    global _refresh_process
    if _refresh_process is not None and _refresh_process.poll() is None:
        return SimpleNamespace(returncode=0, stderr="refresh already running")
    _refresh_process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return SimpleNamespace(returncode=0, stderr="")


def _refresh_argv(db_path: Path | None) -> list[str]:
    argv = [sys.executable, "-m", "agent_comms.cli"]
    if db_path is not None and Path(db_path) != paths.db_path():
        argv += ["--db", str(db_path)]
    return argv + [REFRESH_COMMAND]


def kick_stale_codex_lineages(
    actions: list[dict],
    *,
    db_path: Path | None = None,
    kick_runner=_default_kick_runner,
) -> list[dict]:
    """Start one refresh pass when any lineage is stale, recording the outcome per lineage."""
    stale = {str(a.get("lineage_key")) for a in actions
             if a.get("status") == "token_stale" and a.get("lineage_key")}
    if not stale:
        return []
    outcome: dict = {}
    try:
        result = kick_runner(_refresh_argv(db_path))
        outcome["kick_rc"] = int(result.returncode)
        stderr = str(getattr(result, "stderr", "") or "").strip()
        if stderr:
            outcome["kick_stderr"] = stderr
    except Exception as exc:
        outcome["kick_rc"] = None
        outcome["kick_error"] = _safe_exception_summary(exc)
    return [{"status": "token_stale_kick", "lineage_key": key, **outcome} for key in sorted(stale)]


def reconcile_once(
    db_path: Path,
    human_actor_id: str | None = None,
    *,
    is_default_db_open: bool = False,
) -> list[dict]:
    store = Store(db_path, is_default_db_open=is_default_db_open)
    return store.reconcile_dispatches(adapter_for, human_actor_id=human_actor_id)


def upsert_heartbeat(db_path: Path, *, interval: float, is_default_db_open: bool = False) -> dict:
    store = Store(db_path, is_default_db_open=is_default_db_open)
    return store.upsert_monitor_heartbeat(interval_seconds=interval, monitor_version=MONITOR_VERSION)


def enrich_worker_usage(
    db_path: Path,
    *,
    batch: int = WORKER_USAGE_BATCH,
    age_out: timedelta = WORKER_USAGE_AGE_OUT,
    now: datetime | None = None,
    is_default_db_open: bool = False,
) -> dict[str, object]:
    started = time.monotonic()
    counts: dict[str, object] = {
        "candidate": 0,
        "enriched": 0,
        "marked": 0,
        "skipped": 0,
        "malformed": 0,
    }
    now = now or datetime.now(timezone.utc)
    store = Store(db_path, is_default_db_open=is_default_db_open)
    rows, counts["malformed"] = store.worker_usage_candidates(batch=batch)
    counts["candidate"] = len(rows)
    for row in rows:
        try:
            observed = json.loads(row["observed_values_json"])
            if not isinstance(observed, dict):
                raise ValueError("observed_values must be a mapping")
            if "worker_log" not in observed and "worker_events" not in observed:
                result_fields: dict[str, object] = {}
                completeness, reason = "unavailable", "no_worker_log"
                should_write = True
            else:
                result = parse_worker_usage(row["runtime"], observed)
                result_fields = result.fields
                completeness, reason = result.completeness, result.reason
                terminal_at = datetime.fromisoformat(row["terminal_ts"])
                should_write = completeness == "complete" or terminal_at <= now - age_out
            if not should_write:
                counts["skipped"] = int(counts["skipped"]) + 1
                continue
            payload: dict[str, object] = {
                "runtime": row["runtime"],
                "parser_version": PARSER_VERSION,
                "completeness": completeness,
                "total_basis": None,
                "measured_at": now.isoformat(timespec="seconds"),
                "total_tokens": None,
                **result_fields,
            }
            if reason is not None:
                payload["reason"] = reason
            if store.write_worker_usage(row["dispatch_id"], payload):
                key = "enriched" if completeness == "complete" else "marked"
                counts[key] = int(counts[key]) + 1
        except Exception:
            counts["skipped"] = int(counts["skipped"]) + 1
    counts["duration_seconds"] = round(time.monotonic() - started, 6)
    return counts


def heartbeat_is_fresh(heartbeat: dict | None, *, now: datetime | None = None) -> bool:
    if heartbeat is None or not heartbeat.get("last_pass_at"):
        return False
    now = now or datetime.now(timezone.utc)
    try:
        last_pass_at = datetime.fromisoformat(str(heartbeat["last_pass_at"]))
    except ValueError:
        return False
    return last_pass_at > now - timedelta(seconds=STALE_HEARTBEAT_SECONDS)


def check_heartbeat(
    db_path: Path,
    human_actor_id: str,
    *,
    is_default_db_open: bool = False,
    fallback_page_root: str | None = None,
) -> int:
    heartbeat = None
    eval_error: Exception | None = None
    try:
        evaluation_store = Store(db_path, is_default_db_open=is_default_db_open)
        heartbeat = evaluation_store.monitor_heartbeat()
        if heartbeat_is_fresh(heartbeat):
            return 0
    except Exception as exc:
        eval_error = exc

    try:
        page_store = Store(db_path, is_default_db_open=is_default_db_open)
        claimed_at = page_store.claim_monitor_stale_page(
            stale_page_interval_seconds=STALE_PAGE_INTERVAL_SECONDS
        )
        if claimed_at is not None:
            last_pass_at = heartbeat.get("last_pass_at", "<absent>") if heartbeat else "<absent>"
            error_detail = f"\nevaluation_error={_safe_exception_summary(eval_error)}" if eval_error else ""
            page_store.send_message(
                human_actor_id,
                [human_actor_id],
                "[monitor stale] dispatch reconciliation heartbeat stale",
                (
                    "The continuous dispatch monitor heartbeat is stale or absent.\n"
                    f"last_pass_at={last_pass_at}\n"
                    f"detected_at={claimed_at}{error_detail}"
                ),
                [],
                priority="blocker",
                requires_ack=True,
            )
        return 1
    except Exception as page_error:
        return _watchdog_fallback(db_path, eval_error, page_error, fallback_page_root, human_actor_id)


def _safe_exception_summary(error: Exception | None) -> str | None:
    if error is None:
        return None
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:
        return "<exception summary unavailable>"


def _watchdog_paths() -> tuple[Path, Path]:
    logs = Path.home() / ".agent-comms" / "logs"
    return logs / "monitor-watchdog.page.log", logs / "monitor-watchdog.page.marker"


def _fallback_semaphore(root_value: str, human_actor_id: str, detected_at: str) -> None:
    root = Path(root_value).expanduser()
    semaphore_dir = root / ".agent-comms" / "monitor-watchdog"
    semaphore_dir.mkdir(parents=True, exist_ok=True)
    semaphore_path = semaphore_dir / "new_messages"
    temp_path = semaphore_dir / f".new_messages.{os.getpid()}.{uuid4().hex}.tmp"
    payload = {
        "agent_id": human_actor_id,
        "updated_at": detected_at,
        "messages": [{"message_id": WATCHDOG_FALLBACK_MESSAGE_ID, "delivered_at": detected_at}],
    }
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp_path.replace(semaphore_path)


def _watchdog_fallback(
    db_path: Path | str,
    eval_error: Exception | None,
    page_error: Exception,
    fallback_page_root: str | None,
    human_actor_id: str,
) -> int:
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        page_log, marker = _watchdog_paths()
    except Exception:
        return 2

    # DB-independent alarm-throttle state, documented exception to lifecycle-in-SQL,
    # used only on the SQL-unavailable page path, never read by the substrate for lifecycle decisions.
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < STALE_PAGE_INTERVAL_SECONDS:
            return 2
    except Exception:
        pass

    try:
        page_log.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        record = {
            "detected_at": detected_at,
            "eval_error": _safe_exception_summary(eval_error),
            "page_error": _safe_exception_summary(page_error),
            "db_path": str(db_path),
        }
        with page_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass
    if fallback_page_root is not None:
        try:
            _fallback_semaphore(fallback_page_root, human_actor_id, detected_at)
        except Exception:
            pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
        os.utime(marker, None)
    except Exception:
        pass
    return 2


def run_monitor_loop(
    db_path: Path,
    *,
    human_actor_id: str | None = None,
    interval: float = 5.0,
    once: bool = False,
    max_passes: int | None = None,
    output: TextIO = sys.stdout,
    logger: logging.Logger | None = None,
    heartbeat: Callable[[Path, float], dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    reconcile: Callable[[Path, str | None], list[dict]] = reconcile_once,
    enrich: Callable[[Path], dict[str, object]] = enrich_worker_usage,
    kick_runner=_default_kick_runner,
) -> int:
    passes = 0
    while True:
        actions = reconcile(db_path, human_actor_id)
        actions.extend(kick_stale_codex_lineages(actions, db_path=db_path, kick_runner=kick_runner))
        passes += 1
        if heartbeat is not None:
            heartbeat(db_path, interval)
        try:
            usage_counts = enrich(db_path)
        except Exception:
            usage_counts = {"candidate": 0, "enriched": 0, "marked": 0, "skipped": 0, "stage_error": True}
        if logger is not None:
            logger.info(json.dumps({"worker_usage": usage_counts}, sort_keys=True))
        payload = json.dumps({"actions": actions}, sort_keys=True)
        if logger is None:
            print(payload, file=output, flush=True)
        elif actions:
            logger.info(payload)
        elif passes % 40 == 0:
            logger.info(json.dumps({"actions": [], "liveness_pass": passes}, sort_keys=True))
        if once or (max_passes is not None and passes >= max_passes):
            return 0
        sleep(max(interval, 0.1))


def configure_file_logger(log_file: Path) -> logging.Logger:
    log_file = log_file.expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    err_file = log_file.with_name("monitor.err.log")
    if err_file.exists() and err_file.stat().st_size > LOG_MAX_BYTES:
        err_file.write_text("")
    logger = logging.getLogger("agent_comms.monitor")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-comms-monitor")
    parser.add_argument("--db", default=None, help="SQLite database path")
    parser.add_argument("--human-actor-id", help="Human actor to page for DLQ rows")
    parser.add_argument("--once", action="store_true", help="Run one reconciliation pass")
    parser.add_argument("--interval", type=float, default=5.0, help="Polling interval in seconds")
    parser.add_argument("--max-passes", type=int, help="Stop after N reconciliation passes")
    parser.add_argument("--log-file", help="Write monitor output to a rotating log file")
    parser.add_argument("--check-heartbeat", action="store_true", help="Check monitor heartbeat freshness")
    parser.add_argument("--fallback-page-root", help="Project root for a DB-independent watchdog semaphore")
    args = parser.parse_args(argv)
    if args.max_passes is not None and args.max_passes < 1:
        parser.error("--max-passes must be at least 1")
    if args.check_heartbeat and not args.human_actor_id:
        parser.error("--check-heartbeat requires --human-actor-id")
    if args.check_heartbeat and args.log_file:
        parser.error("--log-file is not supported with --check-heartbeat")

    if args.check_heartbeat:
        try:
            db_explicit = args.db is not None or bool(os.environ.get("AGENT_COMMS_DB"))
            db_path = Path(args.db) if args.db is not None else paths.db_path()
            return check_heartbeat(
                db_path,
                args.human_actor_id,
                is_default_db_open=not db_explicit,
                fallback_page_root=args.fallback_page_root,
            )
        except KeyboardInterrupt:
            return 0
        except Exception as page_error:
            return _watchdog_fallback(
                "<unresolved>", None, page_error, args.fallback_page_root, args.human_actor_id
            )

    db_explicit = args.db is not None or bool(os.environ.get("AGENT_COMMS_DB"))
    db_path = Path(args.db) if args.db is not None else paths.db_path()

    logger = configure_file_logger(Path(args.log_file)) if args.log_file else None
    try:
        return run_monitor_loop(
            db_path,
            human_actor_id=args.human_actor_id,
            interval=args.interval,
            once=args.once,
            max_passes=args.max_passes,
            logger=logger,
            heartbeat=lambda path, interval: upsert_heartbeat(
                path,
                interval=interval,
                is_default_db_open=not db_explicit,
            ),
            reconcile=lambda path, human: reconcile_once(
                path,
                human,
                is_default_db_open=not db_explicit,
            ),
            enrich=lambda path: enrich_worker_usage(
                path,
                is_default_db_open=not db_explicit,
            ),
        )
    except KeyboardInterrupt:
        return 0
    except Exception:
        if logger is not None:
            logger.exception("monitor crashed")
        raise


if __name__ == "__main__":
    sys.exit(main())
