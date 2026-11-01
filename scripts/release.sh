#!/bin/sh
# Release: build the artifacts of a tagged version, publish them as the GitHub
# release of that tag, and verify what GitHub serves.
#
#   sh scripts/release.sh build <version>            export the tag, build wheel + sdist, print digests
#   sh scripts/release.sh publish <version> [--draft] gh release create v<version> with the artifacts and notes
#   sh scripts/release.sh verify <version>           download the published wheel and compare its digest
#
# Everything lands under local/release/<version>/ (gitignored): src/ is the
# `git archive` export of the tag (tracked files only, so the build never sees
# the working tree), wheels/ holds the wheel and sdist built from it with
# `uv build`, notes.md is the release text (written by hand before publish).
#
# Preconditions, checked here: the tag v<version> exists locally and on
# origin and names the commit whose pyproject version is <version> (the
# working tree plays no part: the build exports the tag); build.txt records
# the commit and both artifact digests of the last successful tag build and
# publish and verify refuse artifacts that do not match it; notes.md exists
# before publish and states the wheel's digest. Not checked here, the release
# skill's job: the gate passed on that commit and docs/releases/ has the
# version's record.
set -eu
cd "$(dirname "$0")/.."

usage() { echo "usage: sh scripts/release.sh build|publish|verify <version> [--draft]" >&2; exit 2; }
[ $# -ge 2 ] || usage
command=$1; version=$2; shift 2
tag="v$version"
root="local/release/$version"
wheel="$root/wheels/agent_comms-$version-py3-none-any.whl"
sdist="$root/wheels/agent_comms-$version.tar.gz"
notes="$root/notes.md"
record="$root/build.txt"

fail() { echo "FAIL release: $*" >&2; exit 1; }

# Print the sha256 of a file, or fail. Callers run this in a command
# substitution, where a failure inside is invisible to set -e, so every call
# is `x=$(digest f) || exit 1`.
digest() {
  sum=$(shasum -a 256 "$1") || { echo "FAIL release: shasum failed on $1" >&2; return 1; }
  sum=${sum%% *}
  if [ ${#sum} -ne 64 ]; then echo "FAIL release: no sha256 digest for $1" >&2; return 1; fi
  case "$sum" in *[!0-9a-f]*) echo "FAIL release: no sha256 digest for $1" >&2; return 1 ;; esac
  printf '%s\n' "$sum"
}

# The build record binds the artifacts to the tag build that produced them.
# Publish and verify refuse artifacts that were replaced since, or built from
# some other commit.
check_build() {
  [ -f "$wheel" ] || fail "no wheel at $wheel; run build first"
  [ -f "$sdist" ] || fail "no sdist at $sdist; run build first"
  [ -f "$record" ] || fail "no build record at $record; run build first"
  recorded_commit=$(sed -n 's/^commit=//p' "$record")
  recorded_wheel=$(sed -n 's/^wheel_sha256=//p' "$record")
  recorded_sdist=$(sed -n 's/^sdist_sha256=//p' "$record")
  [ "$recorded_commit" = "$commit" ] || fail "build record names commit $recorded_commit but $tag is $commit; run build again"
  wheel_sha=$(digest "$wheel") || exit 1
  sdist_sha=$(digest "$sdist") || exit 1
  [ "$wheel_sha" = "$recorded_wheel" ] || fail "wheel sha256 $wheel_sha differs from the build record's $recorded_wheel; run build again"
  [ "$sdist_sha" = "$recorded_sdist" ] || fail "sdist sha256 $sdist_sha differs from the build record's $recorded_sdist; run build again"
}

check_tag() {
  commit=$(git rev-parse --verify -q "$tag^{commit}") || fail "tag $tag does not exist locally"
  remote=$(git ls-remote --tags origin "refs/tags/$tag" | cut -f1)
  [ -n "$remote" ] || fail "tag $tag is not on origin; push it first"
  remote_commit=$(git rev-parse --verify -q "$remote^{commit}") || fail "remote tag object $remote is not fetched"
  [ "$remote_commit" = "$commit" ] || fail "tag $tag names $commit locally but $remote_commit on origin"
  tagged_version=$(git show "$tag:pyproject.toml" | sed -n 's/^version = "\(.*\)"/\1/p' | head -1)
  [ "$tagged_version" = "$version" ] || fail "pyproject at $tag says version $tagged_version, not $version"
  echo "INFO release: $tag -> $commit (pyproject $tagged_version)"
}

case "$command" in
  build)
    check_tag
    rm -rf "$root/src" "$root/wheels"
    mkdir -p "$root/src" "$root/wheels"
    git archive "$tag" | tar -x -C "$root/src"
    uv build --project "$root/src" --out-dir "$root/wheels" >/dev/null
    [ -f "$wheel" ] || fail "no wheel at $wheel"
    [ -f "$sdist" ] || fail "no sdist at $sdist"
    wheel_sha=$(digest "$wheel") || exit 1
    sdist_sha=$(digest "$sdist") || exit 1
    printf 'commit=%s\nwheel_sha256=%s\nsdist_sha256=%s\n' "$commit" "$wheel_sha" "$sdist_sha" > "$record"
    echo "INFO release: wheel $wheel sha256 $wheel_sha"
    echo "INFO release: sdist $sdist sha256 $sdist_sha"
    echo "INFO release: build record $record"
    [ -f "$notes" ] && echo "INFO release: notes present at $notes" || echo "INFO release: write $notes before publish"
    echo "PASS release build ($tag)"
    ;;
  publish)
    draft=""
    for arg in "$@"; do
      case "$arg" in --draft) draft="--draft" ;; *) usage ;; esac
    done
    check_tag
    check_build
    [ -s "$notes" ] || fail "no release notes at $notes"
    grep -q "$wheel_sha" "$notes" || fail "notes do not state the wheel's sha256 $wheel_sha"
    if gh release view "$tag" >/dev/null 2>&1; then fail "release $tag already exists; edit it with gh release edit/upload"; fi
    # The release names the tag already on origin (--verify-tag), so it can
    # never create a tag of its own at some other commit.
    gh release create "$tag" "$wheel" "$sdist" --verify-tag --title "agent-comms $version" --notes-file "$notes" $draft
    echo "PASS release publish ($tag${draft:+, draft})"
    ;;
  verify)
    check_tag
    check_build
    gh release view "$tag" --json tagName,isDraft,assets --jq '"INFO release: \(.tagName) draft=\(.isDraft) assets=\([.assets[].name] | join(","))"'
    scratch="$root/verify"
    rm -rf "$scratch"; mkdir -p "$scratch"
    gh release download "$tag" --pattern "$(basename "$wheel")" --dir "$scratch"
    served=$(digest "$scratch/$(basename "$wheel")") || exit 1
    rm -rf "$scratch"
    [ "$served" = "$wheel_sha" ] || fail "served wheel sha256 $served differs from local $wheel_sha"
    echo "PASS release verify ($tag, wheel sha256 $served)"
    ;;
  *) usage ;;
esac
