# Working on agent-comms

This checkout holds the mailbox, bounded dispatch with supervised workers,
and the review and landing cycle. The remaining milestone work, in the order
set by [docs/ROADMAP.md](docs/ROADMAP.md), makes it installable and operable
without a checkout. Roadmap items are not implemented just because they are
listed. [README.md](README.md) describes what works today;
[CONTRIBUTING.md](CONTRIBUTING.md) covers setup and tests.

Use the smallest process that gets the requested outcome implemented, verified,
and reviewed. Keep moving within the user's authorized scope; ask only when a
missing answer blocks progress or a consequential choice needs the user.

## Choose a path

- **Defect / broken expected behavior:** reproduce → root cause → TDD →
  review the work.
- **Feature / open-ended problem:** scout → plan → review the plan →
  implement and verify → review the work.

Route by whether the expected behavior is established, not whether the cause is
already known. If investigation reveals a product or design decision, switch to
the planning path and reuse what you learned.

## Do the work

- Start with the requested outcome and an observable completion check.
- **Root cause:** reproduce the failure and trace the responsible behavior
  before changing it.
- **TDD:** add a regression test, confirm it fails for the expected reason,
  make the smallest correct fix, then refactor if useful. If automated testing
  is impractical, record why and use a concrete reproduction instead.
- **Scout:** inspect relevant code, conventions, and constraints. Stop when
  there is enough evidence to choose an approach.
- **Plan:** a few bullets covering outcome, approach, affected areas, and
  verification, in a concrete file the reviewer can read. Resolve substantive
  findings before implementing.
- **Implement and verify:** follow the plan, adjusting to evidence. Re-review
  the plan only if its scope or approach materially changes. Use behavior tests
  for code and direct checks for docs or configuration.
- **Review the work:** review the complete change, including new files. Assess
  findings against evidence, fix valid issues, recheck affected behavior, and
  review substantive fixes again. Explain rejected findings briefly.

Use `agy-review` for plan and work reviews if you have it; otherwise use another
independent reviewer, ideally a different model family, or a human. Reviewers
inspect and report only. If review is unavailable, keep working and report the
missing review as a completion blocker; never claim it passed.

## Keep overhead low

- No mandatory tickets, ceremony, or task database. Keep context in the
  conversation.
- Finish when the outcome is met, relevant checks pass, and review findings are
  resolved or accounted for. For anything that will be pushed, relevant checks
  means `make gate`. Report the result, verification, and remaining limits
  concisely.

## Checkpoints

Long work may reset context. Before a reset, write a short checkpoint in
`local/` (gitignored) that a fresh session can resume from:

- the goal and the plan or brief it follows (path)
- the slice in progress, what is done, and how it was verified
- review state: what was reviewed, open findings
- the next concrete action

Record facts and paths, not reasoning. Recheck them after resuming.

## Repository rules

- Tests use stdlib `unittest` and temporary directories only: never the real
  home directory, a real mailbox, or the source tree. Tests that need real
  runtime logins are skipped and reported as skipped, never counted as passing.
- Keep dependencies minimal; justify any new one.
- Nothing may depend on a maintainer's private checkout, paths, actors,
  credentials, or runtime state. Keep secrets and personal configuration out
  of Git.
- The current MCP server trusts caller-supplied identity. Do not claim identity
  isolation or expose it beyond one workstation.
- Platform and runtime support is claimed only with test evidence on that
  platform and version.
- Do not push or publish without explicit instruction.
