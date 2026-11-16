#!/bin/bash
# Ask apt what purging the stale kernel packages would do, and refuse if it
# is anything but removing kernels.
#
#   kernel-purge-guard.sh RUNNING_VERSION PACKAGE...
#
# apt takes a package's reverse dependencies with it without a word. A
# driver package that depended on a kernel metapackage would leave with the
# kernel, and the guest would still boot and look fine. So before anything
# is removed: nothing may be installed, nothing named for the running kernel
# may go, and whatever goes must be one of the packages asked for or be
# named for a kernel version that is being purged.
#
# Removes nothing itself. Exit 0 when the purge is safe to run.
set -euo pipefail

running=$1
shift
[ "$#" -gt 0 ] || exit 0

plan=$(LC_ALL=C apt-get --simulate purge "$@")

# The versions being purged: linux-image-7.0.0-31-generic -> 7.0.0-31.
versions=()
for stale in "$@"; do
    if [[ "$stale" =~ [0-9]+\.[0-9]+\.[0-9]+-[0-9]+ ]]; then
        versions+=("${BASH_REMATCH[0]}")
    fi
done

bad=0
while read -r verb pkg _; do
    case "$verb" in
        Inst)
            echo "the purge would INSTALL $pkg" >&2
            bad=1
            ;;
        Remv | Purg)
            if [[ "$pkg" == *"$running"* ]]; then
                echo "the purge would remove $pkg, of the running kernel" >&2
                bad=1
                continue
            fi
            ok=0
            for stale in "$@"; do
                [ "$pkg" = "$stale" ] && ok=1
            done
            for version in ${versions[@]+"${versions[@]}"}; do
                [[ "$pkg" == linux-*"$version"* ]] && ok=1
            done
            if [ "$ok" = 0 ]; then
                echo "the purge would also remove $pkg" >&2
                bad=1
            fi
            ;;
    esac
done <<< "$plan"
exit "$bad"
