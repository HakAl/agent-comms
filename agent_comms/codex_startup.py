"""Keeping operator shell startup files out of a Codex worker's own shells.

Codex starts a shell for every worker command and, by default, starts it as
a login shell and replays a snapshot captured from the operator's interactive
startup files. The worker keeps the operator's ``HOME``, so under bash that
loads ``.bash_profile``, ``.profile`` and ``.bashrc`` into every command; an
empty ``ZDOTDIR`` only covers zsh. A worker launch therefore pins, with
``-c``:

- ``allow_login_shell = false``: a command shell is never a login shell;
- ``features.shell_snapshot`` and ``shell_snapshot_v2`` off: no capture of
  startup state to replay;
- ``shell_environment_policy.set`` ``BASH_ENV`` and ``ENV`` empty: explicit
  values apply after every exclusion, so a config layer cannot name a file
  for a non-login shell to load, and an empty value names none;
- ``shell_environment_policy.set`` ``SHLVL`` = ``1``: bash loads ``.bashrc``
  for a plain ``bash -c`` when its stdin is a socket (its remote-shell rule)
  and the shell level is below 2. Codex gives command shells a socket, and
  the worker environment carries no ``SHLVL``, so without this every command
  loaded ``.bashrc``. Starting from 1, the command shell is at level 2.

The pins set single keys, and Codex merges every other key of
``shell_environment_policy`` from its config layers (the worker's home, a
profile the stored args select, the project's ``.codex/config.toml``). A
layer can therefore undo them without any stored option: ``include_only`` is
applied after ``set`` and removes ``SHLVL``; ``set.ZDOTDIR`` replaces the
empty directory the worker is given, and ``exclude`` or a narrower
``inherit`` drops it, so zsh falls back to ``HOME``. The dispatch preflight
therefore reads the configuration Codex resolved (``config/read``) and
refuses one that would not keep the protected variables
(``effective_problems``). Other restrictions are left as configured.

This is launch hygiene for the shells the runtime creates. A shell the
worker starts itself as ``bash -l`` or ``bash -i`` still reads the files in
``HOME``. Settings follow Codex 0.164.0; certification rechecks them against
the pinned build (``tests/cells/test_cell_codex.py``).
"""

from __future__ import annotations

import tomllib
from typing import Callable

from . import codex_hook

PINNED_FEATURES = ("shell_snapshot", "shell_snapshot_v2")
# Variables that name a file for a shell to load at startup.
STARTUP_ENV_NAMES = frozenset({"BASH_ENV", "ENV"})
# What shell_environment_policy.set pins for every command shell.
PINNED_ENV = {"BASH_ENV": "", "ENV": "", "SHLVL": "1"}
_TOP_LEVEL = ("allow_login_shell", "features", "shell_environment_policy")
# Variables a command shell must end up with. SHLVL comes from the pin; the
# worker's empty ZDOTDIR is inherited from the launch environment.
PROTECTED_ENV_NAMES = ("SHLVL", "ZDOTDIR")
# The policy keys this check understands. Any other key with a value is
# refused: it could filter or replace the protected variables unseen.
_KNOWN_POLICY_KEYS = frozenset(
    {"inherit", "ignore_default_excludes", "exclude", "include_only", "set", "experimental_use_profile"}
)


def config_overrides() -> list[str]:
    """The ``-c`` pairs that pin the settings above for one run."""
    values = ["allow_login_shell=false"]
    values += [f"features.{name}=false" for name in PINNED_FEATURES]
    values += [f'shell_environment_policy.set.{name}="{value}"' for name, value in sorted(PINNED_ENV.items())]
    argv: list[str] = []
    for value in values:
        argv += ["-c", value]
    return argv


def touches_startup(override: str) -> bool:
    """Whether a ``-c key=value`` sets a pinned setting: ``allow_login_shell``,
    a pinned feature or the whole ``features`` table, or anything under
    ``shell_environment_policy``. A key that does not parse is read as ours
    when it names one of them at all."""
    key = override.partition("=")[0].strip()
    try:
        path = tomllib.loads(f"{key} = 0")
    except tomllib.TOMLDecodeError:
        return any(name in key for name in _TOP_LEVEL)
    if "allow_login_shell" in path or "shell_environment_policy" in path:
        return True
    features = path.get("features")
    return features == 0 or (isinstance(features, dict) and any(name in features for name in PINNED_FEATURES))


def startup_toggles(toggles: list[tuple[str, str]]) -> list[str]:
    """The ``--enable``/``--disable`` options that name a pinned feature.
    Codex turns each into a ``features.<name>`` override applied after the
    ``-c`` values, so it would outrank the pin."""
    return [f"{flag} {name}" for flag, name in toggles if name.strip() in PINNED_FEATURES]


def read_config(
    codex: str,
    *,
    cwd: str,
    env: dict[str, str],
    overrides: list[str],
    timeout: float = 5.0,
    request: Callable[..., "tuple[dict | None, str | None]"] = codex_hook.app_server_request,
) -> tuple[dict | None, str | None]:
    """The configuration Codex resolves for ``cwd`` with ``overrides``
    (``config/read`` from ``codex app-server``): (config, None) or (None, why)."""
    message, error = request(
        codex, "config/read", {"cwd": cwd, "includeLayers": False},
        cwd=cwd, env=env, overrides=overrides, timeout=timeout,
    )
    if message is None:
        return None, error
    if "error" in message:
        return None, f"codex config/read failed: {message['error']}"
    config = (message.get("result") or {}).get("config")
    if not isinstance(config, dict):
        return None, "codex config/read answered without a config"
    return config, None


def _codex_pattern_matches(pattern: str, name: str) -> bool:
    """Whether a Codex environment pattern matches ``name``. Codex 0.164.0
    knows two wildcards, ``*`` (any run) and ``?`` (one character); every
    other character, brackets included, is literal and compared by its
    lowercase form, one character at a time. Matched iteratively, restarting
    from the last ``*``, so the work is bounded by the two lengths' product
    (a regex built from the pattern backtracks over a run of stars)."""
    at = position = 0
    star = resume = -1
    while position < len(name):
        char = pattern[at] if at < len(pattern) else None
        if char == "*":
            star, resume = at, position
            at += 1
        elif char is not None and (char == "?" or char.lower() == name[position].lower()):
            at += 1
            position += 1
        elif star >= 0:
            # Let the last star take one more character and try again.
            resume += 1
            at, position = star + 1, resume
        else:
            return False
    return all(char == "*" for char in pattern[at:])


def effective_problems(config: dict) -> list[str]:
    """Why the configuration Codex resolved (``config/read``) would not keep
    operator startup files out of the shells Codex creates. Empty when every
    pin holds and nothing filters out or replaces a protected variable."""
    problems: list[str] = []
    if config.get("allow_login_shell") is not False:
        problems.append(f"allow_login_shell is {config.get('allow_login_shell')!r}, not false")
    features = config.get("features")
    features = features if isinstance(features, dict) else {}
    if features.get("shell_snapshot") is not False:
        problems.append(f"features.shell_snapshot is {features.get('shell_snapshot')!r}, not false")
    # Codex 0.164.0 does not report a feature it does not know; absent is off.
    if features.get("shell_snapshot_v2") not in (False, None):
        problems.append(f"features.shell_snapshot_v2 is {features.get('shell_snapshot_v2')!r}, not false")
    policy = config.get("shell_environment_policy")
    policy = policy if isinstance(policy, dict) else {}
    env_set = policy.get("set")
    env_set = env_set if isinstance(env_set, dict) else {}
    for name, value in sorted(PINNED_ENV.items()):
        if env_set.get(name) != value:
            problems.append(f"shell_environment_policy.set.{name} is {env_set.get(name)!r}, not {value!r}")
    if "ZDOTDIR" in env_set:
        problems.append("shell_environment_policy.set.ZDOTDIR replaces the worker's empty ZDOTDIR")
    # Applied after set: a list without the exact names removes them. A
    # pattern is not accepted in their place; the names cost nothing to add.
    include_only = policy.get("include_only")
    if include_only:
        names = include_only if isinstance(include_only, list) else []
        missing = [name for name in PROTECTED_ENV_NAMES if name not in names]
        if missing:
            problems.append(
                f"shell_environment_policy.include_only {include_only!r} omits {', '.join(missing)}; "
                "add the exact names"
            )
    # ZDOTDIR is inherited, so what is applied before set can drop it.
    if policy.get("inherit") not in (None, "all"):
        problems.append(f"shell_environment_policy.inherit is {policy.get('inherit')!r}, which drops ZDOTDIR")
    exclude = policy.get("exclude")
    for pattern in exclude if isinstance(exclude, list) else ([exclude] if exclude else []):
        if _codex_pattern_matches(str(pattern), "ZDOTDIR"):
            problems.append(f"shell_environment_policy.exclude {str(pattern)!r} removes ZDOTDIR")
    if policy.get("experimental_use_profile"):
        problems.append("shell_environment_policy.experimental_use_profile runs commands through the login profile")
    for key in sorted(set(policy) - _KNOWN_POLICY_KEYS):
        if policy[key] is not None:
            problems.append(f"shell_environment_policy.{key} is not a setting this check knows")
    return problems
