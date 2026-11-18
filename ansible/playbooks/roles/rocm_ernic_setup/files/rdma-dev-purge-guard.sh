#!/bin/bash
# Ask apt what purging the archive's rdma-core development packages would
# do, and refuse if it is anything but removing them.
#
#   rdma-dev-purge-guard.sh PACKAGE...
#
# apt takes a package's reverse dependencies with it without a word, and the
# packages beside these three are the ones a guest cannot lose: libibverbs1
# is what the archive's perftest, fio and libpcap are linked against, and
# rdma-core carries the udev rules and systemd units. So before anything is
# removed: nothing may be installed, and whatever goes must be one of the
# packages asked for.
#
# No companion is allowed. On a guest of the published image (2026-11-17)
# the purge of libibverbs-dev, librdmacm-dev and libibumad-dev removed those
# three and nothing else: the only installed package that depends on any of
# them is librdmacm-dev, on libibverbs-dev.
#
# Removes nothing itself. Exit 0 when the purge is safe to run.
set -euo pipefail

[ "$#" -gt 0 ] || exit 0

plan=$(LC_ALL=C apt-get --simulate purge "$@")

bad=0
while read -r verb pkg _; do
    case "$verb" in
        Inst)
            echo "the purge would INSTALL $pkg" >&2
            bad=1
            ;;
        Remv | Purg)
            # apt names a package of another architecture "name:arch".
            ok=0
            for asked in "$@"; do
                [ "${pkg%%:*}" = "${asked%%:*}" ] && ok=1
            done
            if [ "$ok" = 0 ]; then
                echo "the purge would also remove $pkg" >&2
                bad=1
            fi
            ;;
    esac
done <<< "$plan"
exit "$bad"
