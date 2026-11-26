#!/bin/sh
# Write a file in a directory and read it back: random data, the page cache
# dropped between, compared by digest. For a mounted filesystem, local or
# NFS.
#
#   storage-test-file.sh NAME DIRECTORY [MIB] [FSTYPE]
#
# With FSTYPE, the directory has to be on a filesystem of that type, so
# that a mount which did not happen is not a pass on the directory under
# it. Run as root: dropping the page cache needs it.
set -eu

[ $# -ge 2 ] || { echo "usage: $0 NAME DIRECTORY [MIB] [FSTYPE]" >&2; exit 2; }
name=$1 dir=$2 mib=${3:-32} want=${4:-}

fail() { echo "storage $name: FAIL: $*" >&2; exit 1; }
[ -d "$dir" ] || fail "$dir is not a directory"
fstype=$(findmnt -n -o FSTYPE -T "$dir")
if [ -n "$want" ] && [ "$fstype" != "$want" ]; then
    fail "$dir is on $fstype, not $want: the mount is not there"
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/storage-file.XXXXXX")
file="$dir/storage-test.$$"
trap 'rm -rf "$work" "$file"' EXIT

dd if=/dev/urandom of="$work/out" bs=1M count="$mib" status=none
start=$(date +%s.%N)
dd if="$work/out" of="$file" bs=1M conv=fsync status=none
sync
echo 3 > /proc/sys/vm/drop_caches
dd if="$file" of="$work/in" bs=1M status=none
end=$(date +%s.%N)

wrote=$(sha256sum < "$work/out" | cut -d' ' -f1)
read=$(sha256sum < "$work/in" | cut -d' ' -f1)
[ "$wrote" = "$read" ] || fail "$file read back differs from what was written ($mib MiB)"
rate=$(awk -v m="$mib" -v s="$start" -v e="$end" \
    'BEGIN { d = e - s; printf "%.1f", (d > 0) ? 2 * m / d : 0 }')
echo "storage $name: ok, $mib MiB written to $dir ($fstype) and read back identical, $rate MiB/s"
