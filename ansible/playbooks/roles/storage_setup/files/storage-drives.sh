#!/bin/sh
# The guest's own NVMe drives, one block device a line: namespace 1 of each
# controller on the PCIe transport that has a block device. A controller
# reached over a fabric is somebody else's drive, and a Key Value namespace
# has no block device; both are left out.
#
#   storage-drives.sh
#
# In the order of the controllers' serial numbers, which end in the drive's
# number, and not of their instance numbers: the kernel numbers controllers
# in the order they probe, so nvme8 is one drive on one boot and another on
# the next, and "the ninth drive" has to be the same file every time.
set -eu
sys=${STORAGE_SYSFS:-/sys}
dev=${STORAGE_DEV:-/dev}
for ctrl in "$sys"/class/nvme/nvme*; do
    [ "$(cat "$ctrl/transport" 2>/dev/null)" = pcie ] || continue
    for ns in "$ctrl"/nvme*n*; do
        [ -r "$ns/nsid" ] || continue
        [ "$(cat "$ns/nsid")" = 1 ] || continue
        name=$(basename "$ns")
        [ -e "$dev/$name" ] || continue
        # The number the serial ends in. run-vm keeps the end of a name
        # that is too long for a serial, so the text before it varies.
        number=$(tr -d ' ' < "$ctrl/serial" | sed 's/.*[^0-9]//')
        echo "${number:-0} /dev/$name"
    done
done | sort -n | cut -d' ' -f2
