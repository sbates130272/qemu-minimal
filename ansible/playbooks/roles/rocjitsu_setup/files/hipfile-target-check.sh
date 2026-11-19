#!/usr/bin/env bash
# Refuse a hipFile read whose target is not on an NVMe namespace.
#
#   hipfile-target-check.sh MOUNTPOINT
#
# Run in a guest by the hipFile play of vm-quickstart.yml, after it has
# mounted the namespace and before anything reads through hipFile. Exit 0
# and one line when MOUNTPOINT is a filesystem on an NVMe namespace; exit 1
# with the reason when it is anything else, a directory nothing is mounted
# on included.
#
# The lines between the two markers are the check
# scripts/generate-vm-report.sh and scripts/vm-report/fio-hipfile-bench.sh
# make before their own reads. tests/test_vm_report_bench.py holds the
# three copies equal, byte for byte.
set -euo pipefail
MNT=${1:?usage: hipfile-target-check.sh MOUNTPOINT}
# begin: hipfile target is on nvme
# A hipFile direct read of a file that is not on an NVMe namespace can hang
# the guest for good: AIS takes any PCI block device, and a virtio disk
# cannot DMA to a GPU BAR address (seen on the root disk: "virtio: bogus
# descriptor or out of resources", and the guest never answered again).
src=$(findmnt -n -o SOURCE --target "${MNT}" 2>/dev/null || true)
# A namespace or a partition of one, with btrfs's "[/subvolume]" allowed.
nvme_re='^/dev/nvme[0-9]+n[0-9]+(p[0-9]+)?(\[.*\])?$'
if ! [[ "${src}" =~ ${nvme_re} ]]; then
  echo "${MNT} is on '${src:-no filesystem}', not an NVMe namespace: refusing a hipFile direct read there"
  exit 1
fi
# end: hipfile target is on nvme
echo "${MNT} is on ${src}, an NVMe namespace"
