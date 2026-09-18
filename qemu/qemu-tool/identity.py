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
