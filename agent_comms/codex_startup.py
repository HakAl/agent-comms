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
  for a non-login shell to load, and an empty value names none.

This is launch hygiene for the shells the runtime creates. A shell the
worker starts itself as ``bash -l`` or ``bash -i`` still reads the files in
``HOME``. Settings follow Codex 0.164.0; certification rechecks them against
the pinned build (``tests/cells/test_cell_codex.py``).
"""

from __future__ import annotations

import tomllib

PINNED_FEATURES = ("shell_snapshot", "shell_snapshot_v2")
# Variables that name a file for a shell to load at startup.
STARTUP_ENV_NAMES = frozenset({"BASH_ENV", "ENV"})
_TOP_LEVEL = ("allow_login_shell", "features", "shell_environment_policy")


def config_overrides() -> list[str]:
    """The ``-c`` pairs that pin the settings above for one run."""
    values = ["allow_login_shell=false"]
    values += [f"features.{name}=false" for name in PINNED_FEATURES]
    values += [f'shell_environment_policy.set.{name}=""' for name in sorted(STARTUP_ENV_NAMES)]
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
