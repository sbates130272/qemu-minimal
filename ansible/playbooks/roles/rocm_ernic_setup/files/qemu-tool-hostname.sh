#!/bin/bash
# shellcheck shell=bash
#
# Give the guest the name the host calls it by.
#
# Every guest of a stack boots an overlay of one image, so the hostname baked
# into the image is the bake's name on all of them, and nothing inside a guest
# says which guest it is. The host knows: `qemu-tool run-vm` puts the VM name
# -- rocm-golden-1 for the first guest of the default stack -- in the SMBIOS
# system serial number (-smbios type=1,serial=<name>), which the kernel shows
# as /sys/class/dmi/id/product_serial, readable by root only. This reads it
# and makes it the hostname, on every boot, so an overlay booted under another
# name follows.
#
#   qemu-tool-hostname.sh         set the hostname; what the boot unit runs
#   qemu-tool-hostname.sh name    print the name the host gave and change
#                                 nothing; exit 1, with the reason on stderr,
#                                 when it gave none
#
# rocm-ernic-netcfg.sh asks with "name" before it falls back to ernic-vm-<n>,
# and qemu/checks/rocm-golden.sh asks the same to know what to expect.
#
# What counts as a name:
#
#   * Not a placeholder: empty, "Not Specified", "To be filled by O.E.M.",
#     "0" and the like, which is what firmware writes for a serial nobody set.
#     Some of those are hostnames in their own right: none, null, empty,
#     unknown, invalid, oem, na, all zeros, four or more of one character,
#     123456789. A guest started under one of them as its VM name is NOT
#     given it: the hostname is left alone, and qemu/checks/rocm-golden.sh
#     fails such a guest and says so. No stack makes such a name; its guests
#     are <prefix>-<n>.
#   * The machine has to be QEMU's (sys_vendor "QEMU"). QEMU sets no serial
#     of its own, so one that is there was given by whoever started it. A
#     physical machine's serial number, or a cloud's instance id, is not a
#     name anybody chose for this guest.
#   * Made a hostname by rule: lower case, every character outside [a-z0-9-]
#     a hyphen, no hyphen at either end, and the LAST 63 characters of a
#     longer one -- a fleet's guests are <prefix>-<n>, so the end is the part
#     that tells them apart. A name that is already a hostname is unchanged.
#     Nothing left means no name.
#
# With no name the hostname is left alone: it is whatever /etc/hostname
# holds, which is the bake's name on a fresh overlay.
#
# The name is written to /etc/hostname and to the kernel directly, not
# through hostnamectl: this runs before D-Bus is up. /etc/hosts gets one
# "127.0.1.1 <name>" line, replacing any that is there, so the name resolves
# without systemd-resolved and no line for an earlier name is left behind.
#
# cloud-init is told to keep its hands off while the host names the guest:
# /etc/cloud/cloud.cfg.d/99-qemu-tool-hostname.cfg says preserve_hostname:
# true, and is removed again on a boot the host gives no name. On this image
# cloud-init is disabled by its own generator when no datasource is attached,
# and when it runs for the instance it already knows it leaves a changed
# hostname alone. A NEW instance is the case this is for: its set_hostname
# would write that datasource's name over this one, once, and measured on
# 2026-11-18 it does exactly that without the file and nothing with it.
#
# QEMU_TOOL_HOSTNAME_DMI, QEMU_TOOL_HOSTNAME_ETC and
# QEMU_TOOL_HOSTNAME_KERNEL stand in for /sys/class/dmi/id, /etc and
# /proc/sys/kernel/hostname, so tests/test_guest_hostname.py can run this
# against fabricated trees.
#
# Managed by Ansible (roles/rocm_ernic_setup) -- edit the role's file.
set -u

DMI=${QEMU_TOOL_HOSTNAME_DMI:-/sys/class/dmi/id}
ETC=${QEMU_TOOL_HOSTNAME_ETC:-/etc}
KERNEL=${QEMU_TOOL_HOSTNAME_KERNEL:-/proc/sys/kernel/hostname}

# Set by provided_name: the hostname and the serial it was made from, or why
# there is none.
NAME=""
SERIAL=""
WHY=""

lower() { printf '%s' "$1" | LC_ALL=C tr '[:upper:]' '[:lower:]'; }

is_placeholder() {
    local v
    v=$(lower "$1")
    case $v in
        ""|none|null|empty|unknown|invalid|oem|n/a|na) return 0 ;;
        "not specified"|"not applicable"|"not available"|"not present") return 0 ;;
        unspecified|"default string"|"system serial number") return 0 ;;
        "serial number"|"chassis serial number") return 0 ;;
        "to be filled by o.e.m."|"to be filled by oem") return 0 ;;
        123456789|0123456789|1234567890) return 0 ;;
    esac
    # All zeros at any length, and four or more of any one character:
    # "00000000", "XXXXXXXX", "........".
    case $v in
        *[!0]*) ;;
        *) return 0 ;;
    esac
    if [ "${#v}" -ge 4 ] && [ -z "${v//"${v:0:1}"/}" ]; then
        return 0
    fi
    return 1
}

to_hostname() {
    local s
    s=$(lower "$1" | LC_ALL=C tr -c 'a-z0-9-' '-')
    while [ "${s#-}" != "$s" ]; do s=${s#-}; done
    while [ "${s%-}" != "$s" ]; do s=${s%-}; done
    if [ "${#s}" -gt 63 ]; then
        s=${s: -63}
        while [ "${s#-}" != "$s" ]; do s=${s#-}; done
    fi
    printf '%s' "$s"
}

provided_name() {
    local vendor serial
    if [ ! -r "$DMI/product_serial" ]; then
        WHY="$DMI/product_serial cannot be read: no SMBIOS, or not root"
        return 1
    fi
    # Less the newline sysfs adds, and less blanks at either end: padding,
    # not part of a name.
    serial=$(cat "$DMI/product_serial" 2>/dev/null)
    serial=${serial#"${serial%%[![:space:]]*}"}
    serial=${serial%"${serial##*[![:space:]]}"}
    if is_placeholder "$serial"; then
        WHY="the SMBIOS serial number is '$serial', which is no name"
        return 1
    fi
    vendor=$(cat "$DMI/sys_vendor" 2>/dev/null)
    if [ "$vendor" != QEMU ]; then
        WHY="the SMBIOS serial number is '$serial' and the machine is '$vendor', not QEMU: a serial number, not a name"
        return 1
    fi
    NAME=$(to_hostname "$serial")
    if [ -z "$NAME" ]; then
        WHY="the SMBIOS serial number '$serial' leaves nothing once it is made a hostname"
        return 1
    fi
    SERIAL=$serial
    return 0
}

write_static() {
    local tmp
    tmp=$(mktemp "$ETC/hostname.XXXXXX") || return 1
    if printf '%s\n' "$1" > "$tmp" && chmod 0644 "$tmp" &&
            mv "$tmp" "$ETC/hostname"; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# One "127.0.1.1 <name>" line: in place of the first that is there, else
# after the first 127.0.0.1 line, else at the end. Written only when the
# file would change.
update_hosts() {
    local hosts=$ETC/hosts tmp has
    [ -f "$hosts" ] || return 0
    has=$(grep -c '^127\.0\.1\.1[[:space:]]' "$hosts")
    tmp=$(mktemp "$hosts.XXXXXX") || return 1
    if ! awk -v name="$1" -v has="$has" '
            /^127\.0\.1\.1[ \t]/ {
                if (!done) { print "127.0.1.1 " name; done = 1 }
                next
            }
            { print }
            !has && !done && /^127\.0\.0\.1[ \t]/ {
                print "127.0.1.1 " name; done = 1
            }
            END { if (!done) print "127.0.1.1 " name }
        ' "$hosts" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if cmp -s "$tmp" "$hosts"; then
        rm -f "$tmp"
        return 0
    fi
    if chmod --reference="$hosts" "$tmp" && mv "$tmp" "$hosts"; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# preserve_hostname for cloud-init, present exactly while the host names the
# guest. Nothing to do on an image with no cloud-init.
CLOUD_DROPIN=$ETC/cloud/cloud.cfg.d/99-qemu-tool-hostname.cfg
CLOUD_TEXT="# Written by qemu-tool-hostname.sh: the host names this guest.
preserve_hostname: true"

cloud_init_keeps_off() {
    local tmp
    [ -d "${CLOUD_DROPIN%/*}" ] || return 0
    [ "$(cat "$CLOUD_DROPIN" 2>/dev/null)" = "$CLOUD_TEXT" ] && return 0
    tmp=$(mktemp "$CLOUD_DROPIN.XXXXXX") || return 1
    if printf '%s\n' "$CLOUD_TEXT" > "$tmp" && chmod 0644 "$tmp" &&
            mv "$tmp" "$CLOUD_DROPIN"; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}

case ${1:-} in
    name)
        if provided_name; then
            printf '%s\n' "$NAME"
            exit 0
        fi
        echo "$WHY" >&2
        exit 1
        ;;
    "") ;;
    *)
        echo "usage: ${0##*/} [name]" >&2
        exit 2
        ;;
esac

current=$(cat "$KERNEL" 2>/dev/null)
if ! provided_name; then
    rm -f "$CLOUD_DROPIN"
    echo "the host gave this guest no name ($WHY): hostname left as '$current'"
    exit 0
fi

rc=0
if [ "$(cat "$ETC/hostname" 2>/dev/null)" != "$NAME" ]; then
    write_static "$NAME" || {
        echo "could not write $ETC/hostname" >&2
        rc=1
    }
fi
if [ "$current" != "$NAME" ]; then
    printf '%s\n' "$NAME" > "$KERNEL" || {
        echo "could not set the running hostname through $KERNEL" >&2
        rc=1
    }
fi
update_hosts "$NAME" || {
    echo "could not put 127.0.1.1 $NAME in $ETC/hosts" >&2
    rc=1
}
cloud_init_keeps_off || {
    echo "could not write $CLOUD_DROPIN" >&2
    rc=1
}

if [ "$current" = "$NAME" ]; then
    echo "hostname is '$NAME', the host's name for this guest ('$SERIAL')"
else
    echo "hostname set to '$NAME', the host's name for this guest ('$SERIAL'); it was '$current'"
fi
exit $rc
