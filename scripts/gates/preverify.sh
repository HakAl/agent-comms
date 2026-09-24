#!/bin/sh
# Preverify gate: what CI will see, before anything is pushed.
#
# Clones HEAD into local/preverify/<sha>/ (a clone tests committed work, not
# the working tree), runs the isolated suite on each Python in
# PREVERIFY_PYTHONS (default: the minimum and the current version), then the
# lint and hygiene gates inside the clone. The private hygiene lists of this
# checkout are handed to the clone through GATES_PRIVATE_DIR, and the commit
# range base (GATES_BASE, else origin/main, else main) is resolved here and
# fetched into the clone, which otherwise has no base ref and would skip the
# commit scan. Logs land next to the clone. Run through `make preverify`.
set -eu
cd "$(dirname "$0")/../.."

PYTHONS="${PREVERIFY_PYTHONS:-3.11 3.14}"
allow_dirty=0
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) allow_dirty=1 ;;
    *) echo "usage: preverify.sh [--allow-dirty]" >&2; exit 2 ;;
  esac
done

if [ "$allow_dirty" -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
  echo "FAIL preverify: working tree is dirty; a clone tests HEAD only. Commit first or pass --allow-dirty." >&2
  exit 1
fi

base_sha=""
for candidate in ${GATES_BASE:-} origin/main main; do
  if base_sha=$(git rev-parse --verify -q "$candidate^{commit}"); then
    base_name=$candidate
    break
  fi
done
if [ -n "${GATES_BASE:-}" ] && [ "${base_name:-}" != "$GATES_BASE" ]; then
  echo "FAIL preverify: GATES_BASE=$GATES_BASE does not resolve to a commit" >&2
  exit 1
fi

sha=$(git rev-parse --short HEAD)
root="local/preverify"
clone="$root/$sha"
rm -rf "$root"
mkdir -p "$root"
git clone -q "$PWD" "$clone"
git -C "$clone" checkout -q "$(git rev-parse HEAD)"
private_dir="${GATES_PRIVATE_DIR:-$PWD/local/gates}"
echo "INFO preverify: clone of $sha at $clone"
if [ -n "$base_sha" ]; then
  git -C "$clone" fetch -q "$PWD" "$base_sha"
  echo "INFO preverify: commit range base $base_name ($base_sha)"
else
  echo "SKIP preverify: no commit range base (set GATES_BASE); the clone's hygiene skips the commit scan"
fi

status=0
step() {
  name=$1
  command=$2
  log="$root/$sha.$name.log"
  if (cd "$clone" && sh -c "$command") >"$log" 2>&1; then
    echo "PASS $name $(grep -E '^(Ran |OK|PASS)' "$log" | tail -2 | tr '\n' ' ')"
    grep -E '^SKIP' "$log" | sed 's/^/  /' || true

  else
    echo "FAIL $name (see $log)"
    grep -E '^(Ran |FAILED|FAIL|Error|error)' "$log" | tail -5 | sed 's/^/  /'
    status=1
  fi
}

for version in $PYTHONS; do
  step "test-py$version" "uv run --python $version --frozen --extra test python -m unittest discover -s tests -t ."
done
step "lint" "python3 scripts/gates/lint.py"
step "hygiene" "GATES_PRIVATE_DIR='$private_dir' GATES_BASE='$base_sha' python3 scripts/gates/hygiene.py"

if [ "$status" -eq 0 ]; then
  echo "PASS preverify ($sha on Python $PYTHONS)"
else
  echo "FAIL preverify ($sha)"
fi
exit "$status"
