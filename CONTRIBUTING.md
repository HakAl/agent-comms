# Contributing

The goal is a system an agent can set up and operate for its user, with useful
failure reports and little maintainer intervention. [The README](README.md)
describes what the checkout does today, [the roadmap](docs/ROADMAP.md) what
remains before a release, and [AGENTS.md](AGENTS.md) how to work on it.

Contributions can be small: a reproducible failure, a Linux test result, a
better agent instruction, or one reviewed implementation slice. Nothing
outside this repository is a prerequisite.

## Development setup and tests

Use Python 3.11 or newer, Git, and `uv`. From a development checkout:

```sh
uv sync --extra test
make test
```

`make test` runs the whole isolated suite with the pinned test dependencies;
the direct form is `uv run --frozen --extra test python -m unittest discover
-s tests -t .`.

Importing the `tests` package moves the process into a scratch home before
any `agent_comms` module loads. It points `HOME` and
`AGENT_COMMS_SUPERVISOR_ROOT` at fresh temporary directories, drops every
ambient `AGENT_COMMS_*` override plus `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and
the XDG variables, and refuses to run if a home-derived path still resolves
into the real `~/.agent-comms`. The scratch directories are removed at exit.
This holds for every invocation: discovery with `tests` or `tests/substrate`
as the top-level directory, a single module such as
`python -m unittest tests.substrate.test_store`, or a run started by an
agent. Every test module starts with `import tests.isolation`, which imports
the `tests` package (and so runs the guard) first even when discovery would
not load that package itself; a test that enforces this line is in
`tests/substrate/test_isolation.py`, next to the guard's other tests. Running a test file directly as a script fails loudly instead
of running unisolated. The guard is `tests/isolation.py`.

Two switches exist. `AGENT_COMMS_TEST_KEEP_SCRATCH=1` keeps the scratch
directories and prints their paths, which shows what a run would have written
under a real home. `AGENT_COMMS_TEST_LIVE_HOME=1` disables isolation. It is
for operator certification runs of `tests/cells`, which need real runtime
logins and probe the real protected supervisor root; set it from your own
shell, never from an agent. The guard refuses it unless every target on the
command line is under `tests/cells`, so the substrate suite never runs
against a real home. Without it those tests skip, and a skip is reported as
a skip, never as a pass.

The full suite exercises an MCP client subprocess; it does not require a
Claude/Codex account. Do not delete or reset live data to make tests pass.

## Gates

Run `make gate` before any push. It runs, in order, the four checks that a
public push must pass; each can also run on its own. `make help` lists them.

| Target | What it checks | Needs |
|---|---|---|
| `make hygiene` | Every tracked file, and every commit beyond `origin/main` (message, author and committer identities, added diff lines, files added or changed), for personal paths, email addresses, machine hostnames, key and token material, and the tracked layout (nothing under `data/`, `logs/`, `local/`, only `*.example*` under `config/`, no `.env` files, no binaries outside the allowlist). Commit identities must use an `example.*`, `noreply` or reserved-domain address. | git |
| `make lint` | ruff (pinned through `uvx`) against `scripts/gates/ruff-baseline.txt`; fails when a file gains findings of a rule or a new file and rule pair appears. Fewer findings pass; `make lint-baseline` records the lower count. | uv |
| `make test` | The isolated suite with the pinned test extra. `PYTHON=3.11` selects an interpreter. | uv |
| `make preverify` | A fresh clone of HEAD under `local/preverify/`, the suite on Python 3.11 and 3.14, then lint and hygiene inside the clone. Refuses a dirty tree unless `PREVERIFY_ARGS=--allow-dirty`. | uv, network for missing interpreters |

Hygiene has two optional inputs. A maintainer may keep private word and
fragment lists in `local/gates/private-words.txt` and
`local/gates/private-fragments.txt` (gitignored; another directory through
`GATES_PRIVATE_DIR`). When they are absent the gate prints a skip and still
runs every public check, so a contributor never needs them. `GATES_BASE`
overrides the commit range base. A line that must contain something
token-shaped, such as a test value, carries the marker `hygiene:allow`
in a comment; the marker exempts that line from the pattern checks only,
never from the private lists.

The GitHub Actions workflow `.github/workflows/gate.yml` runs hygiene, lint
and the test matrix on every pull request and push to `main`.

## Scope and review

Start with the issue's observable outcome. For features, put a short plan in
the issue or a document and have it reviewed before implementation. For bugs,
show a failing regression test before the fix. Review the complete change,
including new files. A human or independent agent can review; `agy-review` is
an option for maintainers who have it installed, not a required private tool.

Include the problem, resulting behavior, related issue if any, checks performed, and any
unresolved limitations in the contribution description. Explain any rejected
review finding. Keep unrelated cleanups separate. Runtime-dependent tests
must state versions and required credentials and must never silently count a
skipped integration as a pass. No credentials are needed for mailbox-only work.

## Useful failure reports

Include the package revision, OS/architecture, Python and relevant runtime
versions, expected behavior, actual behavior, minimal reproduction, and
sanitized error output. Use a disposable project where possible. Do not attach
auth files, tokens, your full mailbox/database, private prompts, or entire
runtime homes. For a potentially sensitive defect, share only a sanitized
description initially. Diagnostic bundles and an agent-driven reproduction
path are planned; they are not commands available in this release.
