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
# working tree plays no part: the build exports the tag); notes.md exists
# before publish and states the wheel's digest. Not checked here, the release skill's job: the gate
# passed on that commit and docs/releases/ has the version's record.
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

fail() { echo "FAIL release: $*" >&2; exit 1; }
digest() { shasum -a 256 "$1" | cut -d' ' -f1; }

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
    echo "INFO release: wheel $wheel sha256 $(digest "$wheel")"
    echo "INFO release: sdist $sdist sha256 $(digest "$sdist")"
    [ -f "$notes" ] && echo "INFO release: notes present at $notes" || echo "INFO release: write $notes before publish"
    echo "PASS release build ($tag)"
    ;;
  publish)
    draft=""
    for arg in "$@"; do
      case "$arg" in --draft) draft="--draft" ;; *) usage ;; esac
    done
    check_tag
    [ -f "$wheel" ] || fail "no wheel at $wheel; run build first"
    [ -f "$sdist" ] || fail "no sdist at $sdist; run build first"
    [ -s "$notes" ] || fail "no release notes at $notes"
    grep -q "$(digest "$wheel")" "$notes" || fail "notes do not state the wheel's sha256 $(digest "$wheel")"
    if gh release view "$tag" >/dev/null 2>&1; then fail "release $tag already exists; edit it with gh release edit/upload"; fi
    # The release names the tag already on origin (--verify-tag), so it can
    # never create a tag of its own at some other commit.
    gh release create "$tag" "$wheel" "$sdist" --verify-tag --title "agent-comms $version" --notes-file "$notes" $draft
    echo "PASS release publish ($tag${draft:+, draft})"
    ;;
  verify)
    check_tag
    [ -f "$wheel" ] || fail "no local wheel at $wheel to compare with"
    gh release view "$tag" --json tagName,isDraft,assets --jq '"INFO release: \(.tagName) draft=\(.isDraft) assets=\([.assets[].name] | join(","))"'
    scratch="$root/verify"
    rm -rf "$scratch"; mkdir -p "$scratch"
    gh release download "$tag" --pattern "$(basename "$wheel")" --dir "$scratch"
    served=$(digest "$scratch/$(basename "$wheel")")
    local_digest=$(digest "$wheel")
    rm -rf "$scratch"
    [ "$served" = "$local_digest" ] || fail "served wheel sha256 $served differs from local $local_digest"
    echo "PASS release verify ($tag, wheel sha256 $served)"
    ;;
  *) usage ;;
esac
