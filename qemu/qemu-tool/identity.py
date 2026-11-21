"""How a QEMU process is marked as one qemu-tool started.

`identity_args()` gives every guest a `-name` and a `-uuid` derived from
the command that started it and the VM's name; `match_identity()` derives
the UUID again from a running process's name, which is how `list` tells
its own guests from any other QEMU. `guest_name_args()` is separate: it
hands the guest the name the host calls it by.
"""

from __future__ import annotations

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
