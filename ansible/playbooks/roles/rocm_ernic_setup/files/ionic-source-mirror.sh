#!/bin/sh
# Send git fetches of the stable kernel tree to a mirror, and take that
# back out.
#
#   ionic-source-mirror.sh install MIRROR UPSTREAM
#   ionic-source-mirror.sh remove UPSTREAM
#
# rocm-ernic's fetch-ionic-sources.sh fetches from UPSTREAM unless it is
# given --repo, and the collection's ernic_guest_setup role runs it in the
# guest with no --repo and no variable for one. "install" writes
#
#   [url "MIRROR"]
#       insteadOf = UPSTREAM
#
# into git's system configuration, so that fetch goes to MIRROR while the
# script still believes, and records in its sentinel, that it asked
# UPSTREAM. System scope because the fetch runs as root under sudo, and
# /etc/gitconfig is read whoever runs git and whatever HOME is.
#
# "remove" takes out every rule that rewrites UPSTREAM, whichever mirror it
# names, so a rule left by a run that named another mirror goes too. If the
# system configuration file is then empty it is deleted: an image that never
# had one does not gain one. Nothing else in the file is touched.
#
# Prints one line, "STATE: detail", and exits 0:
#
#   installed  the rule was written
#   unchanged  the rule was already the only rewrite of UPSTREAM
#   removed    one or more rewrites of UPSTREAM were taken out
#   absent     there was no rewrite of UPSTREAM to take out
#
# Anything else is an error: one line on stderr and exit 1.
set -u

say() {
    printf '%s: %s\n' "$1" "$2"
    exit 0
}
die() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

# Keys of the system configuration whose values include UPSTREAM, one per
# line. Key names come back canonical: "url.<base>.insteadof".
rewrites_of() {
    git config --system --name-only --get-regexp '^url\..*\.insteadof$' \
        2>/dev/null | sort -u |
    while IFS= read -r key; do
        git config --system --get-all "$key" 2>/dev/null |
            grep -Fxq -- "$1" && printf '%s\n' "$key"
    done
}

# Take UPSTREAM ($1) out of every key but the one named in $2, and print how
# many keys lost it.
drop() {
    count=0
    for key in $(rewrites_of "$1"); do
        [ "$key" = "$2" ] && continue
        git config --system --fixed-value --unset-all "$key" "$1" ||
            die "could not remove $key from git's system configuration"
        count=$((count + 1))
    done
    printf '%s\n' "$count"
}

system_file() {
    file=$(git var GIT_CONFIG_SYSTEM 2>/dev/null) || file=
    printf '%s\n' "${file:-${GIT_CONFIG_SYSTEM:-/etc/gitconfig}}"
}

# No git means no rule: git is what wrote it. "install" still needs one.
if ! command -v git > /dev/null; then
    [ "${1-}" = remove ] && [ $# -eq 2 ] &&
        say absent "git is not installed, so nothing rewrites $2"
    die "git is not installed"
fi

case "${1-}" in
    install)
        [ $# -eq 3 ] || die "usage: $0 install MIRROR UPSTREAM"
        mirror=$2 upstream=$3
        case "$mirror$upstream" in
            *[[:space:]]*) die "a URL with whitespace in it: '$mirror' '$upstream'" ;;
        esac
        [ -n "$mirror" ] && [ -n "$upstream" ] ||
            die "install needs a mirror and the URL it stands in for"
        key="url.$mirror.insteadof"
        dropped=$(drop "$upstream" "$key") || exit 1
        if git config --system --get-all "$key" 2>/dev/null |
                grep -Fxq -- "$upstream"; then
            [ "$dropped" -eq 0 ] &&
                say unchanged "git already fetches $upstream from $mirror"
        else
            git config --system --add "url.$mirror.insteadOf" "$upstream" ||
                die "could not write the rewrite to $(system_file)"
        fi
        say installed "git now fetches $upstream from $mirror"
        ;;
    remove)
        [ $# -eq 2 ] || die "usage: $0 remove UPSTREAM"
        upstream=$2
        [ -n "$upstream" ] || die "remove needs the URL that was rewritten"
        dropped=$(drop "$upstream" "") || exit 1
        [ "$dropped" -eq 0 ] && say absent "nothing rewrites $upstream"
        file=$(system_file)
        if [ -f "$file" ] && ! grep -q '[^[:space:]]' "$file"; then
            rm -f -- "$file" || die "could not delete the empty $file"
            say removed "$dropped rewrite(s) of $upstream, and the empty $file"
        fi
        say removed "$dropped rewrite(s) of $upstream"
        ;;
    *)
        die "usage: $0 install MIRROR UPSTREAM | remove UPSTREAM"
        ;;
esac
