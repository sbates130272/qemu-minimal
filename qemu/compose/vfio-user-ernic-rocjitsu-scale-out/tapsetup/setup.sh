#!/bin/sh
# Create the TAP the ernic attaches to, and bridge it onto the shared L2.
#
# rocm-ernic only gives the guest working Ethernet when attached to a TAP
# (-T); its TCP mesh carries RDMA payload only. Without this the guest's
# driver transmits, the ernic drops the frames, ARP goes unanswered and
# ib_send_bw cannot do its out-of-band exchange -- so no inter-node test
# runs at all.
#
# A TAP lives in exactly one netns, so putting every guest on one L2 means
# bridging each node's tap to a network all the ernic containers share. We
# bridge to the `l2` interface, never to the mesh interface: that one carries
# the address workers dial and must not move.
#
# NET_ADMIN is needed to create a tap and a bridge, and it lives here only --
# this container exits as soon as the wiring is up. The tap is created
# persistent and owned, so rocm-ernic later attaches to it with no capability
# at all, just /dev/net/tun. Verified: an ernic at Docker's default cap set
# reports "Ethernet attached to TAP tap0".
set -eu
TAP=${TAP:-tap0}
BR=${BR:-br0}
L2=${L2IF:-eth1}

ip link show "$TAP" >/dev/null 2>&1 || ip tuntap add dev "$TAP" mode tap user 0
ip link set "$TAP" up

ip link show "$BR" >/dev/null 2>&1 || ip link add name "$BR" type bridge
# A bridge drops frames for up to 15 s while it learns unless STP is off and
# the forward delay is zero. The guest's first ARP lands inside that window,
# so without this the first ping of a fresh fleet fails for no visible reason.
ip link set "$BR" type bridge stp_state 0 forward_delay 0
ip link set "$BR" up

ip link set "$TAP" master "$BR"
ip link set "$L2" master "$BR"
ip link set "$L2" up

echo "tapsetup: bridged $TAP and $L2 on $BR"
ip -br link show master "$BR"
