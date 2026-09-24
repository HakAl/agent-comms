#!/bin/sh
# Leftovers check: a test run must leave nothing in HOME or the checkout.
#
#   sh scripts/gates/leftovers.sh before   # snapshot HOME and the checkout
#   make test
#   sh scripts/gates/leftovers.sh after    # fail on anything new
#
# The checkout snapshot covers tracked changes, untracked paths and every
# ignored file, so a stray file under data/ or logs/ counts; only bytecode
# caches (__pycache__, *.pyc) are tolerated, since the interpreter writes
# them regardless of what a test does. The HOME snapshot is its top-level entries; the protected
# runtime roots (~/.agent-comms, ~/.codex, ~/.claude) must hold nothing newer
# than the before marker, so a real home with thousands of files under them
# is checked in one traversal. On a machine where a live deployment is
# writing under ~/.agent-comms, that check reports the deployment's own
# writes; the CI runner, with a fresh HOME, is the authoritative run.
# Snapshots live under local/leftovers/ (gitignored). Used by the CI test
# job; run locally the same way.
set -eu
cd "$(dirname "$0")/../.."
dir="$PWD/local/leftovers"
mkdir -p "$dir"
home_dir=${HOME:?HOME is unset}

snapshot() {
  # Tracked changes and untracked paths, then every ignored file one by one
  # (git status folds an ignored directory such as data/ into a single entry).
  {
    git status --porcelain -z | tr '\0' '\n'
    git ls-files -o -i --exclude-standard
  } | grep -v -E '(^|/)__pycache__/|\.pyc$|^local/leftovers/' | sort >"$dir/checkout.$1"
  ls -A "$home_dir" | sort >"$dir/home.$1"
}

# Anything under a protected root that is newer than the before marker.
protected_changes() {
  for name in .agent-comms .codex .claude; do
    [ -e "$home_dir/$name" ] || continue
    (cd "$home_dir" && find "$name" -newer "$dir/marker" 2>/dev/null) || true
  done
}

case "${1:-}" in
  before)
    snapshot before
    : >"$dir/marker"
    echo "INFO leftovers: snapshot of $home_dir and the checkout taken"
    ;;
  after)
    for f in checkout.before home.before marker; do
      [ -f "$dir/$f" ] || { echo "FAIL leftovers: no before snapshot; run 'leftovers.sh before' first" >&2; exit 2; }
    done
    snapshot after
    status=0
    changed=$(protected_changes || true)
    if [ -n "$changed" ]; then
      echo "FAIL leftovers (protected): written under a protected home root during the test run:" >&2
      printf '%s\n' "$changed" | head -50 | sed 's/^/  /' >&2
      status=1
    fi
    for f in checkout home; do
      # Only additions and changes count; something a test removed is a
      # different bug, and the checkout list shrinks when caches are cleaned.
      new=$(comm -13 "$dir/$f.before" "$dir/$f.after" || true)
      if [ -n "$new" ]; then
        echo "FAIL leftovers ($f): new or changed after the test run:" >&2
        printf '%s\n' "$new" | sed 's/^/  /' >&2
        status=1
      fi
    done
    if [ "$status" -eq 0 ]; then
      echo "PASS leftovers (nothing new in $home_dir or the checkout; protected roots untouched)"
    fi
    exit "$status"
    ;;
  *)
    echo "usage: leftovers.sh before|after" >&2
    exit 2
    ;;
esac
