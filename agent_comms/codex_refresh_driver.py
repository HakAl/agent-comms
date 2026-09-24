"""DB-resolved Codex authentication refresh after expiry.

Codex logins cannot be refreshed before they expire. When dispatch finds a
lineage's token stale, the monitor starts one run of this driver (see
``agent_comms.monitor``); ``codex_auth_refresh.refresh_if_due`` then refreshes
only lineages whose token has actually expired. There is no scheduled or
proactive refresh. The driver runs as its own process-group leader, so the
containment sweep below covers every ``codex exec`` child it starts.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime, timezone

from . import paths
from .codex_home import _codex_actor_rows, _scan_codex_actors
from .codex_auth_refresh import FAILURE_OUTCOMES, PageFailed, refresh_if_due

REFRESH_EXEC_TIMEOUT_SECONDS = 300
CONTAINMENT_SWEEP_ATTEMPTS = 3


def _default_process_enumerator(*, parent_pid=None, group_id=None):
    """Return ``(pid, pgid)`` pairs selected by parent or process group."""
    flag, value = ("-P", parent_pid) if parent_pid is not None else ("-g", group_id)
    result = subprocess.run(
        ["/usr/bin/pgrep", flag, str(value)], capture_output=True, text=True,
        check=False,
    )
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise RuntimeError(f"pgrep_{flag[1:]}_failed:{result.returncode}")
    processes = []
    for pid in map(int, result.stdout.split()):
        try:
            processes.append((pid, os.getpgid(pid)))
        except ProcessLookupError:
            pass
    return processes


def _descendant_snapshot(root_pid, process_enumerator):
    snapshot = []
    pending = [root_pid]
    seen = {root_pid}
    while pending:
        parent = pending.pop()
        for pid, pgid in process_enumerator(parent_pid=parent):
            if pid not in seen:
                seen.add(pid)
                snapshot.append((pid, pgid))
                pending.append(pid)
    return snapshot


def _resolve_rows(rows) -> list[dict]:
    actors = []
    for row in rows:
        home = paths.resolve_codex_home(row["id"], row["codex_home_value"])
        auth_path = home / "auth.json"
        try:
            stat_result = auth_path.stat()
            fs_identity = (stat_result.st_dev, stat_result.st_ino)
        except OSError:
            fs_identity = None
        actors.append({"actor_id": row["id"], "codex_home": str(home),
                       "lineage_key": paths.codex_auth_lineage_key(row["id"], row["codex_home_value"]),
                       "fs_identity": fs_identity, "auth_identity": fs_identity})
    return actors


def scan_codex_actors(store) -> tuple[list[dict], list[dict]]:
    store._db.init()
    with store._db.connection() as conn:
        rows, defects = _scan_codex_actors(conn)
    return _resolve_rows(rows), defects


def resolve_codex_actors(store) -> list[dict]:
    store._db.init()
    with store._db.connection() as conn:
        rows = _codex_actor_rows(conn)
    return _resolve_rows(rows)


def group_by_lineage(actors: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for actor in actors:
        groups.setdefault(actor["lineage_key"], []).append(actor)
    return groups


def _default_exec_runner(codex_home: str, codex_binary: str, *,
                         process_enumerator=_default_process_enumerator,
                         kill=os.kill, getpgid=os.getpgid, getpid=os.getpid,
                         sleep=time.sleep):
    """Run ``codex exec`` synchronously in the inherited process group.

    Deliberately do not use ``setsid``, ``start_new_session``, or a detached
    process group: the containment sweep checks this process group, so the
    child must stay in it.
    """
    env = dict(os.environ)
    env["CODEX_HOME"] = codex_home
    argv = [
        codex_binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
        "-c", "mcp_servers={}", "-c", "features.plugins=false",
        "-c", "features.remote_plugin=false", "Reply exactly: OK",
    ]
    proc = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(
            timeout=int(
                os.environ.get(
                    "AGENT_COMMS_CODEX_REFRESH_EXEC_TIMEOUT_SECONDS",
                    REFRESH_EXEC_TIMEOUT_SECONDS,
                )
            )
        )
        if proc.returncode == 0:
            return True
        output = (stderr.decode(errors="replace") + stdout.decode(errors="replace"))[
            -65536:
        ]
        return {"ok": False, "containment_failed": False, "output": output}
    except subprocess.TimeoutExpired:
        try:
            snapshot = _descendant_snapshot(proc.pid, process_enumerator)
            proc.kill()
            proc.wait()
            own_pid = getpid()
            own_pgid = getpgid(0)
            for attempt in range(CONTAINMENT_SWEEP_ATTEMPTS):
                for pid, pgid in snapshot:
                    try:
                        kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    if pgid != own_pgid:
                        try:
                            kill(-pgid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                live_snapshot = {
                    pid for pid, _pgid in snapshot
                    if _pid_exists(pid, kill)
                }
                in_group = {
                    pid for pid, _pgid in process_enumerator(group_id=own_pgid)
                    if pid != own_pid
                }
                if not live_snapshot and not in_group:
                    return {"ok": False, "containment_failed": False}
                if attempt + 1 < CONTAINMENT_SWEEP_ATTEMPTS:
                    sleep(0.25)
            return {"ok": False, "containment_failed": True}
        except Exception:
            return {"ok": False, "containment_failed": True}


def _pid_exists(pid, kill):
    try:
        kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _base(actors, groups, results):
    failures = sum(r["outcome"] in FAILURE_OUTCOMES for r in results)
    deferred = sum(r["outcome"] in {"deferred_busy", "claim_contention"} for r in results)
    return {"ok": failures == 0, "actors": len(actors), "lineages": len(groups),
            "lineages_refreshed": sum(r["outcome"] == "refreshed" for r in results),
            "unresolved_actors": sum(a["fs_identity"] is None for a in actors),
            "failures": failures, "deferred": deferred, "results": results}


def refresh(store, *, exec_runner=_default_exec_runner) -> dict:
    actors = resolve_codex_actors(store)
    groups = group_by_lineage(actors)
    ordered = sorted(groups.items())
    # Distinct realpaths sharing an inode are unsupported hard-link topology.
    by_inode: dict[tuple[int, int], set[str]] = {}
    for actor in actors:
        if actor["fs_identity"] is not None:
            by_inode.setdefault(actor["fs_identity"], set()).add(actor["lineage_key"])
    refused_keys = {key for keys in by_inode.values() if len(keys) > 1 for key in keys}
    results = []
    try:
        for ordinal, (lineage_key, members) in enumerate(ordered):
            if lineage_key in refused_keys:
                # Let the refresh module own durable page throttling and output.
                from .codex_auth_refresh import _page
                with store._db.connection() as conn:
                    conn.execute("insert into codex_refresh_claims(lineage_key) values(?) on conflict(lineage_key) do update set first_deferred_at=NULL", (lineage_key,))
                _page(store, ordinal, "hardlink_refused", len(members), lineage_key,
                      datetime.now(timezone.utc))
                item = {"outcome": "hardlink_refused", "ok": False}
            else:
                item = refresh_if_due(store, lineage_key=lineage_key,
                    codex_home=members[0]["codex_home"], actor_count=len(members),
                    lineage_ordinal=ordinal, exec_runner=exec_runner)
            results.append({"lineage_ordinal": ordinal, "actor_count": len(members), **item})
    except PageFailed:
        value = _base(actors, groups, results)
        value["ok"] = False
        value["page_failed"] = True
        return value
    return _base(actors, groups, results)


def auth_targets_report(store) -> dict:
    """Credential-free report of Codex actors grouped by distinct auth lineage.

    Exposes only opaque group ordinals, actor ids, and counts -- never a
    resolved auth path or credential bytes (the canonical lineage identity is a
    device+inode pair, deliberately not surfaced).
    """
    actors, actor_defects = scan_codex_actors(store)
    groups = group_by_lineage(actors)
    lineages = [
        {
            "group": ordinal,
            "actor_count": len(members),
            "actors": sorted(a["actor_id"] for a in members),
        }
        for ordinal, (identity, members) in enumerate(sorted(groups.items(), key=lambda kv: kv[0]))
    ]
    unresolved = sorted(a["actor_id"] for a in actors if a["auth_identity"] is None)
    report = {
        "actors": len(actors),
        "lineages": lineages,
        "unresolved_actors": unresolved,
    }
    if actor_defects:
        report["actor_defects"] = actor_defects
        report["refresh_blocked"] = True
    return report
