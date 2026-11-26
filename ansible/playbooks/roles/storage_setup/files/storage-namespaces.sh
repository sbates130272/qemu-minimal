#!/bin/sh
# The block namespaces of one NVMe subsystem, as `NSID DEVICE` lines in NSID
# order, once there are at least WANT of them.
#
#   storage-namespaces.sh NQN WANT [SECONDS]
#
# By the block device and not by the controller: a multipath-capable target,
# which the kernel's is, hangs its namespaces off the subsystem, and the
# controller then holds only path devices that have no node in /dev. By
# NSID and not by instance number: the kernel numbers namespaces in the
# order they finish scanning.
set -eu

[ $# -ge 2 ] || { echo "usage: $0 NQN WANT [SECONDS]" >&2; exit 2; }
nqn=$1 want=$2 wait=${3:-15}

sys=${STORAGE_SYSFS:-/sys}
dev=${STORAGE_DEV:-/dev}
tries=0
while :; do
    found=$(for b in "$sys"/block/nvme*; do
        [ -r "$b/nsid" ] && [ -r "$b/device/subsysnqn" ] || continue
        [ "$(cat "$b/device/subsysnqn")" = "$nqn" ] || continue
        name=$(basename "$b")
        [ -e "$dev/$name" ] || continue
        echo "$(cat "$b/nsid") /dev/$name"
    done | sort -n)
    count=$(printf '%s\n' "$found" | grep -c . || true)
    if [ "$count" -ge "$want" ]; then
        printf '%s\n' "$found"
        exit 0
    fi
    tries=$((tries + 1))
    [ "$tries" -lt "$wait" ] || break
    sleep 1
done
echo "$count block namespace(s) of $nqn after ${wait} s; $want were wanted" >&2
exit 1
