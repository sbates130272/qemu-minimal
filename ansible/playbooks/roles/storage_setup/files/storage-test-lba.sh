#!/bin/sh
# Write a block device and read it back: random data through O_DIRECT, at
# an offset, compared by digest. The device is scratch.
#
#   storage-test-lba.sh NAME DEVICE [MIB] [OFFSET_MIB]
#
# Prints `storage NAME: ok ...` or says what went wrong and exits 1.
set -eu

[ $# -ge 2 ] || { echo "usage: $0 NAME DEVICE [MIB] [OFFSET_MIB]" >&2; exit 2; }
name=$1 dev=$2 mib=${3:-16} offset=${4:-0}

[ -b "$dev" ] || { echo "storage $name: FAIL: $dev is not a block device" >&2; exit 1; }
if findmnt -n -S "$dev" >/dev/null 2>&1; then
    echo "storage $name: FAIL: $dev is mounted; the test writes it raw" >&2
    exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/storage-lba.XXXXXX")
trap 'rm -rf "$work"' EXIT

dd if=/dev/urandom of="$work/out" bs=1M count="$mib" status=none
start=$(date +%s.%N)
dd if="$work/out" of="$dev" bs=1M seek="$offset" oflag=direct status=none
dd if="$dev" of="$work/in" bs=1M skip="$offset" count="$mib" iflag=direct status=none
end=$(date +%s.%N)

wrote=$(sha256sum < "$work/out" | cut -d' ' -f1)
read=$(sha256sum < "$work/in" | cut -d' ' -f1)
if [ "$wrote" != "$read" ]; then
    echo "storage $name: FAIL: $dev returned other data than was written" \
         "($mib MiB at ${offset} MiB)" >&2
    exit 1
fi
rate=$(awk -v m="$mib" -v s="$start" -v e="$end" \
    'BEGIN { d = e - s; printf "%.1f", (d > 0) ? 2 * m / d : 0 }')
echo "storage $name: ok, $mib MiB written to $dev and read back identical, $rate MiB/s"
