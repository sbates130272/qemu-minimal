#!/usr/bin/env bash
# Cut a qemu-tool release: stamp the version everywhere, commit, and tag.
# Usage: scripts/release.sh <version> [--dry-run]
#
# <version> is a bare X.Y.Z with no leading "v"; the tag gets the v.
#
# Three files carry the version and all three have to agree, because the
# release workflow refuses to publish a tag that disagrees with them --
# v1.3.0 shipped untagged precisely because nothing enforced that. The
# debian/changelog stanza is generated from the CHANGELOG.md [Unreleased]
# section rather than typed again, so the two cannot drift either.
#
# This never pushes. It prints the push command and stops, because pushing
# a signed tag is the point of no return.
set -euo pipefail

VERSION=${1:-}
DRY_RUN=${2:-}

die() { echo "release: $*" >&2; exit 1; }

[ -n "${VERSION}" ] || die "usage: scripts/release.sh <version> [--dry-run]"
case "${DRY_RUN}" in
  ""|--dry-run) ;;
  *) die "unknown argument '${DRY_RUN}' (expected --dry-run)" ;;
esac

# Debian and PEP 440 agree on X.Y.Z and diverge almost immediately after,
# so only X.Y.Z is allowed rather than trying to translate between them.
[[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version '${VERSION}' is not X.Y.Z"

ROOT=$(git rev-parse --show-toplevel) || die "not in a git checkout"
cd "${ROOT}"

PYPROJECT=qemu/pyproject.toml
DEB_CHANGELOG=qemu/debian/changelog
CHANGELOG=CHANGELOG.md
TAG="v${VERSION}"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "${BRANCH}" = "main" ] || die "on branch '${BRANCH}', releases are cut from main"

[ -z "$(git status --porcelain)" ] || die "working tree is dirty, commit or stash first"

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  die "tag ${TAG} already exists"
fi

# A tag that exists only on the remote still collides on push, and finding
# that out after signing is worse than a slow check. Offline is fine.
if git ls-remote --exit-code --tags origin "${TAG}" >/dev/null 2>&1; then
  die "tag ${TAG} already exists on origin"
fi

CURRENT=$(sed -n 's/^version = "\(.*\)"$/\1/p' "${PYPROJECT}" | head -1)
[ -n "${CURRENT}" ] || die "no version found in ${PYPROJECT}"
[ "${CURRENT}" != "${VERSION}" ] || die "${PYPROJECT} is already at ${VERSION}"
NEWEST=$(printf '%s\n%s\n' "${CURRENT}" "${VERSION}" | sort -V | tail -1)
[ "${NEWEST}" = "${VERSION}" ] || die "${VERSION} is older than the current ${CURRENT}"

grep -q '^## \[Unreleased\]$' "${CHANGELOG}" || die "no '## [Unreleased]' heading in ${CHANGELOG}"

# Everything between [Unreleased] and the next version heading.
unreleased_body() {
  awk '/^## \[Unreleased\]$/ { inside = 1; next }
       /^## \[/            { inside = 0 }
       inside' "${CHANGELOG}"
}

[ -n "$(unreleased_body | grep '^- ' || true)" ] ||
  die "${CHANGELOG} has no entries under [Unreleased], nothing to release"

# Turn the markdown bullets into a debian changelog stanza. Each bullet is
# rejoined into one paragraph and rewrapped, because a markdown bullet wraps
# wherever prose happened to end and those breaks mean nothing here. The
# Added/Changed/Fixed/Removed headings become dpkg's "[ Section ]" markers:
# flattened into one list, a removal reads exactly like an addition.
#
# A section marker is held back until a bullet actually follows it, so an
# empty section in CHANGELOG.md does not leave a dangling header.
#
# Blank lines do not end a bullet. A bullet here often runs to several
# paragraphs, and treating the blank line as a terminator dropped every
# paragraph after the first -- silently, since the result still parses.
# A nested markdown item becomes an entry of its own rather than more
# continuation text, because appending it kept its literal "- " marker in
# the middle of the parent sentence.
deb_entries() {
  unreleased_body | awk '
    function flush() { if (buf != "") { print "b" buf; buf = "" } }
    /^### /       { flush(); print "s" substr($0, 5); next }
    /^- /         { flush(); buf = substr($0, 3); next }
    /^[ \t]*$/    { next }
    /^ +[-*] /    { flush(); sub(/^ +[-*] /, ""); buf = $0; next }
    /^ +[^ ]/     { if (buf != "") { sub(/^ +/, ""); buf = buf " " $0 }; next }
                  { flush() }
    END           { flush() }
  ' | while IFS= read -r line; do
    case ${line} in
      s*) pending="  [ ${line#s} ]" ;;
      b*)
        if [ -n "${pending:-}" ]; then
          printf '%s\n' "${pending}"
          pending=
        fi
        printf '%s\n' "${line#b}" | fmt -w 72 | sed -e '1s/^/  * /' -e '2,$s/^/    /'
        ;;
    esac
  done
}

# Reuse whoever signed the last stanza unless the environment overrides it,
# so the trailer stays consistent without hardcoding a name here.
LAST_TRAILER=$(grep -m1 '^ -- ' "${DEB_CHANGELOG}")
DEB_NAME=${DEBFULLNAME:-$(printf '%s' "${LAST_TRAILER}" | sed -n 's/^ -- \(.*\) <.*/\1/p')}
DEB_EMAIL=${DEBEMAIL:-$(printf '%s' "${LAST_TRAILER}" | sed -n 's/^ -- .*<\(.*\)>.*/\1/p')}
[ -n "${DEB_NAME}" ] && [ -n "${DEB_EMAIL}" ] ||
  die "cannot determine the changelog trailer, set DEBFULLNAME and DEBEMAIL"

DEB_DATE=$(date -uR)
TODAY=$(date -u +%Y-%m-%d)

STANZA=$(
  printf 'python3-qemu-tool (%s) unstable; urgency=low\n\n' "${VERSION}"
  deb_entries
  printf '\n -- %s <%s>  %s' "${DEB_NAME}" "${DEB_EMAIL}" "${DEB_DATE}"
)

if [ "${DRY_RUN}" = "--dry-run" ]; then
  echo "release: would stamp ${CURRENT} -> ${VERSION} and tag ${TAG}"
  echo "release: would write this ${DEB_CHANGELOG} stanza:"
  echo
  printf '%s\n' "${STANZA}"
  echo "release: would retitle [Unreleased] as [${TAG}] - ${TODAY} in ${CHANGELOG}"
  exit 0
fi

# The dirty-tree check above means these three files are the only thing we
# can have touched, so restoring them is always safe and never loses work.
COMMITTED=no
restore() {
  if [ "${COMMITTED}" = yes ]; then
    return
  fi
  # HEAD, not the index: by the time a commit can fail these are staged, and
  # a bare "git checkout --" would restore them from the index it just wrote.
  git checkout HEAD -- "${PYPROJECT}" "${DEB_CHANGELOG}" "${CHANGELOG}" 2>/dev/null || true
  echo "release: failed, working tree restored" >&2
}
trap restore EXIT

sed -i "0,/^version = \"${CURRENT}\"$/s//version = \"${VERSION}\"/" "${PYPROJECT}"
[ "$(sed -n 's/^version = "\(.*\)"$/\1/p' "${PYPROJECT}" | head -1)" = "${VERSION}" ] ||
  die "failed to stamp ${PYPROJECT}"

# Two newlines, not one: command substitution ate every trailing newline the
# stanza was built with, so without both the trailer line runs straight into
# the previous release's header and dpkg silently loses that stanza.
printf '%s\n\n' "${STANZA}" | cat - "${DEB_CHANGELOG}" > "${DEB_CHANGELOG}.new"
mv "${DEB_CHANGELOG}.new" "${DEB_CHANGELOG}"

# dpkg-parsechangelog exits 0 on a malformed stanza and merely warns, so check
# stderr rather than the status. Only warns here too if dpkg-dev is absent,
# since the release workflow builds the deb regardless.
if command -v dpkg-parsechangelog >/dev/null 2>&1; then
  PARSE_WARNINGS=$(dpkg-parsechangelog -l "${DEB_CHANGELOG}" -S Version 2>&1 >/dev/null)
  [ -z "${PARSE_WARNINGS}" ] || die "generated ${DEB_CHANGELOG} is malformed: ${PARSE_WARNINGS}"
  PARSED=$(dpkg-parsechangelog -l "${DEB_CHANGELOG}" -S Version 2>/dev/null)
  [ "${PARSED}" = "${VERSION}" ] || die "${DEB_CHANGELOG} parses as ${PARSED}, expected ${VERSION}"
else
  echo "release: dpkg-parsechangelog not installed, skipping changelog validation" >&2
fi

# Leave an empty [Unreleased] behind for the next cycle.
awk -v tag="${TAG}" -v today="${TODAY}" '
  /^## \[Unreleased\]$/ { print; print ""; printf "## [%s] - %s\n", tag, today; next }
  { print }
' "${CHANGELOG}" > "${CHANGELOG}.new"
mv "${CHANGELOG}.new" "${CHANGELOG}"

git add "${PYPROJECT}" "${DEB_CHANGELOG}" "${CHANGELOG}"
git commit -s -m "chore(release): ${TAG}" -m "Stamp ${VERSION} in ${PYPROJECT}, ${DEB_CHANGELOG} and ${CHANGELOG}."
COMMITTED=yes

git tag -s "${TAG}" -m "qemu-minimal ${TAG}"

cat <<EOF

release: ${TAG} committed and signed locally. Nothing has been pushed.

Review it:

  git show ${TAG}

Then publish, which is what triggers the release workflow:

  git push origin main ${TAG}
EOF
