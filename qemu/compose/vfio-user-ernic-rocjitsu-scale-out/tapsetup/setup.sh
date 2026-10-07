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

# Pick the l2 interface BY ADDRESS, never by name.
#
# "eth1" was the old default and it is a coin flip: Docker does not guarantee
# which attached network lands on which interface, and it genuinely differs
# between containers in one `up`. Measured on a 2-VM fleet:
#
#   ernic-1  eth0=172.19.0.2 (l2)     eth1=172.31.1.1 (fleet)
#   ernic-2  eth0=172.31.1.2 (fleet)  eth1=172.19.0.3 (l2)
#
# so a hardcoded eth1 enslaved the MANAGER's mesh interface into the bridge.
# That failure is nasty: the manager still passes its own healthcheck, which
# reads /proc/net/tcp for a LISTEN socket and needs no working routing at all,
# while every worker dialling it dies on "No route to host" and takes its
# guest with it. The stack then looks like a worker bug.
#
# FLEET_PREFIX is the first two octets of FLEET_NET_PREFIX; the l2 interface
# is the one whose address is not in it. L2IF still overrides, for a topology
# this rule does not fit.
FLEET_PREFIX=${FLEET_PREFIX:-172.31}

pick_l2() {
    for dev in $(ls /sys/class/net); do
        case "$dev" in lo|"$TAP"|"$BR"|docker*|veth*) continue ;; esac
        addr=$(ip -4 -o addr show dev "$dev" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
        [ -n "$addr" ] || continue
        case "$addr" in "$FLEET_PREFIX".*) continue ;; esac
        echo "$dev"; return 0
    done
    return 1
}

if [ -n "${L2IF:-}" ]; then
    L2=$L2IF
elif ! L2=$(pick_l2); then
    echo "tapsetup: no interface outside $FLEET_PREFIX.* to bridge." >&2
    echo "tapsetup: interfaces seen:" >&2
    ip -4 -br addr >&2
    echo "tapsetup: is the ernic attached to the l2 network?" >&2
    exit 1
fi
echo "tapsetup: bridging $L2 ($(ip -4 -o addr show dev "$L2" | awk '{print $4}')),"\
     "fleet prefix $FLEET_PREFIX.* excluded"

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
