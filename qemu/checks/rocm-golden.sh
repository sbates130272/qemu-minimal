# shellcheck shell=bash
#
# Assertions run inside the "rocm-golden" guest during the build's probe
# boot.  Appended to a "set -eu" bash script, so any failing command fails the
# build.
#
# This boot has neither device -- the NIC exists only when a consumer runs
# rocm-ernic and the GPU only when it runs rocjitsu -- so nothing here can
# assert either comes up.  What it can assert is that everything the consumer
# would otherwise have had to install, patch and build at runtime is present
# and in the state it promised.

# The floor the whole flavour turns on.  ionic_rdma calls ib_umem_get_va, a
# static inline that exists in 7.2.4 and does not in 7.0 or 7.1.13, so a guest
# without it fails at compile time in the consumer's job rather than here.
# Compare the whole version tuple, not major-and-minor separately -- "major >= 6
# and minor >= 18" rejects 7.0, which is the most likely way a correct image
# still looks wrong -- and then assert the symbol, not just the number.
test "$(printf '7.2\n%s\n' "$(uname -r)" | sort -V | head -1)" = "7.2"
grep -q ib_umem_get_va /lib/modules/"$(uname -r)"/build/include/rdma/ib_umem.h
. /etc/os-release

# The toolchain and headers, so both out-of-tree modules can be built here:
# ionic-ernic, which the consuming jobs build because building it is what they
# exist to test, and amdgpu, which this image has already built.
# One name each: command -v with several succeeds if any one is found.
for _tool in gcc make cmake ninja dkms git; do command -v "${_tool}"; done
test -d /lib/modules/"$(uname -r)"/build
python3 -c 'import docutils'
pkg-config --exists libnl-3.0 libnl-route-3.0 libudev libsystemd

# --- the ionic half ------------------------------------------------------

# The RDMA stack ships with the mainline kernel, and this image then replaces
# both halves of it with SRQ-capable builds.
command -v ibv_devinfo
modinfo ionic_rdma > /dev/null
ldconfig -p | grep -q libibverbs

# The SRQ stack, which is the difference between a consumer getting RC and
# getting Unreliable Datagram.  Three separate things are asserted because
# three separate things can go wrong, and only the first is obvious:
#
#   1. the post-provision playbook ran at all,
#   2. the module modprobe would load is the built one and not the in-tree
#      driver it was supposed to displace,
#   3. that module actually implements the verbs.
#
# (3) is why this uses nm.  "strings ionic_rdma.ko | grep -i srq" returns
# around a dozen hits on a module with no SRQ support whatsoever --
# IB_USER_VERBS_CMD_CREATE_SRQ, IONIC_QPT_SRQ, rq_tbl_index_srq_id are BTF
# enumerator and struct-field names from the protocol header -- while a build
# carrying no BTF returns zero for the same grep.  The two together read as
# "the new image has SRQ and the old one did not" when neither did.  The
# symbol table is the only one of the three that answers the question.
test -s /etc/ionic-srq-p4.json
python3 -c 'import json; json.load(open("/etc/ionic-srq-p4.json"))'

# (4) the ionic sources were fetched at a ref that matches the kernel they
# were built against.
#
# Validating the stamp as JSON was all this used to do, which meant the two
# version fields inside it were written, shipped and never read. They drifted:
# a guest built 2026-10-24 records kernel_ref v7.2.4 against built_for_kernel
# 7.2.6-070206-generic, because qemu-minimal's vm-rocm-golden.yml still pins
# ionic_kernel_pin v7.2.4 while this flavour's kernel_ref and rocm-ernic's own
# IONIC_KERNEL_REF both moved to v7.2.6.
#
# major.minor is the contract, not equality: qemu-minimal's driver_ionic.yml
# compares exactly that, and ionic's in-tree API is stable within a stable
# series, so 7.2.4 sources against a 7.2.6 kernel do build and do work. That
# is also why nothing caught the drift -- the guard that exists passes.
#
# So fail on the contract and say the rest out loud. A point-release gap is
# not a defect, but it is the shape of one, and it is invisible unless
# something prints it.
python3 - <<'PY'
import json, os, sys
s = json.load(open("/etc/ionic-srq-p4.json"))
src = str(s.get("kernel_ref", "")).lstrip("v")
built = str(s.get("built_for_kernel", ""))
if not src or not built:
    sys.exit("ERROR: /etc/ionic-srq-p4.json has no kernel_ref/built_for_kernel")
mm = lambda v: ".".join(v.split("-")[0].split(".")[:2])
if mm(src) != mm(built):
    sys.exit(
        f"ERROR: ionic sources at v{src} were built against {built}; "
        f"driver_ionic.yml requires the major.minor to agree"
    )
if src.split("-")[0] != built.split("-")[0]:
    print(
        f"note: ionic sources are v{src} but the kernel is {built} -- same "
        f"series, different point release. Bump ionic_kernel_pin in "
        f"qemu-minimal's ansible/playbooks/vm-rocm-golden.yml to close it."
    )
print(f"ionic: sources v{src}, built for {built}")
PY

IONIC_KO=$(modinfo -n ionic_rdma)
case "${IONIC_KO}" in
    */updates/dkms/*) ;;
    *)
        echo "ERROR: modprobe would load ${IONIC_KO}, not the SRQ-capable" \
             "DKMS module" >&2
        exit 1
        ;;
esac
SRQ_TMP=$(mktemp -d)
case "${IONIC_KO}" in
    *.zst) zstd -qdc "${IONIC_KO}" > "${SRQ_TMP}/m.ko" ;;
    *.xz)  xz -dc "${IONIC_KO}"    > "${SRQ_TMP}/m.ko" ;;
    *.gz)  gzip -dc "${IONIC_KO}"  > "${SRQ_TMP}/m.ko" ;;
    *)     cp "${IONIC_KO}" "${SRQ_TMP}/m.ko" ;;
esac
nm "${SRQ_TMP}/m.ko" | grep -q ionic_create_srq
rm -rf "${SRQ_TMP}"

# The userspace half, in /usr/local over the untouched archive copy.  Both
# halves or neither: with only the kernel one the device advertises an SRQ and
# ibv_create_srq() returns EOPNOTSUPP, which is a more confusing guest than
# one that never claimed to do SRQ at all.
ls /usr/local/lib/libibverbs/libionic-rdmav*.so
ldd "$(command -v ibv_devinfo)" | grep -q '/usr/local/lib/libibverbs'

# providers/ionic, which is what makes the emulated NIC usable through
# libibverbs.  Upstream first shipped it in v61 and resolute packages 61.0, so
# the archive supplies it.  The provider carries the ABI suffix rather than a
# bare name -- libionic-rdmav59.so at 61.0 -- so match on the prefix.
ls /usr/lib/*/libibverbs/libionic*.so
# And it is still the packaged file.  A source build installed over /usr leaves
# this same path in place while orphaning it from dpkg, which is the state this
# flavour deliberately does not create.
dpkg -S /usr/lib/*/libibverbs/libionic*.so > /dev/null

# The stamp ernic_guest_setup reads before deciding to build rdma-core itself.
# No version literal in this file: the stamp has to describe the rdma-core that
# is actually installed, so compare it against dpkg rather than against a copy
# of the pin -- which the pin would satisfy even if the guest carried something
# else entirely.
STAMP=$(cat /usr/local/share/rocm-ernic/provider.stamp)
test "${STAMP#*:}" = none
INSTALLED=$(dpkg-query -W -f='${Version}' rdma-core)
test "${STAMP%%:*}" = "${INSTALLED%%-*}"

# Traffic generators for jobs that exercise the queue pairs.  Name the read and
# write verbs separately: perftest splits across binaries and a partial install
# is the failure worth catching here.
for _tool in ib_send_bw ib_write_bw ib_read_bw ib_send_lat; do
    command -v "${_tool}"
done

# Locked memory, which RDMA needs and this release defaults to 8192 KB.
# Asserted here rather than in provisioning because this is the only boot that
# can: the limits file takes effect at the next login and the systemd drop-in
# at the next boot, so the script that wrote them is running in the one
# session where they do not apply.
#
# This is the shape a consumer gets -- a fresh SSH login on a booted guest --
# and the failure it prevents is every ibv_reg_mr() returning "Cannot allocate
# memory", reported several layers away from anything that says memlock.
test "$(ulimit -l)" = unlimited
grep -q 'DefaultLimitMEMLOCK=infinity' /etc/systemd/system.conf.d/*.conf
# And the unit side of it, which the PAM limit above does not cover: read the
# value systemd is actually running with rather than the file that asked for
# it.  "infinity" is reported as the string, not as a number.
systemctl show -p DefaultLimitMEMLOCK | grep -q 'infinity$'

# --- the rocjitsu half ---------------------------------------------------

# ROCm userspace, from the therock stream rather than the universe copies
# pinned out in provisioning.  Assert the libraries, not rocminfo: it is not in
# the minimal set, the therock runtime package does not carry it, and the
# distro copy is pinned to never-install.
# Matched by glob, not by exact name: the guest installs the component-line
# package (amdrocm-runtime-dev10.1 and friends), because a bare name follows
# whichever therock line is newest and can land outside the root /opt/rocm
# resolves to. This file gets no build-side vars, so it cannot spell the line.
dpkg-query -W -f='${Package} ${Status}\n' 'amdrocm-runtime-dev*' |
    grep -q ' install ok installed$'
dpkg-query -W -f='${Package} ${Status}\n' 'amdrocm-blas-dev*' |
    grep -q ' install ok installed$'
ldconfig -p | grep -q libamdhip64
ldconfig -p | grep -q libhsa-runtime64

# ROCM_PATH and PATH, from /etc/profile.d/rocm.sh. ld.so.conf above covers the
# linker; this covers the shell, which is a different failure and the one
# consumers actually hit -- the therock component directory is versioned, so a
# login session finds no ROCm tooling and no ROCM_PATH without being told.
#
# Asserted through an actual login shell rather than by reading the file: that
# the file exists proves nothing about whether anything sources it.
test -s /etc/profile.d/rocm.sh
LOGIN_ROCM_PATH=$(bash -lc 'echo "${ROCM_PATH}"')
test -n "${LOGIN_ROCM_PATH}"
test -d "${LOGIN_ROCM_PATH}"
# By the PATH entry itself, not by resolving a tool through it: rocminfo and
# friends are not in this guest's minimal set, for the reasons above.
bash -lc 'case ":${PATH}:" in *":${ROCM_PATH}/bin:"*) ;; *) exit 1 ;; esac'
echo "note: login-shell ROCM_PATH=${LOGIN_ROCM_PATH}"

# And the same two values in the shell shape consumers actually use. This probe
# runs over "ssh ... 'bash -s'" -- non-login and non-interactive -- which is
# precisely the session /etc/profile.d does not reach, so what follows asserts
# the published failure rather than a reconstruction of it: ROCm was on the
# login PATH only, and every CI step, Ansible command task and systemd unit saw
# a guest with no ROCm in it. /etc/environment, through pam_env, is what covers
# this; the login assertions above do not imply it.
test -n "${ROCM_PATH:-}"
test "${ROCM_PATH}" = "${LOGIN_ROCM_PATH}"
case ":${PATH}:" in *":${ROCM_PATH}/bin:"*) ;; *) exit 1 ;; esac

# The versionless paths, which third-party tooling and most ROCm documentation
# spell literally and which the therock layout does not otherwise provide.
# Resolution rather than presence: a symlink left pointing at a component
# directory the stream has since renamed satisfies "test -L" and leads nowhere.
test -d /opt/rocm/bin
test -d /opt/rocm/lib
test "$(readlink -f /opt/rocm/bin)" = "$(readlink -f "${ROCM_PATH}/bin")"

# amd-smi, resolved through the non-login PATH above rather than by its path:
# both halves of this are new, and a check that names the file would pass on a
# guest where nothing can find it. rocm-smi is deprecated upstream and absent
# from this stream, so this is the only per-device health tool here.
command -v amd-smi > /dev/null
# Its Python bindings ship outside every import path; provisioning adds a .pth.
python3 -c 'import amdsmi'

# BLAS is asserted by its headers, not by a library: amdrocm-blas-dev is
# headers-only in the therock stream and does not depend on the runtime
# amdrocm-blas, so there is no librocblas.so here.
HIPBLAS_H=""
for h in /opt/rocm/include/hipblas/hipblas.h \
         /opt/rocm/*/include/hipblas/hipblas.h; do
    [ -e "${h}" ] && HIPBLAS_H="${h}"
done
test -n "${HIPBLAS_H}"

# The module modprobe would pick must be the DKMS one, built for the kernel
# this guest actually booted, and it must be a driver with GC 12.1.0 -- which
# is asserted by the firmware it names, since a driver without gfx1250 support
# does not reference gc_12_1_0 at all.
AMDGPU_KO=$(modinfo -n amdgpu)
test -n "${AMDGPU_KO}"
echo "amdgpu.ko: ${AMDGPU_KO}"
modinfo -F firmware "${AMDGPU_KO}" | grep -q 'gc_12_1_0'
modinfo -F firmware "${AMDGPU_KO}" | grep -q 'sdma_7_1_0'
# emu_mode is what rocjitsu passes; a driver without it is the wrong driver.
modinfo -F parm "${AMDGPU_KO}" | grep -q '^emu_mode:'
dkms status amdgpu -k "$(uname -r)" | grep -q 'installed'
case "${AMDGPU_KO}" in
    */updates/dkms/*) ;;
    *)
        echo "error: modprobe would load ${AMDGPU_KO}, not the DKMS module" >&2
        exit 1
        ;;
esac
modinfo -F version "${AMDGPU_KO}"

# Every patch, asserted by a marker unique to its own hunk, in the *source*
# tree -- which is what survives a later "dkms autoinstall" on a new kernel and
# what a consumer rebuilding the module would get.  A driver bump that quietly
# drops one has to fail here and not in the emulated device's early init.
AMDGPU_SRC=$(ls -d /usr/src/amdgpu-* | tail -1)
# KFD atomics: the emulated device cannot advertise PCIe atomics through the
# vfio-user PCI capability, and KFD refuses a device without them.
grep -q 'amdgpu_emu_mode == 1' "${AMDGPU_SRC}/amd/amdkfd/kfd_device.c"
# 0001, the unreviewed ptrace gate.  See
# assets/rocm-golden/patches/amdgpu/README.md.
grep -q 'KERNEL_VERSION(7, 2, 0)' \
    "${AMDGPU_SRC}/amd/amdkfd/kfd_process_queue_manager.c"
# 0002, the panel_type probes.
grep -q 'AC_AMDGPU_DRM_DISPLAY_INFO_PANEL_TYPE' \
    "${AMDGPU_SRC}/amd/dkms/m4/drm-display-info.m4"
grep -q 'AC_AMDGPU_DRM_MODE_PANEL_TYPE_LCD' \
    "${AMDGPU_SRC}/amd/dkms/m4/drm-display-info.m4"
grep -q 'HAVE_DRM_DISPLAY_INFO_PANEL_TYPE' \
    "${AMDGPU_SRC}/amd/display/amdgpu_dm/amdgpu_dm_connector.c"
# 0003, the RAS vbios guard: rocjitsu serves no option ROM, so atom_context is
# NULL and the unguarded query oopses in amdgpu_atom_parse_data_header.
grep -q 'adev->mode_info.atom_context' "${AMDGPU_SRC}/amd/amdgpu/amdgpu_ras.c"

# amdgpu must not autoload -- it is modprobed by hand with the emulation
# parameters once the vfio-user server is serving.  Belt (modprobe.d) and
# braces (kernel cmdline, which also covers a load from the initramfs).
grep -qx 'blacklist amdgpu' /etc/modprobe.d/amdgpu-blacklist.conf
grep -q 'modprobe.blacklist=amdgpu' /proc/cmdline
if lsmod | grep -q '^amdgpu '; then
    echo "ERROR: amdgpu is loaded; it must be modprobed by hand after the" >&2
    echo "       vfio-user server is serving, not autoloaded at boot" >&2
    exit 1
fi

# And absent from the initramfs, not merely blacklisted inside it. The hook in
# provisioning drops it from the staging tree; this is what proves the hook
# ran and kept running across the DKMS rebuilds. Every initramfs on the disk,
# not just the booted one -- a consumer can select any of them from grub.
#
# if/exit, not `! lsinitramfs ... | grep -q ...`: bash never applies set -e to
# a command whose status is inverted with `!`, so the negated form silently
# passes whatever it finds. It did exactly that -- two flavours shipped with
# amdgpu.ko.zst in every initramfs and this check green.
for img in /boot/initrd.img-*; do
    [ -e "${img}" ] || continue
    if lsinitramfs "${img}" | grep -qE '(^|/)amdgpu\.ko(\.[a-z]+)?$'; then
        echo "ERROR: amdgpu.ko is present in ${img}" >&2
        exit 1
    fi
done

# The probe helper is offered, not run: the blacklist above still stands.  The
# same file the rocjitsu flavour installs, from assets/shared -- asserted in
# both so the two cannot drift.
test -x /usr/local/bin/amdgpu-probe
bash -n /usr/local/bin/amdgpu-probe
grep -q 'ip_block_mask=0x7f' /usr/local/bin/amdgpu-probe
grep -q 'vramlimit=1024' /usr/local/bin/amdgpu-probe

# The login hint that goes with it.  Asserted as installed, syntactically
# valid, and -- in this boot, where no device is attached and amdgpu is
# blacklisted -- silent: a hint that fires with no GPU present would nag every
# guest at every login.  The installed name has no dot in it because run-parts,
# which is what pam_motd runs over /etc/update-motd.d, skips files that do.
#
# ROCM_GOLDEN_EXPECT_PROBE is for a caller that runs this with the emulated
# GPU attached.  The right answer there is "nofirmware", not "ready": this
# file asserts further down that the image carries no ip_discovery.bin, and
# with a GPU on the bus and no blob the probe must refuse.  The hint is meant
# to print in that boot, and to send the reader to the host.
test -x /etc/update-motd.d/99-amdgpu-probe
sh -n /etc/update-motd.d/99-amdgpu-probe
_probe_want=${ROCM_GOLDEN_EXPECT_PROBE:-absent}
test "$(/usr/local/bin/amdgpu-probe --status)" = "${_probe_want}"
if [ "${_probe_want}" = absent ]; then
    test -z "$(sudo -n /etc/update-motd.d/99-amdgpu-probe)"
fi
if [ "${_probe_want}" = nofirmware ]; then
    sudo -n /etc/update-motd.d/99-amdgpu-probe |
        grep -q 'qemu-tool quick-start test'
fi

# The firmware guard, which is what stops a login hint walking a user into a
# failed probe: with a GPU on the bus and no discovery blob the hint used to
# say "sudo amdgpu-probe", the probe failed, and amdgpu was left resident
# with nothing bound and no safe way to unload it.  An image whose probe
# lacks the guard must not be published, so it is exercised here rather than
# grepped for.  This boot may have no GPU, so the installed scripts are run
# against a scratch tree through their own test seam: AMDGPU_PROBE_TEST_ROOT
# naming a directory that holds the marker file.  Nothing real is read or
# loaded.  The first assertion is also what makes the second safe -- a copy
# that ignores the seam answers for the real system, fails here, and never
# reaches the run that would load a driver -- and sudo and modprobe are
# stubbed on PATH for that run all the same.
_pt=$(mktemp -d)
touch "${_pt}/.amdgpu-probe-test-root"
mkdir -p "${_pt}/sys/bus/pci/devices/0000:00:01.0" "${_pt}/stub" \
    "${_pt}/usr/local/bin" "${_pt}/lib/firmware/amdgpu"
echo 0x1002 > "${_pt}/sys/bus/pci/devices/0000:00:01.0/vendor"
echo 0x120000 > "${_pt}/sys/bus/pci/devices/0000:00:01.0/class"
ln -s /usr/local/bin/amdgpu-probe "${_pt}/usr/local/bin/amdgpu-probe"
printf '#!/bin/sh\necho called >> "%s/called"\n' "${_pt}" > "${_pt}/stub/sudo"
cp "${_pt}/stub/sudo" "${_pt}/stub/modprobe"
chmod 0755 "${_pt}/stub/sudo" "${_pt}/stub/modprobe"
test "$(AMDGPU_PROBE_TEST_ROOT="${_pt}" /usr/local/bin/amdgpu-probe --status)" = nofirmware
if AMDGPU_PROBE_TEST_ROOT="${_pt}" PATH="${_pt}/stub:${PATH}" \
        /usr/local/bin/amdgpu-probe > /dev/null 2>&1; then
    echo "ERROR: amdgpu-probe did not refuse with no discovery blob staged" >&2
    exit 1
fi
test ! -e "${_pt}/called"
_hint=$(AMDGPU_PROBE_TEST_ROOT="${_pt}" /etc/update-motd.d/99-amdgpu-probe)
case "${_hint}" in
    *'qemu-tool quick-start test'*) ;;
    *)
        echo "ERROR: the login hint does not send a guest with no" >&2
        echo "       discovery blob to the host" >&2
        exit 1
        ;;
esac
case "${_hint}" in
    *'    sudo amdgpu-probe'*)
        echo "ERROR: the login hint offers the probe before the firmware" >&2
        exit 1
        ;;
esac
echo blob > "${_pt}/lib/firmware/amdgpu/ip_discovery.bin"
test "$(AMDGPU_PROBE_TEST_ROOT="${_pt}" /usr/local/bin/amdgpu-probe --status)" = ready
rm -rf "${_pt}"

# Firmware.  Unlike the rocjitsu flavour, this guest carries the generated gap
# set as well as the packaged blobs, so there is nothing left that amdgpu opens
# and the guest lacks -- and that is asserted as an empty list rather than an
# expected-absent one.  Which names the driver actually opens is asked of the
# module rather than listed here: modinfo reports its MODULE_FIRMWARE
# declarations, so a driver bump that adds a name shows up as a missing file
# instead of going unnoticed.
#
# ip_discovery.bin is not in this set and is not a firmware name the module
# declares: amdgpu.discovery=2 reads it by path, and it comes from the
# consumer's own rocjitsu pin.
MISSING=$(modinfo -F firmware "${AMDGPU_KO}" |
    grep -E 'gc_12_1_0|sdma_7_1_0' |
    while read -r fw; do
        [ -n "${fw}" ] || continue
        for dir in /lib/firmware/updates /lib/firmware; do
            if [ -e "${dir}/${fw}" ] || [ -e "${dir}/${fw}.xz" ]; then
                continue 2
            fi
        done
        echo "${fw}"
    done)
if [ -n "${MISSING}" ]; then
    echo "Error: amdgpu opens firmware this image does not carry:" >&2
    echo "${MISSING}" | sed 's/^/  /' >&2
    exit 1
fi
# And the three the generator produced are the three it said it produced.
test -s /lib/firmware/updates/amdgpu/rocjitsu-gap-manifest.json
for fw in $(python3 -c \
    'import json,sys;print(" ".join(json.load(open(sys.argv[1]))["files"]))' \
    /lib/firmware/updates/amdgpu/rocjitsu-gap-manifest.json); do
    test -s "/lib/firmware/updates/amdgpu/${fw}"
done
test ! -e /lib/firmware/updates/amdgpu/ip_discovery.bin

# The place the consumer has to stage it, which exists and is empty.  Both
# halves matter: without the directory the documented "copy it to
# /lib/firmware/amdgpu/" lands nowhere, and with a stale ip_discovery.bin in
# it the guest would silently run one generated by a rocjitsu build that is
# not the one serving the socket.
test -d /lib/firmware/amdgpu
test ! -e /lib/firmware/amdgpu/ip_discovery.bin

# --- the guest itself ----------------------------------------------------

# /dev/kfd and /dev/dri/render* are root:render 0660, and a login user in
# neither group gets a HIP runtime that enumerates no agent and a hipMalloc
# returning hipErrorNoDevice with a healthy KFD node three lines up the log.
id -nG | tr ' ' '\n' | grep -qx render
id -nG | tr ' ' '\n' | grep -qx video

# The flavour's own record, for a consumer reading it from inside the guest,
# and the same facts as prose where someone who has just ssh'd in will see them.
test -s /etc/rocm-golden-guest.json
test -s "${HOME}/WELCOME.md"

# Nothing of a bake's compiler cache.  Both workflows that bake this guest,
# the playbook-test lane and the publish workflow, build the modules through
# ccache, by a system configuration file and by symlinks that shadow the
# compiler in /usr/local/bin; the bake_ccache role removes all of it before
# the image is finished.  These five lines are what stops a published image
# that still carries any of it: the publish workflow runs this check before
# it pushes.  A guest shipping either compiles everything through a cache
# nobody asked for.
test ! -e /etc/ccache.conf
test ! -e /var/tmp/bake-ccache
test ! -e /var/tmp/bake-ccache.tar
test -z "$(find /usr/local/bin -maxdepth 1 -lname /usr/bin/ccache)"
test "$(readlink -f "$(command -v gcc)")" != /usr/bin/ccache

# Nor anything of its git redirection.  The bake fetches the ionic kernel
# sources from a mirror of the stable tree, and for the one fetch that takes
# no repository it does that with a url.*.insteadOf rule in git's system
# configuration, taken out again when the play that needed it ends.  A rule
# left in the image sends a consumer's git somewhere other than the URL they
# typed, and says nothing.  So: no rewrite of any URL at system scope, for
# fetch or for push.  git exits 1 when nothing matches, which includes an
# image with no /etc/gitconfig at all; any other failure is a failure here.
#
# The lines between the two markers are run on their own as well: by
# tests/test_ionic_source_mirror.py against a scratch configuration, and by
# the playbook-test lane after its configure stage, which installs the rule
# in the live guest and removes it again.  Keep them free of anything else,
# and keep the line they end on: the lane looks for it.
# begin: git url rewrites
_git_rewrites=$(git config --system --get-regexp \
    '^url\..*\.(insteadof|pushinsteadof)$') && _git_rc=0 || _git_rc=$?
if [ "${_git_rc}" -gt 1 ]; then
    echo "ERROR: git could not read its system configuration" \
         "(exit ${_git_rc})" >&2
    exit 1
fi
if [ -n "${_git_rewrites}" ]; then
    echo "ERROR: git's system configuration rewrites URLs; the bake's" \
         "mirror rule was not removed:" >&2
    echo "${_git_rewrites}" >&2
    exit 1
fi
echo "git: no URL rewrite at system scope"
# end: git url rewrites

# Headroom for a kernel tree, rdma-core, the ionic-ernic DKMS build and a
# consumer's own build tree.
test "$(df --output=avail -BG / | tail -1 | tr -dc 0-9)" -ge 20

# The shipped netplan has to match the NIC by name, not by the MAC cloud-init
# happened to see at creation.  Three different MACs boot this one image:
# gen-vm's first boot derives 52:54:00:00:08:ae from --ssh-port 2222, this
# probe boot gets QEMU's default 52:54:00:12:34:56 because probe-guest.sh
# passes no mac=, and a consumer's run-vm is back to 08:ae.  So any macaddress:
# pin is wrong for at least one of them, and it surfaces as an addressless NIC
# and an SSH timeout in a consumer's job rather than as anything anyone can act
# on.  qemu-tool writes the name match late in first boot via cloud-init
# write_files defer; this asserts it landed.
sudo -n grep -q 'name: *"\?en\*' /etc/netplan/50-cloud-init.yaml
test -z "$(sudo -n grep -l macaddress /etc/netplan/*.yaml || true)"

# ernic_p4.ko, which this flavour gained when post_playbook moved from
# vm-ernic-srq.yml to vm-ernic-srq-p4.yml. The server image is built with
# -DERNIC_BUILD_P4=ON; without this module `rocm-ernic --p4-program` loads a
# program with no guest-side consumer.
#
# Asserted for the same reason the ionic pair is: the combined playbook builds
# ernic_p4.ko against the same srq+p4 ionic it installs, and a module built
# against one ionic and loaded against another is the symbol-version failure
# this repo spent a week on.
_P4KO=$(modinfo -n ernic_p4)
case "${_P4KO}" in
    */updates/dkms/*) echo "ernic_p4: ${_P4KO}" ;;
    *)
        echo "ERROR: ernic_p4 is ${_P4KO:-absent}, not a DKMS module." \
             "/etc/ionic-srq-p4.json says vm-ernic-srq-p4.yml built it." >&2
        exit 1
        ;;
esac

# The userspace half of the P4 path, which the P4 FP8 bandwidth benchmark runs
# as the ordinary guest user: rocm-ernic's own guest client built from the
# pinned sources, the timed driver beside it, and the program they load.
test -x /usr/local/bin/p4_fp8_client
test -x /usr/local/bin/p4_fp8_bench
test -s /usr/local/share/rocm-ernic/fp8_policy.json
python3 -c 'import json; json.load(open("/usr/local/share/rocm-ernic/fp8_policy.json"))'
# It links the libibverbs the SRQ provider was installed with, as ibv_devinfo
# does; against the archive copy it would not find the device.
ldd /usr/local/bin/p4_fp8_client | grep -q '/usr/local/lib/libibverbs'

# /dev/ernic-p4 is root-only as the driver creates it. This image opens it to
# the ernic-p4 group and no further, so the benchmark needs no sudo. There is
# no node on this boot, so what is asserted is everything that decides its
# mode when there is one: the rule, the group, and the account's membership.
grep -qx 'SUBSYSTEM=="misc", KERNEL=="ernic-p4\*", GROUP="ernic-p4", MODE="0660"' \
    /etc/udev/rules.d/99-ernic-p4.rules
getent group ernic-p4 > /dev/null
id -nG "$(id -un)" | tr ' ' '\n' | grep -qx ernic-p4
echo "ernic_p4: client, program and the ernic-p4 group are in place"

# ionic_rdma has no alias udev can act on -- it binds `auxiliary:ionic.rdma`,
# which exists only after ionic registers it -- so without this file nothing
# loads it and ibv_devinfo reports "0 HCAs found". Checked here because the
# symptom surfaces in a consumer's job rather than in the build.
_conf=/etc/modules-load.d/rocm-ernic-ionic.conf
if [ ! -s "${_conf}" ]; then
    echo "ERROR: ${_conf} is missing. ionic autoloads on its PCI id but" \
         "ionic_rdma does not, so this guest would boot with no verbs" \
         "device." >&2
    exit 1
fi
_want=$(printf 'ionic\nionic_rdma\n')
_got=$(grep -vE '^\s*#|^\s*$' "${_conf}")
if [ "${_got}" != "${_want}" ]; then
    echo "ERROR: ${_conf} should list ionic then ionic_rdma, in that order;" \
         "it has:" >&2
    printf '%s\n' "${_got}" >&2
    exit 1
fi
echo "modules-load.d: ionic, ionic_rdma"

# The rules that name the NIC are qemu-minimal's, not rocm-ernic's. The two
# files have one name and one destination, and the image published as
# 20261117.g80c17f8 carried rocm-ernic's: its Ethernet rule is
# NAME="rocm-ernic%n", %n is the kernel number, and every emulated NIC comes
# up as "...np0", so one NIC per guest is renamed however many it has. Its
# InfiniBand rule takes the first free index, which two udev workers race
# for. qemu-minimal's rules give each device its position among the matching
# PCI devices instead, in both stanzas.
#
# No device is needed to read a file, so this holds on the boot with no NIC.
# Comment lines are dropped first: the file explains %n at length.
#
# tests/test_ernic_identity.py runs the lines between the two markers against
# both files, so keep them free of anything but this one path.
# begin: ernic naming rules
_rules=/etc/udev/rules.d/99-rocm-ernic.rules
if [ ! -s "${_rules}" ]; then
    echo "ERROR: ${_rules} is missing: the ernic devices keep their kernel" \
         "names" >&2
    exit 1
fi
_rules_live=$(grep -v '^[[:space:]]*#' "${_rules}")
case "${_rules_live}" in
    *'rocm-ernic%n'*)
        echo "ERROR: ${_rules} names the Ethernet device rocm-ernic%n, which" \
             "is rocm-ernic0 for every NIC. That is rocm-ernic's copy of the" \
             "rules, not qemu-minimal's" \
             "roles/rocm_ernic_setup/files/99-rocm-ernic.rules." >&2
        exit 1
        ;;
esac
case "${_rules_live}" in
    *'while [ -e /sys/class/infiniband/'*)
        echo "ERROR: ${_rules} numbers the InfiniBand device by the first" \
             "free index, which two udev workers race for. That is" \
             "rocm-ernic's copy of the rules, not qemu-minimal's." >&2
        exit 1
        ;;
esac
case "${_rules_live}" in
    *'NAME="%c"'*) ;;
    *)
        echo "ERROR: ${_rules} does not name the Ethernet device from the" \
             "index its PROGRAM computes (NAME=\"%c\")" >&2
        exit 1
        ;;
esac
# One walk of the PCI devices in each stanza.
if [ "$(printf '%s\n' "${_rules_live}" |
        grep -c 'for d in /sys/bus/pci/devices/\*')" != 2 ]; then
    echo "ERROR: ${_rules} does not index both the Ethernet and the" \
         "InfiniBand device by PCI position" >&2
    exit 1
fi
echo "udev: 99-rocm-ernic.rules indexes both devices by PCI position"
# end: ernic naming rules

# No ionic module in any initramfs, asserted against the FINISHED image.
#
# This runs in the verification boot, which is the only place that sees the
# final state: the DKMS pair is installed by the post-provision playbook and
# the kernel trim runs after that, both later than any provision script. The
# hook that prevents this is rocm_ernic_setup's files/ionic-exclude, in
# qemu-minimal's ansible roles; this is what proves it took.
#
# Why it matters: a stale in-tree ionic.ko in the initramfs loads before any
# rootfs modules.dep is consulted, so ionic_rdma -- built against the patched
# DKMS ionic -- fails on a symbol version mismatch, and nothing in that
# failure names the initramfs. It shipped that way for weeks.
#
# Written as an if/exit rather than `! lsinitramfs ... | grep -q`: a command
# whose status is inverted with `!` never trips `set -e`.
for _img in /boot/initrd.img-*; do
    [ -e "${_img}" ] || continue
    if lsinitramfs "${_img}" | grep -qE '(^|/)(ionic|ionic_rdma|ernic_p4)\.ko(\.[a-z]+)?$'; then
        echo "ERROR: an ionic module is present in ${_img}" >&2
        echo "       the initramfs would load it before the DKMS pair on the" >&2
        echo "       rootfs, and ionic_rdma would fail on a symbol version" >&2
        echo "       mismatch. See /etc/initramfs-tools/hooks/ionic-exclude" >&2
        lsinitramfs "${_img}" | grep -E 'ionic|ernic_p4' >&2 || true
        exit 1
    fi
done
echo "initramfs: no ionic modules in $(ls -1 /boot/initrd.img-* | wc -l) image(s)"

# ---------------------------------------------------------------------------
# SDXI driver and CLI
# ---------------------------------------------------------------------------
test -f /etc/sdxi-guest-ref
_sdxi_ref=$(cat /etc/sdxi-guest-ref)
echo "sdxi: ref=${_sdxi_ref}"

for _m in "sdxi_char:GPL" "sdxi_perf:Dual MIT/GPL"; do
    _name=${_m%%:*}
    _lic=${_m#*:}
    _ko="/lib/modules/$(uname -r)/updates/${_name}.ko"
    test -f "${_ko}"
    modinfo "${_ko}" > /dev/null
    test "$(modinfo -F vermagic "${_ko}" | awk '{print $1}')" = "$(uname -r)"
    test "$(modinfo -F license "${_ko}")" = "${_lic}"
    modprobe --dry-run --show-depends "${_name}" > /dev/null
    echo "sdxi: ${_name} ok (${_lic})"
done

grep -q '^blacklist sdxi_perf$' /etc/modprobe.d/sdxi.conf
echo "sdxi: sdxi_perf blacklisted"

grep -q '^sdxi_char$' /etc/modules-load.d/sdxi.conf
echo "sdxi: sdxi_char autoloaded"

# /proc/config.gz exists only with CONFIG_IKCONFIG_PROC built in; Ubuntu's
# kernels ship the config in /boot instead.
test "$( (zcat /proc/config.gz 2>/dev/null || cat "/boot/config-$(uname -r)") |
    grep -c '^CONFIG_IO_URING=y')" = "1"
echo "sdxi: CONFIG_IO_URING=y"

command -v sdxi-cli > /dev/null
sdxi-cli --help > /dev/null
sdxi-cli --help 2>&1 | grep -q bench
echo "sdxi: sdxi-cli ok (bench subcommand present)"

# ---------------------------------------------------------------------------
# One kernel, and it is the one grub boots
# ---------------------------------------------------------------------------
# The cloud image's release kernel is purged at the end of the bake: no
# module here was built for it, and a guest that fell back to it would come
# up with no ionic, no amdgpu and no SDXI and say nothing about why.
_kver=$(uname -r)

# Installed ("ii"), not merely known to dpkg: a purged package can linger as
# "rc" or "un" and is not a kernel.
_kimages=$(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'linux-image-*' |
    awk '$1 == "ii" { print $2 }')
if [ "$(printf '%s\n' "${_kimages}" | grep -c .)" != 1 ]; then
    echo "ERROR: expected one installed kernel image, found:" >&2
    printf '%s\n' "${_kimages}" >&2
    exit 1
fi
case "${_kimages}" in
    *"${_kver}") ;;
    *)
        echo "ERROR: the installed kernel image is ${_kimages}, but the" \
             "guest is running ${_kver}" >&2
        exit 1
        ;;
esac

# A metapackage would bring a release kernel back on the next upgrade.
_kmeta=$(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' \
    linux-generic linux-virtual linux-image-generic linux-image-virtual \
    linux-headers-generic linux-headers-virtual 2>/dev/null |
    awk '$1 == "ii" { print $2 }')
if [ -n "${_kmeta}" ]; then
    echo "ERROR: kernel metapackages are installed: ${_kmeta}" >&2
    exit 1
fi

# Nothing of another kernel: no module tree, no image in /boot, and no DKMS
# build.
if [ "$(ls /lib/modules)" != "${_kver}" ]; then
    echo "ERROR: /lib/modules holds more than ${_kver}:" >&2
    ls /lib/modules >&2
    exit 1
fi
if [ "$(ls /boot/vmlinuz-*)" != "/boot/vmlinuz-${_kver}" ]; then
    echo "ERROR: /boot holds a kernel other than ${_kver}:" >&2
    ls /boot/vmlinuz-* >&2
    exit 1
fi
test -s "/boot/initrd.img-${_kver}"
_kdkms=$(find /var/lib/dkms -mindepth 3 -maxdepth 3 -type d -name '[0-9]*' \
    ! -name "${_kver}")
if [ -n "${_kdkms}" ]; then
    echo "ERROR: DKMS builds for another kernel remain:" >&2
    printf '%s\n' "${_kdkms}" >&2
    exit 1
fi
# "<module>/<version>, <kernel>, <arch>: installed"; a module that is only
# added has no kernel field.
_kdkms=$(dkms status | awk -F', ' -v k="${_kver}" 'NF >= 3 && $2 != k')
if [ -n "${_kdkms}" ]; then
    echo "ERROR: dkms reports modules for another kernel:" >&2
    printf '%s\n' "${_kdkms}" >&2
    exit 1
fi

# The pin: grub is told which kernel to put first by name, so a newer kernel
# installed later does not take the boot by sorting above this one.
grep -qx "GRUB_TOP_LEVEL=\"/boot/vmlinuz-${_kver}\"" \
    /etc/default/grub.d/99-rocm-ernic-kernel.cfg
grep -qx 'GRUB_DEFAULT=0' /etc/default/grub.d/99-rocm-ernic-kernel.cfg

# And what that comes to. /etc/default/grub is an input; the configuration
# grub reads is the generated one, so the default is resolved against its
# menu the way grub does: the "set default" it would execute, through the
# environment block if it says saved, down the submenus to one entry, whose
# every "linux" line must load this kernel. A one-shot next_entry would
# outrank it for exactly the first boot a consumer makes, so it must be
# unset.
_grubcfg=$(mktemp)
_grubenv=$(mktemp)
# grub.cfg is root's alone on this release. An unreadable or absent
# environment block is an empty one, which is what grub makes of it too.
sudo -n cat /boot/grub/grub.cfg | tee "${_grubcfg}" > /dev/null
test -s "${_grubcfg}"
{ sudo -n grub-editenv /boot/grub/grubenv list 2>/dev/null || true; } |
    tee "${_grubenv}" > /dev/null
python3 - "${_grubcfg}" "${_grubenv}" "${_kver}" <<'GRUBPY'
import re
import sys

cfg_path, env_path, running = sys.argv[1:4]
text = open(cfg_path).read()
env = dict(line.split("=", 1) for line in open(env_path).read().splitlines()
           if "=" in line)


def fail(message):
    sys.exit(f"ERROR: grub: {message}")


if env.get("next_entry"):
    fail(f"a one-shot next_entry is set: {env['next_entry']!r}")

# 00_header writes the one-shot assignment and then the configured one.
defaults = {value for value in
            re.findall(r'^\s*set default="([^"]*)"\s*$', text, re.M)
            if value != "${next_entry}"}
if len(defaults) != 1:
    fail(f"expected one configured default, found {sorted(defaults)}")
spec = defaults.pop()
if spec == "${saved_entry}":
    # An unset saved_entry leaves default empty, which grub takes as 0.
    spec = env.get("saved_entry", "") or "0"
if "$" in spec:
    fail(f"the default is not a literal: {spec!r}")

# The menu: every line ending in "{" opens a block, and "}" alone closes
# one. Only menuentry and submenu blocks are entries.
root = {"kind": "root", "children": []}
stack = [root]
for line in text.splitlines():
    stripped = line.strip()
    if stripped == "}":
        if len(stack) == 1:
            fail("unbalanced braces in grub.cfg")
        stack.pop()
        continue
    if stripped.endswith("{"):
        head = re.match(r"(menuentry|submenu)\s+'((?:[^']|'\\'')*)'", stripped)
        if head is None:
            stack.append({"kind": "other"})
            continue
        ident = re.search(
            r"(?:\$menuentry_id_option|--id)\s+'?([^'\s]+)'?", stripped)
        node = {"kind": head.group(1), "title": head.group(2),
                "id": ident.group(1) if ident else "", "children": [],
                "linux": []}
        owner = next(n for n in reversed(stack) if n["kind"] != "other")
        if owner["kind"] == "menuentry":
            fail(f"an entry nested inside entry {owner['title']!r}")
        owner["children"].append(node)
        stack.append(node)
        continue
    loads = re.match(r"linux(?:16|efi)?\s+(\S+)", stripped)
    if loads:
        owner = next(n for n in reversed(stack) if n["kind"] != "other")
        if owner["kind"] == "menuentry":
            owner["linux"].append(loads.group(1))
if len(stack) != 1:
    fail("unbalanced braces in grub.cfg")

node = root
for part in spec.split(">"):
    if node["kind"] == "menuentry":
        fail(f"the default {spec!r} goes below entry {node['title']!r}")
    entries = node["children"]
    if part.isdigit():
        found = entries[int(part)] if int(part) < len(entries) else None
    else:
        found = next((e for e in entries
                      if part in (e["id"], e["title"])), None)
    if found is None:
        # grub falls back to the first entry; that is luck, not a default.
        fail(f"the default {spec!r} names no entry at {part!r}")
    node = found
if node["kind"] != "menuentry":
    fail(f"the default {spec!r} is the submenu {node['title']!r}")
if not node["linux"]:
    fail(f"the default entry {node['title']!r} loads no kernel")
want = f"vmlinuz-{running}"
wrong = sorted({path for path in node["linux"]
                if path.rsplit("/", 1)[-1] != want})
if wrong:
    fail(f"the default entry {node['title']!r} loads {wrong}, but the "
         f"guest is running {running}")
print(f"grub: default {spec!r} is {node['title']!r}, which loads {want}")
GRUBPY
rm -f "${_grubcfg}" "${_grubenv}"
echo "kernel: ${_kver} is the only kernel installed and the one grub boots"

# ---------------------------------------------------------------------------
# AppArmor loaded its profiles
# ---------------------------------------------------------------------------
# The image published as 20261117.g80c17f8 boots with apparmor.service failed
# and the system "degraded". The base bake removes snapd without purging it
# and deletes /var/lib/snapd; the profile snapd leaves in /etc/apparmor.d
# includes a path under that directory, and the parser stops there:
#
#   AppArmor parser error for /etc/apparmor.d in profile
#   /etc/apparmor.d/usr.lib.snapd.snap-confine.real at line 13: Could not
#   open '/var/lib/snapd/apparmor/snap-confine'
#
# vm-basic.yml now purges the package. The unit and the leftover are both
# asserted: the unit says what this boot did, the file what the next will.
#
# Not "no unit failed". This check also runs on a boot with no device, where
# rocm-ernic-netcfg.service waits 30 s for a NIC that is not there and exits
# 1, by design; which other units fail without a device has not been
# established, so the failed units are printed and only this one is asserted.
#
# tests/test_bake_faults.py runs the lines between the two markers with a
# stub systemctl.
# begin: apparmor
if systemctl is-failed --quiet apparmor.service; then
    echo "ERROR: apparmor.service is failed: a profile in /etc/apparmor.d" \
         "does not load" >&2
    systemctl status --no-pager --lines=20 apparmor.service >&2 || true
    exit 1
fi
_aa_profile=/etc/apparmor.d/usr.lib.snapd.snap-confine.real
_aa_include=/var/lib/snapd/apparmor/snap-confine
if [ -e "${_aa_profile}" ] && [ ! -e "${_aa_include}" ]; then
    echo "ERROR: ${_aa_profile} is left over from a snapd that was removed" \
         "and not purged, and includes ${_aa_include}, which is gone:" \
         "apparmor.service fails on it at the next boot" >&2
    exit 1
fi
echo "apparmor: apparmor.service is not failed"
echo "note: units failed on this boot, if any:"
systemctl --failed --no-legend --plain --no-pager || true
# end: apparmor

# ---------------------------------------------------------------------------
# One rdma-core to build against
# ---------------------------------------------------------------------------
# The image has two rdma-cores. The archive's, under /usr, is a runtime: the
# rdma-core package's udev rules and systemd units, ibv_devinfo, and the
# libraries the archive's perftest, fio and libpcap are linked against.
# rocm-ernic's, built from source into /usr/local with the SRQ-capable ionic
# provider, is the one the loader gives every tool, and the only one with
# headers and link libraries.
#
# It was not the only one. The image carried libibverbs-dev, librdmacm-dev
# and libibumad-dev as well, and the toolchain does not pick one rdma-core:
# gcc reads /usr/local/include ahead of /usr/include, ld searches
# /usr/lib/<multiarch> ahead of /usr/local/lib, and the loader has
# /usr/local/lib first. A build with no flags compiled against one, linked
# the other and ran against the first, and perftest failed to link that way.
# The bake installs none of the three now and purges them from a base that
# has them; this is what says an image is in that state.
#
# Nothing here needs a device: three small programs are compiled and linked,
# and none of them is run. The same lines are
# ansible/playbooks/roles/rocm_ernic_setup/files/rdma-core-check.sh, which
# the bake runs as its last check; tests/test_rdma_dev_packages.py holds the
# two equal and runs them against stand-ins. RDMA_CHECK_ROOT is for that: it
# is prefixed to every path below, and is empty on a guest.
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

# ---------------------------------------------------------------------------
# perftest built with ROCm memory support
# ---------------------------------------------------------------------------
# The archive's perftest has no --use_rocm, so the bake builds the tools from
# a pinned commit against the image's ROCm and installs them under a prefix
# ahead of /usr/bin. The package stays, in /usr/bin, so there are two of each
# tool; what is asserted first is which one a bare name runs, in the three
# shells that have a PATH of their own: this one, which is pam_env's
# /etc/environment, a login shell, and sudo's secure_path.
#
# Nothing here needs a device, and this runs on a boot that has none. The
# tools are asked for their help and for nothing else: perftest prints its
# usage while it is still parsing arguments, before it opens an RDMA device,
# and a binary that carries a HIP kernel registers it at load without
# touching the GPU.
#
# No pin is spelled in this file. The bake wrote what it built to
# /etc/perftest-rocm.json and the pin it was given to
# /etc/rocm-golden-guest.json, and the two are compared.
#
# One thing is spelled here, because it is a rule about the image and not a
# pin: the SIMD level of the host-memory validator, the only code in the
# tools compiled for more than baseline x86-64. perftest's configure takes
# it from the CPU of the machine that builds unless told, and an image must
# not be whatever its bake machine was: the first bake with this build
# stopped because that CPU, QEMU's default model, had no level at all, and
# a build on a guest with the EPYC model came out AVX2. So the stamp has to
# say the role named the level and that it is sse4.2, which every CPU a
# fleet runs on has, and the binary has to agree.
#
# tests/test_perftest_setup.py runs the lines between the two markers
# against stand-ins. PERFTEST_CHECK_ROOT is for that: it is prefixed to the
# two /etc paths and to /usr/bin/ib_send_bw, and is empty on a guest.
# begin: perftest
_pt_root=${PERFTEST_CHECK_ROOT:-}
_pt_stamp="${_pt_root}/etc/perftest-rocm.json"
_pt_golden="${_pt_root}/etc/rocm-golden-guest.json"
if [ ! -s "${_pt_stamp}" ]; then
    echo "ERROR: perftest: no ${_pt_stamp}: the bake did not build perftest" \
         "with ROCm support" >&2
    exit 1
fi
# Eight lines: the prefix, the commit, the ROCm tree, whether the build has
# device code and the dma-buf option, the libibverbs it was linked against,
# the SIMD level of the host-memory validator and whether the build needed
# the _mm_pause workaround. A stamp that does not hold together prints its
# reason and stops the check.
_pt_facts=$(python3 - "${_pt_stamp}" "${_pt_golden}" <<'PTPY'
import json
import re
import sys


def fail(msg):
    print(f"ERROR: perftest: {msg}", file=sys.stderr)
    sys.exit(1)


stamp_path, golden_path = sys.argv[1:3]
try:
    with open(stamp_path) as handle:
        stamp = json.load(handle)
except (OSError, ValueError) as exc:
    fail(f"{stamp_path} is not readable JSON: {exc}")
for key in ("repo", "ref", "commit", "commit_required", "prefix",
            "rocm_path", "configure", "device_code", "dmabuf",
            "libibverbs", "simd", "simd_forced", "mm_pause_workaround"):
    if key not in stamp:
        fail(f"{stamp_path} has no \"{key}\"")
commit = stamp["commit"]
if not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
    fail(f"{stamp_path} records commit {commit!r}, which is not a commit id")
# An image is built from a commit somebody chose, not from wherever a
# branch was that day: the role only skips its check when given no commit.
if stamp["commit_required"] != commit:
    fail(f"perftest was built at {commit} with "
         f"{stamp['commit_required']!r} required: the commit check was off, "
         "or the stamp is not the one this build wrote")
try:
    with open(golden_path) as handle:
        golden = json.load(handle)
except (OSError, ValueError) as exc:
    fail(f"{golden_path} is not readable JSON: {exc}")
if golden.get("perftest_source_pin") != commit:
    fail(f"{golden_path} says the image was baked with perftest_source_pin "
         f"{golden.get('perftest_source_pin')!r}, and {stamp_path} says "
         f"perftest was built at {commit}")
if "--enable-rocm" not in stamp["configure"].split():
    fail(f"the recorded configure line has no --enable-rocm: "
         f"{stamp['configure']}")
for key in ("prefix", "rocm_path", "libibverbs"):
    if not str(stamp[key]).startswith("/"):
        fail(f"{stamp_path} records {key} {stamp[key]!r}")
for key in ("device_code", "dmabuf", "simd_forced", "mm_pause_workaround"):
    if not isinstance(stamp[key], bool):
        fail(f"{stamp_path} records {key} {stamp[key]!r}, not true or false")
# The level the role named, not the one configure found on the bake
# machine's CPU, and the one every fleet's CPUs have.
if not stamp["simd_forced"]:
    fail(f"{stamp_path} says the SIMD level of the host-memory validator, "
         f"{stamp['simd']!r}, was left to the CPU of the machine that "
         "baked the image: perftest_setup_simd was empty")
if stamp["simd"] != "sse4.2":
    fail(f"the host-memory validator was built for {stamp['simd']!r}, and "
         "a published image is built for 'sse4.2': on a CPU without that "
         "level the tools refuse --data_validation on host memory")
print(stamp["prefix"])
print(commit)
print(stamp["rocm_path"])
print("true" if stamp["device_code"] else "false")
print("true" if stamp["dmabuf"] else "false")
print(stamp["libibverbs"])
print(stamp["simd"])
print("true" if stamp["mm_pause_workaround"] else "false")
PTPY
)
{
    read -r _pt_prefix
    read -r _pt_commit
    read -r _pt_rocm
    read -r _pt_device_code
    read -r _pt_dmabuf
    read -r _pt_linked
    read -r _pt_simd
    read -r _pt_mm_pause
} <<PTFACTS
${_pt_facts}
PTFACTS

# Which copy a bare name runs. /usr/bin still has the archive's.
for _pt_tool in ib_send_bw ib_send_lat ib_write_bw ib_write_lat \
                ib_read_bw ib_read_lat; do
    _pt_want="${_pt_prefix}/bin/${_pt_tool}"
    if [ ! -x "${_pt_want}" ]; then
        echo "ERROR: perftest: ${_pt_want} is missing" >&2
        exit 1
    fi
    _pt_here=$(command -v "${_pt_tool}" || true)
    _pt_login=$(bash -lc "command -v ${_pt_tool}" < /dev/null || true)
    _pt_sudo=$(sudo -n sh -c "command -v ${_pt_tool}" < /dev/null || true)
    for _pt_got in "this shell:${_pt_here}" "a login shell:${_pt_login}" \
                   "sudo:${_pt_sudo}"; do
        if [ "${_pt_got#*:}" != "${_pt_want}" ]; then
            echo "ERROR: perftest: ${_pt_tool} resolves to" \
                 "'${_pt_got#*:}' in ${_pt_got%%:*}, not ${_pt_want}: the" \
                 "ROCm build is not the one a bare name runs" >&2
            exit 1
        fi
    done
done

# And the archive's copy is still there: vm-quickstart.yml's RDMA test runs
# /usr/bin/ib_send_bw by that path, so the stock build keeps doing what it
# did whatever the one above turns out to do.
if [ ! -x "${_pt_root}/usr/bin/ib_send_bw" ]; then
    echo "ERROR: perftest: no /usr/bin/ib_send_bw: the archive's perftest," \
         "which the quickstart RDMA test runs by path, is gone" >&2
    exit 1
fi

# The flags the build exists for, by the text usage() prints. The help exits
# non-zero by design, so the output is what is tested.
_pt_offers() {
    _pt_help=$("${_pt_prefix}/bin/$1" --help < /dev/null 2>&1 || true)
    case "${_pt_help}" in
        *"$2"*) ;;
        *)
            echo "ERROR: perftest: ${_pt_prefix}/bin/$1 --help does not" \
                 "offer $2" >&2
            printf '%s\n' "${_pt_help}" | tail -5 >&2
            exit 1
            ;;
    esac
}
for _pt_tool in ib_send_bw ib_write_bw ib_read_bw; do
    _pt_offers "${_pt_tool}" '--use_rocm=<rocm device id>'
    _pt_offers "${_pt_tool}" '--data_validation '
    if [ "${_pt_dmabuf}" = true ]; then
        _pt_offers "${_pt_tool}" '--use_rocm_dmabuf'
    fi
done

# Linked against the image's ROCm, every library found, and libibverbs the
# SRQ-capable build in /usr/local that ibv_devinfo loads too.
_pt_libs=$(ldd "${_pt_prefix}/bin/ib_write_bw")
case "${_pt_libs}" in
    *'not found'*)
        echo "ERROR: perftest: ib_write_bw has a library the loader cannot" \
             "find:" >&2
        printf '%s\n' "${_pt_libs}" >&2
        exit 1
        ;;
esac
_pt_hip=$(printf '%s\n' "${_pt_libs}" |
    awk '$1 ~ /^libamdhip64\.so/ { print $3 }')
if [ -z "${_pt_hip}" ]; then
    echo "ERROR: perftest: ib_write_bw is not linked against libamdhip64" >&2
    exit 1
fi
case "$(readlink -f "${_pt_hip}")" in
    "$(readlink -f "${_pt_rocm}")"/*) ;;
    *)
        echo "ERROR: perftest: ib_write_bw loads ${_pt_hip}, which is not" \
             "under ${_pt_rocm}" >&2
        exit 1
        ;;
esac
case "${_pt_libs}" in
    *'=> /usr/local/lib/libibverbs.so'*) ;;
    *)
        echo "ERROR: perftest: ib_write_bw does not load libibverbs from" \
             "/usr/local/lib, where the SRQ-capable provider is" >&2
        printf '%s\n' "${_pt_libs}" >&2
        exit 1
        ;;
esac
# And it is the file the tools were linked against. The block above says
# the image has one rdma-core to build against; this says these tools were
# built against it, which a guest that had the archive's link libraries when
# perftest was compiled would not show there.
_pt_loaded=$(printf '%s\n' "${_pt_libs}" |
    awk '$1 ~ /^libibverbs\.so/ { print $3 }')
if [ "$(readlink -f "${_pt_loaded}")" != "${_pt_linked}" ]; then
    echo "ERROR: perftest: ib_write_bw loads $(readlink -f "${_pt_loaded}")" \
         "and ${_pt_stamp} says it was linked against ${_pt_linked}" >&2
    exit 1
fi

# Device code is in the binary exactly when it registers a fat binary with
# the HIP runtime at load, and the stamp has to say the same.
_pt_syms=$(nm -D --undefined-only "${_pt_prefix}/bin/ib_write_bw")
case "${_pt_syms}" in
    *__hipRegisterFatBinary*) _pt_has_kernels=true ;;
    *) _pt_has_kernels=false ;;
esac
if [ "${_pt_has_kernels}" != "${_pt_device_code}" ]; then
    echo "ERROR: perftest: ${_pt_stamp} says device_code is" \
         "${_pt_device_code}, and ib_write_bw says ${_pt_has_kernels}" >&2
    exit 1
fi

# The stamp says sse4.2; this is the binary's word for it. AVX2 and AVX-512
# are the levels that use the 256- and 512-bit registers, and nothing else
# in the tools is compiled for them, so a build at the level of the image
# has no instruction that names one. One tool is disassembled, which takes
# some tens of milliseconds; objdump is in binutils, with the nm above. The
# disassembly is taken first and counted after: grep exits 1 on none.
_pt_code=$(objdump -d --no-show-raw-insn "${_pt_prefix}/bin/ib_write_bw")
_pt_wide=$(printf '%s\n' "${_pt_code}" | grep -c -E '%[yz]mm[0-9]' || true)
if [ "${_pt_wide}" != 0 ]; then
    echo "ERROR: perftest: ${_pt_stamp} says the host-memory validator was" \
         "built for ${_pt_simd}, and ib_write_bw has ${_pt_wide}" \
         "instructions on ymm or zmm registers: it was compiled for AVX2" \
         "or AVX-512" >&2
    exit 1
fi
echo "perftest: ${_pt_commit} with ROCm in ${_pt_prefix}/bin," \
     "device code ${_pt_device_code}, ahead of the archive's on PATH"
echo "perftest: host-memory validator built for ${_pt_simd}, and no wider" \
     "instruction in ib_write_bw; _mm_pause workaround ${_pt_mm_pause}"
# end: perftest
