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
make hooks
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

Run `make gate` before any push. It runs, in order, the five checks that a
public push must pass; each can also run on its own. `make help` lists them.
The checks exist to keep personal data and secrets out of a public
repository, so they have to run before a push, not after: once a commit is
on GitHub, a finding is a leak report, not a block.

| Target | What it checks | Needs |
|---|---|---|
| `make hygiene` | Every tracked file, and every commit beyond `origin/main` (message, author and committer identities, added diff lines, files added or changed), for personal paths, email addresses, machine hostnames, key and token material, and the tracked layout (nothing under `data/`, `logs/`, `local/`, only `*.example*` under `config/`, no `.env` files, no binaries outside the allowlist). Commit identities must use an `example.*`, `noreply` or reserved-domain address. | git |
| `make lint` | ruff (pinned through `uvx`) against `scripts/gates/ruff-baseline.txt`; fails when a file gains findings of a rule or a new file and rule pair appears. Fewer findings pass; `make lint-baseline` records the lower count. Also fails when a GitHub Action in `.github/workflows/` is not pinned to a full commit SHA with a `# vN` version comment. | uv |
| `make test` | The isolated suite with the pinned test extra. `PYTHON=3.11` selects an interpreter. | uv |
| `make preverify` | A fresh clone of HEAD under `local/preverify/`, the suite on Python 3.11 and 3.14, then lint and hygiene inside the clone. Refuses a dirty tree unless `PREVERIFY_ARGS=--allow-dirty`. | uv, network for missing interpreters |
| `make install-smoke` | Exports HEAD under `local/install-smoke/`, builds a wheel with `uv build`, deletes the export, installs the wheel with `uv tool install` into a tool directory and `HOME` whose paths contain a space, then from a directory outside the checkout: `agent-comms bootstrap` with the example registry, a mailbox exchange through `agent-comms-mcp` over stdio, a dispatch to the fake worker, `agent-comms-monitor --once`, `agent-comms wait` for the reply, and `dispatch-status` showing the closed row. `agent-comms version` must report git fields `unknown`. The scratch root is removed unless `INSTALL_SMOKE_KEEP=1`. | uv, git |

Hygiene has two optional inputs. A maintainer may keep private word and
fragment lists in `local/gates/private-words.txt` and
`local/gates/private-fragments.txt` (gitignored; another directory through
`GATES_PRIVATE_DIR`). When they are absent the gate prints a skip and still
runs every public check, so a contributor never needs them. `GATES_BASE`
overrides the commit range base. A line that must contain something
token-shaped, such as a test value, carries the marker `hygiene:allow`
in a comment; the marker exempts that line from the pattern checks only,
never from the private lists.

### The pre-push hook

`make hooks` installs `scripts/hooks/pre-push` as `.git/hooks/pre-push`
(a symlink, so it follows the tracked script). On every `git push` the hook
runs hygiene on exactly the commits the push would publish (the remote
branch's current commit is the range base; a new branch falls back to
`origin/main`) and then lint, and refuses the push when either fails. The
test suite and preverify are not in the hook; they take minutes and `make
gate` remains the full check. The hook scans the checkout, so the pushed
branch must be the checked-out one, and pushing another branch is refused
with a message rather than scanned wrongly. Deletions and tags are not
scanned. `git push --no-verify` bypasses it; do not do that for a public
remote. A global `core.hooksPath` that dispatches to `.git/hooks` keeps
working, since the hook is installed there rather than by changing the
config.

The GitHub Actions workflow `.github/workflows/gate.yml` runs hygiene, lint,
the test matrix and install-smoke on every pull request and push to `main`. It is a mirror
of the local gates on a fresh macOS runner, not a gate in front of the push:
a failure there means something already public needs fixing. The test jobs
also prove the suite's isolation: `scripts/gates/leftovers.sh before` takes a
snapshot of `HOME` and the checkout, and `leftovers.sh after` fails the job
if the run added anything to either (bytecode caches excepted) or wrote under
`~/.agent-comms`, `~/.codex` or `~/.claude`. The same two commands work
around a local `make test`; on a machine with a live deployment the
protected-root part reports that deployment's own writes, so the CI run is
the authoritative one. Skipped tests appear in the unittest summary as
`OK (skipped=N)`.

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
