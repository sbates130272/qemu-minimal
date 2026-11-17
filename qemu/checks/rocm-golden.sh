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
