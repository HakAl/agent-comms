#!/bin/sh
# Install-smoke gate: the package works installed, with no source tree around.
#
# Exports HEAD into local/install-smoke/src/, builds a wheel there, copies the
# wheel and the example registry out and deletes the export. From then on no
# source tree exists apart from the maintainer's own checkout, which the
# script never enters. The wheel is installed with `uv tool install` into a
# tool directory, bin directory and HOME whose paths contain a space, and the
# ledger path has one too. From a working directory outside the checkout the
# installed commands then set up a team with the fake runtime (`agent-comms
# setup` writes the registry and registers it; no example file is copied),
# get a clean `agent-comms doctor` report, run `agent-comms demo` and require
# doctor to stay clean after it, run a mailbox exchange through
# agent-comms-mcp over stdio, dispatch to the fake worker, spawn it with one
# monitor pass, wait for its reply, reconcile with a second pass and check
# that dispatch-status shows the closed row. The final `agent-comms doctor`
# must then fail on exactly one check, `monitor`, whose recorded pid is the
# exited `--once` monitor, with that one command as its only fix: the failure
# surface on a real install. `agent-comms version` must report git fields as
# unknown, since there is no checkout.
#
# UV_PYTHON selects the interpreter (CI runs 3.11 and 3.14). The scratch root
# is removed at exit unless INSTALL_SMOKE_KEEP=1. Run through
# `make install-smoke`.
#
# INSTALL_SMOKE_SOURCE=head (default) tests the commit, so a dirty tree is
# refused as preverify does. INSTALL_SMOKE_SOURCE=worktree snapshots the
# working tree as it is, untracked files included and ignored files excluded,
# through a temporary index (`git write-tree`; nothing is committed or staged)
# so a change can be tried before it is committed.
set -eu
cd "$(dirname "$0")/../.."
checkout="$PWD"
root="$checkout/local/install-smoke"
client="$checkout/scripts/gates/install-smoke-mcp-client.py"
human="01M36YTJV9XBW95S6ZWV47C4RG"
architect="team-a-architect"
worker="team-a-fake-worker"

fail() {
  echo "FAIL install-smoke: $*" >&2
  exit 1
}
info() {
  echo "INFO install-smoke: $*"
}
cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "${INSTALL_SMOKE_KEEP:-0}" = "1" ]; then
    echo "INFO install-smoke: kept scratch root $root"
  else
    rm -rf "$root"
  fi
  exit "$status"
}
# A signal exits through its own status (130, 143) so the EXIT trap keeps
# it; trapping the signals on cleanup directly would report the last
# command's status, usually 0.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

source="${INSTALL_SMOKE_SOURCE:-head}"
case "$source" in
  head)
    if [ -n "$(git status --porcelain)" ]; then
      fail "working tree is dirty; the wheel is built from HEAD. Commit first or set INSTALL_SMOKE_SOURCE=worktree."
    fi
    snapshot=$(git rev-parse HEAD)
    ;;
  worktree)
    snapshot=""
    ;;
  *) fail "INSTALL_SMOKE_SOURCE must be head or worktree, not '$source'" ;;
esac

# uv's cache and managed interpreters stay where the operator has them; the
# scratch HOME below would otherwise hide them and force downloads.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$(uv cache dir)}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(uv python dir)}"

rm -rf "$root"
mkdir -p "$root"
sha=$(git rev-parse --short HEAD)
status_before=$(git status --porcelain)
if [ -z "$snapshot" ]; then
  # A tree object of the working tree, built in a scratch index so the real
  # index is untouched.
  export GIT_INDEX_FILE="$root/index"
  git read-tree HEAD && git add -A && snapshot=$(git write-tree) \
    || fail "could not snapshot the working tree"
  unset GIT_INDEX_FILE
fi

# 1. Build the wheel from an export of the snapshot, never in the checkout
#    (setuptools writes build/ next to the sources).
mkdir -p "$root/src"
git archive --format=tar "$snapshot" | tar -x -C "$root/src"
(cd "$root/src" && uv build -q --wheel --out-dir "$root/wheels") || fail "uv build failed"
set -- "$root"/wheels/agent_comms-*.whl
wheel=$1
[ "$#" -eq 1 ] && [ -f "$wheel" ] || fail "expected one wheel under $root/wheels, found: $*"
info "built $(basename "$wheel") from $sha ($source)"

# 2. Delete the source tree; the install must not depend on it.
rm -rf "$root/src"

# 3. Install into paths with spaces, with HOME and the ledger under them too.
#    The operator's own overrides must not reach the run: with, say,
#    AGENT_COMMS_DISPATCH_LOG_DIR exported the fake worker's log would land
#    outside the scratch root and survive cleanup. Strip what the test
#    isolation guard (tests/isolation.py) strips, then set only this run's.
for name in $(env | sed -n 's/^\(AGENT_COMMS_[A-Z0-9_]*\)=.*/\1/p') \
    CODEX_HOME CLAUDE_CONFIG_DIR XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME ZDOTDIR; do
  unset "$name"
done
space="$root/with space"
export HOME="$space/home"
export UV_TOOL_DIR="$space/tools"
export UV_TOOL_BIN_DIR="$space/bin"
export AGENT_COMMS_DB="$space/ledger dir/agent-comms.sqlite"
mkdir -p "$HOME/.agent-comms" "$space/ledger dir"
# The supervisor binds an AF_UNIX socket under its control root, and macOS
# caps that path at 103 bytes. A real home (~/.agent-comms/run/s) fits; this
# scratch HOME under the checkout does not, so the test-only override points
# the control root at the shortest path the scratch root offers. The limit is
# checked here so a deep checkout fails with the reason, not with a worker
# that never reaches READY.
export AGENT_COMMS_SUPERVISOR_ROOT="$root/s"
socket_path_length=$(printf '%s' "$AGENT_COMMS_SUPERVISOR_ROOT/0123456789abcdef0123456789abcdef/s" | wc -c | tr -d ' ')
[ "$socket_path_length" -le 103 ] \
  || fail "supervisor socket path would be $socket_path_length bytes (limit 103); run from a checkout at a shorter path"
uv tool install -q "$wheel" || fail "uv tool install failed"
for name in agent-comms agent-comms-mcp agent-comms-monitor agent-comms-seat; do
  [ -x "$UV_TOOL_BIN_DIR/$name" ] || fail "$name was not installed under $UV_TOOL_BIN_DIR"
done
bin="$UV_TOOL_BIN_DIR"
info "installed under $UV_TOOL_DIR"

# 4. Everything from here runs from outside the checkout.
work="$space/work dir"
export PROJECT_A_ROOT="$work/project"
mkdir -p "$PROJECT_A_ROOT"
cd "$work"

"$bin/agent-comms" version >version.json
python3 - "$UV_TOOL_DIR" <<'PY' || fail "version does not describe an install without a checkout"
import json, os, sys
info = json.load(open("version.json"))
tool_dir = os.path.realpath(sys.argv[1])
repo_root = os.path.realpath(info["repo_root"])
problems = []
if not repo_root.startswith(tool_dir + os.sep):
    problems.append(f"repo_root {info['repo_root']} is not under the tool directory {tool_dir}")
for key in ("git_commit", "git_branch", "git_describe", "git_head_state"):
    if info.get(key) != "unknown":
        problems.append(f"{key}={info.get(key)!r}, expected 'unknown'")
if not info.get("certified_runtimes"):
    problems.append("certified_runtimes is empty: the pins did not ship in the wheel")
platform = info.get("platform")
pins = info.get("platform_pins") or {}
if not platform:
    problems.append("platform is missing from version")
for runtime in ("codex", "claude"):
    if not pins.get(runtime):
        problems.append(f"platform_pins lacks {runtime} for {platform}: the wheel carries no certified pin for this machine")
if problems:
    sys.exit("FAIL install-smoke: " + "; ".join(problems))
PY
info "version: repo_root under the tool venv, git fields unknown, pins present for this platform"

"$bin/agent-comms" setup --project-root "$PROJECT_A_ROOT" --team team-a --runtimes fake --clients "" \
  --human alice --human-id "$human" --yes >setup.json || fail "setup failed: $(cat setup.json)"
[ -f "$HOME/.agent-comms/actors.json" ] || fail "setup did not write the registry under $HOME/.agent-comms"
"$bin/agent-comms" actors >/dev/null || fail "actors failed"
info "setup registered team-a: $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(", ".join(d["actors"]))' setup.json)"
# The clean report after setup: nothing failed, so nothing to fix (the
# monitor has not run yet, which is a warning, not a failure).
"$bin/agent-comms" doctor >doctor-1.json || fail "doctor after setup failed: $(cat doctor-1.json)"
python3 - <<'PY' || fail "doctor after setup is not clean"
import json, sys
report = json.load(open("doctor-1.json"))
if not report["ok"] or report["fixes"]:
    sys.exit("FAIL install-smoke: doctor after setup: " + json.dumps(report, sort_keys=True))
PY
info "doctor after setup: ok, no fixes"

# The demo a new user runs next: one dispatch to the fake worker, driven to
# closed/satisfied in-process with no monitor and no admin token. It writes
# no heartbeat, so doctor stays clean afterwards (the monitor check warns).
"$bin/agent-comms" demo >demo.json 2>demo.err || fail "demo failed: $(cat demo.json) $(cat demo.err)"
python3 - "$worker" <<'PY' || fail "demo did not show the dispatch, the reply and the final status"
import json, sys
result = json.load(open("demo.json"))
problems = []
if not result.get("ok") or not result.get("worker_reaped"):
    problems.append("ok or worker_reaped is false")
if (result.get("final") or {}).get("status") != "closed" or result["final"].get("result") != "satisfied":
    problems.append(f"final is {result.get('final')}")
reply = result.get("reply") or {}
if reply.get("from") != sys.argv[1] or reply.get("parent_message_id") != result["dispatch"]["message_id"]:
    problems.append(f"reply is {reply}")
if problems:
    sys.exit("FAIL install-smoke: demo: " + "; ".join(problems) + ": " + json.dumps(result, sort_keys=True))
PY
"$bin/agent-comms" doctor >doctor-demo.json || fail "doctor after the demo failed: $(cat doctor-demo.json)"
info "demo: $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["dispatch"]["dispatch_id"], d["final"]["status"] + "/" + d["final"]["result"], repr(d["reply"]["body"]))' demo.json); doctor still ok"

# 5. Mailbox exchange over stdio MCP: architect sends, worker reads and acks.
python3 "$client" --mcp "$bin/agent-comms-mcp" --sender "$architect" --recipient "$worker" >exchange.json \
  || fail "mailbox exchange through agent-comms-mcp failed"
info "mailbox exchange through agent-comms-mcp: $(cat exchange.json)"

# 6. Dispatch to the fake worker and drive it to a terminal state.
umask 077
python3 -c 'import secrets; print(secrets.token_hex(32))' >"$HOME/.agent-comms/admin-token"
umask 022
AGENT_COMMS_ADMIN_TOKEN="$(cat "$HOME/.agent-comms/admin-token")"
export AGENT_COMMS_ADMIN_TOKEN
"$bin/agent-comms" admin dispatch \
  --from-actor-id "$architect" --target-actor-id "$worker" \
  --idempotency-key install-smoke-1 --requested-policy worker_dispatch_readwrite_bounded \
  --override-reason "install smoke" --subject ping --body "Reply with PONG." >dispatch.json \
  || fail "admin dispatch failed"
message_id=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("message_id") or d["message"]["id"])' dispatch.json) \
  || fail "dispatch output has no message id: $(cat dispatch.json)"
dispatch_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dispatch_id"])' dispatch.json) \
  || fail "dispatch output has no dispatch id: $(cat dispatch.json)"
info "dispatched $dispatch_id (message $message_id)"

# --once returns after the worker is READY, not after it exits: wait for the
# reply, then a second pass reconciles the supervisor exit.
"$bin/agent-comms-monitor" --human-actor-id "$human" --once >monitor-1.log 2>&1 || fail "first monitor pass failed: $(cat monitor-1.log)"
"$bin/agent-comms" wait "$architect" --after-message-id "$message_id" --timeout 90 >wait.json || fail "wait failed"
python3 - "$worker" "$message_id" <<'PY' || fail "no threaded reply from the fake worker"
import json, sys
waited = json.load(open("wait.json"))
replies = [m for m in waited["messages"] if m.get("from") == sys.argv[1] and m.get("parent_message_id") == sys.argv[2]]
if waited["timed_out"] or not replies:
    sys.exit("FAIL install-smoke: wait returned " + json.dumps(waited, sort_keys=True))
PY
# The worker sends its reply and closes the dispatch in separate
# transactions, so the reply is not a closure barrier: poll the row.
row_state() {
  "$bin/agent-comms" dispatch-status --json >status.json || fail "dispatch-status failed"
  python3 - "$dispatch_id" <<'PY'
import json, sys
rows = json.load(open("status.json"))
row = next((r for r in rows if r["dispatch_id"] == sys.argv[1]), None)
print("missing" if row is None else f"{row['status']}/{row.get('result')}")
PY
}
deadline=$(( $(date +%s) + 60 ))
while :; do
  state=$(row_state)
  case "$state" in
    closed/satisfied) break ;;
    queued/*|in_flight/*) ;;
    *) fail "dispatch $dispatch_id ended as $state: $(cat status.json)" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || fail "dispatch $dispatch_id still $state after 60s: $(cat status.json)"
  sleep 1
done
# The supervisor's exit is reconciled by a further pass; the row must stay closed.
"$bin/agent-comms-monitor" --human-actor-id "$human" --once >monitor-2.log 2>&1 || fail "second monitor pass failed: $(cat monitor-2.log)"
state=$(row_state)
[ "$state" = closed/satisfied ] || fail "dispatch row changed after reconciliation: $state"
info "dispatch $dispatch_id closed with result satisfied"

# 7. Doctor on the used install: the heartbeat names the exited --once
#    monitor, so exactly the monitor check fails, with its one command as the
#    only fix; everything else is ok, a warning or a skip.
set +e
"$bin/agent-comms" doctor >doctor-2.json
doctor_rc=$?
set -e
[ "$doctor_rc" -eq 3 ] || fail "final doctor exited $doctor_rc, expected 3: $(cat doctor-2.json)"
python3 - <<'PY' || fail "final doctor did not report exactly the dead monitor"
import json, sys
report = json.load(open("doctor-2.json"))
failing = [c["id"] for c in report["checks"] if c["status"] == "fail"]
problems = []
if failing != ["monitor"]:
    problems.append(f"failing checks {failing}, expected ['monitor']")
if len(report["fixes"]) != 1 or not report["fixes"][0].startswith("agent-comms-monitor "):
    problems.append(f"fixes {report['fixes']}, expected the one monitor command")
if problems:
    sys.exit("FAIL install-smoke: " + "; ".join(problems) + ": " + json.dumps(report, sort_keys=True))
PY
info "final doctor: exit 3, monitor is the only failing check"

# 8. The read side still answers, and the ledger stayed under the scratch root.
"$bin/agent-comms" inbox "$architect" >inbox.txt || fail "inbox failed"
"$bin/agent-comms" version >/dev/null || fail "version failed after the run"
[ -f "$AGENT_COMMS_DB" ] || fail "ledger not found at $AGENT_COMMS_DB"
status_after=$(cd "$checkout" && git status --porcelain)
[ "$status_after" = "$status_before" ] || fail "the run changed the checkout:
$status_after"
echo "PASS install-smoke ($sha $source, $(basename "$wheel"), python $("$bin/agent-comms" version >/dev/null && "$UV_TOOL_DIR/agent-comms/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown))"
