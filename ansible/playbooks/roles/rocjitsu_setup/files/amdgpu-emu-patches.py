#!/usr/bin/env python3
"""Patch an amdgpu DKMS source tree so the module binds a rocjitsu device.

Usage: amdgpu-emu-patches.py [SRC]    (default: newest /usr/src/amdgpu-*)

Each patch is an exact-string replacement, applied once and skipped when it
is already in place. A patch function returns True when it changed the tree,
False when its change was already there, and None when this driver release
does not need it. A patch whose target is gone fails the run rather than
passing quietly: a module built without it loads and then misbehaves in ways
that name nothing. Prints "patched <name>" for each one it applied, which is
what the calling task keys its changed status on, "already applied <name>"
for one found in place and "not needed <name>" for one this release has no
use for; the calling role records the last two kinds for the image's stamp.
"""

import glob
import re
import sys


class Missing(Exception):
    pass


def read(path):
    with open(path) as f:
        return f.read()


def write(path, content):
    with open(path, "w") as f:
        f.write(content)


def near(content, needles, before=6, after=8):
    """The source around each line naming one of `needles`, numbered.

    For the message of a patch whose target is gone: the driver source is in
    the guest, and what the lines look like now is what says how to port it.
    """
    lines = content.split("\n")
    keep = set()
    for index, line in enumerate(lines):
        if any(needle in line for needle in needles):
            keep.update(range(max(0, index - before),
                              min(len(lines), index + after + 1)))
    out, last = [], None
    for index in sorted(keep):
        if last is not None and index != last + 1:
            out.append("      ...")
        out.append(f"{index + 1:6}: {lines[index]}")
        last = index
    return "\n".join(out) or "      (no line names any of: " + ", ".join(needles) + ")"


def swap(content, old, new, path):
    if old not in content:
        raise Missing(f"target string not found in {path}")
    return content.replace(old, new, 1)


def kfd_atomics(root):
    """Assume PCIe atomics under emu_mode=1.

    The emulated device cannot advertise atomics through the vfio-user PCI
    capability, but KFD requires them to accept the device.
    """
    path = root + "/amd/amdkfd/kfd_device.c"
    old = "kfd->pci_atomic_requested = amdgpu_amdkfd_have_atomics_support(kfd->adev);"
    new = ("kfd->pci_atomic_requested = "
           "amdgpu_amdkfd_have_atomics_support(kfd->adev) || (amdgpu_emu_mode == 1);")
    content = read(path)
    if new in content:
        return False
    write(path, swap(content, old, new, path))
    return True


def ras_vbios(root):
    """Guard the RAS VBIOS capability query against a NULL atom_context.

    The device is exposed with rombar=0, so there is no option ROM and
    amdgpu_device_init leaves adev->mode_info.atom_context NULL. The
    atomfirmware helpers amdgpu_ras_init calls dereference it unconditionally
    and the probe oopses in amdgpu_atom_parse_data_header. Guarded the way
    amdgpu_ras_get_quirks, immediately above it, already guards that field.
    """
    path = root + "/amd/amdgpu/amdgpu_ras.c"
    head = ("static void amdgpu_ras_query_ras_capablity_from_vbios("
            "struct amdgpu_device *adev)\n{\n")
    old = head + "\t/* mem_ecc cap */"
    new = (head + "\tif (!adev->mode_info.atom_context)\n\t\treturn;\n\n"
           "\t/* mem_ecc cap */")
    content = read(path)
    if new in content:
        return False
    write(path, swap(content, old, new, path))
    return True


def kfd_ptrace_gate(root):
    """Fail the KFD ptrace gate closed on kernels 7.2 and newer.

    amdgpu-dkms 7.1.9 predates kernel 7.2, which moved dumpability and the
    exec-time user_ns off mm_struct onto task->exec_state. The replacements
    are not exported, so porting the gate compiles and then fails at modpost.
    Require CAP_SYS_PTRACE unconditionally instead, which only ever denies
    what the in-tree check would have allowed. Version-gated, so <= 7.1 keeps
    the upstream logic. From ROCm/hipObject ci/ernic/patches/amdgpu/0001.

    amdgpu-dkms 7.2.3 (driver release 31.70) no longer open-codes the gate:
    it calls kcl_task_ptrace_dumpable_denied(), a helper in the driver's own
    kernel compatibility layer. That is the driver's port of the same check,
    so there is nothing here to patch, and whether its helper links on this
    kernel is the DKMS build's to say.
    """
    path = root + "/amd/amdkfd/kfd_process_queue_manager.c"
    content = read(path)
    if "LINUX_VERSION_CODE < KERNEL_VERSION(7, 2, 0)" in content:
        return False
    if "kcl_task_ptrace_dumpable_denied(" in content:
        return None
    old = ("\tif (get_dumpable(mm) != SUID_DUMP_USER &&\n"
           "\t    !ns_capable(mm->user_ns, CAP_SYS_PTRACE)) {\n"
           "\t\tmmput(mm);\n"
           "\t\tgoto deny;\n"
           "\t}\n")
    new = ("#if LINUX_VERSION_CODE < KERNEL_VERSION(7, 2, 0)\n"
           + old +
           "#else\n"
           "\tif (!ns_capable(tcred_ns, CAP_SYS_PTRACE)) {\n"
           "\t\tmmput(mm);\n"
           "\t\tgoto deny;\n"
           "\t}\n"
           "#endif\n")
    inc_old = "#include <linux/sched/coredump.h>"
    inc_new = "#include <linux/version.h>\n#include <linux/sched/coredump.h>"
    if old not in content or inc_old not in content:
        # A driver release that dropped the gate needs no patch.
        if "get_dumpable" not in content and "SUID_DUMP_USER" not in content:
            return None
        raise Missing(
            f"target string not found in {path}\n"
            "the source there now reads:\n"
            + near(content, ("get_dumpable", "SUID_DUMP_USER",
                             "CAP_SYS_PTRACE", "exec_state",
                             "linux/sched/coredump.h", "linux/version.h")))
    write(path, content.replace(inc_old, inc_new, 1).replace(old, new, 1))
    return True


PANEL_PROBES = """\
AC_DEFUN([AC_AMDGPU_DRM_DISPLAY_INFO_PANEL_TYPE], [
\tAC_KERNEL_DO_BACKGROUND([
\t\tAC_KERNEL_TRY_COMPILE([
\t\t\t#include <drm/drm_connector.h>
\t\t], [
\t\t\tstruct drm_display_info *display_info = NULL;
\t\t\tdisplay_info->panel_type = DRM_MODE_PANEL_TYPE_LCD;
\t\t], [
\t\t\tAC_DEFINE(HAVE_DRM_DISPLAY_INFO_PANEL_TYPE, 1,
\t\t\t\t[display_info->panel_type is available])
\t\t])
\t])
])

AC_DEFUN([AC_AMDGPU_DRM_MODE_PANEL_TYPE_LCD], [
\tAC_KERNEL_DO_BACKGROUND([
\t\tAC_KERNEL_TRY_COMPILE([
\t\t\t#include <uapi/drm/drm_mode.h>
\t\t], [
\t\t\tint v = DRM_MODE_PANEL_TYPE_LCD;
\t\t\t(void)v;
\t\t], [
\t\t\tAC_DEFINE(HAVE_DRM_MODE_PANEL_TYPE_LCD, 1,
\t\t\t\t[DRM_MODE_PANEL_TYPE_LCD is available])
\t\t])
\t])
])

"""


def panel_type(root):
    """Probe display_info->panel_type and DRM_MODE_PANEL_TYPE_LCD separately.

    amdgpu_dm_set_panel_type() is gated on HAVE_DRM_DISPLAY_INFO_AMD_VSDB,
    whose probe cites a commit that merged at v6.19 -- but the body uses a
    member and a constant that are AMD-downstream only. On any mainline
    kernel >= 6.19 the probe says yes and the build fails on two things that
    are not there. A plain bug in amdgpu, independent of emulation.
    From ROCm/hipObject ci/ernic/patches/amdgpu/0002.
    """
    m4 = root + "/amd/dkms/m4/drm-display-info.m4"
    c = root + "/amd/display/amdgpu_dm/amdgpu_dm_connector.c"
    m4_content = read(m4)
    c_content = read(c)
    if "HAVE_DRM_DISPLAY_INFO_PANEL_TYPE" in m4_content:
        return False

    anchor = "AC_DEFUN([AC_AMDGPU_DRM_DISPLAY_INFO], ["
    call = "\tAC_AMDGPU_DRM_DISPLAY_INFO_AMD_VSDB\n"
    m4_content = swap(m4_content, anchor, PANEL_PROBES + anchor, m4)
    m4_content = swap(
        m4_content, call,
        call + "\tAC_AMDGPU_DRM_DISPLAY_INFO_PANEL_TYPE\n"
               "\tAC_AMDGPU_DRM_MODE_PANEL_TYPE_LCD\n", m4)

    did_old = ("\tif (panel_type == PANEL_TYPE_NONE) {\n"
               "\t\tif (display_info->panel_type == DRM_MODE_PANEL_TYPE_LCD)\n"
               "\t\t\tpanel_type = PANEL_TYPE_LCD;\n"
               "\t\telse if (display_info->panel_type == DRM_MODE_PANEL_TYPE_OLED)\n"
               "\t\t\tpanel_type = PANEL_TYPE_OLED;\n"
               "\t}\n")
    set_old = ("\telse if (link->panel_type == PANEL_TYPE_LCD)\n"
               "\t\tdrm_object_property_set_value(&connector->base,\n"
               "\t\t    adev_to_drm(adev)->mode_config.panel_type_property,\n"
               "\t\t    DRM_MODE_PANEL_TYPE_LCD);\n")
    c_content = swap(
        c_content, did_old,
        "#ifdef HAVE_DRM_DISPLAY_INFO_PANEL_TYPE\n" + did_old + "#endif\n", c)
    c_content = swap(
        c_content, set_old,
        "#ifdef HAVE_DRM_MODE_PANEL_TYPE_LCD\n" + set_old + "#endif\n", c)

    # Both files or neither: every target was found before anything is written.
    write(m4, m4_content)
    write(c, c_content)
    return True


VSDB_PROBE = """\
AC_DEFUN([AC_AMDGPU_DRM_AMD_VSDB_INFO_FREESYNC], [
\tAC_KERNEL_DO_BACKGROUND([
\t\tAC_KERNEL_TRY_COMPILE([
\t\t\t#include <drm/drm_connector.h>
\t\t], [
\t\t\tstruct drm_display_info *display_info = NULL;
\t\t\tdisplay_info->amd_vsdb.freesync_supported = 0;
\t\t], [
\t\t\tAC_DEFINE(HAVE_DRM_AMD_VSDB_INFO_FREESYNC, 1,
\t\t\t\t[display_info->amd_vsdb carries the freesync fields])
\t\t])
\t])
])

"""

VSDB_MEMBERS = ("freesync_supported", "min_frame_rate", "max_frame_rate",
                "freesync_vcp_code")
VSDB_LINE = re.compile(
    r"^[ \t]*vsdb_info->\w+ = connector->display_info\.amd_vsdb\.("
    + "|".join(VSDB_MEMBERS) + r");$")


def amd_vsdb_freesync(root):
    """Probe the freesync members of display_info->amd_vsdb separately.

    The same bug as panel_type, new in amdgpu-dkms 7.2.3. get_amd_vsdb()
    copies four freesync fields out of connector->display_info.amd_vsdb
    under HAVE_DRM_DISPLAY_INFO_AMD_VSDB, whose probe says yes on any
    mainline kernel that has the struct. Mainline's struct drm_amd_vsdb_info
    has replay_mode and version, which the function also reads, and none of
    the four, so the build stops on "has no member named freesync_supported".
    Without them the function reports no FreeSync from the VSDB, which is
    what a kernel without the fields can know. A plain bug in amdgpu,
    independent of emulation.
    """
    m4 = root + "/amd/dkms/m4/drm-display-info.m4"
    c = root + "/amd/display/amdgpu_dm/amdgpu_dm_connector.c"
    m4_content = read(m4)
    c_content = read(c)
    if "HAVE_DRM_AMD_VSDB_INFO_FREESYNC" in m4_content:
        return False

    lines = c_content.split("\n")
    hits = [(i, VSDB_LINE.match(line).group(1))
            for i, line in enumerate(lines) if VSDB_LINE.match(line)]
    if not hits:
        # A driver release that does not read them needs no patch.
        if "display_info.amd_vsdb." not in c_content:
            return None
    first, last = (hits[0][0], hits[-1][0]) if hits else (0, 0)
    if (not hits or last - first != len(hits) - 1
            or sorted(name for _, name in hits) != sorted(VSDB_MEMBERS)):
        raise Missing(
            f"the four amd_vsdb freesync assignments are not one block in {c}\n"
            "the source there now reads:\n"
            + near(c_content, ("display_info.amd_vsdb.",)))

    anchor = "AC_DEFUN([AC_AMDGPU_DRM_DISPLAY_INFO], ["
    call = "\tAC_AMDGPU_DRM_DISPLAY_INFO_AMD_VSDB\n"
    m4_content = swap(m4_content, anchor, VSDB_PROBE + anchor, m4)
    m4_content = swap(
        m4_content, call,
        call + "\tAC_AMDGPU_DRM_AMD_VSDB_INFO_FREESYNC\n", m4)

    # The lines around them read replay_mode and version, which mainline
    # has, so the function still uses both of its variables without these.
    lines[first:last + 1] = (
        ["#ifdef HAVE_DRM_AMD_VSDB_INFO_FREESYNC"]
        + lines[first:last + 1]
        + ["#endif"])

    # Both files or neither: every target was found before anything is written.
    write(m4, m4_content)
    write(c, "\n".join(lines))
    return True


PATCHES = (kfd_atomics, ras_vbios, kfd_ptrace_gate, panel_type,
           amd_vsdb_freesync)


def main():
    if len(sys.argv) > 1:
        root = sys.argv[1]
    else:
        trees = sorted(glob.glob("/usr/src/amdgpu-*"))
        if not trees:
            sys.exit("no amdgpu DKMS source found in /usr/src")
        root = trees[-1]

    for patch in PATCHES:
        try:
            applied = patch(root)
        except (Missing, OSError) as err:
            sys.exit(f"{patch.__name__}: {err}")
        word = {True: "patched", False: "already applied",
                None: "not needed"}[applied]
        print(f"{word} {patch.__name__}")


if __name__ == "__main__":
    main()
