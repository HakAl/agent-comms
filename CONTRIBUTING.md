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

The suite, the gates and the lockfile are run on CPython 3.11 and 3.14 (CI
and `make preverify`). On a machine whose only interpreter is another
version, name one of those: `uv sync --python 3.14 --extra test`, and uv
downloads it. A Mac with only Python 3.15 needs this today, since the locked
`rpds-py` has no wheel for it and its source build fails; the quickstart's
wheel install resolves fresh and is not affected.

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

Three switches exist. `AGENT_COMMS_TEST_KEEP_SCRATCH=1` keeps the scratch
directories and prints their paths, which shows what a run would have written
under a real home. `AGENT_COMMS_TEST_CERT=1` is certification mode, set by
`make certify` (see "Runtime pins"): isolation stays on, the scratch home
sits under a directory short enough for a control socket, and the cert login
and cert custody copy reach the cells as `AGENT_COMMS_TEST_<RUNTIME>_CERT_HOME`
and `AGENT_COMMS_TEST_<RUNTIME>_VERSIONS_DIR` (`CODEX` or `CLAUDE`).
`AGENT_COMMS_TEST_LIVE_HOME=1` disables isolation. It is for cell runs that
must use the real home, such as the probe of the real protected supervisor
root. Set it from your own shell, never from an agent.
The guard refuses either of the last two unless every target on the command
line is under `tests/cells`, so the substrate suite never runs against a
real home. Without a login the cell tests skip, and a skip is reported as a
skip, never as a pass.

The full suite exercises an MCP client subprocess; it does not require a
Claude/Codex account. Do not delete or reset live data to make tests pass.

The docs are under test too. `tests/substrate/test_docs_commands.py` reads
every fenced `sh` block in README.md, CONTRIBUTING.md, AGENTS.md and
`docs/*.md`, parses each `agent-comms`, `agent-comms-mcp`,
`agent-comms-monitor` and `agent-comms-seat` command against the real
parser (and the command's preflight rules), and requires its command path
(`admin <verb>` included) to be exercised: run by a test (a `cli.run`,
`main` or `python -m agent_comms.<module>` call found in the test's AST,
with the argv it passes; a string that merely mentions the subcommand is not
evidence) or by `scripts/gates/install-smoke.sh`. `make <target>` must name
a Makefile target. When a doc change fails it, add the test or walkthrough
step that exercises the command, or correct the doc; the test has no
exception list.

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
| `make install-smoke` | Exports HEAD under `local/install-smoke/`, builds a wheel with `uv build`, deletes the export, installs the wheel with `uv tool install` into a tool directory and `HOME` whose paths contain a space, then from a directory outside the checkout: `agent-comms setup` with the fake runtime and no client seats, `agent-comms doctor` exiting 0 with no fixes, `agent-comms demo` ending closed/satisfied with a threaded reply and a clean `doctor` after it, a mailbox exchange through `agent-comms-mcp` over stdio, a dispatch to the fake worker, `agent-comms-monitor --once`, `agent-comms wait` for the reply, `dispatch-status` showing the closed row, and a final `agent-comms doctor` that must exit 3 with the exited once-only monitor as its only failing check. `agent-comms version` must report git fields `unknown`. The scratch root is removed unless `INSTALL_SMOKE_KEEP=1`. | uv, git |

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

## Runtime pins

`agent_comms/runtime_pins.json` records which runtime versions the gated
cells certified, and it ships inside the wheel. It is schema 2: for each
runtime a `boundary` (`exact` or `minor`, how strictly an installed version
must match) and `platforms`, a map from a platform key to the certified
`version`, the date it was certified (`certified_on`) and the `sha256` of
what runs under runtime custody: the Claude binary's file digest, or the
digest of the native Codex bundle (a canonical manifest of every file's
path, owner execute bit and sha256). A platform key is the OS
and CPU architecture as Python reports them, `<sys.platform>-<machine>`:
`darwin-arm64`, `darwin-x86_64`, `linux-x86_64`, `linux-aarch64`. On a
shell, `uname -m` prints the second half. `agent-comms version` prints the
key it resolved as `platform` and, under `platform_pins`, the certified
version per runtime for it, `null` where there is none.

An entry is a claim that the gated cells passed on that platform with that
version, so a platform is listed only with that evidence (see AGENTS.md).
Where the manifest has no entry for the current platform every consumer of
a pin fails closed with one message that names the platform, the platforms
that are certified, and this section: a Claude dispatch refuses before it
touches the disk, `tests/cells/test_cell_version_drift.py` and the Claude
cells skip with that message, and the skip is reported as a skip. A
dispatch reads only its own runtime's pin, so the others keep working.

Workers never run a runtime from PATH. A Codex dispatch runs
`<runtime root>/runtime-custody/codex/<version>/bin/codex`, a copy of the
native bundle the npm install runs, and refuses before anything starts when
the copy is missing or its digest differs from the pin; a Codex self-update
changes only the architect's interactive `codex`. `agent-comms custody
codex` fills the copy from the install on PATH (`--from DIR` names a bundle
directory), refusing a version or build that is not the pinned one, and
`--replace` swaps out a copy that no longer matches. Setup runs it when the
team has a Codex worker; doctor's `runtime:codex` names it when the copy is
missing.

To certify a runtime on a platform that is not listed yet, or to re-certify
it after an upgrade, run in a checkout on that machine:

```sh
make certify RUNTIME=codex
make certify RUNTIME=claude
```

An agent or a dispatch may run it; it needs no live-home switch. It needs a
dedicated cert login at `<runtime root>/<runtime>-cert-home`, where the
runtime root is `AGENT_COMMS_HOME` or else `~/.agent-comms`
(`CERTIFY_ARGS="--cert-home DIR"` names another, never `~/.codex`,
`~/.claude` or the rest of the runtime root). If you also run another
agent-comms install from `~/.agent-comms`, set `AGENT_COMMS_HOME` for this
checkout to a home of its own; certify then refuses any cert home under
`~/.agent-comms`. The login file may be a link you make to an existing
login; Codex writes refreshed tokens through such a link.

- Codex: a login made with
  `CODEX_HOME=<runtime root>/codex-cert-home codex login`, or a link
  `<runtime root>/codex-cert-home/auth.json` to an existing one.
- Claude: a token file. Run `claude setup-token`, then save the token it
  prints with
  `mkdir -p <runtime root>/claude-cert-home && (umask 077; pbpaste > <runtime root>/claude-cert-home/oauth-token)`.
  Before anything is pinned, certify runs the candidate binary's
  `auth status` under a scratch HOME and config dir, once without the token
  and once with it, and refuses unless only the token logs it in, so the
  cells cannot fall back on your own login. Each Claude cell then gets a
  fresh `CLAUDE_CONFIG_DIR` under its temp directory and the token as
  `CLAUDE_CODE_OAUTH_TOKEN`; nothing is written into the cert home or
  `~/.claude`. A worker's shell can read that variable, and the worker
  sandbox denies it the network; use the token only for certification.

It copies what workers would run into a cert custody directory under
`local/certifications/`: the native Codex bundle behind `codex` on PATH
(`CERTIFY_ARGS="--bundle DIR"` names one), or the Claude binary behind
`claude` on PATH (`CERTIFY_ARGS="--binary FILE"` names one). Because
dispatch checks the pin, it writes the candidate platform entry (version,
date, `sha256`) and refreshed `CONTRACT_SURFACE_DIGEST` before the cells
run, after saving both files to `local/certifications/pending-restore.json`.
It then runs the runtime's cells (`tests/cells/test_cell_<runtime>.py` and
`tests/cells/test_adapter_<runtime>.py`) in certification mode against that
copy, then the runtime's drift cell and the digest check. Unless every one
of those tests ran and passed, both files are restored byte for byte; a
skip is not a pass. The journal is deleted last; a run that was killed
leaves it, the next `make certify` restores from it first, and `make test`
fails while it exists. The journal is flushed to stable storage before the
pin changes, and the kept or restored files before it is removed
(`F_FULLFSYNC` on macOS, `fsync` elsewhere; a run that cannot flush is
refused), so this holds across a power loss too. One run at a time:
`local/certifications/certify.lock` is held throughout, and a second run is
refused rather than restoring the first run's live journal. It writes a
record under `local/certifications/` and prints the diff to commit. After
committing, fill the live custody copy: `agent-comms custody codex`, or for
Claude the `install` command it prints. When the installed version and
digest already are the pin it does nothing (`CERTIFY_ARGS=--force` re-runs
it). In certification mode the protected supervisor root probe targets the
scratch home's control root, with the system temp directories removed from
a Codex worker's writable roots; a live-home run of the cell probes the
real root.

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

## Release records

Each release's acceptance walkthrough on a clean machine is saved under
`docs/releases/`; for 0.2.0 it is
[docs/releases/0.2.0-macos-walkthrough.md](docs/releases/0.2.0-macos-walkthrough.md).
A record is a frozen snapshot of one run, scrubbed of personal paths and
account details. The docs checker does not parse it and a later doc change
does not update it; the next release gets its own.

## Useful failure reports

Include the package revision, OS/architecture, Python and relevant runtime
versions, expected behavior, actual behavior, minimal reproduction, and
sanitized error output. Use a disposable project where possible. Do not attach
auth files, tokens, your full mailbox/database, private prompts, or entire
runtime homes. For a potentially sensitive defect, share only a sanitized
description initially. Diagnostic bundles and an agent-driven reproduction
path are planned; they are not commands available in this release.
