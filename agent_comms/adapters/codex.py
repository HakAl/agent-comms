from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DispatchContext
from ._base import ProcessSpawnAdapter
from .. import paths
from ..codex_home import preflight_home_snapshot
from ..codex_auth_refresh import codex_auth_snapshot
from ..spawn import CODEX_ARGS_INTRODUCED


def _configured_auth_stale_after() -> timedelta:
    try:
        days = int(os.environ.get("AGENT_COMMS_CODEX_AUTH_STALE_DAYS", "13"))
    except ValueError:
        days = 13
    return timedelta(days=days)


CODEX_AUTH_STALE_AFTER = timedelta(days=13)


class AuthStale(RuntimeError):
    """Codex auth cannot be trusted for a worker spawn."""


class CodexAdapter(ProcessSpawnAdapter):
    """Codex-runtime command-template adapter."""

    runtime_label = "codex"
    supported_runtimes = ("codex",)

    def _separate_stdout(
        self,
        context: DispatchContext,
        resolved_args: list[str],
        prompt_index: int | None,
    ) -> bool:
        for index, arg in enumerate(resolved_args):
            if index == prompt_index:
                continue
            if arg == "--":
                break
            if arg in (*CODEX_ARGS_INTRODUCED, "--experimental-json"):
                return True
        return False

    def _preflight(self, context: DispatchContext) -> dict | None:
        if context.recipient.get("runtime") not in self.supported_runtimes:
            return

        codex_home = self._resolved_codex_home(context)
        if self._custody_managed(context, codex_home):
            # Managed home: strict preflight is authoritative (never an
            # environment switch). A home that resolves outside the runtime
            # custody root, is a symlink, is missing the regular generated
            # base/profile configs, or has an unresolvable/non-regular/invalid/
            # stale auth file creates zero native process. Homes outside the
            # custody root keep the auth-only check below.
            custody_root = paths.codex_custody_root()
            if not self._within(codex_home, custody_root):
                raise self._auth_stale(
                    codex_home, "resolved home is outside the runtime custody root"
                )
            ok, why, snapshot = preflight_home_snapshot(
                codex_home,
                actor_id=str(context.recipient.get("id", "")) or None,
                stale_after=_configured_auth_stale_after(),
                now=self._now_utc(),
            )
            if not ok:
                raise self._auth_stale(codex_home, str(why))
            return {"codex_auth": snapshot}

        try:
            return {"codex_auth": self._check_auth_fresh(codex_home)}
        except AuthStale:
            raise
        except Exception as exc:
            raise self._auth_stale(codex_home, str(exc)) from exc

    @staticmethod
    def _custody_managed(context: DispatchContext, codex_home: Path) -> bool:
        """Is this actor's home one of the mechanically-identifiable managed cases?

        (a) The checkout-independent production DEFAULT home
        (``paths.provisioned_codex_home(actor_id)``) is managed WITHOUT any SQL
        lookup. (b) ANY home resolved INSIDE the runtime custody root is managed
        (fail closed). Managedness never consults an environment switch; a home
        OUTSIDE the custody root stays on the permissive auth-only check.
        """
        actor_id = str(context.recipient.get("id", "") or "")
        if not actor_id:
            return False
        # (a) production default home -- SQL-independent, fail closed.
        try:
            default_home = paths.provisioned_codex_home(actor_id)
        except Exception:
            default_home = None
        if default_home is not None and str(codex_home) == str(default_home):
            return True
        # (b) any home inside the runtime custody root -- fail closed.
        try:
            if CodexAdapter._within(codex_home, paths.codex_custody_root()):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _within(child: Path, root: Path) -> bool:
        try:
            child_resolved = child.resolve()
            root_resolved = root.resolve()
        except OSError:
            return False
        return child_resolved == root_resolved or root_resolved in child_resolved.parents

    def _resolved_codex_home(self, context: DispatchContext) -> Path:
        spawn = context.recipient.get("spawn") or {}
        raw_env = spawn.get("env", {}) or {}
        if not isinstance(raw_env, dict) or "CODEX_HOME" not in raw_env:
            actor_id = str(context.recipient.get("id", "unknown"))
            raise self._auth_stale(
                paths.codex_home(actor_id),
                "codex recipient requires spawn.env.CODEX_HOME",
            )
        return Path(self._format_arg(str(raw_env["CODEX_HOME"]), context))

    def _check_auth_fresh(self, codex_home: Path) -> dict:
        if not codex_home.is_dir():
            raise self._auth_stale(codex_home, "CODEX_HOME directory is missing")

        auth_path = codex_home / "auth.json"
        snapshot = codex_auth_snapshot(auth_path, now=self._now_utc())
        status = snapshot["read_status"]
        if status == "missing":
            raise self._auth_stale(codex_home, "auth.json is missing")

        if status != "ok":
            raise self._auth_stale(codex_home, "auth.json is not parseable JSON")
        gaps = snapshot["gaps"]
        if "no_last_refresh" in gaps:
            raise self._auth_stale(codex_home, "auth.json last_refresh is missing")

        if "bad_last_refresh" in gaps or "naive_last_refresh" in gaps:
            raise self._auth_stale(codex_home, "auth.json last_refresh is unparseable")
        refreshed_at = datetime.fromisoformat(snapshot["created"])
        if self._now_utc() - refreshed_at > _configured_auth_stale_after():
            raise self._auth_stale(codex_home, "auth.json last_refresh is stale")
        return snapshot

    @staticmethod
    def _parse_last_refresh(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("last_refresh must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _auth_stale(codex_home: Path, reason: str) -> AuthStale:
        home = str(codex_home)
        return AuthStale(
            f"{reason}; CODEX_HOME={home}; recover with: CODEX_HOME={home} codex login"
        )
