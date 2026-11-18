#!/bin/bash
# shellcheck shell=bash
#
# Is the ionic driver this guest runs the SRQ + P4 build its image was baked
# with? Run in the guest; changes nothing. The live stage loads ernic_p4 when
# it is not loaded, which is the only way to ask whether it can be, and
# unloads it again unless ionic made a P4 device for it to bind.
#
# Why this exists. vm-rocm-golden.yml bakes ionic-ernic from the kernel's
# ionic sources plus rocm-ernic's SRQ and P4 series. Its configure stage then
# ran the collection's ernic_guest_setup with the driver build on: that
# fetched the BASE series only, ran setup-ionic-dkms.sh, which removes and
# rebuilds ionic-ernic/<kernel ref> under the same name and version, and
# reloaded the modules. Two CI runs show the rebuild; neither read max_srq,
# so no log says what the guest was left with. This asks.
#
# Two stages, chosen with ERNIC_DRIVER_CHECK_STAGE:
#
#   installed  what is on disk, which needs no device: the bake's record, the
#              modules modprobe would load, and what they contain. A bake
#              runs this.
#   live       the default. All of the above, then the loaded modules and the
#              device: every emulated NIC reports max_srq above zero, and
#              ernic_p4 loads against the ionic that is loaded.
#
# ERNIC_DRIVER_EXPECT_P4 says whether the device serving this guest offers
# the P4 engine to it (rocm-ernic --p4-guest-scope, <p4-guest-scope> in a
# fleet file):
#
#   device     ionic must have created its P4 auxiliary device and
#              /dev/ernic-p4 must exist
#   nodevice   neither is asked for
#   auto       the default: /dev/ernic-p4 is asked for when ionic created the
#              auxiliary device
#
# One line per answer, "ok:", "note:" or "FAIL:", all on stdout so that they
# stay in order under ssh and under Ansible's script module alike. Exit 1 on
# any FAIL.
#
# ERNIC_SYSFS_ROOT, ERNIC_DEV_ROOT and ERNIC_DRIVER_RECORD stand in for /sys,
# /dev and the bake's record, and ERNIC_DRIVER_NODE_WAIT for the seconds
# /dev/ernic-p4 is waited for, so tests/test_configure_keeps_baked_driver.py
# can run this against a fabricated guest.
set -u

SYSFS=${ERNIC_SYSFS_ROOT:-/sys}
DEVFS=${ERNIC_DEV_ROOT:-/dev}
RECORD=${ERNIC_DRIVER_RECORD:-/etc/ionic-srq-p4.json}
STAGE=${ERNIC_DRIVER_CHECK_STAGE:-live}
EXPECT_P4=${ERNIC_DRIVER_EXPECT_P4:-auto}

# The emulated NIC, by all four fields: 1dd8:1002 alone is a real Pensando
# card's id.
ERNIC_TUPLE=0x1dd8:0x1002:0x1dd8:0x5400
# In ionic_create_srq(): present only with rocm-ernic's SRQ series.
SRQ_SYMBOL=ionic_create_srq
# The warning ionic_auxbus_register() gained with rocm-ernic's P4 series.
# ionic exports nothing new for P4, so there is no symbol to ask nm for;
# ionic_adminq_post_wait, which ernic_p4 calls, is in the base driver too.
P4_STRING='Failed to add p4 aux device'

fails=0
replaced=0
unbaked=0

ok()   { echo "ok: $*"; }
note() { echo "note: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
# A failure that says the driver is not the baked one.
differs() { fail "$@"; replaced=1; }

case ${STAGE} in
    installed|live) ;;
    *) echo "FAIL: ERNIC_DRIVER_CHECK_STAGE is '${STAGE}'; expected installed or live"; exit 2 ;;
esac
case ${EXPECT_P4} in
    device|nodevice|auto) ;;
    *) echo "FAIL: ERNIC_DRIVER_EXPECT_P4 is '${EXPECT_P4}'; expected device, nodevice or auto"; exit 2 ;;
esac

# Nothing this script starts may read stdin: over "bash -s" that is the
# script itself.
as_root() {
    if [ "$(id -u)" = 0 ]; then "$@" < /dev/null; else sudo -n "$@" < /dev/null; fi
}

# A string field of the bake's record; empty when the record has none. Not
# by line: the playbook writes one field a line, and a controller on an
# older ansible-core (2.16, measured) parses that text as JSON and writes it
# back out on one line.
recorded() {
    grep -o "\"$1\":[[:space:]]*\"[^\"]*\"" "${RECORD}" 2>/dev/null |
        head -1 | sed 's/^[^:]*:[[:space:]]*"\(.*\)"$/\1/'
}

unpack() {
    case $1 in
        *.zst) zstd -qdc "$1" ;;
        *.xz)  xz -dc "$1" ;;
        *.gz)  gzip -dc "$1" ;;
        *)     cat "$1" ;;
    esac
}

TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT

# nm answers the SRQ question. Without it the answer is "not known", which
# must not read as "no such symbol": that is the verdict for a rebuilt driver.
have_nm=1
if ! command -v nm > /dev/null 2>&1; then
    have_nm=0
    fail "nm is not installed (binutils), so whether ionic_rdma carries the" \
         "SRQ series cannot be read"
fi

# ── the bake's record ─────────────────────────────────────────────────────

if [ -s "${RECORD}" ]; then
    ok "${RECORD} is present: $(recorded dkms_package) for $(recorded built_for_kernel)"
else
    unbaked=1
    fail "${RECORD} is missing: this guest was not baked by" \
         "vm-rocm-golden.yml, whose bake is the only thing that builds the" \
         "SRQ + P4 driver. The configure stage builds none"
fi

# ── one module: where it would load from, and whether it is the baked build ──

# Sets ko to the path modprobe would load NAME from, empty when it is not a
# DKMS build, and file_src to that file's srcversion.
installed_module() {
    local name=$1 baked
    ko=$(modinfo -n "${name}" 2>/dev/null) || ko=""
    file_src=""
    case ${ko} in
        */updates/dkms/*) ;;
        "")
            differs "${name}: modinfo finds no such module for this kernel"
            return 1 ;;
        *)
            differs "${name}: modprobe would load ${ko}, not the DKMS build" \
                    "under updates/dkms"
            ko=""
            return 1 ;;
    esac
    file_src=$(modinfo -F srcversion "${name}" 2>/dev/null) || file_src=""
    if [ -z "${file_src}" ]; then
        fail "${name}: ${ko} has no srcversion"
        return 1
    fi
    baked=$(recorded "${name}_srcversion")
    if [ -z "${baked}" ]; then
        note "${name}: ${RECORD} records no srcversion, so the installed" \
             "build is judged by what it contains"
    elif [ "${baked}" = "${file_src}" ]; then
        ok "${name}: the installed DKMS build is the baked one (srcversion ${baked})"
    else
        differs "${name}: the installed DKMS build has srcversion ${file_src};" \
                "the bake recorded ${baked}. It was built again after the bake"
    fi
    return 0
}

# The loaded NAME against the installed file's srcversion, given as $2.
loaded_module() {
    local name=$1 want=$2 have
    if [ ! -d "${SYSFS}/module/${name}" ]; then
        fail "${name}: not loaded"
        return 1
    fi
    have=$(cat "${SYSFS}/module/${name}/srcversion" 2>/dev/null) || have=""
    if [ -z "${have}" ]; then
        fail "${name}: loaded, with no srcversion in ${SYSFS}/module/${name}"
    elif [ "${have}" = "${want}" ]; then
        ok "${name}: the loaded module is the installed DKMS build"
    else
        differs "${name}: the loaded module has srcversion ${have}; the" \
                "installed DKMS build has ${want}. Another build is loaded"
    fi
}

# ── ionic_rdma: the SRQ verbs ─────────────────────────────────────────────

if installed_module ionic_rdma; then
    if ! unpack "${ko}" > "${TMP}/ionic_rdma.ko"; then
        fail "ionic_rdma: could not read ${ko}"
    elif [ "${have_nm}" = 0 ]; then
        :
    elif nm "${TMP}/ionic_rdma.ko" 2>/dev/null | grep -q "[[:space:]]${SRQ_SYMBOL}\$"; then
        ok "ionic_rdma: the installed build defines ${SRQ_SYMBOL}"
    else
        differs "ionic_rdma: the installed build defines no ${SRQ_SYMBOL}:" \
                "it was built without rocm-ernic's SRQ series"
    fi
    [ "${STAGE}" = installed ] || loaded_module ionic_rdma "${file_src}"
fi

# ── ionic: the P4 auxiliary device ────────────────────────────────────────

if installed_module ionic; then
    if ! unpack "${ko}" > "${TMP}/ionic.ko"; then
        fail "ionic: could not read ${ko}"
    elif grep -a -q "${P4_STRING}" "${TMP}/ionic.ko"; then
        ok "ionic: the installed build registers a P4 auxiliary device"
    else
        differs "ionic: the installed build registers no P4 auxiliary device:" \
                "it was built without rocm-ernic's P4 series"
    fi
    [ "${STAGE}" = installed ] || loaded_module ionic "${file_src}"
fi

# ── ernic_p4 ──────────────────────────────────────────────────────────────

p4_installed=0
if installed_module ernic_p4; then
    p4_installed=1
    p4_src=${file_src}
fi

if [ "${STAGE}" = live ]; then
    # ── every emulated NIC offers shared receive queues ───────────────────
    nics=0
    for vendor in "${SYSFS}"/class/infiniband/*/device/vendor; do
        [ -r "${vendor}" ] || continue
        pci=${vendor%/vendor}
        tuple="$(cat "${pci}/vendor"):$(cat "${pci}/device" 2>/dev/null)"
        tuple="${tuple}:$(cat "${pci}/subsystem_vendor" 2>/dev/null)"
        tuple="${tuple}:$(cat "${pci}/subsystem_device" 2>/dev/null)"
        [ "${tuple}" = "${ERNIC_TUPLE}" ] || continue
        nics=$((nics + 1))
        ib=${pci%/device}
        ib=${ib##*/}
        srq=$(ibv_devinfo -v -d "${ib}" 2>/dev/null < /dev/null |
              awk '$1 == "max_srq:" { print $2; exit }')
        case ${srq} in
            ''|*[!0-9]*)
                fail "${ib}: ibv_devinfo -v reported no max_srq" ;;
            0)
                if [ "${replaced}" = 1 ]; then
                    fail "${ib}: max_srq is 0, so no shared receive queue" \
                         "and no rc_verbs through UCX"
                else
                    fail "${ib}: max_srq is 0 although the loaded driver is" \
                         "the SRQ build, so it is the device that offers" \
                         "none: check the rocm-ernic server serving this" \
                         "guest (<ernic-ref>) is built from the tree the" \
                         "image was"
                fi ;;
            *)
                ok "${ib}: max_srq ${srq}" ;;
        esac
    done
    if [ "${nics}" = 0 ]; then
        fail "no RDMA device with the emulated NIC's PCI identity" \
             "(${ERNIC_TUPLE}): ionic_rdma has not probed one"
    fi

    # ── ernic_p4 loads against the ionic that is loaded ───────────────────
    # Symbol versions are compared at load and nowhere else, so loading it is
    # the only way to ask. It binds nothing unless ionic made a P4 device.
    p4_loaded_here=0
    if [ "${p4_installed}" = 1 ]; then
        if [ ! -d "${SYSFS}/module/ernic_p4" ]; then
            if as_root modprobe ernic_p4 2>"${TMP}/modprobe.err"; then
                p4_loaded_here=1
                ok "ernic_p4: loads against the loaded ionic"
            else
                differs "ernic_p4: does not load against the loaded ionic" \
                        "($(tr '\n' ' ' < "${TMP}/modprobe.err" | sed 's/ *$//')). It was" \
                        "built against the baked SRQ + P4 ionic"
            fi
        fi
        if [ -d "${SYSFS}/module/ernic_p4" ]; then
            loaded_module ernic_p4 "${p4_src}"
        fi
    fi

    aux=""
    for candidate in "${SYSFS}"/bus/auxiliary/devices/ionic.p4.*; do
        [ -e "${candidate}" ] || continue
        aux=${candidate##*/}
        break
    done
    # Loaded here to ask, with no P4 device for it to bind: take it out
    # again. Left in, it holds ionic, which then cannot be unloaded, and a
    # guest whose stack offers no P4 engine did not have it loaded before.
    if [ "${p4_loaded_here}" = 1 ] && [ -z "${aux}" ]; then
        if ! as_root modprobe -r ernic_p4 2>/dev/null; then
            note "ernic_p4: loaded by this check and could not be unloaded" \
                 "again; it stays loaded and holds ionic"
        fi
    fi
    want_node=0
    case ${EXPECT_P4} in
        device)
            want_node=1
            if [ -n "${aux}" ]; then
                ok "ionic created the P4 auxiliary device ${aux}"
            else
                differs "the device offers this guest the P4 engine and" \
                        "ionic created no P4 auxiliary device: the loaded" \
                        "ionic is not the P4 build"
            fi ;;
        auto)
            if [ -n "${aux}" ]; then
                want_node=1
                ok "ionic created the P4 auxiliary device ${aux}"
            else
                note "ionic created no P4 auxiliary device, as when the" \
                     "device offers this guest no P4 engine; ${DEVFS}/ernic-p4" \
                     "is not asked for"
            fi ;;
        nodevice)
            note "the device offers this guest no P4 engine; ${DEVFS}/ernic-p4" \
                 "is not asked for" ;;
    esac
    if [ "${want_node}" = 1 ]; then
        # udev makes the node once ernic_p4 has bound, which may be just now.
        left=${ERNIC_DRIVER_NODE_WAIT:-10}
        while [ ! -c "${DEVFS}/ernic-p4" ] && [ "${left}" -gt 0 ]; do
            sleep 1
            left=$((left - 1))
        done
        if [ -c "${DEVFS}/ernic-p4" ]; then
            ok "${DEVFS}/ernic-p4 exists"
        else
            fail "${DEVFS}/ernic-p4 does not exist: ernic_p4 bound no P4 device"
        fi
    fi
fi

# ── the verdict ───────────────────────────────────────────────────────────

if [ "${fails}" = 0 ]; then
    echo "ok: the ionic driver is the SRQ + P4 build the image was baked with (${STAGE})"
    exit 0
fi
if [ "${replaced}" = 1 ] && [ "${unbaked}" = 0 ]; then
    echo "FAIL: the SRQ + P4 ionic driver this image was baked with has been" \
         "replaced. The known cause is a build of ionic-ernic after the bake:" \
         "ernic_guest_setup with its driver build on, as a configure stage" \
         "runs it, installs the base patch series under the same DKMS name" \
         "and version and reloads the modules."
fi
echo "FAIL: ${fails} check(s) failed (${STAGE})"
exit 1
