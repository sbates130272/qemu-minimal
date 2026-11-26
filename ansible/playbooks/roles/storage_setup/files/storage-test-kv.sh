#!/bin/sh
# Store a value under a key in an NVMe Key Value namespace, retrieve it and
# compare, then delete the key and check it is gone.
#
#   storage-test-kv.sh NAME GENERIC_DEVICE NSID
#
# GENERIC_DEVICE is the namespace's character device, /dev/ng<N>n<M>: a KV
# namespace has no block device. The commands are the NVMe KV command set's,
# sent with `nvme io-passthru`: Store 0x01, Retrieve 0x02, Delete 0x10,
# Exist 0x14. The 16-byte key is in command words 2, 3, 14 and 15.
set -eu

[ $# -eq 3 ] || { echo "usage: $0 NAME GENERIC_DEVICE NSID" >&2; exit 2; }
name=$1 dev=$2 nsid=$3

[ -c "$dev" ] || { echo "storage $name: FAIL: $dev is not a character device" >&2; exit 1; }

work=$(mktemp -d "${TMPDIR:-/tmp}/storage-kv.XXXXXX")
trap 'rm -rf "$work"' EXIT
len=65536

kv() {
    op=$1
    shift
    nvme io-passthru "$dev" --namespace-id="$nsid" --opcode="$op" \
        --cdw2=0x04030201 --cdw3=0x08070605 \
        --cdw14=0x0c0b0a09 --cdw15=0x100f0e0d --cdw11=16 "$@"
}
fail() { echo "storage $name: FAIL: $*" >&2; exit 1; }

# A key an earlier run left is removed first, so Exist below means this run.
kv 0x10 >/dev/null 2>&1 || true
if kv 0x14 >/dev/null 2>&1; then
    fail "Exist succeeded for a key that is not stored"
fi

dd if=/dev/urandom of="$work/value" bs=1K count=64 status=none
kv 0x01 --cdw10=$len --data-len=$len --write --input-file="$work/value" \
    >/dev/null || fail "Store failed"
kv 0x02 --cdw10=$len --data-len=$len --read --raw-binary > "$work/back" \
    || fail "Retrieve failed"
cmp -s "$work/value" "$work/back" || fail "Retrieve returned another value than was stored"
kv 0x14 >/dev/null || fail "Exist failed for a key that was just stored"
kv 0x10 >/dev/null || fail "Delete failed"
if kv 0x14 >/dev/null 2>&1; then
    fail "Exist still reports the key after Delete"
fi

echo "storage $name: ok, $len bytes stored under a key in $dev nsid $nsid, retrieved identical and deleted"
