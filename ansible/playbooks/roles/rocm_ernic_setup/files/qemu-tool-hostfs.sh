#!/bin/sh
# Mount the directory the host shares with this guest, at /mnt/host.
#
# `qemu-tool run-vm --filesystem DIR` (a fleet's <vm-filesystem>) gives the
# guest a virtio-9p channel with the mount tag "hostfs". With no such
# channel this does nothing and exits 0, so the unit is silent on every
# guest that has no share.
#
# Whose files they are. QEMU does every host access as the user it runs as,
# so every file in the share is that user's on the host, and the guest sees
# that user's uid as the owner. When it is also the uid of this guest's
# login user the share is mounted as it is. When it is not, the login user
# would be "other" to every file: read what is world-readable, write
# nothing. The 9p client cannot remap ids (no id-mapped mounts), so the
# share is then mounted out of the way and shown at /mnt/host through
# bindfs, with the host owner's ids mapped to the login user's. Nothing on
# the host changes either way.
#
# cache=mmap is not optional: measured on a guest of this image, a share
# mounted with no cache option read and wrote at 5 to 7 MB/s, and with it
# at 161 and 288 MB/s. It caches file data and asks the host again at each
# open, so a file changed on the host is seen. cache=loose is faster on
# many small files and can show a stale file; it is not used.
#
#   qemu-tool-hostfs.sh           mount the share, if there is one
#   qemu-tool-hostfs.sh status    say what is mounted and change nothing
set -eu

TAG=hostfs
MNT=/mnt/host
# Under a directory only root can enter: the unmapped view is not a second
# way in.
HOLD=/run/qemu-tool/hostfs
HOSTFS_USER=
OPTIONS=trans=virtio,version=9p2000.L,msize=1048576,cache=mmap,nosuid,nodev
SYS=${QEMU_TOOL_HOSTFS_SYS:-/sys/bus/virtio/devices}

# HOSTFS_USER, as the image's bake wrote it; MNT and HOLD may be set there
# too. The two variables read from the environment are for the tests: the
# unit runs with systemd's environment, which has neither.
DEFAULTS=${QEMU_TOOL_HOSTFS_DEFAULTS:-/etc/default/qemu-tool-hostfs}
if [ -r "$DEFAULTS" ]; then
    # shellcheck disable=SC1090
    . "$DEFAULTS"
fi
RAW=$HOLD/raw
if [ -z "$HOSTFS_USER" ]; then
    HOSTFS_USER=$(getent passwd 1000 | cut -d: -f1)
fi

say() { echo "hostfs: $*"; }

mounted() { mountpoint -q "$1" 2>/dev/null; }

# A virtio device of type 9 is a 9p channel. Its tag is readable only once
# the transport's module is in, which is why the type is asked first.
has_channel() {
    for dev in "$SYS"/virtio*; do
        [ -r "$dev/device" ] || continue
        [ "$(cat "$dev/device")" = 0x0009 ] && return 0
    done
    return 1
}

has_tag() {
    for dev in "$SYS"/virtio*; do
        [ -r "$dev/mount_tag" ] || continue
        [ "$(tr -d '\0' < "$dev/mount_tag")" = "$TAG" ] && return 0
    done
    return 1
}

if [ "${1:-}" = status ]; then
    if mounted "$MNT"; then
        say "$MNT is mounted: $(findmnt -n -o SOURCE,FSTYPE "$MNT")"
    elif has_channel; then
        say "the host shares a directory and $MNT is not mounted"
    else
        say "the host shares no directory with this guest"
    fi
    exit 0
fi

if ! has_channel; then
    say "the host shares no directory with this guest"
    exit 0
fi
modprobe 9pnet_virtio 2>/dev/null || true
modprobe 9p 2>/dev/null || true
if ! has_tag; then
    say "this guest has a 9p channel, and none with the tag $TAG; nothing mounted"
    exit 0
fi
if mounted "$MNT"; then
    say "$MNT is already mounted"
    exit 0
fi

mkdir -p "$MNT"
mkdir -p "$HOLD"
chmod 0700 "$HOLD"
mkdir -p "$RAW"
mounted "$RAW" || mount -t 9p -o "$OPTIONS" "$TAG" "$RAW"

owner_uid=$(stat -c %u "$RAW")
owner_gid=$(stat -c %g "$RAW")
user_uid=
user_gid=
if [ -n "$HOSTFS_USER" ] && id -u "$HOSTFS_USER" > /dev/null 2>&1; then
    user_uid=$(id -u "$HOSTFS_USER")
    user_gid=$(id -g "$HOSTFS_USER")
fi

direct() {
    umount "$RAW"
    mount -t 9p -o "$OPTIONS" "$TAG" "$MNT"
}

if [ -z "$user_uid" ]; then
    direct
    say "$MNT is the host's share; this guest has no login user to give it to"
elif [ "$owner_uid" = "$user_uid" ] && [ "$owner_gid" = "$user_gid" ]; then
    direct
    say "$MNT is the host's share, owned by $HOSTFS_USER here as on the host"
elif [ "$owner_uid" = 0 ]; then
    # Root's directory on the host, or a root QEMU keeping guest ownership
    # in extended attributes. Handing root's files to the login user is not
    # a mapping anybody asked for.
    direct
    say "$MNT is the host's share; its files are root's and are left so"
elif command -v bindfs > /dev/null 2>&1; then
    bindfs --map="${owner_uid}/${user_uid}:@${owner_gid}/@${user_gid}" \
        "$RAW" "$MNT"
    say "$MNT is the host's share; the host's uid ${owner_uid} and gid" \
        "${owner_gid} are shown as $HOSTFS_USER"
else
    direct
    say "$MNT is the host's share, owned by uid ${owner_uid}, which is not" \
        "$HOSTFS_USER (${user_uid}): install bindfs for $HOSTFS_USER to" \
        "write it, or use sudo"
fi
