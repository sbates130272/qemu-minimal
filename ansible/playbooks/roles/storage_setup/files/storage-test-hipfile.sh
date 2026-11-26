#!/bin/sh
# Read a file into GPU memory through hipFile and check what arrived: the
# GEMM bench, which writes a payload in a directory, reads it back through
# hipFile into a GPU buffer, multiplies on the GPU and compares the product
# with the host's.
#
#   storage-test-hipfile.sh NAME BENCH DIRECTORY [VARIABLE=VALUE...]
#
# The read has to take hipFile's direct path, storage to GPU memory with no
# copy through host memory (AIS). hipFile otherwise falls back, silently,
# to reading into a host bounce buffer and copying, and a fallback verifies
# just as well. So the bench runs with HIPFILE_ALLOW_COMPAT_MODE=false,
# which makes hipFile fail a read it cannot do directly, and this script
# requires the bench to say it refused the fallback.
#
# The other variables go into the bench's environment: a filesystem hipFile
# does not list as supported, NFS for one, is refused the direct path
# unless HIPFILE_UNSUPPORTED_FILE_SYSTEMS=true.
set -eu

[ $# -ge 3 ] || {
    echo "usage: $0 NAME BENCH DIRECTORY [VARIABLE=VALUE...]" >&2; exit 2; }
name=$1 bench=$2 dir=$3
shift 3

fail() { echo "storage $name: FAIL: $*" >&2; exit 1; }
[ -x "$bench" ] || fail "$bench is not built"
[ -d "$dir" ] || fail "$dir is not a directory"
fstype=$(findmnt -n -o FSTYPE -T "$dir")

log=$(mktemp "${TMPDIR:-/tmp}/storage-hipfile.XXXXXX")
trap 'rm -f "$log"' EXIT

# A read that never returns is a failure of its own, not a hung run.
rc=0
env "$@" HIPFILE_ALLOW_COMPAT_MODE=false \
    timeout "${STORAGE_HIPFILE_TIMEOUT:-240}" "$bench" "$dir" 4 \
    > "$log" 2>&1 || rc=$?
if [ "$rc" -eq 124 ]; then
    tail -5 "$log" >&2
    fail "the read from $dir ($fstype) did not finish in time"
fi
if [ "$rc" -ne 0 ] || ! grep -q '^gemm-hipfile-bench: PASS$' "$log"; then
    tail -8 "$log" >&2
    fail "the bench exited $rc on $dir ($fstype) with hipFile's fallback" \
         "refused: the read did not go direct to GPU memory"
fi
# A bench that does not honour the variable would have allowed the fallback.
grep -q '^gemm-hipfile-bench: compat_allowed=no$' "$log" \
    || fail "the bench did not refuse hipFile's fallback, so a pass would" \
            "not say which path the read took"
rate=$(sed -n 's/^gemm-hipfile-bench: read_bytes=\([0-9]*\) .* read_gbs=\([0-9.e+-]*\)$/\1 \2/p' "$log")
[ -n "$rate" ] || fail "the bench passed and reported no read rate"
bytes=${rate%% *}
mbs=$(awk -v g="${rate##* }" 'BEGIN { printf "%.1f", g * 1000 }')
echo "storage $name: ok, $bytes bytes read from $dir ($fstype) into GPU memory through hipFile's direct path (fallback refused) and verified, $mbs MB/s"
