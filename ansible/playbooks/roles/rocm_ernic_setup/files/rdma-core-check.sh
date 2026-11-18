#!/bin/bash
# shellcheck shell=bash
#
# Does this guest have one rdma-core to build against, in /usr/local? Run in
# the guest; changes nothing, and needs no device.
#
# A golden guest has two rdma-cores: the archive's under /usr, kept as a
# runtime, and rocm-ernic's, built from source into /usr/local, which every
# tool on the image loads. The archive's development packages
# (libibverbs-dev, librdmacm-dev, libibumad-dev) gave a build a second set
# of headers and link libraries, and the toolchain took the headers from one
# rdma-core and the library from the other. The bake installs none of them
# and tasks/rdma_dev_purge.yml takes them out of a guest that has them.
#
# This is the bake's last word on it, after the guest stamp is written: the
# three packages are not installed and the runtime ones are; no rdma-core
# header set, link library or pkg-config file is left outside /usr/local;
# pkg-config resolves libibverbs, librdmacm and libibumad there; a program
# built with no flags reads, links and loads the copy there; and
# /etc/rocm-golden-guest.json names both rdma-cores as they are.
#
# The lines between the two markers are, byte for byte, the block of the
# same name in qemu/checks/rocm-golden.sh, which the lanes run on the booted
# image before anything is published. tests/test_rdma_dev_packages.py holds
# the two equal: change one and the other has to follow.
#
# Prints one line and exits 0, or "ERROR: rdma-core: ..." on stderr and
# exits 1. RDMA_CHECK_ROOT, for the tests, is prefixed to every path.
set -eu

# begin: rdma-core
_rc_root=${RDMA_CHECK_ROOT:-}
_rc_local="${_rc_root}/usr/local"
_rc_tmp=''
_rc_fail() {
    echo "ERROR: rdma-core: $*" >&2
    [ -z "${_rc_tmp}" ] || rm -rf "${_rc_tmp}"
    exit 1
}

# The three packages. Anything but "never installed" and "removed" counts:
# a half-installed package still has its files on disk.
for _rc_pkg in libibverbs-dev librdmacm-dev libibumad-dev; do
    _rc_state=$(dpkg-query -W -f='${db:Status-Status}\n' "${_rc_pkg}" \
                    2> /dev/null || true)
    if printf '%s\n' "${_rc_state}" |
            grep -qvx -e '' -e not-installed -e config-files; then
        _rc_fail "${_rc_pkg} is in this image" \
                 "($(printf '%s' "${_rc_state}" | tr '\n' ' ')). With the" \
                 "archive's rdma-core development packages installed a" \
                 "build reads the headers in /usr/local and links the" \
                 "archive's library"
    fi
done

# What stays: the archive's rdma-core as a runtime. Other packages are
# linked against these libraries, rdma-core carries the udev rules and the
# systemd units, and a purge that took one of them took too much.
for _rc_pkg in rdma-core ibverbs-utils ibverbs-providers libibverbs1 \
               libibumad3 'librdmacm1*'; do
    _rc_state=$(dpkg-query -W -f='${db:Status-Status}\n' "${_rc_pkg}" \
                    2> /dev/null || true)
    if ! printf '%s\n' "${_rc_state}" | grep -qx installed; then
        _rc_fail "${_rc_pkg} is not installed: the archive's rdma-core" \
                 "runtime, which stays in this image, has lost a package"
    fi
done

# Nothing of an rdma-core for a compiler or a linker outside /usr/local: no
# header set, and for each library no link without a version, no archive and
# no pkg-config file where the toolchain looks by itself.
for _rc_path in /usr/include/infiniband /usr/include/rdma/rdma_cma.h; do
    if [ -e "${_rc_root}${_rc_path}" ] || [ -L "${_rc_root}${_rc_path}" ]; then
        _rc_fail "${_rc_path} exists: rdma-core headers outside /usr/local"
    fi
done
# The kernel's own uapi headers are not rdma-core's and have to stay: the
# verbs.h in /usr/local includes them.
if [ ! -f "${_rc_root}/usr/include/rdma/ib_user_verbs.h" ]; then
    _rc_fail "no /usr/include/rdma/ib_user_verbs.h: the kernel's uapi" \
             "headers, from linux-libc-dev, which rdma-core's verbs.h" \
             "includes"
fi
_rc_ma=$(gcc -print-multiarch)
for _rc_lib in libibverbs librdmacm libibumad; do
    for _rc_path in "/usr/lib/${_rc_ma}/${_rc_lib}.so" \
                    "/usr/lib/${_rc_ma}/${_rc_lib}.a" \
                    "/usr/lib/${_rc_ma}/pkgconfig/${_rc_lib}.pc" \
                    "/usr/lib/${_rc_lib}.so" "/usr/lib/${_rc_lib}.a" \
                    "/usr/lib/pkgconfig/${_rc_lib}.pc" \
                    "/usr/lib64/${_rc_lib}.so" "/usr/lib64/${_rc_lib}.a" \
                    "/usr/share/pkgconfig/${_rc_lib}.pc"; do
        if [ -e "${_rc_root}${_rc_path}" ] ||
                [ -L "${_rc_root}${_rc_path}" ]; then
            _rc_fail "${_rc_path} exists: a second ${_rc_lib} to build" \
                     "against, outside /usr/local"
        fi
    done
    if [ ! -e "${_rc_local}/lib/${_rc_lib}.so" ]; then
        _rc_fail "no /usr/local/lib/${_rc_lib}.so: there is no ${_rc_lib}" \
                 "to link against at all"
    fi
    for _rc_var in libdir:lib includedir:include; do
        _rc_got=$(pkg-config --variable="${_rc_var%%:*}" "${_rc_lib}" \
                      2> /dev/null || true)
        if [ "${_rc_got}" != "${_rc_local}/${_rc_var##*:}" ]; then
            _rc_fail "pkg-config gives ${_rc_lib} the ${_rc_var%%:*}" \
                     "'${_rc_got}', not /usr/local/${_rc_var##*:}"
        fi
    done
done

# And what a build does: a program for each library, compiled and linked
# with no flag but -l, never run. The header the compiler reads, the library
# the linker takes and the one the loader gives the result have to be one
# rdma-core, in /usr/local.
_rc_tmp=$(mktemp -d)
_rc_verbs=''
for _rc_case in 'infiniband/verbs.h:ibv_get_device_list(0):ibverbs' \
                'rdma/rdma_cma.h:rdma_create_event_channel():rdmacm' \
                'infiniband/umad.h:umad_init():ibumad'; do
    _rc_hdr=${_rc_case%%:*}
    _rc_lib=${_rc_case##*:}
    _rc_call=${_rc_case#*:}
    _rc_call=${_rc_call%:*}
    printf '#include <%s>\nint main(void) { return %s == 0; }\n' \
        "${_rc_hdr}" "${_rc_call}" > "${_rc_tmp}/t.c"
    # Nothing here may read stdin: over "bash -s" that is this script.
    _rc_out=$(gcc -H -fsyntax-only "${_rc_tmp}/t.c" 2>&1 < /dev/null) ||
        _rc_fail "a program that includes <${_rc_hdr}> does not compile:" \
                 "$(printf '%s\n' "${_rc_out}" | tail -3 | tr '\n' ' ')"
    _rc_read=$(printf '%s\n' "${_rc_out}" |
        sed -n "s|^\. \(.*/${_rc_hdr}\)\$|\1|p" | sed -n 1p)
    if [ "${_rc_read}" != "${_rc_local}/include/${_rc_hdr}" ]; then
        _rc_fail "a build with no flags reads <${_rc_hdr}> from" \
                 "'${_rc_read}', not from /usr/local/include"
    fi
    # --trace has the linker name every file it takes.
    _rc_out=$(gcc -o "${_rc_tmp}/t" "${_rc_tmp}/t.c" "-l${_rc_lib}" \
                  -Wl,--trace 2>&1 < /dev/null) ||
        _rc_fail "a program does not link with -l${_rc_lib} and no other" \
                 "flag: $(printf '%s\n' "${_rc_out}" | tail -3 | tr '\n' ' ')"
    # shellcheck disable=SC2020
    _rc_link=$(printf '%s\n' "${_rc_out}" | tr ' ()' '\n\n\n' |
        sed -n "\|/lib${_rc_lib}\.so|p" | sed -n 1p)
    [ -n "${_rc_link}" ] ||
        _rc_fail "the linker did not say which lib${_rc_lib}.so it took"
    _rc_link=$(readlink -f "${_rc_link}")
    _rc_load=$(ldd "${_rc_tmp}/t" 2> /dev/null < /dev/null |
        awk -v n="lib${_rc_lib}.so" 'index($1, n) == 1 { print $3 }' |
        sed -n 1p)
    case "${_rc_load}" in
        /*) _rc_load=$(readlink -f "${_rc_load}") ;;
        *) _rc_fail "the loader cannot find the lib${_rc_lib} that" \
                    "${_rc_link} was linked as" ;;
    esac
    if [ "${_rc_link}" != "${_rc_load}" ]; then
        _rc_fail "a build with no flags links ${_rc_link} and loads" \
                 "${_rc_load}: two rdma-cores"
    fi
    case "${_rc_load}" in
        "${_rc_local}/lib/"*) ;;
        *) _rc_fail "a build with no flags links and loads ${_rc_load}," \
                    "which is not in /usr/local/lib" ;;
    esac
    if [ "${_rc_lib}" = ibverbs ]; then
        _rc_verbs=${_rc_load}
    fi
done
rm -rf "${_rc_tmp}"
_rc_tmp=''

# The image's own record names both rdma-cores, and has to name these two:
# the archive package dpkg reports, and the libibverbs just linked and
# loaded, whose file name ends in its rdma-core's version.
_rc_archive=$(dpkg-query -W -f='${Version}' rdma-core)
_rc_version=$(python3 - "${_rc_root}/etc/rocm-golden-guest.json" \
                  "${_rc_archive}" "${_rc_verbs}" <<'RCPY'
import json
import os
import re
import sys


def fail(msg):
    print(f"ERROR: rdma-core: {msg}", file=sys.stderr)
    sys.exit(1)


path, archive, loaded = sys.argv[1:4]
try:
    with open(path) as handle:
        stamp = json.load(handle)
except (OSError, ValueError) as exc:
    fail(f"{path} is not readable JSON: {exc}")
for key in ("rdma_core_version", "rdma_core_archive_version",
            "rdma_core_local_version", "rdma_core_local_libibverbs"):
    if not stamp.get(key):
        fail(f"{path} has no \"{key}\"")
# rdma_core_version is the archive's too: the name earlier images used.
for key in ("rdma_core_archive_version", "rdma_core_version"):
    if stamp[key] != archive:
        fail(f"{path} records {key} {stamp[key]!r}, and the rdma-core "
             f"package installed is {archive}")
if stamp["rdma_core_local_libibverbs"] != loaded:
    fail(f"{path} records rdma_core_local_libibverbs "
         f"{stamp['rdma_core_local_libibverbs']!r}, and a build links and "
         f"loads {loaded}")
named = re.fullmatch(r"libibverbs\.so\.\d+\.\d+\.(\d+(?:\.\d+)*)",
                     os.path.basename(loaded))
if not named or named.group(1) != stamp["rdma_core_local_version"]:
    fail(f"{path} records rdma_core_local_version "
         f"{stamp['rdma_core_local_version']!r}, which is not the version "
         f"{loaded} is named for")
print(stamp["rdma_core_local_version"])
RCPY
)
echo "rdma-core: ${_rc_version} in /usr/local is the one a build finds," \
     "links and loads; the archive's ${_rc_archive} is a runtime only"
# end: rdma-core
