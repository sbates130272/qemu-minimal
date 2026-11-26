#!/bin/sh
# Put an object to an S3 store over RDMA with hipObject, get it back and
# compare: minio-getput-rdma, with its own report of what it did checked.
#
#   storage-test-hipobject.sh NAME PROGRAM ENDPOINT RDMA_DEVICE BYTES gpu|nogpu
#
# The program exits 0 when it silently fell back to plain HTTP, so the exit
# status is not the answer: its `Data integrity check passed` line and its
# `hipobj-stats:` counters are. A pass is every PUT and GET over RDMA.
set -eu

[ $# -eq 6 ] || {
    echo "usage: $0 NAME PROGRAM ENDPOINT RDMA_DEVICE BYTES gpu|nogpu" >&2; exit 2; }
name=$1 program=$2 endpoint=$3 device=$4 bytes=$5 mode=$6

fail() { echo "storage $name: FAIL: $*" >&2; exit 1; }
[ -x "$program" ] || fail "$program is not installed"
case "$mode" in gpu|nogpu) ;; *) fail "the buffer is gpu or nogpu, got $mode" ;; esac

log=$(mktemp "${TMPDIR:-/tmp}/storage-hipobject.XXXXXX")
trap 'rm -f "$log"' EXIT

# Without a hint hipObject walks the GPU's topology for a NIC, which an
# emulated guest does not have.
rc=0
HIPOBJ_NIC_HINT="$device" "$program" "$endpoint" minioadmin minioadmin \
    "$bytes" "$mode" > "$log" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
    tail -20 "$log" >&2
    fail "$(basename "$program") exited $rc"
fi
grep -q 'Data integrity check passed' "$log" || {
    tail -20 "$log" >&2
    fail "the GET did not return what the PUT wrote"; }
stats=$(grep -m1 '^hipobj-stats:' "$log") || {
    tail -20 "$log" >&2
    fail "no hipobj-stats line: the client did not report its transport"; }
field() { echo "$stats" | tr ' ' '\n' | sed -n "s/^$1=//p"; }
rdma_put=$(field rdma_put) rdma_get=$(field rdma_get)
http_put=$(field http_put) http_get=$(field http_get)
for value in "$rdma_put" "$rdma_get" "$http_put" "$http_get"; do
    case "$value" in ''|*[!0-9]*) fail "malformed stats line: $stats" ;; esac
done
[ "$rdma_put" -ge 1 ] && [ "$rdma_get" -ge 1 ] \
    || fail "nothing went over RDMA: $stats"
[ "$http_put" -eq 0 ] && [ "$http_get" -eq 0 ] \
    || fail "a transfer fell back to plain HTTP: $stats"

echo "storage $name: ok, $bytes bytes put and got over RDMA on $device ($mode buffer), verified; $stats"
