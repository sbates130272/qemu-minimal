#!/bin/sh
# Fetch one ref of a repository at depth 1 into DIR, and refuse it unless it
# is the commit the caller pinned.
#
#   rocm-xio-fetch.sh REPO REF COMMIT DIR
#
# REF is what is asked of the server: a branch, a tag, or a full commit id.
# COMMIT is what that has to turn out to be. A branch is a name for whatever
# its owner pushed last, so a bake that fetched one by name alone would
# build a different tree the day after a push and say nothing. With COMMIT
# given, a ref that has moved is a failure that names both commits, before
# anything is compiled. With COMMIT empty the check is off and the commit
# that was fetched is reported, which is the only way to turn it off.
#
# DIR is replaced. It has to be absolute, and when it exists it has to be
# empty or a git checkout: this script will not delete anything else.
#
# Prints one line. "ok: <commit> ..." and exit 0 with the tree checked out
# at that commit; "FAIL: ..." otherwise, with exit 1 for an answer that will
# not change on a retry (a moved ref, a bad argument) and exit 2 for a fetch
# that did not complete, which may.
set -u

fail() {
    printf 'FAIL: %s\n' "$2"
    exit "$1"
}

[ $# -eq 4 ] || fail 1 "usage: $0 REPO REF COMMIT DIR"
repo=$1 ref=$2 want=$3 dir=$4

[ -n "$repo" ] || fail 1 "no repository was given"
[ -n "$ref" ] || fail 1 "no ref was given"
case "$ref" in
    -*) fail 1 "'$ref' is not a ref" ;;
esac
case "$dir" in
    /) fail 1 "refusing to use / as the source directory" ;;
    /*) ;;
    *) fail 1 "'$dir' is not an absolute path" ;;
esac
if [ -n "$want" ]; then
    printf '%s\n' "$want" | grep -Eq '^[0-9a-f]{40}$' ||
        fail 1 "'$want' is not a 40-character commit id; leave the commit empty to build $ref unchecked"
fi

if [ -e "$dir" ] || [ -L "$dir" ]; then
    if [ -L "$dir" ] || [ ! -d "$dir" ]; then
        fail 1 "$dir exists and is not a directory"
    fi
    if [ ! -e "$dir/.git" ] && [ -n "$(ls -A "$dir")" ]; then
        fail 1 "$dir exists, is not empty and is not a git checkout; refusing to replace it"
    fi
    rm -rf "$dir" || fail 1 "could not remove the old $dir"
fi

# Never a prompt: a repository that wants credentials is a failure here.
export GIT_TERMINAL_PROMPT=0

git init -q "$dir" || fail 1 "could not create a repository in $dir"
git -C "$dir" remote add origin "$repo" || fail 1 "could not name $repo as the origin"
if ! git -C "$dir" fetch -q --depth=1 origin "$ref"; then
    rm -rf "$dir"
    fail 2 "could not fetch $ref from $repo"
fi
got=$(git -C "$dir" rev-parse --verify -q 'FETCH_HEAD^{commit}') ||
    { rm -rf "$dir"; fail 1 "$ref of $repo is not a commit"; }

if [ -n "$want" ] && [ "$got" != "$want" ]; then
    rm -rf "$dir"
    fail 1 "$ref of $repo is at $got, and the pin requires $want. The ref has moved, or the pin names a commit of another ref. Nothing was built."
fi

git -C "$dir" checkout -q --detach "$got" ||
    { rm -rf "$dir"; fail 1 "could not check out $got in $dir"; }

if [ -n "$want" ]; then
    printf 'ok: %s is %s of %s, as pinned\n' "$got" "$ref" "$repo"
else
    printf 'ok: %s is %s of %s today; no commit was required\n' "$got" "$ref" "$repo"
fi
