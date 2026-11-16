from __future__ import annotations

import argparse
import re
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from . import __version__
from .caps import probe_caps
from .config import VMConfig
from .compose import (
    _DEFAULT_STACK,
    _SOURCE_COMPOSE_ROOT,
    _STACKS,
    _compose_dir,
    run as compose_run,
)
from .ernic_stats import run as ernic_stats_run
from .gen_compose import run as gen_compose_run, stack_for as gen_compose_stack_for
from .gen_vm import run as gen_vm_run
from .libvirt_xml import LibvirtXml
from .inspect import run as inspect_run
from .inspect import ssh_run as inspect_ssh_run
from .check_system import run as check_system_run
from .down_all import discover as _discover_stacks, run as down_all_run, teardown_one
from .quick_start import VERBS as _QUICK_START_VERBS, VARIANTS as _QUICK_START_VARIANTS, run as quick_start_run
from .shutdown import run as shutdown_run
from .list_vms import run as list_vms_run, collect as _list_vms_collect
from . import qcow as _qcow
from . import settings
from .run_vm import run as run_vm_run
from . import summary

_UNSET = object()  # sentinel for "flag not provided on CLI"

# dpkg owns this tree. Generating into it would leave a package file modified
# with no record, so it has to be asked for by name.
_INSTALLED_COMPOSE_ROOT = Path("/usr/share/qemu-tool/compose")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args(_split_compose_argv(argv))
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


# Our own flags on `compose`; everything else on that line is docker's.
_COMPOSE_OURS = frozenset({
    "--images", "--stack", "--stack-dir", "--xml",
})


def _split_compose_argv(argv: list[str] | None) -> list[str]:
    """Insert the "--" that `qemu-tool compose` used to demand of the user.

    compose_args is argparse.REMAINDER, which only starts capturing at the
    first positional, so `compose --stack X --profile metrics up -d` died with
    "unrecognized arguments: --profile" -- there was no way to pass a profile
    without knowing to type a bare "--" first, and the man page's own example
    got it wrong. Claiming only our flags and handing argparse the rest behind
    a separator keeps docker's arguments in the order typed, which matters
    because docker compose is order-sensitive.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens or tokens[0] != "compose":
        return tokens
    ours: list[str] = []
    theirs: list[str] = []
    rest = tokens[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            # A user-typed separator still means "all docker from here", and
            # is not forwarded: docker reads a literal "--" as end-of-options
            # and ignored the profile that followed it.
            theirs.extend(rest[index + 1:])
            break
        if token.split("=", 1)[0] in _COMPOSE_OURS:
            if "=" in token:
                ours.append(token)
                index += 1
            else:
                ours.extend(rest[index:index + 2])
                index += 2
            continue
        if token in ("-h", "--help"):
            ours.append(token)
            index += 1
            continue
        theirs.append(token)
        index += 1
    return ["compose", *ours, *(["--", *theirs] if theirs else [])]


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------

class _HelpFormatter(argparse.HelpFormatter):
    """Wrapped prose that keeps its paragraphs, and examples left as typed."""

    def __init__(self, prog: str, **kwargs: Any) -> None:
        kwargs.setdefault("max_help_position", 30)
        super().__init__(prog, **kwargs)

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        if text.startswith("examples:"):
            return "\n".join(indent + line for line in text.splitlines())
        return "\n\n".join(
            super(_HelpFormatter, self)._fill_text(paragraph, width, indent)
            for paragraph in text.split("\n\n")
        )

    def _format_action(self, action: argparse.Action) -> str:
        text = super()._format_action(action)
        if isinstance(action, argparse._SubParsersAction):
            # Drop the heading row that only repeats the metavar.
            text = text.split("\n", 1)[1]
        return text


class _Parser(argparse.ArgumentParser):
    # add_subparsers() builds each subcommand with type(self), so setting the
    # formatter here reaches every one of them.
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(**kwargs)


_VM_DEFAULTS = {f.name: f.default for f in fields(VMConfig)}

_SEARCH_PATH = (
    "$QEMU_TOOL_FLEET, ./fleet.xml, qemu/stacks/fleet.xml (in a checkout), "
    "$XDG_CONFIG_HOME/qemu-tool/fleet.xml, /etc/qemu-tool/fleet.xml"
)
_LAYERS = "Repeatable: a later file overrides the elements it sets."
_XML_HELP = (
    f"Fleet XML settings file. {_LAYERS} Default: the first that exists of "
    f"{_SEARCH_PATH}."
)
_STACK_NAMES = ", ".join(_STACKS)


def _examples(*lines: str) -> str:
    return "examples:\n" + "\n".join(f"  {line}".rstrip() for line in lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="qemu-tool",
        description=(
            "Generate, run and inspect QEMU guests, and the docker compose "
            "stacks that put emulated GPUs, RDMA NICs and SDXI devices "
            "behind them. Settings come from fleet XML files (--xml)."
        ),
        epilog=(
            "Run 'qemu-tool COMMAND --help' for a command's options and "
            "'qemu-tool man' for the manual."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    shared = _shared_parent()
    # The metavar is a fixed word, not a list of names: the list below it is
    # derived from what is registered, so it cannot go stale.
    sub = parser.add_subparsers(title="commands", metavar="COMMAND")

    # Start here.
    _add_quick_start(sub)
    _add_check(sub)
    # One guest.
    _add_gen_vm(sub, shared)
    _add_run_vm(sub, shared)
    # A stack of guests and their device servers.
    _add_gen_compose(sub)
    _add_compose(sub)
    _add_down(sub)
    # What is running.
    _add_list(sub)
    _add_inspect(sub)
    _add_ssh(sub)
    _add_qcow(sub)
    # Everything else.
    _add_publish_qcow(sub)
    _add_ernic_stats(sub)
    _add_man(sub)

    return parser


def _shared_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)

    g = p.add_argument_group("settings")
    g.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=_XML_HELP + " A flag on this command line outranks the file.",
    )
    g.add_argument(
        "--domain", default=None, metavar="FILE",
        help=(
            "Libvirt domain XML to use as the base configuration ('-' for "
            "stdin). The fleet XML and the flags apply on top of it."
        ),
    )

    g = p.add_argument_group("guest")
    g.add_argument(
        "--vm-name", default=_UNSET, metavar="NAME",
        help=(
            "Name of the guest; its disk is <images>/NAME.qcow2 "
            f"(default: {_VM_DEFAULTS['vm_name']})."
        ),
    )
    g.add_argument(
        "--arch", choices=["amd64", "arm64", "riscv64"], default=_UNSET,
        help=f"Guest architecture (default: {_VM_DEFAULTS['arch']}).",
    )
    g.add_argument(
        "--vcpus", type=int, default=_UNSET, metavar="N",
        help=f"Virtual CPUs (default: {_VM_DEFAULTS['vcpus']}).",
    )
    g.add_argument(
        "--vmem", type=int, default=_UNSET, metavar="MiB",
        help=f"Guest RAM in MiB (default: {_VM_DEFAULTS['vmem']}).",
    )
    g.add_argument(
        "--images", type=Path, default=_UNSET, metavar="DIR",
        help=(
            "Directory holding the guest images (default: "
            "/var/lib/qemu-tool/images when writable, else "
            "$XDG_DATA_HOME/qemu-tool/images)."
        ),
    )
    g.add_argument(
        "--ssh-port", type=int, default=_UNSET, metavar="PORT",
        help=(
            "Host port forwarded to the guest's sshd "
            f"(default: {_VM_DEFAULTS['ssh_port']})."
        ),
    )
    g.add_argument(
        "--mac", default=_UNSET, metavar="ADDR",
        help=(
            "Management NIC MAC. Defaults to 52:54:00:00:<hi>:<lo> derived "
            "from --ssh-port. Set it when the guest image pins its netplan "
            "to a specific MAC, as images this repo built before 3969f07 do."
        ),
    )
    g.add_argument(
        "--kvm", action=argparse.BooleanOptionalAction, default=_UNSET,
        help="Use KVM acceleration (default: on).",
    )
    g.add_argument(
        "--qemu-path", default=_UNSET, metavar="PATH",
        help="QEMU binary to run (default: qemu-system-<arch> from PATH).",
    )
    return p


def _add_quick_start(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "quick-start",
        help="Boot the default two-guest stack sized for this host, and test it.",
        description=(
            "Two guests from a published image, each with an emulated "
            "gfx1250 GPU, an NVMe, a rocm-ernic RDMA NIC and an SDXI device, "
            "with Prometheus, Grafana and log collection beside them -- then "
            "a GPU test on each and an RDMA test across them.\n\n"
            "Every image is pulled, the guest's included; nothing is built "
            "on this host. The guests are sized from this "
            "machine, and anything still unknown is asked for on stdin; "
            "pass --yes to take every default instead, which is also what "
            "happens when stdin is not a terminal."
        ),
        epilog=_examples(
            "qemu-tool quick-start preflight        # check this host, touch nothing",
            "qemu-tool quick-start                  # pull, boot, configure, test",
            "qemu-tool quick-start --flavor helios up",
            "qemu-tool quick-start --xml qemu/stacks/rocm-golden.xml --xml my-overlay.xml up",
            "qemu-tool quick-start logs -f",
            "qemu-tool quick-start down",
        ),
    )
    p.add_argument(
        "verb", nargs="?", default="all", choices=list(_QUICK_START_VERBS),
        metavar="VERB",
        help=(
            f"One of {', '.join(_QUICK_START_VERBS)} (default: all, which "
            "does the lot). preflight checks this machine and touches "
            "nothing, up stops once the guests are booted, test configures "
            "and tests an already-up stack, report rewrites the report, "
            "logs shows the compose logs, down stops the stack and keeps "
            "the overlays."
        ),
    )

    g = p.add_argument_group("what to run")
    g.add_argument(
        "--flavor",
        dest="variant",
        choices=list(_QUICK_START_VARIANTS),
        default="default",
        metavar="NAME",
        help=(
            f"One of {', '.join(_QUICK_START_VARIANTS)} (default: "
            "%(default)s). default: the rocm-golden fleet, 2 guests, each "
            "with a gfx1250 GPU, an "
            "NVMe, a rocm-ernic NIC and an SDXI device. mi355x: 4 guests, "
            "each with 8 GPUs, 8 ernics and 8 PCIe switches. helios: 1 guest "
            "with 4 gfx1250 GPUs, 8 ernics and an SDXI device."
        ),
    )
    g.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=(
            f"Fleet XML settings file. {_LAYERS} Default: the flavor's own "
            "file, qemu/stacks/rocm-golden.xml for the default flavor."
        ),
    )
    g.add_argument(
        "--clean-overlays", action="store_true", default=False,
        help=(
            "Delete the guest overlay and NVMe qcow2 files before starting, "
            "so the guests boot from a fresh overlay of the backing image. "
            "The backing file itself is kept. Only meaningful with the up or "
            "all verbs."
        ),
    )
    g.add_argument(
        "-f", "--follow", action="store_true", default=False,
        help="Follow log output (only meaningful with the logs verb).",
    )
    g.add_argument(
        "--yes", "-y", action="store_true",
        help="Take the default answer to every prompt without asking.",
    )

    g = p.add_argument_group("where things go")
    g.add_argument(
        "--stack-dir", type=Path, default=None, metavar="DIR",
        help=(
            "Where the stack is generated. Default: "
            "/var/lib/qemu-tool/stacks/quickstart, falling back to "
            "$XDG_STATE_HOME when that is not writable."
        ),
    )
    g.add_argument(
        "--key-dir", type=Path,
        default=None, metavar="DIR",
        help="Where the guest keypair is pulled to. Default: "
             "/var/lib/qemu-tool/quickstart-key, falling back to "
             "$XDG_DATA_HOME when that is not writable.",
    )
    g.add_argument(
        "--images", type=Path, default=None, metavar="DIR",
        help=(
            "Directory for the guest qcow2 files. Overrides <vm-images-dir>; "
            "without it an unwritable default is asked about rather than "
            "failing part way into the pull."
        ),
    )
    g.add_argument(
        "--report-dir", type=Path, default=None, metavar="DIR",
        help="Write a markdown report here (implies the report step).",
    )
    g.add_argument(
        "--repo-root", type=Path, default=None, metavar="DIR",
        help=(
            "The checkout holding ansible/playbooks/vm-quickstart.yml. "
            "Found automatically from a source install; an installed tool "
            "with no checkout brings the guests up and skips the tests."
        ),
    )

    g = p.add_argument_group("sizing and services")
    g.add_argument(
        "--size-from-host", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Derive <vm-vcpus>, <vm-vmem> and <vm-shm-size> from this "
            "machine. On by default; --no-size-from-host uses the settings "
            "file as written."
        ),
    )
    g.add_argument(
        "--metrics", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Bring Prometheus and Grafana up with the stack, with their data "
            "directories beside it. On by default."
        ),
    )
    g.add_argument(
        "--in-network", action="store_true",
        help=(
            "Reach the guests on the compose network rather than on "
            "published host ports, for a caller that is itself a container "
            "attached to that network (the CI lane)."
        ),
    )

    g = p.add_argument_group("tests")
    g.add_argument(
        "--rdma-duration", type=int, default=None, metavar="SECONDS",
        help=(
            "Run ib_send_bw for this many seconds instead of the default "
            "1000 iterations. Useful for a longer soak test."
        ),
    )
    g.add_argument(
        "--nvme-duration", type=int, default=None, metavar="SECONDS",
        help="Run each fio NVMe pass for this many seconds (default: 10).",
    )
    p.set_defaults(func=_quick_start_cmd)


def _add_check(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "check",
        help="Check this host has everything the stacks need. Read-only.",
        description=(
            "Ask every host question the stacks depend on, before anything "
            "has been pulled or written: the tools on PATH, the Docker "
            "daemon and your access to it, KVM, the uid the containers will "
            "run as, which settings file is in force, and whether the images "
            "directory has room. Read-only and needs no sudo. Exits non-zero "
            "if anything would stop a stack starting; warnings do not fail "
            "it. This is the same code quick-start runs in its preflight."
        ),
    )
    p.add_argument(
        "--xml", type=Path, action="append", metavar="FILE",
        help=(
            f"Fleet XML settings file to check against. {_LAYERS} Without "
            f"it the search path is used ({_SEARCH_PATH}), and which file "
            "that found is itself part of the report."
        ),
    )
    p.set_defaults(func=_check_cmd)


def _add_gen_vm(
    sub: argparse._SubParsersAction, shared: argparse.ArgumentParser
) -> None:
    p = sub.add_parser(
        "gen-vm",
        parents=[shared],
        help="Create a guest image: bake one, or overlay a published one.",
        description=(
            "Create a guest disk image. By default an Ubuntu cloud image is "
            "downloaded and provisioned with cloud-init, then optionally "
            "with a package manifest and an Ansible playbook, giving a "
            "backing image and an overlay on it. With --backing-image or "
            "--backing-file nothing is baked: the overlay goes on an image "
            "that already exists. A settings file that names a "
            "<backing-ref> counts as --backing-image; --bake overrides it."
        ),
        epilog=_examples(
            "qemu-tool gen-vm --vm-name myvm",
            "qemu-tool gen-vm --vm-name myvm --backing-image REPO:TAG",
            "qemu-tool gen-vm --xml qemu/stacks/rocm-golden.xml --vm-name rocm-golden-1",
            "",
            "# Bake the image the default fleet publishes, from its <bake-*> elements.",
            "# The key it authorises is the one publish-qcow must be given:",
            "ssh-keygen -t rsa -N '' -f ./image-key/id_rsa",
            "qemu-tool gen-vm --bake --xml qemu/stacks/rocm-golden.xml \\",
            "    --ssh-key-file ./image-key/id_rsa.pub",
        ),
    )

    g = p.add_argument_group("image")
    g.add_argument(
        "--size", type=int, default=_UNSET, metavar="GB",
        help=f"Disk size in GB (default: {_VM_DEFAULTS['size']}).",
    )
    g.add_argument(
        "--release", default=_UNSET, metavar="NAME",
        help=(
            "Ubuntu codename (resolute) or version (26.04) "
            f"(default: {_VM_DEFAULTS['release']})."
        ),
    )
    g.add_argument(
        "--force", action="store_true", default=_UNSET,
        help="Overwrite an image that already exists.",
    )
    g.add_argument(
        "--compact", action=argparse.BooleanOptionalAction, default=_UNSET,
        help="Recompress the backing image after the bake (default: on).",
    )
    g.add_argument(
        "--no-backing", action="store_true", default=_UNSET,
        help="Create one flat image instead of a backing image and an overlay.",
    )

    g = p.add_argument_group("image source")
    g.add_argument(
        "--bake", action="store_true",
        help=(
            "Bake from the cloud image using the fleet XML's <bake-*> "
            "elements, ignoring its <backing-ref>. Without it those "
            "elements are not read."
        ),
    )
    g.add_argument(
        "--backing-image", default=_UNSET, metavar="OCI_REF",
        help=(
            "Pull a published backing qcow2 with oras, then overlay on it. "
            "Same job as the cloud-image download, different source; no "
            "cloud-init and no bake."
        ),
    )
    g.add_argument(
        "--backing-file", type=Path, default=_UNSET, metavar="FILE",
        help="Overlay on this existing image instead of downloading one.",
    )
    g.add_argument(
        "--restore-image", action="store_true", default=_UNSET,
        help=(
            "Recreate the overlay from its backing image, discarding what "
            "the guest has written."
        ),
    )

    g = p.add_argument_group("guest account")
    g.add_argument(
        "--username", default=_UNSET, metavar="NAME",
        help=f"Login to create (default: {_VM_DEFAULTS['username']}).",
    )
    g.add_argument(
        "--user-id", type=int, default=_UNSET, metavar="UID",
        help=f"Uid of that login (default: {_VM_DEFAULTS['user_id']}).",
    )
    g.add_argument(
        "--password", default=_UNSET, metavar="PASSWORD",
        help=f"Its password (default: {_VM_DEFAULTS['password']}).",
    )
    g.add_argument(
        "--ssh-key-file", type=Path, default=_UNSET, metavar="FILE",
        help="Public key the login authorises (default: ~/.ssh/id_rsa.pub).",
    )

    g = p.add_argument_group("provisioning")
    g.add_argument(
        "--packages", default=_UNSET, metavar="FILE",
        help=(
            "Package manifest file, or 'none' (default: the shipped "
            "packages-default)."
        ),
    )
    g.add_argument(
        "--ansible-playbook", type=Path, default=_UNSET, metavar="FILE",
        help="Ansible playbook to run against the image after cloud-init.",
    )
    g.add_argument(
        "--ansible-only", action="store_true", default=_UNSET,
        help="Skip cloud-init; re-run Ansible against an existing backing image.",
    )
    g.add_argument(
        "--ca-cert", type=Path, default=_UNSET, metavar="FILE",
        help="CA certificate to inject into the guest trust store.",
    )
    g.add_argument(
        "--mgmt-tap", action="store_true", default=_UNSET,
        help="Use tap+bridge for the management NIC instead of SLIRP.",
    )
    p.set_defaults(func=_gen_vm_cmd)


def _add_run_vm(
    sub: argparse._SubParsersAction, shared: argparse.ArgumentParser
) -> None:
    p = sub.add_parser(
        "run-vm",
        parents=[shared],
        help="Run a guest created by gen-vm.",
        description=(
            "Run a QEMU guest previously created by gen-vm. This is also "
            "what each qemu container of a compose stack runs."
        ),
        epilog=_examples(
            "qemu-tool run-vm --vm-name myvm",
            "qemu-tool run-vm --vm-name myvm --nvme 2 --no-kvm --dry-run",
            "qemu-tool run-vm --vm-name myvm --vfio-userdev /run/vfu/rocjitsu-1.sock",
            "qemu-tool run-vm --vm-name myvm --convert-to-libvirt myvm.xml",
        ),
    )

    g = p.add_argument_group("CPU and PCI topology")
    g.add_argument(
        "--cpu-sockets", type=int, default=_UNSET, metavar="N",
        help=(
            "Pin the guest socket count. Without it QEMU infers sockets=cpus, "
            "so the guest sees N single-core sockets."
        ),
    )
    g.add_argument(
        "--cpu-phys-bits", default=_UNSET, metavar="N", type=int,
        help=(
            "Physical address bits for the EPYC CPU (default 40 = 1 TiB). "
            "Set to 41 alongside --pci-hole64-size when a large BAR exceeds "
            "the default address space; both must move together."
        ),
    )
    g.add_argument(
        "--pci-hole64-size", default=_UNSET, metavar="SIZE",
        help="Size of the 64-bit PCI hole, e.g. 512G. Needed for many large BARs.",
    )
    # Both halves, because the default is now on: --vfio-user-root-port alone
    # became a flag that could only restate the default, leaving no way to ask
    # for the old topology from the command line at all. The settings file has
    # always been able to; the CLI should not be the weaker of the two.
    g.add_argument("--vfio-user-root-port", action="store_true",
                   default=_UNSET,
                   help="Put each vfio-user device behind its own PCIe root "
                        "port. On by default; a GPU cannot bind amdgpu "
                        "without it.")
    g.add_argument("--no-vfio-user-root-port", action="store_false",
                   dest="vfio_user_root_port", default=_UNSET,
                   help="Keep vfio-user devices on the root bus as "
                        "conventional endpoints. A GPU will not work.")
    g.add_argument(
        "--vfio-user-switch-width", type=int, default=_UNSET, metavar="N",
        help=(
            "Group vfio-user devices under PCIe switches, N per switch, in "
            "--vfio-userdev order. 0 (default) is off."
        ),
    )
    g.add_argument(
        "--pci-mmio-bridge", action="store_true", default=_UNSET,
        help="Add a PCI-to-PCI bridge for an extended MMIO window.",
    )

    g = p.add_argument_group("devices")
    g.add_argument(
        "--vfio-userdev", action="append", default=None, metavar="SOCK[,SOCK]",
        help=(
            "vfio-user device socket to attach. May be repeated or "
            "comma-separated."
        ),
    )
    g.add_argument(
        "--pci-hostdev", action="append", default=None, metavar="BDF[,BDF]",
        help="PCI BDF(s) for VFIO passthrough. May be repeated or comma-separated.",
    )
    g.add_argument(
        "--vram-dev-index", type=int, default=_UNSET, metavar="N",
        help=(
            "1-based index into --pci-hostdev of the GPU whose VRAM BAR is "
            "mapped."
        ),
    )
    g.add_argument(
        "--vram-bar", type=int, default=_UNSET, metavar="N",
        help=f"BAR index of that VRAM (default: {_VM_DEFAULTS['vram_bar']}).",
    )
    g.add_argument(
        "--pci-testdev", action="store_true", default=_UNSET,
        help="Attach QEMU's pci-testdev.",
    )
    g.add_argument(
        "--filesystem", default=_UNSET, metavar="DIR",
        help="Host directory to share with the guest over virtio-9p.",
    )

    g = p.add_argument_group("NVMe")
    g.add_argument("--nvme", default=_UNSET, metavar="VALUE",
                   help="Positive int=count, negative int=null_blk, else literal args.")
    g.add_argument(
        "--nvme-recreate", action="store_true", default=_UNSET,
        help="Recreate the NVMe image files even if they exist.",
    )
    g.add_argument(
        "--nvme-lbaf-mask", default=_UNSET, metavar="HEX",
        help="Bitmask restricting the LBA formats advertised.",
    )
    g.add_argument("--nvme-trace", default=_UNSET,
                   metavar="{doorbell,all,EVENT}",
                   help="Enable QEMU's NVMe trace events for this category.")
    g.add_argument(
        "--nvme-trace-file", type=Path, default=_UNSET, metavar="FILE",
        help="Write the NVMe trace here instead of to stderr.",
    )
    g.add_argument("--nvme-dbbuf-poll-ns", type=int, default=_UNSET, metavar="NS",
                   help="Shadow doorbell poll period in ns (0=off, the default)")
    g.add_argument("--nvme-dbbuf-poll-eventidx", default=_UNSET,
                   metavar="{on,off}",
                   help="Shadow doorbell event-index mode; off removes the "
                        "last host doorbell write on the submission path.")
    g.add_argument("--nvme-vram-dev", default=_UNSET, metavar="ID",
                   help="qdev id of vfio-user device for NVMe P2P (e.g. rocjitsu-4-1)")
    g.add_argument("--nvme-vram-bar", type=int, default=_UNSET, metavar="N",
                   help="BAR index for NVMe vfio-user P2P (default 0)")

    g = p.add_argument_group("networking")
    g.add_argument("--mgmt-tap", action="store_true", default=_UNSET,
                   help="Use tap+bridge for the management NIC instead of SLIRP.")
    g.add_argument(
        "--extra-hostfwd", action="append", default=None,
        metavar="RULE",
        help="Extra hostfwd rule e.g. tcp::9150-:9100. May be repeated.",
    )
    g.add_argument(
        "--data-nic-queues", type=int, default=_UNSET, metavar="N",
        help=(
            "virtio-net queues on the data NIC "
            f"(default: {_VM_DEFAULTS['data_nic_queues']}, single queue)."
        ),
    )
    g.add_argument(
        "--mcast-group", default=_UNSET, metavar="IP:PORT",
        help="Attach a second NIC to this UDP multicast group.",
    )

    g = p.add_argument_group("disk and control")
    g.add_argument(
        "--backing-shared", action="store_true", default=_UNSET,
        help="The backing image is shared with other running guests.",
    )
    g.add_argument(
        "--qmp-socket", nargs="?", const="true", default=_UNSET, metavar="PATH",
        help="Enable QMP socket. Optional PATH; bare flag uses auto path.",
    )
    g.add_argument(
        "--no-qemu-guest-agent", action="store_true", default=False,
        help="Leave out the guest agent channel.",
    )

    g = p.add_argument_group("instead of running")
    g.add_argument(
        "--dry-run", action="store_true", default=_UNSET,
        help="Print the QEMU command and exit.",
    )
    g.add_argument(
        "--convert-to-libvirt", nargs="?", const="-", default=None,
        metavar="FILE",
        help=(
            "Emit libvirt domain XML instead of running the VM. "
            "Write to FILE, '-' for stdout, or <vm-name>.xml by default."
        ),
    )
    p.set_defaults(func=_run_vm_cmd)


def _add_gen_compose(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "gen-compose",
        help="Render a compose stack from fleet XML.",
        description=(
            "Render docker-compose.yml, prometheus.yml and "
            "fleet-inventory.yml for the fleet a fleet XML file describes: "
            "N guests, each with its rocm-ernic NICs, rocjitsu GPUs and "
            "SDXI devices, and the mesh between them.\n\n"
            f"The shipped stacks ({_STACK_NAMES}) are each rendered from "
            "qemu/stacks/<name>.xml into qemu/compose/<name>. An overlay "
            "render needs --output-dir: without it the output lands on the "
            "committed stack named by the first --xml file."
        ),
        epilog=_examples(
            "# Re-render a shipped stack after editing its fleet file, and check it:",
            "qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml",
            "qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml --check",
            "",
            "# Render the default fleet with an overlay, somewhere of your own:",
            "qemu-tool gen-compose --xml qemu/stacks/rocm-golden.xml --xml my-overlay.xml \\",
            "    --output-dir /var/lib/qemu-tool/stacks/mine",
        ),
    )
    p.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=(
            f"Fleet XML settings file. {_LAYERS} Default: a fleet.xml on the "
            "search path, else the stack's own file in qemu/stacks/."
        ),
    )
    p.add_argument(
        "--stack", choices=_STACKS, default=None, metavar="NAME",
        help=(
            f"Stack to generate: {_STACK_NAMES}. Default: the one named "
            "after the first --xml file when that is a shipped stack, else "
            f"{_DEFAULT_STACK}."
        ),
    )
    p.add_argument(
        "--output-dir", type=Path, default=None, metavar="DIR",
        help="Where to write. Default: the resolved stack directory.",
    )
    p.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if regenerating would change anything. Writes nothing.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the generated files to stdout instead of writing them.",
    )
    p.set_defaults(func=_gen_compose_cmd)


def _add_compose(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "compose",
        help="Run docker compose against a stack, as you.",
        description=(
            "Wrapper around 'docker compose' for a stack rendered by "
            "gen-compose. It finds the stack directory, supplies the "
            "variables the stack interpolates (the caller's uid and gid, "
            "the KVM gid, the images directory) through the environment, "
            "warns when the stack is stale against its fleet XML, and "
            "passes everything else on the line to docker compose in the "
            "order typed.\n\n"
            "A rendered stack has its fleet shape baked in, so from inside "
            "its directory no flags are needed to inspect or stop it. The "
            "images directory and the qemu-tool source mount are not baked "
            "in: a fleet with a non-default <vm-images-dir> needs --xml, "
            "--images or VM_IMAGES_DIR on a command that starts it. Flags "
            "of this command are recognised anywhere on the line; '--' "
            "ends them."
        ),
        epilog=_examples(
            "qemu-tool compose --stack rocm-golden --profile metrics up -d",
            "qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mine ps",
            "cd /var/lib/qemu-tool/stacks/mine && qemu-tool compose logs --tail 200",
            "qemu-tool compose --stack rocm-golden down -v",
        ),
    )
    p.add_argument(
        "--stack", choices=_STACKS, default=None, metavar="NAME",
        help=f"Shipped stack to use: {_STACK_NAMES} (default: {_DEFAULT_STACK}).",
    )
    p.add_argument(
        "--stack-dir", type=Path, default=None, metavar="DIR",
        help=(
            "Run a stack rendered by 'gen-compose --output-dir DIR' instead of "
            "a named one. Required for an overlay fleet: the guest count and "
            "names are baked into the generated YAML, not read from the "
            "settings at run time. Mutually exclusive with --stack. Defaults to "
            "$QEMU_TOOL_STACK, or to the current directory when that is "
            "itself a rendered stack."
        ),
    )
    p.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=(
            f"Fleet XML settings file. {_LAYERS} Default for a named stack: "
            f"the first that exists of {_SEARCH_PATH}, else the stack's own "
            "qemu/stacks/<name>.xml. A stack found by its directory reads "
            "none."
        ),
    )
    p.add_argument(
        "--images", type=Path, default=None, metavar="DIR",
        help=(
            "Directory containing the guest images, passed to docker compose "
            "as VM_IMAGES_DIR. Default: <vm-images-dir>."
        ),
    )
    p.add_argument(
        "compose_args", nargs=argparse.REMAINDER, metavar="...",
        help="Arguments forwarded to docker compose (e.g. up, down, ps, logs).",
    )
    p.set_defaults(func=_compose_cmd)


def _add_down(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "down",
        help="Shut guests down cleanly, and tear down their stack.",
        description=(
            "Without a target, cleanly powers off every running guest through "
            "the QEMU guest agent and waits for them to go. With a VM name, "
            "powers off that one guest. With a stack name, powers off that "
            "stack's guests then tears the stack down with `docker compose "
            "down`. With --all, powers off every guest then tears down every "
            "qemu-tool stack you own.\n\n"
            "A stack is identified by its compose project name (the STACK "
            "column in `qemu-tool list`). run-vm execs QEMU, so `docker "
            "compose down` is a power cut -- the guest agent path here is "
            "what keeps guest filesystems clean."
        ),
    )
    p.add_argument(
        "target", nargs="?", default=None, metavar="TARGET",
        help="A VM name to shut down, or a stack name to shut down and tear "
             "down. Default: every running VM.",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Power off all guests then tear down every owned stack.",
    )
    p.add_argument(
        "--all-users", action="store_true",
        help="With --all: include stacks owned by other users. Needs root.",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the teardown confirmation prompt.",
    )
    p.add_argument(
        "--timeout", type=int, default=120, metavar="SECONDS",
        help="How long to wait for guests to power off (default: %(default)s).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without doing it.",
    )
    p.set_defaults(func=_down_cmd)


def _add_list(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="List the QEMU guests running on this host.",
        description=(
            "List every qemu-system process on this node, including those "
            "inside containers, and identify which were started by qemu-tool."
        ),
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit JSON instead of a table (includes image path and full socket paths).",
    )
    p.add_argument(
        "--qemu-tool-only", action="store_true",
        help="Only show VMs started by qemu-tool.",
    )
    p.add_argument(
        "--summary", action="store_true",
        help=(
            "Aggregate the listing per fleet and add a measured load picture "
            "(CPU, memory, disk, RDMA mesh, GPUs, logins). Takes a "
            f"{summary.SAMPLE_SECONDS:.0f}s sample, so it costs about "
            f"{summary.SAMPLE_SECONDS + 2:.0f}s against a 32-VM fleet; plain "
            "`list` is unchanged."
        ),
    )
    p.add_argument(
        "--no-guests", action="store_true",
        help=(
            "With --summary, skip the only collector that contacts a guest "
            "(the logged-in users line, ~2.8s over 32 VMs). Everything else "
            "is host-side. The users line then reads 'not collected' rather "
            "than zero."
        ),
    )
    p.set_defaults(func=_list_cmd)


def _add_inspect(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "inspect",
        help="Report what a running guest has: login, devices, BARs.",
        description=(
            "Report, for each running VM, how to log in to it (port, "
            "username, the key it accepts) and what hardware it has, joining "
            "the host-side command line to what the guest agent answers."
        ),
    )
    p.add_argument(
        "vm", nargs="?", default=None, metavar="VM",
        help="VM to inspect. Same as --vm-name, which still works.",
    )
    p.add_argument(
        "--vm-name", default=None, metavar="NAME",
        help="Filter to a single VM by name. Default: all running VMs.",
    )
    p.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=_XML_HELP,
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit JSON instead of human-readable output.",
    )
    p.add_argument(
        "--all-devices", action="store_true", dest="all_devices",
        help=(
            "Expand collapsed GPU and RNIC lines. "
            "By default, more than one GPU or RNIC is shown as a single "
            "count line; this flag prints every device individually with BARs."
        ),
    )
    p.set_defaults(func=_inspect_cmd)


def _add_ssh(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ssh",
        help="SSH into a running guest.",
        description=(
            "Open an SSH session to a running VM, resolving the port, "
            "username and key the same way `inspect` reports them -- the "
            "username from the image's own sidecar, and an identity only "
            "when the guest accepts a key whose private half is on this "
            "host. Host-key checking is off by default because guests "
            "sharing a backing image share a host key and the fleet reuses "
            "localhost ports.\n\n"
            "Everything after the VM name is passed to ssh, so this tool's "
            "own options go BEFORE it, the same order ssh(1) and docker "
            "exec take."
        ),
        epilog=_examples(
            "qemu-tool ssh rocm-golden-1",
            "qemu-tool ssh rocm-golden-1 -- ibv_devinfo -l",
            "qemu-tool ssh --dry-run rocm-golden-1",
        ),
    )
    def _complete_vm_name(**_kw: object) -> list[str]:
        try:
            return [v["name"] for v in _list_vms_collect() if v.get("name")]
        except Exception:
            return []
    p.add_argument("vm", metavar="VM", help="Name of the running VM.").completer = _complete_vm_name
    p.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=_XML_HELP,
    )
    p.add_argument(
        "--strict-host-key", action="store_true",
        help=(
            "Keep ssh's own host-key checking. Off by default: every guest "
            "from one backing image has the same host key, so strict "
            "checking fails as a matter of course."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the ssh command instead of running it.",
    )
    p.add_argument(
        "ssh_args", nargs=argparse.REMAINDER, metavar="...",
        help="Remaining arguments, passed through to ssh (e.g. a command).",
    )
    p.set_defaults(func=_ssh_cmd)


def _add_qcow(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "qcow",
        help="List, inspect or reset guest overlay images.",
        description=(
            "List, inspect or clean VM overlay images and their NVMe scratch "
            "disks. Exactly one of --list, --state and --clean is required. "
            "The VM must be stopped before --clean."
        ),
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list", action="store_true",
        help="List all VM overlays with virtual size, on-disk usage, delta and backing.",
    )
    group.add_argument(
        "--state", metavar="VM",
        help="Show overlay info (backing, sizes, dirty flag, NVMe disks) for one VM.",
    )
    group.add_argument(
        "--clean", metavar="VM",
        help=(
            "Delete the overlay and NVMe scratch for VM, then recreate the "
            "overlay from the backing image. VM must be stopped."
        ),
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the --clean confirmation prompt.",
    )
    p.add_argument(
        "--nvme", action="store_true",
        help="With --list: show a separate NVMe scratch disk section.",
    )
    p.add_argument(
        "--images", type=Path, default=None, metavar="DIR",
        help=(
            "Images directory. Default: <vm-images-dir> of --xml when given; "
            "otherwise --list scans every known images directory and the "
            "other modes use the default one."
        ),
    )
    p.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=(
            f"Fleet XML settings file naming the images directory. {_LAYERS}"
        ),
    )
    p.set_defaults(func=_qcow_cmd)


def _add_publish_qcow(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "publish-qcow",
        help="Publish a baked guest qcow2 as an OCI artifact.",
        description=(
            "Convert a baked qcow2 to a standalone image, compress it with "
            "zstd and push it with oras, with vm-info.json and the guest "
            "keypair attached as a referrer. This is the artifact gen-vm "
            "--backing-image and quick-start pull. The file given is what is "
            "published; it must not be an overlay."
        ),
        epilog=_examples(
            "# Publish what 'gen-vm --bake --xml qemu/stacks/rocm-golden.xml' baked.",
            "# The keypair is published too: it is the one generated for the",
            "# image and given to that bake as --ssh-key-file ./image-key/id_rsa.pub.",
            "qemu-tool publish-qcow --xml qemu/stacks/rocm-golden.xml \\",
            "    --tag 20261116.gabc1234 --key ./image-key/id_rsa --dry-run",
            "",
            "# The same with nothing read from a file:",
            "qemu-tool publish-qcow --image images/rocm-golden-backing.qcow2 \\",
            "    --ref docker.io/me/my-guest --tag v1 --vm-name rocm-golden \\",
            "    --key ./image-key/id_rsa",
        ),
    )

    g = p.add_argument_group("what to publish")
    g.add_argument(
        "--xml", action="append", type=Path, default=None, metavar="FILE",
        help=(
            "Fleet XML naming the image (see qemu/stacks/rocm-golden.xml): its "
            "<publish-*> and <bake-*> elements become the defaults of the "
            "flags below. Repeatable; later files win. No search path: "
            "without this flag no settings file is read."
        ),
    )
    g.add_argument("--image", type=Path, default=None, metavar="FILE",
                   help="The baked qcow2. Must have no backing file. "
                        "Default with --xml: <vm-images-dir>/<bake-name>-backing.qcow2.")
    g.add_argument("--ref", default=None, metavar="REPO",
                   help="Registry repository, without a tag. "
                        "Default with --xml: <publish-ref>.")
    g.add_argument("--tag", action="append", default=[], metavar="TAG",
                   help="Tag to push. Repeatable; the referrer attaches to the first.")
    g.add_argument("--key", type=Path, default=None, metavar="FILE",
                   help="Private key the guest authorises; FILE.pub must sit "
                        "beside it. It is PUBLISHED, so generate one for the "
                        "image and never pass a personal key. Required: it "
                        "is never taken from a settings file. Refused when "
                        "it is not the key gen-vm baked into the image.")

    g = p.add_argument_group("recorded in vm-info.json")
    g.add_argument("--vm-name", default=None, metavar="NAME",
                   help="Names the qcow2 inside the artifact. "
                        "Default with --xml: <bake-name>.")
    g.add_argument("--flavour", default=None, metavar="NAME",
                   help="What kind of guest this is. "
                        "Default: <publish-flavour>, else rocm-golden.")
    g.add_argument("--username", default=None, metavar="NAME",
                   help="The guest login. Default: <bake-username>, else ubuntu.")
    g.add_argument("--password", default=None, metavar="PASSWORD",
                   help="Recorded in vm-info.json, never in an annotation. "
                        "Default: <bake-password>, else password.")
    g.add_argument("--release", default=None, metavar="NAME",
                   help="Ubuntu codename of the guest. "
                        "Default: <bake-release>, else resolute.")
    g.add_argument("--architecture", default="amd64", metavar="ARCH",
                   help="Guest architecture (default: %(default)s).")
    g.add_argument("--guest-stamp", type=Path, default=None, metavar="FILE",
                   help="A copy of the guest's /etc/rocm-golden-guest.json; "
                        "vm-info.json takes its kernel and pins from it.")
    g.add_argument("--playbook", default=None, metavar="NAME",
                   help="The playbook the guest was baked with, for the "
                        "record. Default: <bake-playbook>.")
    g.add_argument("--commit", default="", metavar="SHA",
                   help="The qemu-minimal commit the guest was baked from.")
    g.add_argument("--source", default="https://github.com/sbates130272/qemu-minimal",
                   metavar="URL",
                   help="Source repository, for the image.source annotation "
                        "(default: %(default)s).")

    g = p.add_argument_group("how it runs")
    g.add_argument("--work-dir", type=Path, default=None, metavar="DIR",
                   help="Where the copy is made (default: beside the image). "
                        "Needs room for the whole disk.")
    g.add_argument("--digest-file", type=Path, default=None, metavar="FILE",
                   help="Write the pushed reference, with its digest, here.")
    g.add_argument("--dry-run", action="store_true",
                   help="Convert and compress, print what would be pushed, push nothing.")
    g.add_argument("--keep", type=Path, default=None, metavar="DIR",
                   help="With --dry-run: keep the compressed image and vm-info.json in DIR.")
    p.set_defaults(func=_publish_qcow_cmd)


def _add_ernic_stats(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ernic-stats",
        help="Serve rocm-ernic stats files as Prometheus metrics.",
        description=(
            "Parse the stats dumps written by 'rocm-ernic -S PATH' and serve "
            "them on /metrics in Prometheus text format. Metric and label "
            "names match rocm-ernic's own prometheus/ernic-exporter, so "
            "ernic-dashboard.json works against this endpoint unmodified. "
            "The generated stacks run this; it is rarely typed by hand."
        ),
    )
    p.add_argument(
        "--stats-dir", type=Path, default=Path("/run/ernic-stats"), metavar="DIR",
        help="Directory of *.stats files (default: %(default)s).",
    )
    p.add_argument(
        "--port", type=int, default=9840, metavar="PORT",
        help="Listen port (default: %(default)s, the upstream exporter's).",
    )
    p.add_argument(
        "--addr", default="0.0.0.0", metavar="ADDR",
        help="Listen address (default: %(default)s).",
    )
    p.set_defaults(func=_ernic_stats_cmd)


def _add_man(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "man",
        help="Show the manual page.",
        description=(
            "Open the bundled manual page with man(1). Only the Debian "
            "package installs it where man finds it on its own: the wheel "
            "ships it as package data, and neither uv nor pipx has any "
            "mechanism for putting a man page on MANPATH, so an installed "
            "tool otherwise carries a manual nothing can read."
        ),
    )
    p.add_argument(
        "--path", action="store_true",
        help="Print the path to the bundled page and exit, without opening it.",
    )
    p.set_defaults(func=_man_cmd)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _qcow_cmd(args: argparse.Namespace) -> None:
    from .config import default_images
    images: Path | None
    if args.images:
        images = args.images
    elif args.xml:
        images = settings.load(args.xml).get("images")
    else:
        images = None  # let list_overlays scan all known dirs

    if args.list:
        _qcow.list_overlays(images, show_nvme=getattr(args, "nvme", False))
    elif args.state:
        _qcow.state(images or default_images(), args.state)
    elif args.clean:
        _qcow.clean(images or default_images(), args.clean, yes=args.yes)


def _publish_qcow_cmd(args: argparse.Namespace) -> None:
    from .publish_qcow import run as publish_qcow_run
    publish_qcow_run(args)


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


def _check_cmd(args: argparse.Namespace) -> None:
    check_system_run(args.xml)


def _down_cmd(args: argparse.Namespace) -> None:
    target = args.target
    if args.all:
        # Shut down all guests, then tear down all owned stacks.
        shutdown_run(None, timeout=args.timeout, dry_run=args.dry_run)
        if not args.dry_run:
            down_all_run(assume_yes=args.yes, dry_run=False,
                         all_users=args.all_users)
        else:
            down_all_run(assume_yes=args.yes, dry_run=True,
                         all_users=args.all_users)
        return
    # Resolve target: is it a stack name or a VM name?
    if target is not None:
        stacks = _discover_stacks()
        stack_names = {s.project for s in stacks}
        if target in stack_names:
            # Gracefully shut down this stack's guests, then tear it down.
            shutdown_run(None, timeout=args.timeout, dry_run=args.dry_run,
                         stack_filter=target)
            if not args.dry_run:
                teardown_one(target, dry_run=False, assume_yes=args.yes)
            else:
                teardown_one(target, dry_run=True, assume_yes=args.yes)
            return
    # VM name (or None for all VMs): graceful shutdown only.
    shutdown_run(target, timeout=args.timeout, dry_run=args.dry_run)


def _quick_start_cmd(args: argparse.Namespace) -> None:
    quick_start_run(
        args.verb,
        args.xml,
        args.stack_dir,
        args.key_dir,
        args.report_dir,
        args.repo_root,
        args.images,
        in_network=args.in_network,
        metrics=args.metrics,
        size_from_host=args.size_from_host,
        assume_yes=args.yes,
        follow=args.follow,
        clean_qcow2=args.clean_overlays,
        rdma_duration=args.rdma_duration,
        nvme_duration=args.nvme_duration,
        variant=args.variant,
    )


def _compose_cmd(args: argparse.Namespace) -> None:
    compose_run(args.images, args.compose_args, stack=args.stack,
                xml=args.xml, stack_dir=args.stack_dir)


def _inspect_cmd(args: argparse.Namespace) -> None:
    inspect_run(
        _one_vm_name(args), args.xml,
        as_json=args.as_json,
        all_devices=args.all_devices,
    )


def _one_vm_name(args: argparse.Namespace) -> str | None:
    """The VM named positionally, by --vm-name, or neither.

    Both spellings are accepted because --vm-name is what every other
    subcommand takes, and a bare name is what you reach for. Giving both and
    disagreeing is a typo, not an intent, so it is an error rather than a
    silent precedence rule nobody can remember.
    """
    positional = getattr(args, "vm", None)
    flagged = getattr(args, "vm_name", None)
    if positional and flagged and positional != flagged:
        raise SystemExit(
            f"Error: VM named twice and differently: {positional!r} "
            f"positionally, {flagged!r} via --vm-name."
        )
    return positional or flagged


def _ssh_cmd(args: argparse.Namespace) -> None:
    # argparse consumes the first "--" itself, so this is usually a no-op.
    # Defensive because when a separator does survive, ssh passes it to the
    # guest's shell as a literal argument and the command fails somewhere
    # that does not mention the separator.
    extra = list(args.ssh_args)
    if extra and extra[0] == "--":
        extra = extra[1:]
    inspect_ssh_run(
        args.vm, args.xml,
        strict_host_key=args.strict_host_key,
        extra=extra,
        dry_run=args.dry_run,
    )


def _man_cmd(args: argparse.Namespace) -> None:
    # as_file, not a bare path: the page is package data, so a zipimported
    # install has no real file until it is extracted, and man(1) cannot read
    # a path inside a wheel.
    import shutil
    import subprocess
    from contextlib import ExitStack
    from importlib.resources import as_file, files

    with ExitStack() as stack:
        page = stack.enter_context(
            as_file(files("qemu_tool").joinpath("man/qemu-tool.1")))
        if not page.is_file():
            sys.exit(f"the bundled manual page is missing at {page}")
        if args.path:
            print(page)
            return
        man = shutil.which("man")
        if man is None:
            sys.exit(
                "man(1) is not installed, so the page cannot be rendered. "
                f"It is at:\n  {page}\nor run `qemu-tool man --path` to print "
                "that path alone."
            )
        # Hand man the file rather than a name: the whole point is that the
        # page is not on MANPATH for a uv, uvx or pipx install.
        raise SystemExit(subprocess.call([man, "-l", str(page)]))


def _list_cmd(args: argparse.Namespace) -> None:
    if args.summary:
        summary.run(as_json=args.as_json,
                    qemu_tool_only=args.qemu_tool_only,
                    guests=not args.no_guests)
        return
    if args.no_guests:
        # Silently ignoring it would teach that plain `list` talks to guests,
        # which is the one property it has.
        sys.exit("Error: --no-guests only applies to --summary "
                 "(plain `list` never contacts a guest).")
    list_vms_run(as_json=args.as_json, qemu_tool_only=args.qemu_tool_only)


def _gen_compose_cmd(args: argparse.Namespace) -> None:
    output_dir = args.output_dir
    stack = args.stack or gen_compose_stack_for(args.xml)
    if output_dir is None:
        try:
            output_dir = _compose_dir(stack)
        except FileNotFoundError:
            # First generation: the stack directory does not exist yet.
            output_dir = _SOURCE_COMPOSE_ROOT / stack
        if output_dir.is_relative_to(_INSTALLED_COMPOSE_ROOT):
            sys.exit(
                f"Error: {output_dir} is owned by the qemu-tool package.\n"
                "Pass --output-dir to generate somewhere writable, or name "
                "that path explicitly if you really mean to modify it."
            )
    gen_compose_run(args.xml, output_dir, check=args.check,
                    dry_run=args.dry_run, stack=stack)


def _ernic_stats_cmd(args: argparse.Namespace) -> None:
    ernic_stats_run(args.stats_dir, args.port, args.addr)


def _gen_vm_cmd(args: argparse.Namespace) -> None:
    if args.bake:
        for flag in ("backing_image", "backing_file", "restore_image"):
            if getattr(args, flag) is not _UNSET:
                sys.exit(
                    f"Error: --bake and --{flag.replace('_', '-')} cannot "
                    "both be set: --bake builds the image from the cloud image."
                )
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
    # Lowest to highest: dataclass defaults, --domain XML, the fleet XML
    # settings, CLI flags.
    load = settings.load_bake if getattr(args, "bake", False) else settings.load
    cfg = _merge(xml_cfg, load(args.xml))
    cfg = _merge(cfg, cli_overrides)
    if "," in cfg.vm_name:
        # A comma terminates a qemu option value, so it corrupts -name, -uuid,
        # -drive file= and -chardev path= alike.
        sys.exit(f"Error: --vm-name must not contain a comma: {cfg.vm_name!r}")
    if cfg.mac is not None:
        # Checked here rather than left to qemu: a malformed MAC makes qemu
        # fail at device-creation time, long after gen-vm has downloaded an
        # image and built a seed, and the error names the device, not the flag.
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", cfg.mac):
            sys.exit(f"Error: --mac is not a MAC address: {cfg.mac!r}")
        if int(cfg.mac[:2], 16) & 1:
            sys.exit(f"Error: --mac has the multicast bit set: {cfg.mac!r}")
    return cfg


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
    _take("mac", "mac")
    _take("kvm", "kvm")
    _take("qemu_path", "qemu_path")

    if subcommand == "run-vm":
        _take("filesystem", "filesystem")
        _take("vfio_user_root_port", "vfio_user_root_port")
        _take("vfio_user_switch_width", "vfio_user_switch_width")
        _take("cpu_sockets", "cpu_sockets")
        _take("cpu_phys_bits", "cpu_phys_bits")
        _take("pci_hole64_size", "pci_hole64_size")
        _take("nvme", "nvme")
        _take("nvme_trace", "nvme_trace")
        _take("nvme_trace_file", "nvme_trace_file")
        _take("nvme_lbaf_mask", "nvme_lbaf_mask")
        _take("nvme_recreate", "nvme_recreate")
        _take("nvme_dbbuf_poll_ns", "nvme_dbbuf_poll_ns")
        _take("nvme_dbbuf_poll_eventidx", "nvme_dbbuf_poll_eventidx")
        _take("nvme_vram_dev", "nvme_vram_dev")
        _take("nvme_vram_bar", "nvme_vram_bar")
        _take("pci_testdev", "pci_testdev")
        _take("vram_dev_index", "vram_dev_index")
        _take("vram_bar", "vram_bar")
        _take("pci_mmio_bridge", "pci_mmio_bridge")
        _take("data_nic_queues", "data_nic_queues")
        _take("mcast_group", "mcast_group")
        _take("qmp_socket", "qmp_socket")
        _take("backing_shared", "backing_shared")
        _take("dry_run", "dry_run")
        _take("mgmt_tap", "mgmt_tap")

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
        _take("compact", "compact")
        _take("no_backing", "no_backing")
        _take("restore_image", "restore_image")
        _take("backing_file", "backing_file")
        _take("backing_image", "backing_image")
        # The two are mutually exclusive, but only within one layer. A fleet's
        # settings file names <backing-ref> for VM 1, and every other VM is then
        # created with --backing-file against the image that fetched -- so an
        # explicit flag has to clear its partner rather than collide with it,
        # the same way CLI outranks the settings file everywhere else.
        if ns.get("backing_file", _UNSET) is not _UNSET:
            overrides["backing_image"] = None
        elif ns.get("backing_image", _UNSET) is not _UNSET:
            overrides["backing_file"] = None
        _take("ansible_playbook", "ansible_playbook")
        _take("ca_cert", "ca_cert_file")
        _take("ansible_only", "ansible_only")
        _take("mgmt_tap", "mgmt_tap")

        raw_pkg = ns.get("packages", _UNSET)
        if raw_pkg is not _UNSET:
            overrides["packages"] = None if raw_pkg == "none" else raw_pkg

    return overrides


def _merge(base: VMConfig, overrides: dict[str, Any]) -> VMConfig:
    """Return a new VMConfig with override values applied on top of base.

    Fleet-only elements (<vm-count>, <vm-name-prefix>, …) present in an XML
    descriptor are silently skipped — they have no VMConfig field.
    """
    import dataclasses
    d = dataclasses.asdict(base)
    for k, v in overrides.items():
        if k not in d:
            continue
        if v is not _UNSET and v is not None or k in (
            # fields that can legitimately be set to None/False
            "filesystem", "nvme", "nvme_trace", "nvme_trace_file",
            "nvme_lbaf_mask", "mcast_group", "qmp_socket",
            "backing_file", "backing_image",
            "ansible_playbook", "packages",
            "qemu_guest_agent",
        ):
            d[k] = v
    return VMConfig(**d)


def _flatten_csv(values: list[str]) -> list[str]:
    result: list[str] = []
    for v in values:
        result.extend(s.strip() for s in v.split(",") if s.strip())
    return result
