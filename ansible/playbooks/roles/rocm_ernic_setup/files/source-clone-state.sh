#!/bin/sh
# Make sure the controller's rocm-ernic clone can still be updated.
#
#   source-clone-state.sh DIR
#
# ernic_source fetches the pinned commit by name and, when that fails,
# every branch and tag, unforced; it used to run ansible.builtin.git, which
# fetched every tag on every run. Upstream moves tags, and git refuses a
# moved tag ("would clobber existing tag"), which fails the role an hour
# into a bake over a tag the bake never reads: it checks out a pinned
# commit.
#
# The fetch here names no filter. In a clone the role has made or converted
# to a blob-less one, git applies the filter recorded for origin.
#
# Nothing is deleted, here or in tasks/source_clone.yml: the directory is
# shared by every bake and configure run on the controller, and one of them
# may be reading it. A moved tag is repaired in place with a forced fetch,
# which rewrites tags and remote-tracking refs and leaves the work tree,
# local branches and uncommitted changes alone. A local tag of the same name
# as an upstream one is the only thing it overwrites.
#
# Prints one line, "STATE: detail":
#
#   absent    nothing at DIR, or nothing there that is a clone
#   healthy   the fetch succeeded
#   repaired  git refused a moved tag, and a forced fetch took it
#   offline   the fetch timed out, or failed and the remote does not answer
#   broken    the fetch failed some other way; left for the role to report
#
# Always exits 0 with exactly one of those; anything else is a failure.
set -u

dir=${1-}
say() {
    printf '%s: %s\n' "$1" "$2"
    exit 0
}

case "$dir" in
    /*) ;;
    *) say absent "'$dir' is not an absolute path" ;;
esac
[ -e "$dir" ] || say absent "$dir does not exist; the role clones it"
# Only ever a clone: a directory someone else put at this path is theirs.
[ -e "$dir/.git" ] || say absent "$dir is not a git clone; left for the role to report"

# Never a prompt: a bake has nobody to answer one.
export GIT_TERMINAL_PROMPT=0
fetch_timeout=${SOURCE_CLONE_FETCH_TIMEOUT:-300}
probe_timeout=${SOURCE_CLONE_PROBE_TIMEOUT:-60}

out=$(timeout "$fetch_timeout" git -C "$dir" fetch --tags origin 2>&1)
rc=$?
[ "$rc" -eq 0 ] && say healthy "$dir fetched from origin"
reason() {
    line=$(printf '%s\n' "$1" | grep -E 'rejected|fatal|error' | head -1)
    printf '%s' "${line:-$(printf '%s\n' "$1" | tail -1)}"
}
why=$(reason "$out")
# A remote too slow to finish is the network, whatever it managed to print.
[ "$rc" -eq 124 ] &&
    say offline "the fetch timed out after ${fetch_timeout}s, so $dir is left as it is"

case "$out" in
    *"would clobber existing tag"*)
        forced=$(timeout "$fetch_timeout" \
            git -C "$dir" fetch --tags --force origin 2>&1)
        rc=$?
        [ "$rc" -eq 0 ] && say repaired "upstream moved a tag: $why"
        [ "$rc" -eq 124 ] &&
            say offline "the forced fetch timed out after ${fetch_timeout}s: $why"
        say broken "upstream moved a tag and the forced fetch failed: $(reason "$forced")"
        ;;
esac

# Anything else is either the network or the clone. Ask the remote directly,
# from outside the clone, so that a corrupt clone cannot fail the question.
if ! url=$(git -C "$dir" config --get remote.origin.url 2>/dev/null) ||
        [ -z "$url" ]; then
    # A sound repository that was never a clone of anything is not ours.
    if git -C "$dir" rev-parse --git-dir > /dev/null 2>&1; then
        say absent "$dir is a repository with no origin; left for the role to report"
    fi
    say broken "the clone is unreadable: $why"
fi
case "$url" in
    /* | *://* | *@*:*) ;;
    # A relative path is relative to the clone.
    *) url="$dir/$url" ;;
esac
if (cd / && timeout "$probe_timeout" git ls-remote "$url" HEAD) \
        > /dev/null 2>&1; then
    say broken "origin answers, so the fault is in $dir, which is left as it is: $why"
fi

say offline "origin ($url) does not answer, so $dir is left as it is: $why"
