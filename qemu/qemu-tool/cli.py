from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from . import __version__
from .caps import probe_caps
from .config import VMConfig
from .compose import _DEFAULT_STACK, _STACKS, run as compose_run
from .config import _DEFAULT_IMAGES
from .gen_vm import run as gen_vm_run
from .libvirt_xml import LibvirtXml
from .run_vm import build_command, run as run_vm_run

_UNSET = object()  # sentinel for "flag not provided on CLI"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qemu-tool",
        description="Run and generate QEMU VMs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    shared = _shared_parent()
    sub = parser.add_subparsers(metavar="{run-vm,gen-vm,compose}")

    _add_run_vm(sub, shared)
    _add_gen_vm(sub, shared)
    _add_compose(sub)

    return parser


def _shared_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--vm-name", default=_UNSET, metavar="NAME")
    p.add_argument("--arch", choices=["amd64", "arm64", "riscv64"], default=_UNSET)
    p.add_argument("--vcpus", type=int, default=_UNSET, metavar="N")
    p.add_argument("--vmem", type=int, default=_UNSET, metavar="MiB")
    p.add_argument("--images", type=Path, default=_UNSET, metavar="DIR")
    p.add_argument("--ssh-port", type=int, default=_UNSET, metavar="PORT")
    p.add_argument(
        "--kvm", action=argparse.BooleanOptionalAction, default=_UNSET
    )
    p.add_argument("--qemu-path", default=_UNSET, metavar="PATH")
    p.add_argument(
        "--domain",
        default=None,
        metavar="FILE",
        help="Libvirt domain XML to use as base config ('-' for stdin).",
    )
    return p


def _add_run_vm(
    sub: argparse._SubParsersAction, shared: argparse.ArgumentParser
) -> None:
    p = sub.add_parser(
        "run-vm",
        parents=[shared],
        help="Run a QEMU VM.",
        description="Run a QEMU VM previously created by gen-vm.",
    )
    p.add_argument("--filesystem", default=_UNSET, metavar="DIR")
    p.add_argument("--nvme", default=_UNSET, metavar="VALUE",
                   help="Positive int=count, negative int=null_blk, else literal args.")
    p.add_argument("--nvme-trace", default=_UNSET,
                   metavar="{doorbell,all,EVENT}")
    p.add_argument("--nvme-trace-file", type=Path, default=_UNSET, metavar="FILE")
    p.add_argument("--nvme-lbaf-mask", default=_UNSET, metavar="HEX")
    p.add_argument("--nvme-recreate", action="store_true", default=_UNSET)
    p.add_argument("--pci-testdev", action="store_true", default=_UNSET)
    p.add_argument(
        "--pci-hostdev", action="append", default=None, metavar="BDF[,BDF]",
        help="PCI BDF(s) for VFIO passthrough. May be repeated or comma-separated.",
    )
    p.add_argument("--vram-dev-index", type=int, default=_UNSET, metavar="N")
    p.add_argument("--vram-bar", type=int, default=_UNSET, metavar="N")
    p.add_argument(
        "--vfio-userdev", action="append", default=None, metavar="SOCK[,SOCK]"
    )
    p.add_argument("--pci-mmio-bridge", action="store_true", default=_UNSET)
    p.add_argument("--data-nic-queues", type=int, default=_UNSET, metavar="N")
    p.add_argument("--mcast-group", default=_UNSET, metavar="IP:PORT")
    p.add_argument(
        "--qmp-socket", nargs="?", const="true", default=_UNSET, metavar="PATH",
        help="Enable QMP socket. Optional PATH; bare flag uses auto path.",
    )
    p.add_argument("--no-qemu-guest-agent", action="store_true", default=False)
    p.add_argument("--backing-shared", action="store_true", default=_UNSET)
    p.add_argument(
        "--extra-hostfwd", action="append", default=None,
        metavar="RULE",
        help="Extra hostfwd rule e.g. tcp::9150-:9100. May be repeated.",
    )
    p.add_argument("--dry-run", action="store_true", default=_UNSET)
    p.add_argument(
        "--convert-to-libvirt", nargs="?", const="-", default=None,
        metavar="FILE",
        help=(
            "Emit libvirt domain XML instead of running the VM. "
            "Write to FILE, '-' for stdout, or <vm-name>.xml by default."
        ),
    )
    p.set_defaults(func=_run_vm_cmd)


def _add_compose(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "compose",
        help="Run docker compose for the vfio-user GPU VM stack.",
        description=(
            "Wrapper around 'docker compose' for the vfio-user-vm stack. "
            "Sets VM_NAME and VM_IMAGES_DIR, then passes remaining arguments "
            "directly to docker compose."
        ),
    )
    p.add_argument(
        "--vm-name", default=None, metavar="NAME",
        help="VM name (sets VM_NAME/VM1_NAME). Omit to use the value in .env.",
    )
    p.add_argument(
        "--images", type=Path, default=None, metavar="DIR",
        help="Directory containing VM images (sets VM_IMAGES_DIR). Omit to use the value in .env.",
    )
    p.add_argument(
        "--stack", choices=_STACKS, default=_DEFAULT_STACK,
        help=f"Compose stack to use. Default: {_DEFAULT_STACK}.",
    )
    p.add_argument(
        "compose_args", nargs=argparse.REMAINDER,
        help="Arguments forwarded to docker compose (e.g. up, down, ps, logs).",
    )
    p.set_defaults(func=_compose_cmd)


def _add_gen_vm(
    sub: argparse._SubParsersAction, shared: argparse.ArgumentParser
) -> None:
    p = sub.add_parser(
        "gen-vm",
        parents=[shared],
        help="Generate a new QEMU VM image.",
        description="Create a Ubuntu VM image using cloud-init.",
    )
    p.add_argument("--size", type=int, default=_UNSET, metavar="GB")
    p.add_argument("--release", default=_UNSET,
                   metavar="NAME",
                   help="Ubuntu codename (noble) or version (24.04).")
    p.add_argument("--ssh-key-file", type=Path, default=_UNSET, metavar="FILE")
    p.add_argument("--username", default=_UNSET)
    p.add_argument("--user-id", type=int, default=_UNSET, metavar="UID")
    p.add_argument("--password", default=_UNSET)
    p.add_argument("--packages", default=_UNSET,
                   metavar="FILE",
                   help="Package manifest file, or 'none'.")
    p.add_argument("--force", action="store_true", default=_UNSET)
    p.add_argument("--no-backing", action="store_true", default=_UNSET)
    p.add_argument("--restore-image", action="store_true", default=_UNSET)
    p.add_argument("--backing-file", type=Path, default=_UNSET, metavar="FILE")
    p.add_argument("--ansible-profile", type=Path, default=_UNSET, metavar="FILE")
    p.add_argument("--ca-cert", type=Path, default=_UNSET, metavar="FILE",
                   help="CA certificate to inject into the guest trust store.")
    p.add_argument("--ansible-only", action="store_true", default=_UNSET,
                   help="Skip cloud-init; re-run Ansible against an existing backing image.")
    p.set_defaults(func=_gen_vm_cmd)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _run_vm_cmd(args: argparse.Namespace) -> None:
    cfg = _build_config(args, subcommand="run-vm")

    if args.convert_to_libvirt is not None:
        caps = probe_caps(cfg)
        xml = LibvirtXml.emit(cfg, caps)
        output = args.convert_to_libvirt
        if output == "-":
            print(xml, end="")
        else:
            if output is None or output == _UNSET:
                output = f"{cfg.vm_name}.xml"
            Path(output).write_text(xml)
            print(f"Domain XML written to: {output}")
            print()
            print("To define and start the VM as a system-level libvirt domain:")
            print()
            print(f"  virsh define {output}")
            print(f"  virsh start {cfg.vm_name}")
            print(f"  virsh console {cfg.vm_name}")
            print()
            print("To destroy and undefine:")
            print()
            print(f"  virsh destroy {cfg.vm_name}")
            print(f"  virsh undefine {cfg.vm_name}")
        return

    caps = probe_caps(cfg)
    run_vm_run(cfg, caps)


def _compose_cmd(args: argparse.Namespace) -> None:
    compose_run(args.vm_name, args.images, args.compose_args, stack=args.stack)


def _gen_vm_cmd(args: argparse.Namespace) -> None:
    cfg = _build_config(args, subcommand="gen-vm")
    gen_vm_run(cfg)


# ---------------------------------------------------------------------------
# Config building + merging
# ---------------------------------------------------------------------------

def _build_config(args: argparse.Namespace, subcommand: str) -> VMConfig:
    if args.domain is not None:
        xml_cfg = LibvirtXml.parse(args.domain)
    else:
        xml_cfg = VMConfig()

    cli_overrides = _extract_cli_overrides(args, subcommand)
    return _merge(xml_cfg, cli_overrides)


def _extract_cli_overrides(
    args: argparse.Namespace, subcommand: str
) -> dict[str, Any]:
    """Return {field_name: value} for every CLI flag that was explicitly set."""
    overrides: dict[str, Any] = {}
    ns = vars(args)

    def _take(attr: str, field: str, transform=None) -> None:
        val = ns.get(attr, _UNSET)
        if val is _UNSET:
            return
        overrides[field] = transform(val) if transform else val

    # shared
    _take("vm_name", "vm_name")
    _take("arch", "arch")
    _take("vcpus", "vcpus")
    _take("vmem", "vmem")
    _take("images", "images")
    _take("ssh_port", "ssh_port")
    _take("kvm", "kvm")
    _take("qemu_path", "qemu_path")

    if subcommand == "run-vm":
        _take("filesystem", "filesystem")
        _take("nvme", "nvme")
        _take("nvme_trace", "nvme_trace")
        _take("nvme_trace_file", "nvme_trace_file")
        _take("nvme_lbaf_mask", "nvme_lbaf_mask")
        _take("nvme_recreate", "nvme_recreate")
        _take("pci_testdev", "pci_testdev")
        _take("vram_dev_index", "vram_dev_index")
        _take("vram_bar", "vram_bar")
        _take("pci_mmio_bridge", "pci_mmio_bridge")
        _take("data_nic_queues", "data_nic_queues")
        _take("mcast_group", "mcast_group")
        _take("qmp_socket", "qmp_socket")
        _take("backing_shared", "backing_shared")
        _take("dry_run", "dry_run")

        if ns.get("no_qemu_guest_agent"):
            overrides["qemu_guest_agent"] = False

        # pci_hostdev: list of potentially comma-sep strings -> flat list
        raw_hostdev = ns.get("pci_hostdev")
        if raw_hostdev is not None:
            overrides["pci_hostdev"] = _flatten_csv(raw_hostdev)

        raw_vfio = ns.get("vfio_userdev")
        if raw_vfio is not None:
            overrides["vfio_userdev"] = _flatten_csv(raw_vfio)

        raw_fwd = ns.get("extra_hostfwd")
        if raw_fwd is not None:
            overrides["extra_hostfwd"] = list(raw_fwd)

    elif subcommand == "gen-vm":
        _take("size", "size")
        _take("release", "release")
        _take("ssh_key_file", "ssh_key_file")
        _take("username", "username")
        _take("user_id", "user_id")
        _take("password", "password")
        _take("force", "force")
        _take("no_backing", "no_backing")
        _take("restore_image", "restore_image")
        _take("backing_file", "backing_file")
        _take("ansible_profile", "ansible_profile")
        _take("ca_cert_file", "ca_cert")
        _take("ansible_only", "ansible_only")

        raw_pkg = ns.get("packages", _UNSET)
        if raw_pkg is not _UNSET:
            overrides["packages"] = None if raw_pkg == "none" else raw_pkg

    return overrides


def _merge(base: VMConfig, overrides: dict[str, Any]) -> VMConfig:
    """Return a new VMConfig with override values applied on top of base."""
    import dataclasses
    d = dataclasses.asdict(base)
    for k, v in overrides.items():
        if v is not _UNSET and v is not None or k in (
            # fields that can legitimately be set to None/False
            "filesystem", "nvme", "nvme_trace", "nvme_trace_file",
            "nvme_lbaf_mask", "mcast_group", "qmp_socket", "backing_file",
            "ansible_profile", "packages",
            "qemu_guest_agent",
        ):
            d[k] = v
    return VMConfig(**d)


def _flatten_csv(values: list[str]) -> list[str]:
    result: list[str] = []
    for v in values:
        result.extend(s.strip() for s in v.split(",") if s.strip())
    return result
