"""Keeping what operator shell startup files export out of a Claude worker's commands.

A Claude worker keeps the operator's ``HOME``. Claude Code starts three kinds
of shell there (observed with 2.1.310):

- a probe, ``<shell> -c env``, and one shell per Bash command. Both get a
  socket on stdin. bash loads ``.bashrc`` for a plain ``bash -c`` whose stdin
  is a socket while the shell level is below 2 (its remote-shell rule), and
  the worker environment carries no ``SHLVL``, so without a pin every command
  ran ``.bashrc`` and inherited what it exports;
- a snapshot, ``<shell> -c -l`` with an explicit ``source`` of
  ``$HOME/.bashrc`` (``.zshrc`` for zsh), taken once per run. It records
  functions, aliases and shell options, which Claude replays ahead of every
  command. No setting turns it off and it does not consult ``ZDOTDIR``.

The snapshot is Claude Code's own and is left alone: shell configuration is
wanted, the exported environment is not. A worker launch therefore pins

- ``SHLVL`` = ``1``: the shells Claude starts are at level 2, so bash's
  remote-shell rule does not apply;
- ``BASH_ENV`` and ``ENV`` empty: no file is named for a non-login shell to
  load;

and keeps the worker's own empty ``ZDOTDIR``, which is what stops zsh reading
``.zshenv`` for those shells.

Claude applies the ``env`` of its settings layers after the launch
environment, so a project ``.claude/settings.json`` could replace any of
these. The pins and the ``ZDOTDIR`` therefore ride twice: in the launch
environment, and as ``env`` in the ``--settings`` value the adapter renders
for every run, which outranks user, project and local settings files. Managed
settings outrank it and are outside this.

This is launch hygiene for the shells the runtime creates, not a read
boundary. A shell the worker starts itself as ``bash -l`` or ``bash -i``
still reads the files in ``HOME``. Certification rechecks the behavior
against the pinned build (``tests/cells/test_cell_claude.py``).
"""

from __future__ import annotations

import json

# What every shell Claude starts for a worker is given.
PINNED_ENV = {"BASH_ENV": "", "ENV": "", "SHLVL": "1"}
SETTINGS_OPTION = "--settings"


def settings_option_problem(args: list[str]) -> str | None:
    """Why ``args`` do not carry exactly one adapter-owned settings value.

    The adapter renders the value after the one ``--settings`` token. A second
    token or the joined ``--settings=`` form would hand Claude a settings
    value the adapter did not render."""
    joined = [arg for arg in args if arg.startswith(SETTINGS_OPTION + "=")]
    if joined:
        return f"the joined form {SETTINGS_OPTION}=..."
    count = args.count(SETTINGS_OPTION)
    if count != 1:
        return f"{count} {SETTINGS_OPTION} options, expected exactly one"
    if args.index(SETTINGS_OPTION) + 1 >= len(args):
        return f"no value after {SETTINGS_OPTION}"
    return None


def with_run_env(settings: str, zdotdir: str) -> str:
    """``settings`` (the rendered ``--settings`` JSON) with the pins and this
    run's own ``ZDOTDIR`` as its ``env``."""
    value = json.loads(settings)
    value["env"] = {**PINNED_ENV, "ZDOTDIR": zdotdir}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
