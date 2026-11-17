#!/bin/sh
# Check which upstream commit a tree left by rocm-ernic's
# fetch-ionic-sources.sh is patched on top of, and where it was fetched from.
#
#   ionic-source-base.sh DIR EXPECTED_COMMIT REF MIRROR
#
# The bake fetches the ionic sources from a mirror of the stable kernel
# tree. A mirror is only as good as its agreement with the tree it mirrors,
# so the commit the pinned tag peels to is written down beside the pin, and
# this script fails a bake whose fetch produced anything else.
#
# HEAD is no use for that: the script fetches REF at depth 1, checks it out
# and then applies the rocm-ernic series with "git am", so HEAD is the last
# patch. What identifies the base depends on which of the script's two
# fetches ran:
#
#   depth 1   .git/shallow holds one line, the commit whose parents the
#             fetch cut off. A depth-1 fetch brings exactly one commit, so
#             that is the commit REF resolved to and every other commit in
#             the repository was made here. This is the normal case.
#   fallback  when the depth-1 fetch fails four times the script fetches
#             every branch and tag, and nothing is shallow. The base is then
#             found by walking back from HEAD over the commits the script
#             made: it makes them as "rocm-ernic build
#             <rocm-ernic@localhost>", an identity it pins on the git am
#             command line, so the first commit on the first-parent chain
#             with another committer is the one the patches were applied to.
#
# The tree's origin is checked too, as git resolves it here, url.*.insteadOf
# rewrites included: a role that stopped passing the mirror on would
# otherwise fetch from the script's default and pass this check.
#
# Prints one line. "ok: ..." and exit 0 when the base is EXPECTED_COMMIT and
# the origin is MIRROR; "FAIL: ..." and exit 1 otherwise, naming both
# commits and the repository.
set -u

fail() {
    printf 'FAIL: %s\n' "$1"
    exit 1
}

[ $# -eq 4 ] || fail "usage: $0 DIR EXPECTED_COMMIT REF MIRROR"
dir=$1 expected=$2 ref=$3 mirror=$4
# The committer fetch-ionic-sources.sh gives the commits it makes.
build_email='rocm-ernic@localhost'

case "$dir" in
    /*) ;;
    *) fail "'$dir' is not an absolute path" ;;
esac
printf '%s\n' "$expected" | grep -Eq '^[0-9a-f]{40}$' ||
    fail "'$expected' is not a 40-character commit id, so there is nothing to compare $ref against"
[ -n "$ref" ] || fail "no kernel ref was given"
[ -n "$mirror" ] || fail "no mirror was given"
[ -e "$dir/.git" ] ||
    fail "$dir is not a git checkout, so no ionic sources were fetched there to check against $ref ($expected)"

# Nothing here needs the network, and a partial clone must not go looking.
export GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1

raw=$(git -C "$dir" config --get remote.origin.url 2> /dev/null) || raw=
via=$(git -C "$dir" remote get-url origin 2> /dev/null) || via=
[ -n "$via" ] ||
    fail "$dir has no origin remote, so it is not a tree fetch-ionic-sources.sh fetched"
if [ "$raw" = "$via" ]; then
    origin=$via
else
    origin="$via (configured as $raw, which git rewrites)"
fi
[ "$via" = "$mirror" ] ||
    fail "the origin of the ionic sources in $dir is $origin, not the mirror $mirror: a fetch there is not redirected"

shallow=$(git -C "$dir" rev-parse --git-path shallow 2> /dev/null) || shallow=
case "$shallow" in
    /* | "") ;;
    *) shallow="$dir/$shallow" ;;
esac
if [ -n "$shallow" ] && [ "$(grep -c . "$shallow" 2> /dev/null)" = 1 ]; then
    base=$(cat "$shallow")
    git -C "$dir" merge-base --is-ancestor "$base" HEAD 2> /dev/null ||
        fail "HEAD in $dir does not descend from $base, the one commit fetched from $origin; expected $expected for $ref"
    patches=$(git -C "$dir" rev-list --count "$base..HEAD" 2> /dev/null) ||
        fail "cannot count the patches on $base in $dir"
else
    found=$(git -C "$dir" log --first-parent --format='%H %ce' HEAD 2> /dev/null |
        awk -v who="$build_email" '$2 == who { n++; next } { print $1, n + 0; exit }')
    [ -n "$found" ] ||
        fail "no upstream commit under the patches in $dir (origin $origin), so it cannot be compared with $ref ($expected)"
    base=${found% *}
    patches=${found#* }
fi

[ "$base" = "$expected" ] ||
    fail "$origin serves $ref as commit $base, and the commit pinned for $ref is $expected ($dir)"

printf 'ok: %s is %s (%s) from %s, plus %s rocm-ernic patch(es)\n' \
    "$dir" "$ref" "$base" "$origin" "$patches"
