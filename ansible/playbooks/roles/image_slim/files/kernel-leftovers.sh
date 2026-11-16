#!/bin/sh
# List what purged kernels left behind, one path a line.
#
#   kernel-leftovers.sh RUNNING_RELEASE
#
# dpkg removes the files a kernel package owns and no others: the module
# tree keeps what depmod and DKMS wrote into it, /boot keeps an initramfs
# no package ever owned, and /var/lib/dkms keeps the build made for that
# kernel. Each is small; together they are a second kernel's worth of
# things for update-grub and dkms to find.
#
# This removes nothing: tasks/main.yml does, path by path. A kernel is left
# alone when it is the running one or a package still owns its module tree.
set -eu

running=$1
# For the tests, which build a tree to list.
root=${IMAGE_SLIM_ROOT:-}

owned() {
    dpkg-query -S "$1" > /dev/null 2>&1
}

kept=" $running "
for dir in "$root"/lib/modules/*; do
    [ -d "$dir" ] || continue
    release=${dir##*/}
    [ "$release" = "$running" ] && continue
    if owned "/lib/modules/$release"; then
        kept="$kept$release "
    else
        echo "/lib/modules/$release"
    fi
done

# True for "<release>" and "<release>.<suffix>" of a kernel that stays.
is_kept() {
    for release in $kept; do
        case "$1" in
            "$release" | "$release".*) return 0 ;;
        esac
    done
    return 1
}

for file in "$root"/boot/vmlinuz-* "$root"/boot/initrd.img-* \
            "$root"/boot/System.map-* "$root"/boot/config-*; do
    [ -e "$file" ] || [ -L "$file" ] || continue
    name=${file##*/}
    is_kept "${name#*-}" && continue
    owned "/boot/$name" && continue
    echo "/boot/$name"
done

# /var/lib/dkms/<module>/<version>/<release> is one kernel's build, and
# /var/lib/dkms/<module>/kernel-<release>-<arch> a link to it.
for dir in "$root"/var/lib/dkms/*/*/*; do
    [ -d "$dir" ] && [ ! -L "$dir" ] || continue
    release=${dir##*/}
    case "$release" in
        [0-9]*) ;;
        *) continue ;;
    esac
    is_kept "$release" && continue
    echo "${dir#"$root"}"
done
for link in "$root"/var/lib/dkms/*/kernel-*; do
    [ -L "$link" ] || continue
    release=${link##*/kernel-}
    release=${release%-*}
    is_kept "$release" && continue
    echo "${link#"$root"}"
done
