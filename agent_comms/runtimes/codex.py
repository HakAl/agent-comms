"""The Codex runtime: template, rerender migration, setup and doctor."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .. import paths
from ..checks import (
    FAIL,
    OK,
    SKIP,
    WARN,
    LedgerRef,
    Runner,
    check,
    home_prefix,
    run_quietly,
    sh,
    version_from,
    version_matches,
)
from ..codex_home import preflight_home_snapshot
from ..custody import fill_codex_custody
from ..runtime_pins import (
    CODEX_PINNED_SHA256_ENV,
    BundleNotDigestible,
    PlatformNotCertified,
    RuntimePin,
    bundle_sha256,
    codex_versions_dir,
)
from ..schema import ValidationError
from . import Rerender

# Args the template gained after rows were stored, which rerender adds.
ARGS_INTRODUCED = ("--json",)
# Adjacent pairs an older template stored, which rerender drops: the worker's
# home no longer holds a per-actor profile (ac-4ao.10 slice 3b). The second
# value is the placeholder or the actor's own id.
ARGS_RETIRED = ("--profile",)
# Commands an older template stored, which rerender replaces with the
# custody binary (ac-4ao.10 slice 3a).
COMMANDS_INTRODUCED = {"codex": "{codex_binary}"}


def render_spawn(codex_home: str | None) -> dict:
    env_home = codex_home if codex_home is not None else "{codex_home}"
    return {
        "command": "{codex_binary}",
        "args": [
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "{worker_prompt}",
        ],
        "env": {"CODEX_HOME": env_home},
    }


def _codex_home_of(actor: dict) -> tuple[Path | None, str | None]:
    try:
        spawn = json.loads(actor.get("spawn_json") or "{}")
    except (TypeError, ValueError):
        return None, "malformed spawn_json"
    env = spawn.get("env") if isinstance(spawn, dict) else None
    value = env.get("CODEX_HOME") if isinstance(env, dict) else None
    if not value:
        return None, "no spawn.env.CODEX_HOME"
    try:
        return paths.resolve_codex_home(actor["id"], str(value)), None
    except ValidationError as exc:
        return None, str(exc)


def check_codex_custody(pin: RuntimePin, *, run: Runner = subprocess.run, bundle: Path | None = None) -> dict:
    """Workers run the custody copy of the pinned Codex, never the ``codex``
    on PATH, so this checks the copy: present, digest certified, version."""
    check_id = "runtime:codex"
    bundle = bundle or codex_versions_dir() / pin.version
    codex = bundle / "bin" / "codex"
    fill = home_prefix() + sh("agent-comms", "custody", "codex")
    replace = f"{fill} --replace"
    certify = f"re-certify codex {pin.version} with `make certify RUNTIME=codex` in a checkout"
    if not codex.is_file():
        return check(check_id, FAIL, f"no custody copy of codex {pin.version}: {codex} is missing", fill)
    expected = os.environ.get(CODEX_PINNED_SHA256_ENV) or pin.sha256 or ""
    if not expected:
        return check(check_id, FAIL, f"the codex {pin.version} pin records no bundle sha256", certify)
    try:
        actual = bundle_sha256(bundle)
    except (BundleNotDigestible, OSError) as exc:
        return check(check_id, FAIL, f"custody copy of codex {pin.version} cannot be verified: {exc}", replace)
    if actual != expected:
        return check(
            check_id,
            FAIL,
            f"custody copy of codex {pin.version} differs from the certified bundle: expected sha256 {expected}, got {actual}; path={bundle}",
            replace,
        )
    result = run_quietly(run, [str(codex), "--version"])
    if isinstance(result, Exception):
        return check(check_id, FAIL, f"could not execute {codex} --version: {result}", replace)
    installed, output = version_from(result)
    if result.returncode != 0 or installed is None:
        return check(check_id, FAIL, f"{codex} --version exited {result.returncode}: {output!r}", replace)
    if not version_matches(installed, pin):
        return check(check_id, FAIL, f"custody codex reports {installed}, the pin is {pin.version}; path={codex}", certify)
    return check(check_id, OK, f"codex {installed} custody at {codex}, digest certified")


def check_codex(
    pin: RuntimePin | None,
    workers: list[dict],
    dead_credentials: list[dict],
    *,
    run: Runner = subprocess.run,
    bundle: Path | None = None,
    auth_source: Path | None = None,
    operator_auth: Path | None = None,
    now: datetime | None = None,
    ledger: LedgerRef | None = None,
) -> list[dict]:
    """``runtime:codex`` (the custody copy), ``runtime:codex:auth`` (the
    shared login), one entry per worker home and one per dead login."""
    ledger = ledger or LedgerRef(paths.db_path(), False)
    if pin is None:
        return [check("runtime:codex", SKIP, "platform is not certified for codex (see platform)")]
    checks = [check_codex_custody(pin, run=run, bundle=bundle)]
    checks.append(check_shared_auth(auth_source, operator_auth))
    for worker in workers:
        worker_id = worker["id"]
        home_id = f"runtime:codex:home:{worker_id}"
        home, why = _codex_home_of(worker)
        if home is None:
            checks.append(check(home_id, FAIL, f"{worker_id}: {why}", f"re-register {worker_id} so its spawn carries CODEX_HOME: {ledger.cli('bootstrap')}"))
            continue
        ok, reason, _snapshot = preflight_home_snapshot(home, actor_id=worker_id, now=now)
        if ok:
            checks.append(check(home_id, OK, f"{worker_id}: {home} provisioned, auth fresh"))
            continue
        if reason and reason.startswith("auth.json"):
            checks.append(check(home_id, FAIL, f"{worker_id}: {home}: {reason}", paths.codex_login_command(home)))
            continue
        # The repair names the inspected home and the inspected ledger, so it
        # cannot land on the default home or the default ledger by accident.
        checks.append(
            check(
                home_id,
                FAIL,
                f"{worker_id}: {home}: {reason}; the worker is protected, so the repair needs an override reason",
                ledger.cli(
                    "provision-codex-home",
                    "--actor-id", worker_id,
                    "--project-root", worker.get("project_root") or "<project-root>",
                    "--codex-home", home,
                    "--override-protected", "doctor: restore the missing codex home",
                ),
            )
        )
    for row in dead_credentials:
        lineage = str(row["lineage_key"])
        checks.append(
            check(
                f"runtime:codex:login:{lineage}",
                FAIL,
                f"expired login (refresh failed: {row['dead_reason']} since {row['dead_at']}); lineage {lineage}",
                paths.codex_login_command(Path(lineage)),
            )
        )
    return checks


# The Codex config key that moves the login out of auth.json (0.164.0
# config_toml.rs); ``keyring`` and ``auto`` key the secret by the canonical
# CODEX_HOME, so no other home can share it.
CREDENTIALS_STORE_KEY = "cli_auth_credentials_store"
UNSHAREABLE_STORES = ("keyring", "auto")


def _credentials_store(config: Path) -> str | None:
    """``cli_auth_credentials_store`` from the operator's config.toml, if readable."""
    try:
        document = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = document.get(CREDENTIALS_STORE_KEY)
    return value if isinstance(value, str) else None


def check_shared_auth(link: Path | None = None, operator_auth: Path | None = None) -> dict:
    """``runtime:codex:auth``: the runtime's shared ``auth.json`` is a link
    to the operator's own login, and that login is a file Codex will share."""
    check_id = "runtime:codex:auth"
    link = (link or paths.runtime_codex_auth_source()).expanduser()
    operator_auth = (operator_auth or paths.operator_codex_auth()).expanduser()
    problem = paths.operator_codex_auth_problem(operator_auth)
    if problem:
        return check(check_id, FAIL, problem, f"unset {paths.CODEX_HOME_ENV} and run doctor again")
    bind = home_prefix() + sh("agent-comms", "bind-codex-login")
    replace = f"{bind} --replace"
    config = operator_auth.parent / "config.toml"
    store = _credentials_store(config)
    if store in UNSHAREABLE_STORES:
        return check(
            check_id,
            FAIL,
            f"Codex keeps its login in the keyring ({CREDENTIALS_STORE_KEY} = {store!r} in {config}), which workers cannot share",
            f'set {CREDENTIALS_STORE_KEY} = "file" in {config} and run codex login',
        )
    if link.is_symlink():
        if os.path.realpath(link) == os.path.realpath(operator_auth):
            if operator_auth.is_file():
                return check(check_id, OK, f"shared with your Codex login ({operator_auth})")
            return check(check_id, FAIL, f"not logged in to Codex: {operator_auth} is missing", "codex login")
        current = os.readlink(link)
        if link.is_file():
            return check(check_id, WARN, f"{link} links to {current}, not your Codex login ({operator_auth})", replace)
        return check(check_id, FAIL, f"{link} links to {current}, which does not exist", replace)
    if link.is_file():
        return check(check_id, WARN, f"separate worker login at {link}; your own Codex login ({operator_auth}) is not shared", replace)
    if link.exists():
        return check(check_id, FAIL, f"{link} is neither a file nor a link; move it away", bind)
    return check(check_id, FAIL, f"shared auth {link} is missing; workers share your own Codex login once it is bound", bind)


class CodexRuntime:
    name = "codex"
    capabilities = ("implementation", "review")
    pinned = True
    fallback = False
    monitored = True
    rerender = Rerender(ARGS_INTRODUCED, ARGS_RETIRED, COMMANDS_INTRODUCED)

    @property
    def adapter_class(self) -> type:
        from ..adapters.codex import CodexAdapter

        return CodexAdapter

    def adapter(self):  # type: ignore[no-untyped-def]
        return self.adapter_class()

    def detect(self, which: Callable[[str], str | None], pin: RuntimePin | None) -> bool:
        return bool(which("codex"))

    def render_spawn(self, actor_id: str, *, codex_home: str | None = None) -> dict:
        return render_spawn(codex_home)

    def provision(self, worker_id, *, pin, pin_error, which, run) -> dict:  # type: ignore[no-untyped-def]
        homes = [str(paths.provisioned_codex_home(worker_id))]
        # Best effort: a missing custody copy fails dispatch and doctor names
        # the fill command, so setup records why and carries on.
        try:
            filled = fill_codex_custody(which=which, run=run)
            custody = {"present": True, "path": filled["path"], "status": filled["status"]}
        except (ValidationError, PlatformNotCertified, OSError, ValueError) as exc:
            custody = {"present": False, "reason": str(exc)}
        return {"codex_homes": homes, "codex_custody": custody}

    def provision_absent(self) -> dict:
        return {"codex_homes": [], "codex_custody": None}

    def setup_refusal(self) -> str | None:
        # Workers share the operator's own login, so it must not sit inside
        # the runtime state the link would be written to.
        return paths.operator_codex_auth_problem()

    def doctor_checks(self, pin, snapshot, *, run, now, ledger, which=None) -> list[dict]:  # type: ignore[no-untyped-def]
        return check_codex(
            pin,
            snapshot.workers(self.name) if snapshot else [],
            snapshot.dead_credentials if snapshot else [],
            run=run,
            now=now,
            ledger=ledger,
        )

    def doctor_skip(self) -> dict | None:
        return check("runtime:codex", SKIP, "no codex worker registered")


RUNTIME = CodexRuntime()
