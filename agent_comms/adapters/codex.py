from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import DispatchContext
from ._base import ProcessSpawnAdapter
from .. import codex_binding, codex_hook, paths
from ..codex_home import preflight_home_snapshot
from ..codex_auth_refresh import codex_auth_snapshot
from ..policies import WORKER_DISPATCH_POLICY_VERSION, compile_policy
from ..runtime_pins import (
    BundleNotDigestible,
    bundle_sha256,
    codex_binary_path,
    codex_bundle_path,
    codex_pin,
    codex_pinned_sha256,
)
from ..runtimes.codex import ARGS_INTRODUCED as CODEX_ARGS_INTRODUCED
from ..store import WORKER_DISPATCH_POLICY


def _configured_auth_stale_after() -> timedelta:
    try:
        days = int(os.environ.get("AGENT_COMMS_CODEX_AUTH_STALE_DAYS", "13"))
    except ValueError:
        days = 13
    return timedelta(days=days)


CODEX_AUTH_STALE_AFTER = timedelta(days=13)


class AuthStale(RuntimeError):
    """Codex auth cannot be trusted for a worker spawn."""


class McpBindingMismatch(RuntimeError):
    """Codex would not bind the worker's agent-comms server as intended."""


class PolicyHookUnverified(RuntimeError):
    """Codex would not run the worker's policy hook, or the hook does not run."""


HookLister = Callable[..., "tuple[list | None, str | None]"]


class CodexCustodyUnavailable(RuntimeError):
    """The custody copy of the pinned Codex is missing or unreadable."""


class CodexCustodyDigestMismatch(RuntimeError):
    """The custody copy of the pinned Codex differs from the certified bundle."""


class CodexSpawnRowNotPinnedError(RuntimeError):
    """A Codex actor's stored command is not the custody binary."""


class CodexAdapter(ProcessSpawnAdapter):
    """Codex-runtime command-template adapter.

    Every run binds the worker's ``agent-comms`` server with ``-c``
    overrides, which outrank the worker's base config and any project
    ``.codex/config.toml`` (the architect's seat, in a project the worker's
    home trusts). Codex merges fields across layers, so the overrides pin
    every field that decides identity and availability, and the preflight
    asks Codex itself (``codex mcp get --json`` with the worker's home, cwd
    and overrides) and refuses the dispatch on any difference. The check
    runs through ``mcp_runner`` so hermetic tests stub it.

    The worker's PreToolUse policy hook is declared and trusted the same
    way, per run (``codex_hook``). The preflight asks Codex whether it would
    run that hook (``hooks/list`` through ``hook_lister``) and runs the hook
    once as Codex would (through ``hook_runner``), since Codex runs the tool
    anyway when a hook cannot start.
    """

    runtime_label = "codex"
    supported_runtimes = ("codex",)

    def __init__(
        self,
        *,
        mcp_runner: codex_binding.Runner | None = None,
        hook_lister: HookLister | None = None,
        hook_runner: codex_binding.Runner | None = None,
    ) -> None:
        super().__init__()
        self._mcp_runner = mcp_runner or subprocess.run
        self._hook_lister = hook_lister or codex_hook.list_hooks
        self._hook_runner = hook_runner or subprocess.run

    def mcp_binding(self, context: DispatchContext) -> codex_binding.Binding:
        actor_id = str(context.recipient["id"])
        project_root = str(context.recipient["project_root"])
        home = paths.home_override()
        args = ["--actor-id", actor_id, "--db", str(context.db_path)]
        if home is not None:
            args += ["--home", str(home)]
        env = codex_binding.server_env(
            AGENT_COMMS_ACTOR_ID=actor_id,
            AGENT_COMMS_DB=str(context.db_path),
            AGENT_COMMS_HOME=None if home is None else str(home),
            AGENT_COMMS_PROJECT_ROOT=project_root,
            WAKE_POLICY=WORKER_DISPATCH_POLICY,
            WAKE_POLICY_VERSION=WORKER_DISPATCH_POLICY_VERSION,
        )
        return codex_binding.Binding(str(paths.mcp_command()), args, project_root, env)

    def _runtime_prefix_args(self, context: DispatchContext) -> list[str]:
        if context.recipient.get("runtime") not in self.supported_runtimes:
            return []
        # Headless exec cancels MCP tool calls that need approval.
        return [
            *codex_binding.config_overrides(
                self.mcp_binding(context), extra={"default_tools_approval_mode": "approve"}
            ),
            *codex_hook.config_overrides(self.policy_hook_command(context)),
        ]

    @staticmethod
    def policy_hook_command(context: DispatchContext) -> str:
        """The PreToolUse hook command for this dispatch's policy and root."""
        policy = compile_policy(str(context.dispatch["policy_name"]))
        return codex_hook.hook_command(
            python=sys.executable,
            hook_path=str(paths.hooks_path()),
            policy_name=policy.name,
            project_root=str(context.recipient["project_root"]),
            hook_sha256=str(policy.hook_sha256),
        )

    def _binding_preflight(
        self, context: DispatchContext, command: str, prefix: list[str], stored: list[str], env: dict[str, str]
    ) -> dict | None:
        if not prefix:
            return None
        # A later -c wins, so a stored one that sets our server would undo
        # the binding; refused rather than outranked.
        launch = codex_binding.launch_options(stored)
        conflicting = [value for value in launch.overrides if codex_binding.overrides_server(value)]
        if conflicting:
            raise McpBindingMismatch(
                f"the stored spawn args for {context.recipient['id']} set {codex_binding.SERVER_NAME} with -c "
                f"({', '.join(conflicting)}), which would override the per-run binding; remove them from the spawn block"
            )
        hooks = [value for value in launch.overrides if codex_hook.touches_hooks(value)]
        if hooks:
            raise PolicyHookUnverified(
                f"the stored spawn args for {context.recipient['id']} set hooks with -c ({', '.join(hooks)}), "
                "which could replace or disable the policy hook; remove them from the spawn block"
            )
        # Exact-hash trust is the contract; the bypass would also run every
        # untrusted hook while hooks/list still reports ours trusted.
        if codex_hook.BYPASS_TRUST_FLAG in launch.flags:
            raise PolicyHookUnverified(
                f"the stored spawn args for {context.recipient['id']} pass {codex_hook.BYPASS_TRUST_FLAG}, "
                "which would run untrusted hooks; remove it from the spawn block"
            )
        # The check loads what the launch loads: the same profile layer,
        # start directory and every -c in launch order.
        root = str(context.recipient["project_root"])
        cwd = os.path.join(root, launch.cd) if launch.cd else root
        overrides = [*(["-p", launch.profile] if launch.profile is not None else []), *prefix]
        for value in launch.overrides:
            overrides += ["-c", value]
        observed, error = codex_binding.resolve(
            self._mcp_runner, command, cwd=cwd, env=env, overrides=overrides
        )
        if observed is None:
            raise McpBindingMismatch(f"worker MCP binding not verified: {error}")
        problems = codex_binding.effective_problems(observed, self.mcp_binding(context))
        if problems:
            raise McpBindingMismatch(
                f"Codex would bind {codex_binding.SERVER_NAME} for {context.recipient['id']} differently from the "
                f"dispatch in {cwd}: {'; '.join(problems)}; remove the conflicting entry from the worker's "
                f"CODEX_HOME config or the project's .codex/config.toml layers"
            )
        self._policy_hook_preflight(context, command, cwd=cwd, env=env, overrides=overrides)
        return {"codex_mcp_binding": "verified", "codex_policy_hook": "verified"}

    def _policy_hook_preflight(
        self, context: DispatchContext, command: str, *, cwd: str, env: dict[str, str], overrides: list[str]
    ) -> None:
        hook = self.policy_hook_command(context)
        entries, error = self._hook_lister(command, cwd=cwd, env=env, overrides=overrides)
        if entries is None:
            raise PolicyHookUnverified(f"worker policy hook not verified: {error}")
        problems = codex_hook.trust_problems(entries, hook)
        if problems:
            raise PolicyHookUnverified(
                f"Codex would not run the policy hook for {context.recipient['id']} in {cwd}: "
                f"{'; '.join(problems)}; recover with: make certify RUNTIME=codex"
            )
        # A fresh, empty ZDOTDIR, as the worker gets: the login shell Codex
        # runs a hook in must not read the operator's zsh startup files.
        with tempfile.TemporaryDirectory(prefix="agent-comms-hook-") as zdotdir:
            error = codex_hook.smoke_run(
                hook, cwd=cwd, env={**env, "ZDOTDIR": zdotdir}, runner=self._hook_runner
            )
        if error:
            raise PolicyHookUnverified(f"worker policy hook for {context.recipient['id']} does not run: {error}")

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

        self._check_custody()
        codex_home = self._resolved_codex_home(context)
        if self._custody_managed(context, codex_home):
            # Managed home: strict preflight is authoritative (never an
            # environment switch). A home that resolves outside the runtime
            # custody root, is a symlink, is missing the regular generated
            # base config, or has an unresolvable/non-regular/invalid/
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
    def _check_custody() -> None:
        """Refuse unless the custody bundle is the certified one, byte for byte.

        Workers never run the ``codex`` on PATH, which updates itself; they
        run ``<custody>/<pinned version>/bin/codex``, and the bundle digest
        stands in for the version check because it pins the exact bytes.
        """
        bundle = codex_bundle_path()
        version = codex_pin().version
        if not (bundle / "bin" / "codex").is_file():
            raise CodexCustodyUnavailable(
                f"no custody copy of codex {version}: {bundle / 'bin' / 'codex'} is missing; "
                "recover with: agent-comms custody codex"
            )
        expected = codex_pinned_sha256()
        if not expected:
            raise CodexCustodyDigestMismatch(
                f"the codex {version} pin records no bundle sha256; recover with: make certify RUNTIME=codex"
            )
        try:
            actual = bundle_sha256(bundle)
        except (BundleNotDigestible, OSError) as exc:
            raise CodexCustodyUnavailable(
                f"custody copy of codex {version} cannot be verified: {exc}; path={bundle}; "
                "recover with: agent-comms custody codex --replace"
            ) from exc
        if actual != expected:
            raise CodexCustodyDigestMismatch(
                f"custody copy of codex {version} differs from the certified bundle: expected sha256 {expected}, "
                f"got {actual}; path={bundle}; recover with: agent-comms custody codex --replace"
            )

    def _validate_rendered_command(self, command: str, context: DispatchContext) -> None:
        if context.recipient.get("runtime") not in self.supported_runtimes:
            return
        # Exact match on the absolute custody path, never a resolve() here: the
        # binding check and launch run this string in the worker's directory.
        if command != str(codex_binary_path()):
            actor_id = context.recipient.get("id")
            raise CodexSpawnRowNotPinnedError(
                f"Codex spawn command is not the custody binary: rendered command={command!r}; "
                f"actor_id={actor_id!r}; recover with: agent-comms rerender-spawn {actor_id} --apply"
            )

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
