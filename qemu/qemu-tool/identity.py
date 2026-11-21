"""How a QEMU process is marked as one qemu-tool started.

`identity_args()` gives every guest a `-name` and a `-uuid` derived from
the command that started it and the VM's name; `match_identity()` derives
the UUID again from a running process's name, which is how `list` tells
its own guests from any other QEMU. `guest_name_args()` is separate: it
hands the guest the name the host calls it by.
"""

from __future__ import annotations

import re
import uuid

# Fixed namespace for deriving per-VM UUIDs. Never change this: the value is
# what lets `qemu-tool list` recognise VMs that older or concurrent versions of
# the tool launched.
NS_QEMU_TOOL = uuid.uuid5(uuid.NAMESPACE_DNS, "qemu-tool.qemu-minimal")

ROLES = ("run-vm", "gen-vm")


def vm_uuid(role: str, vm_name: str) -> str:
    return str(uuid.uuid5(NS_QEMU_TOOL, f"{role}:{vm_name}"))


def identity_args(role: str, vm_name: str) -> list[str]:
    """QEMU options stamping a VM as ours.

    Both options are generic (not machine-specific), so they are accepted on
    every system target including riscv64, where -smbios would not be.
    """
    return [
        "-name", f"guest={vm_name},debug-threads=on",
        "-uuid", vm_uuid(role, vm_name),
    ]


# The architectures whose guests are handed their name. amd64 only: the one
# image that reads it, rocm-golden, is amd64, and qemu-system-riscv64 has
# not always taken -smbios (10.2.1 does).
GUEST_NAME_ARCHES = ("amd64",)


def guest_name_args(arch: str, vm_name: str) -> list[str]:
    """QEMU options telling the guest what the host calls it.

    The name goes in the SMBIOS system serial number, which the guest reads
    as /sys/class/dmi/id/product_serial; qemu-tool-hostname.sh in the
    rocm-golden image makes it the hostname. Only the serial is named, so
    the other type 1 fields keep QEMU's defaults: the manufacturer stays
    "QEMU", which that script requires, and the UUID stays the one -uuid
    gave.

    A comma ends a QEMU option value unless it is doubled.
    """
    if arch not in GUEST_NAME_ARCHES:
        return []
    return ["-smbios", "type=1,serial=" + vm_name.replace(",", ",,")]


# A guest is an AMD machine unless very clearly told otherwise: AGENTS.md,
# "The guest is an AMD machine". Under KVM QEMU gives EVERY CPU model the
# HOST's CPUID vendor (target/i386/host-cpu.c, host_cpu_instance_init), so
# on an Intel host a bare `-cpu EPYC` guest is GenuineIntel family 23, and
# the guest kernel's cpu_supports_p2pdma() (drivers/pci/p2pdma.c) then
# refuses P2PDMA between devices that share no upstream bridge. So run-vm
# states
# the vendor. Nothing may change it but <vm-cpu-vendor> or --cpu-vendor.
AMD_CPU_VENDOR = "AuthenticAMD"
INTEL_CPU_VENDOR = "GenuineIntel"

# QEMU's `vendor` property takes exactly twelve characters
# (target/i386/cpu.c, x86_cpuid_set_vendor), and a comma would end the
# value on -cpu. Space is allowed: Zhaoxin's vendor has two at each end.
CPU_VENDOR_FORM = r"[ -+.-~\-]{12}"


def cpu_vendor_problem(value: str, what: str) -> str | None:
    """Why VALUE is not a CPU vendor run-vm can pass on, or None.

    Empty is allowed and means "state no vendor", which under KVM is the
    host's.
    """
    if value == "" or re.match(CPU_VENDOR_FORM + r"\Z", value):
        return None
    return (
        f"{what} must be empty (the host's vendor) or exactly 12 printable "
        f"ASCII characters with no comma, such as {AMD_CPU_VENDOR} or "
        f"{INTEL_CPU_VENDOR}; got {value!r}")


def cpu_vendor_note(vendor: str, source: str, kvm: bool = True) -> str | None:
    """One line for stderr when a guest is not presented as AMD, else None.

    With no vendor stated and no KVM the EPYC model keeps its own vendor,
    AuthenticAMD: only KVM puts the host's in its place. A caller that
    knows the guest runs without KVM says so, and gets no note for that.
    """
    if vendor == AMD_CPU_VENDOR or (not vendor and not kvm):
        return None
    if vendor:
        shown, where = f"vendor {vendor!r}", "the"
    else:
        shown, where = "the host's CPU vendor", "on a host that is not AMD the"
    return (
        f"note: {source}: the guest is not presented as AMD ({shown}, not "
        f"{AMD_CPU_VENDOR}); {where} guest kernel refuses P2PDMA between "
        "devices that share no upstream bridge.")


# run-vm --convert-to-libvirt writes <cpu mode='host-passthrough'/>, the
# host's CPU, and no vendor setting reaches it: libvirt has no way to state
# a CPUID vendor that differs from the host's short of a qemu:commandline
# passthrough. Printed on every amd64 conversion, on every host: nothing
# here looks at the host to decide.
LIBVIRT_CPU_NOTE = (
    "note: run-vm --convert-to-libvirt: the domain's CPU is the host's "
    "(<cpu mode='host-passthrough'/>), not EPYC with vendor "
    f"{AMD_CPU_VENDOR}; --cpu-vendor and <vm-cpu-vendor> do not reach it. "
    "On a host that is not AMD the guest is not presented as AMD, and the "
    "guest kernel refuses P2PDMA between devices that share no upstream "
    "bridge.")


def match_identity(vm_name: str | None, vm_uuid_str: str | None) -> str | None:
    """Return the role that produced this (name, uuid) pair, else None.

    Recomputing the UUID from the name is what makes identification exact: a
    process that merely looks like ours cannot accidentally match.
    """
    if not vm_name or not vm_uuid_str:
        return None
    try:
        parsed = uuid.UUID(vm_uuid_str)
    except (ValueError, AttributeError, TypeError):
        return None
    for role in ROLES:
        if uuid.uuid5(NS_QEMU_TOOL, f"{role}:{vm_name}") == parsed:
            return role
    return None
