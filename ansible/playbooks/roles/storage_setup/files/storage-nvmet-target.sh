#!/bin/sh
# Make this guest an NVMe-oF target over RDMA: one subsystem whose
# namespaces are the block devices named, on one port.
#
#   storage-nvmet-target.sh NQN ADDR PORT DEVICE...
#
# Prints `nvmet: ...` and exits 0 when the target is as asked, whether it
# made it or found it. A subsystem of that name with other namespaces is
# refused, not rebuilt: a host may be connected to it.
set -eu

[ $# -ge 4 ] || { echo "usage: $0 NQN ADDR PORT DEVICE..." >&2; exit 2; }
nqn=$1 addr=$2 port=$3
shift 3

cfg=/sys/kernel/config/nvmet
modprobe nvmet
modprobe nvmet-rdma
[ -d "$cfg" ] || mount -t configfs none /sys/kernel/config
[ -d "$cfg" ] || { echo "no $cfg: the kernel has no NVMe target" >&2; exit 1; }

sub="$cfg/subsystems/$nqn"
pdir="$cfg/ports/1"

want=$#
if [ -d "$sub" ]; then
    have=$(find "$sub/namespaces" -mindepth 1 -maxdepth 1 -type d | wc -l)
    linked=no
    [ -L "$pdir/subsystems/$nqn" ] && linked=yes
    if [ "$have" -eq "$want" ] && [ "$linked" = yes ]; then
        echo "nvmet: nqn=$nqn addr=$addr port=$port namespaces=$have, already there"
        exit 0
    fi
    echo "nvmet: $sub exists with $have namespace(s), linked=$linked, and" \
         "$want were asked for; remove it or restart the guest" >&2
    exit 1
fi

mkdir "$sub"
echo 1 > "$sub/attr_allow_any_host"
nsid=0
for dev in "$@"; do
    [ -b "$dev" ] || { echo "$dev is not a block device" >&2; exit 1; }
    nsid=$((nsid + 1))
    mkdir "$sub/namespaces/$nsid"
    echo "$dev" > "$sub/namespaces/$nsid/device_path"
    echo 1 > "$sub/namespaces/$nsid/enable"
done

[ -d "$pdir" ] || mkdir "$pdir"
echo rdma > "$pdir/addr_trtype"
echo ipv4 > "$pdir/addr_adrfam"
echo "$addr" > "$pdir/addr_traddr"
echo "$port" > "$pdir/addr_trsvcid"
ln -s "$sub" "$pdir/subsystems/$nqn"

echo "nvmet: nqn=$nqn addr=$addr port=$port namespaces=$nsid"
