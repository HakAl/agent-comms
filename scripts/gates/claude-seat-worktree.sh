#!/bin/sh
# Native Claude seat check on a linked worktree (ac-6k7 regression, with the
# real Claude Code CLI).
#
#   sh scripts/gates/claude-seat-worktree.sh
#
# The 0.2.0 walkthrough ran `agent-comms setup` with a linked git worktree as
# the project root and `doctor` failed the Claude seat: Claude Code keys its
# local MCP scope by the main checkout the worktree shares its git dir with.
# This check builds that topology under a scratch HOME and runtime root, runs
# setup with the fake runtime and the claude client, and requires that doctor
# is clean, a repeated bind-seat rewrites nothing, the seat launcher starts
# its command from the worktree without a warning, and Claude itself resolves
# the server from the worktree. It needs `claude` on PATH (CI installs the
# pinned version; locally whatever is installed, printed in the receipt) and
# the checkout's .venv (`uv sync --extra test`). No login: `claude mcp` only
# edits configuration. With CLAUDE_SEAT_REQUIRE_PIN=1 (CI) the `claude` on
# PATH must be the pinned version with the certified digest from
# agent_comms/runtime_pins.json, so the receipt is evidence for the pin, not
# for whatever happens to be installed. Nothing outside the scratch directory
# is written (a logged-in Mac still exposes its keychain to the binary; the
# unauthenticated CI runner is what shows no login is needed); the scratch
# directory is removed on success and kept on failure for inspection.
set -eu
cd "$(dirname "$0")/../.."
checkout=$PWD
command -v claude >/dev/null || { echo "FAIL claude-seat-worktree: claude is not on PATH" >&2; exit 2; }
[ -x "$checkout/.venv/bin/agent-comms" ] || { echo "FAIL claude-seat-worktree: no .venv/bin/agent-comms (run uv sync --extra test)" >&2; exit 2; }

scratch=${CLAUDE_SEAT_SCRATCH:-$checkout/local/claude-seat}
rm -rf "$scratch"
mkdir -p "$scratch/home" "$scratch/root" "$scratch/work"
scratch=$(cd "$scratch" && pwd)  # absolute: the steps below cd into the worktree
# The operator's own overrides must not reach the run (as install-smoke.sh):
# strip every AGENT_COMMS_* variable and the client config locations, then
# set only this run's HOME and runtime root.
for name in $(env | sed -n 's/^\(AGENT_COMMS_[A-Z0-9_]*\)=.*/\1/p') \
    CODEX_HOME CLAUDE_CONFIG_DIR XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME ZDOTDIR; do
  unset "$name"
done
export HOME="$scratch/home"
export AGENT_COMMS_HOME="$scratch/root"
echo "claude $(claude --version 2>/dev/null | head -1) at $(command -v claude); scratch $scratch"

fail() { echo "FAIL claude-seat-worktree: $1" >&2; echo "scratch kept at $scratch" >&2; exit 1; }

# The pin: version and certified digest of the binary behind `claude`.
if [ "${CLAUDE_SEAT_REQUIRE_PIN:-0}" = "1" ]; then
  "$checkout/.venv/bin/python" - "$(command -v claude)" "$(claude --version 2>/dev/null | head -1)" <<'PY' || fail "claude on PATH is not the pinned, certified binary"
import os, sys
from pathlib import Path
from agent_comms import runtime_pins
pin = runtime_pins.claude_pin()
binary = Path(os.path.realpath(sys.argv[1]))
reported = sys.argv[2]
digest = runtime_pins.custody_binary_sha256(binary)
problems = []
if reported.split()[:1] != [pin.version]:
    problems.append(f"claude --version says {reported!r}, pin is {pin.version}")
if digest != pin.sha256:
    problems.append(f"{binary} has sha256 {digest}, pin is {pin.sha256}")
if problems:
    print("; ".join(problems), file=sys.stderr)
    sys.exit(1)
print(f"pin: claude {pin.version} ({pin.platform}, certified {pin.certified_on}), digest matches at {binary}")
PY
fi

# setup refuses a runtime root whose control sockets would exceed the AF_UNIX
# path limit (103 bytes on macOS), so the root must be short; a deep checkout
# fails here with the reason. CLAUDE_SEAT_SCRATCH moves the scratch directory.
socket_path_length=$(printf '%s' "$AGENT_COMMS_HOME/run/0123456789abcdef0123456789abcdef/s" | wc -c | tr -d ' ')
[ "$socket_path_length" -le 103 ] \
  || fail "control socket path would be $socket_path_length bytes (limit 103); set CLAUDE_SEAT_SCRATCH to a shorter directory"

# The runbook topology: an integration checkout and a linked worktree on a branch.
git -c init.defaultBranch=main init -q "$scratch/work/integration"
git -C "$scratch/work/integration" -c user.name=t -c user.email=t@example.invalid commit -q --allow-empty -m init
git -C "$scratch/work/integration" worktree add -q "$scratch/work/change-1" -b change-1
project="$scratch/work/change-1"
cd "$project"

# 1. setup binds the Claude seat from the worktree and its own doctor is clean.
"$checkout/.venv/bin/agent-comms" setup --project-root "$project" --team team-a --runtimes fake --clients claude --human alice --yes >"$scratch/setup.json" \
  || fail "setup exited $? ($scratch/setup.json)"
python3 - "$scratch/setup.json" <<'PY' || fail "setup result"
import json, sys
result = json.load(open(sys.argv[1]))
assert result["ok"], result
assert result["mcp"]["pending"] == [], result["mcp"]
assert result["doctor"] == {"ok": True, "fixes": []}, result["doctor"]
PY

# 2. Where Claude filed the entry (informational: the fix reads either key).
python3 - "$HOME/.claude.json" "$scratch/work/integration" "$project" <<'PY'
import json, os, sys
keys = sorted(json.load(open(sys.argv[1])).get("projects", {}))
main, worktree = (os.path.realpath(p) for p in sys.argv[2:])
where = "the main checkout" if main in keys else "the worktree" if worktree in keys else "neither (!)"
print(f"claude keyed the local scope by {where}: {keys}")
PY

# 3. doctor from a fresh process: exit 0, the seat check ok.
"$checkout/.venv/bin/agent-comms" doctor >"$scratch/doctor.json" || fail "doctor exited $? ($scratch/doctor.json)"
python3 - "$scratch/doctor.json" <<'PY' || fail "doctor report"
import json, sys
report = json.load(open(sys.argv[1]))
assert report["ok"] and report["fixes"] == [], report
seat = [c for c in report["checks"] if c["id"] == "mcp:team-a-architect:claude"]
assert seat and seat[0]["status"] == "ok", seat
assert not any(c["id"].endswith(":shadowed") for c in report["checks"]), report["checks"]
print("doctor:", seat[0]["detail"])
PY

# 4. bind-seat again: already bound, nothing rewritten.
"$checkout/.venv/bin/agent-comms" bind-seat team-a-architect --client claude >"$scratch/bind.json" || fail "bind-seat exited $? ($scratch/bind.json)"
python3 - "$scratch/bind.json" <<'PY' || fail "bind-seat result"
import json, sys
result = json.load(open(sys.argv[1]))
assert result["ok"] and result["mcp"]["pending"] == [], result
(applied,) = result["mcp"]["applied"]
assert applied["commands"] == [] and applied["detail"] == "already bound to team-a-architect", applied
print("bind-seat:", applied["detail"])
PY

# 5. The seat launcher from the worktree: starts its command, no warning.
"$checkout/.venv/bin/agent-comms-seat" team-a-architect -- /bin/echo launched >"$scratch/seat.out" 2>"$scratch/seat.err" || fail "seat launcher exited $? ($scratch/seat.err)"
grep -qx launched "$scratch/seat.out" || fail "seat launcher did not start its command"
[ ! -s "$scratch/seat.err" ] || fail "seat launcher warned: $(cat "$scratch/seat.err")"
echo "seat launcher: launched, no warning"

# 6. Claude itself resolves the server from the worktree.
claude mcp get agent-comms >"$scratch/mcp-get.out" 2>&1 || fail "claude mcp get agent-comms failed in the worktree ($scratch/mcp-get.out)"
grep -q -- "--actor-id team-a-architect" "$scratch/mcp-get.out" || fail "claude mcp get does not show the architect's argv ($scratch/mcp-get.out)"
grep -q -- "--home $AGENT_COMMS_HOME" "$scratch/mcp-get.out" || fail "claude mcp get does not show this run's runtime root ($scratch/mcp-get.out)"
grep -q -- "$checkout/.venv/bin/agent-comms-mcp" "$scratch/mcp-get.out" || fail "claude mcp get does not show this checkout's server ($scratch/mcp-get.out)"
echo "claude mcp get: resolves this checkout's agent-comms-mcp for team-a-architect under this run's runtime root, from the worktree"

cd "$checkout"
rm -rf "$scratch"
echo "PASS claude-seat-worktree"
