#!/bin/sh
# The generic character device of a namespace of the NVMe controller on
# PCIe that has this serial number: /dev/ng<N>n<NSID>.
#
#   storage-kv-device.sh SERIAL NSID
#
# A Key Value namespace has no block device, so it is found by its
# controller. By serial and not by instance number: the kernel numbers
# controllers in the order they probe.
set -eu

[ $# -eq 2 ] || { echo "usage: $0 SERIAL NSID" >&2; exit 2; }
serial=$1 nsid=$2
sys=${STORAGE_SYSFS:-/sys}
dev=${STORAGE_DEV:-/dev}

for ctrl in "$sys"/class/nvme/nvme*; do
    [ -r "$ctrl/serial" ] || continue
    [ "$(cat "$ctrl/transport" 2>/dev/null)" = pcie ] || continue
    # The kernel pads a serial number with spaces.
    [ "$(tr -d ' ' < "$ctrl/serial")" = "$serial" ] || continue
    node="$dev/ng$(basename "$ctrl" | sed 's/^nvme//')n$nsid"
    if [ -e "$node" ]; then
        echo "/dev/$(basename "$node")"
        exit 0
    fi
    echo "the controller with serial $serial is $(basename "$ctrl") and has" \
         "no generic device for namespace $nsid" >&2
    exit 1
done
echo "no NVMe controller on PCIe has the serial number $serial" >&2
exit 1
