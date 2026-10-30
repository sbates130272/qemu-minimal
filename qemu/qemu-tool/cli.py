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
    _GENERATED_STACKS,
    _SOURCE_COMPOSE_ROOT,
    _STACKS,
    _compose_dir,
    run as compose_run,
)
from .envfile import load as load_env_file
from .ernic_stats import run as ernic_stats_run
from .lmcache_stats import run as lmcache_stats_run
from .gen_compose import run as gen_compose_run
from .gen_vm import run as gen_vm_run
from .libvirt_xml import LibvirtXml
from .inspect import run as inspect_run
from .inspect import ssh_run as inspect_ssh_run
from .check_system import run as check_system_run
from .down_all import discover as _discover_stacks, run as down_all_run, teardown_one
from .quick_start import VERBS as _QUICK_START_VERBS, run as quick_start_run
from .shutdown import run as shutdown_run
from .list_vms import run as list_vms_run, collect as _list_vms_collect
from . import qcow as _qcow
from .run_vm import build_command, run as run_vm_run
from . import summary

_UNSET = object()  # sentinel for "flag not provided on CLI"

_DEFAULT_GENERATED_STACK = "vfio-user-ernic-rocjitsu-scale-out"

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
    "--vm-name", "--vm2-name", "--images", "--stack", "--stack-dir", "--env-file",
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

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qemu-tool",
        description="Run and generate QEMU VMs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    shared = _shared_parent()
    # No metavar: argparse derives the list from what is actually registered.
    # The hand-written one had already gone stale -- it still read
    # "...,list,inspect}" after `man` was added, so --help advertised a
    # command set the tool did not have.
    sub = parser.add_subparsers()

    _add_quick_start(sub)
    _add_check(sub)
    _add_down(sub)
    _add_run_vm(sub, shared)
    _add_gen_vm(sub, shared)
    _add_gen_compose(sub)
    _add_compose(sub)
    _add_ernic_stats(sub)
    _add_lmcache_stats(sub)
    _add_list(sub)
    _add_inspect(sub)
    _add_ssh(sub)
    _add_qcow(sub)
    _add_man(sub)

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
        "--mac", default=_UNSET, metavar="ADDR",
        help=(
            "Management NIC MAC. Defaults to 52:54:00:00:<hi>:<lo> derived "
            "from --ssh-port. Set it when the guest image pins its netplan "
            "to a specific MAC, as images this repo built before 3969f07 do."
        ),
    )
    p.add_argument(
        "--kvm", action=argparse.BooleanOptionalAction, default=_UNSET
    )
    p.add_argument("--qemu-path", default=_UNSET, metavar="PATH")
    p.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Settings file (see qemu/env/example). Overrides the search path. "
            "Repeatable: later files override keys set by earlier ones."
        ),
    )
    p.add_argument(
        "--domain",
        default=None,
        metavar="FILE",
        help="Libvirt domain XML to use as base config ('-' for stdin).",
    )
    return p


def _add_quick_start(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "quick-start",
        help="Boot two GPU + RDMA guests sized for this host, and test them.",
        description=(
            "Two guests from a published image, each with an emulated "
            "gfx1250, an emulated NVMe and an emulated rocm-ernic RDMA NIC, "
            "plus Prometheus and Grafana beside them -- then a GPU test on "
            "each and an RDMA test across them. Nothing is built. The guests "
            "are sized from this machine, and anything still unknown is "
            "asked for on stdin; pass --yes to take every default instead, "
            "which is also what happens when stdin is not a terminal."
        ),
    )
    p.add_argument(
        "verb", nargs="?", default="all", choices=list(_QUICK_START_VERBS),
        help=(
            "all (default) does the lot. preflight checks this machine and "
            "touches nothing, up stops once the guests are booted, test "
            "configures and tests an already-up stack, report rewrites the "
            "report, logs shows the compose logs, down stops it and keeps "
            "the overlays."
        ),
    )
    p.add_argument(
        "-f", "--follow", action="store_true", default=False,
        help="Follow log output (only meaningful with the logs verb).",
    )
    p.add_argument(
        "--env-file", action="append", type=Path, default=None, metavar="FILE",
        help=(
            "Settings file. Defaults to the bundled qemu/env/quickstart. "
            "Repeatable: later files override keys set by earlier ones."
        ),
    )
    p.add_argument(
        "--stack-dir", type=Path, default=None, metavar="DIR",
        help=(
            "Where the stack is generated. Default: "
            "/var/lib/qemu-tool/stacks/quickstart, falling back to "
            "$XDG_STATE_HOME when that is not writable."
        ),
    )
    p.add_argument(
        "--key-dir", type=Path,
        default=None, metavar="DIR",
        help="Where the guest keypair is pulled to. Default: "
             "/var/lib/qemu-tool/quickstart-key, falling back to "
             "$XDG_DATA_HOME when that is not writable.",
    )
    p.add_argument(
        "--images", type=Path, default=None, metavar="DIR",
        help=(
            "Directory for the guest qcow2 files. Overrides VM_IMAGES_DIR; "
            "without it an unwritable default is asked about rather than "
            "failing part way into the pull."
        ),
    )
    p.add_argument(
        "--report-dir", type=Path, default=None, metavar="DIR",
        help="Write a markdown report here (implies the report step).",
    )
    p.add_argument(
        "--repo-root", type=Path, default=None, metavar="DIR",
        help=(
            "The checkout holding ansible/playbooks/fleet-quickstart.yml. "
            "Found automatically from a source install; an installed tool "
            "with no checkout brings the guests up and skips the tests."
        ),
    )
    p.add_argument(
        "--size-from-host", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Derive VM_VCPUS, VM_VMEM and VM_SHM_SIZE from this machine. On "
            "by default; --no-size-from-host uses the settings file as "
            "written."
        ),
    )
    p.add_argument(
        "--metrics", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Bring Prometheus and Grafana up with the stack, with their data "
            "directories beside it. On by default."
        ),
    )
    p.add_argument(
        "--in-network", action="store_true",
        help=(
            "Reach the guests on the compose network rather than on "
            "published host ports, for a caller that is itself a container "
            "attached to that network (the CI lane)."
        ),
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Take the default answer to every prompt without asking.",
    )
    p.add_argument(
        "--clean-qcow2", action="store_true", default=False,
        help=(
            "Delete the guest overlay and NVMe qcow2 files before starting, "
            "so the guests boot from a fresh overlay of the backing image. "
            "The backing file itself is kept. Only meaningful with the up or "
            "all verbs."
        ),
    )
    p.set_defaults(func=_quick_start_cmd)


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
    # Both halves, because the default is now on: --vfio-user-root-port alone
    # became a flag that could only restate the default, leaving no way to ask
    # for the old topology from the command line at all. The settings file has
    # always been able to; the CLI should not be the weaker of the two.
    p.add_argument("--vfio-user-root-port", action="store_true",
                   default=_UNSET,
                   help="Put each vfio-user device behind its own PCIe root "
                        "port. On by default; a GPU cannot bind amdgpu "
                        "without it.")
    p.add_argument("--no-vfio-user-root-port", action="store_false",
                   dest="vfio_user_root_port", default=_UNSET,
                   help="Keep vfio-user devices on the root bus as "
                        "conventional endpoints. A GPU will not work.")
    p.add_argument(
        "--vfio-user-switch-width", type=int, default=_UNSET, metavar="N",
        help=(
            "Group vfio-user devices under PCIe switches, N per switch, in "
            "--vfio-userdev order. 0 (default) is off."
        ),
    )
    p.add_argument(
        "--cpu-sockets", type=int, default=_UNSET, metavar="N",
        help=(
            "Pin the guest socket count. Without it QEMU infers sockets=cpus, "
            "so the guest sees N single-core sockets."
        ),
    )
    p.add_argument(
        "--cpu-phys-bits", default=_UNSET, metavar="N", type=int,
        help=(
            "Physical address bits for the EPYC CPU (default 40 = 1 TiB). "
            "Set to 41 alongside --pci-hole64-size when a large BAR exceeds "
            "the default address space; both must move together."
        ),
    )
    p.add_argument(
        "--pci-hole64-size", default=_UNSET, metavar="SIZE",
        help="Size of the 64-bit PCI hole, e.g. 512G. Needed for many large BARs.",
    )
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
    p.add_argument("--mgmt-tap", action="store_true", default=_UNSET,
                   help="Use tap+bridge for management NIC instead of SLIRP.")
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
        help="Run docker compose for a vfio-user VM stack.",
        description=(
            "Wrapper around 'docker compose' for vfio-user VM stacks. "
            "Sets VM_NAME and VM_IMAGES_DIR, then passes remaining arguments "
            "directly to docker compose. A stack rendered by gen-compose "
            "names its own env files in its provenance header, so from inside "
            "such a directory no flags are needed at all."
        ),
    )
    p.add_argument(
        "--vm-name", default=None, metavar="NAME",
        help="VM name (sets VM_NAME/VM1_NAME). Omit to use the value in .env.",
    )
    p.add_argument(
        "--vm2-name", default=None, metavar="NAME",
        help="Second VM name for 2-VM stacks (sets VM2_NAME). Omit to use the value in .env.",
    )
    p.add_argument(
        "--images", type=Path, default=None, metavar="DIR",
        help="Directory containing VM images (sets VM_IMAGES_DIR). Omit to use the value in .env.",
    )
    p.add_argument(
        "--stack", choices=_STACKS, default=None,
        help=f"Compose stack to use. Default: {_DEFAULT_STACK}.",
    )
    p.add_argument(
        "--stack-dir", type=Path, default=None, metavar="DIR",
        help=(
            "Run a stack rendered by 'gen-compose --output-dir DIR' instead of "
            "a named one. Required for an overlay fleet: VM_COUNT and the VM "
            "names are baked into the generated YAML, not read from the env "
            "file. Mutually exclusive with --stack. Defaults to "
            "$QEMU_TOOL_STACK, or to the current directory when that is "
            "itself a rendered stack."
        ),
    )
    p.add_argument(
        "--env-file", action="append", type=Path, default=None, metavar="FILE",
        help=(
            "Settings file (see qemu/env/example). Overrides the search path. "
            "Repeatable: later files override keys set by earlier ones."
        ),
    )
    p.add_argument(
        "compose_args", nargs=argparse.REMAINDER,
        help="Arguments forwarded to docker compose (e.g. up, down, ps, logs).",
    )
    p.set_defaults(func=_compose_cmd)


def _add_gen_compose(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "gen-compose",
        help="Generate a scale-out compose stack from an env file.",
        description=(
            "Generate docker-compose.yml and prometheus.yml for an N-VM "
            "rocm-ernic mesh from VM_COUNT and the per-VM lists in the env "
            "file. Per-VM values are comma-lists, not numbered keys; see "
            "qemu/env/scale-out."
        ),
    )
    p.add_argument(
        "--stack", choices=sorted(_GENERATED_STACKS), default=_DEFAULT_GENERATED_STACK,
        help=f"Stack to generate. Default: {_DEFAULT_GENERATED_STACK}.",
    )
    p.add_argument(
        "--env-file", action="append", type=Path, default=None, metavar="FILE",
        help=(
            "Settings file (see qemu/env/scale-out). Overrides the search path. "
            "Repeatable: later files override keys set by earlier ones."
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


def _add_lmcache_stats(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "lmcache-stats",
        help="Serve the LMCache MP coordinator's directory as Prometheus "
             "metrics.",
        description=(
            "Poll the coordinator's HTTP API and republish it on /metrics. "
            "The coordinator has its own /metrics, but it carries only the "
            "OpenTelemetry event-bus series -- every directory, membership "
            "and per-tier usage figure is HTTP-only, which is why this "
            "exists."
        ),
    )
    p.add_argument(
        "--coordinator-url", default="http://127.0.0.1:9300", metavar="URL",
        help="Base URL of the coordinator HTTP API. "
             "Default: http://127.0.0.1:9300.",
    )
    p.add_argument(
        "--port", type=int, default=9841, metavar="PORT",
        help="Listen port. Default: 9841, one past the ernic exporter.",
    )
    p.add_argument(
        "--addr", default="0.0.0.0", metavar="ADDR",
        help="Listen address. Default: 0.0.0.0.",
    )
    p.add_argument(
        "--timeout", type=float, default=5.0, metavar="SECONDS",
        help="Per-request timeout when polling. Default: 5.",
    )
    p.add_argument(
        "--once", action="store_true",
        help="Print one scrape to stdout and exit.",
    )
    p.set_defaults(func=_lmcache_stats_cmd)


def _add_ernic_stats(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ernic-stats",
        help="Serve rocm-ernic stats files as Prometheus metrics.",
        description=(
            "Parse the stats dumps written by 'rocm-ernic -S PATH' and serve "
            "them on /metrics in Prometheus text format. Metric and label "
            "names match rocm-ernic's own prometheus/ernic-exporter, so "
            "ernic-dashboard.json works against this endpoint unmodified."
        ),
    )
    p.add_argument(
        "--stats-dir", type=Path, default=Path("/run/ernic-stats"), metavar="DIR",
        help="Directory of *.stats files. Default: /run/ernic-stats.",
    )
    p.add_argument(
        "--port", type=int, default=9840, metavar="PORT",
        help="Listen port. Default: 9840 (matches the upstream exporter).",
    )
    p.add_argument(
        "--addr", default="0.0.0.0", metavar="ADDR",
        help="Listen address. Default: 0.0.0.0.",
    )
    p.set_defaults(func=_ernic_stats_cmd)


def _add_list(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="List QEMU VMs running on this node.",
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
        help="Show JSON manifest for running VMs.",
        description=(
            "Emit a JSON manifest for each running VM: SSH access details, "
            "username, public key, and hardware summary. Always outputs JSON."
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
        "--env-file", action="append", type=Path, default=None, metavar="FILE",
        help=(
            "Settings file (see qemu/env/example). Overrides the search path. "
            "Repeatable: later files override keys set by earlier ones."
        ),
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit JSON instead of human-readable output.",
    )
    p.set_defaults(func=_inspect_cmd)


def _add_ssh(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ssh",
        help="SSH into a running VM.",
        description=(
            "Open an SSH session to a running VM, resolving the port, "
            "username and key the same way `inspect` reports them -- the "
            "username from the image's own sidecar, and an identity only "
            "when the guest accepts a key whose private half is on this "
            "host. Host-key checking is off by default because guests "
            "sharing a backing image share a host key and the fleet reuses "
            "localhost ports. "
            "Everything after the VM name is passed to ssh, so this tool's "
            "own options go BEFORE it -- `qemu-tool ssh --dry-run vm1`, the "
            "same order ssh(1) and docker exec take. "
            "Run a remote command with `qemu-tool ssh vm1 -- uptime`."
        ),
    )
    def _complete_vm_name(**_kw: object) -> list[str]:
        try:
            return [v["name"] for v in _list_vms_collect() if v.get("name")]
        except Exception:
            return []
    p.add_argument("vm", metavar="VM", help="Name of the running VM.").completer = _complete_vm_name
    p.add_argument(
        "--env-file", action="append", type=Path, default=None, metavar="FILE",
        help="Settings file. Repeatable; later files win.",
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
        help="Inspect and manage VM overlay qcow2 state.",
        description=(
            "List, inspect or clean VM overlay images and their NVMe scratch disks. "
            "Images are read from VM_IMAGES_DIR (or --images). "
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
        "--images", type=Path, default=None, metavar="DIR",
        help="VM images directory (overrides VM_IMAGES_DIR).",
    )
    p.add_argument(
        "--nvme", action="store_true",
        help="With --list: show a separate NVMe scratch disk section.",
    )
    p.add_argument(
        "--env-file", action="append", type=Path, default=None, metavar="FILE",
        help="Settings file. Repeatable; later files win.",
    )
    p.set_defaults(func=_qcow_cmd)


def _qcow_cmd(args: argparse.Namespace) -> None:
    from .config import VMConfig, default_images
    images: Path | None
    if args.images:
        images = args.images
    elif args.env_file:
        cfg = VMConfig.from_env(args.env_file)
        images = cfg.images
    else:
        images = None  # let list_overlays scan all known dirs

    if args.list:
        _qcow.list_overlays(images, show_nvme=getattr(args, "nvme", False))
    elif args.state:
        _qcow.state(images or default_images(), args.state)
    elif args.clean:
        _qcow.clean(images or default_images(), args.clean, yes=args.yes)


def _add_check(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "check",
        help="Read-only preflight: check this host has everything the stacks need.",
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
        "--env-file", type=Path, action="append", metavar="FILE",
        help=(
            "Settings file to check against, repeatable, later files winning "
            "key by key. Without it the usual search path is used, and which "
            "file that found is itself part of the report."
        ),
    )
    p.set_defaults(func=_check_cmd)


def _add_down(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "down",
        help="Gracefully shut down guests and/or tear down stacks.",
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
        help="How long to wait for guests to power off. Default: 120.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without doing it.",
    )
    p.set_defaults(func=_down_cmd)


def _add_man(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "man",
        help="Show this tool's manual page.",
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
                   help="Ubuntu codename (resolute) or version (26.04).")
    p.add_argument("--ssh-key-file", type=Path, default=_UNSET, metavar="FILE")
    p.add_argument("--username", default=_UNSET)
    p.add_argument("--user-id", type=int, default=_UNSET, metavar="UID")
    p.add_argument("--password", default=_UNSET)
    p.add_argument("--packages", default=_UNSET,
                   metavar="FILE",
                   help="Package manifest file, or 'none'.")
    p.add_argument("--force", action="store_true", default=_UNSET)
    p.add_argument("--compact", action=argparse.BooleanOptionalAction, default=_UNSET,
                   help="Recompress the backing image after the bake (default: on).")
    p.add_argument("--no-backing", action="store_true", default=_UNSET)
    p.add_argument("--restore-image", action="store_true", default=_UNSET)
    p.add_argument("--backing-file", type=Path, default=_UNSET, metavar="FILE")
    p.add_argument("--backing-image", default=_UNSET, metavar="OCI_REF",
                   help="Pull a published backing qcow2 with oras, then overlay "
                        "on it. Same job as the cloud-image download, different "
                        "source; no cloud-init and no bake.")
    p.add_argument("--ansible-playbook", type=Path, default=_UNSET, metavar="FILE",
                   help="Path to an Ansible playbook to run against the VM image after cloud-init.")
    p.add_argument("--ca-cert", type=Path, default=_UNSET, metavar="FILE",
                   help="CA certificate to inject into the guest trust store.")
    p.add_argument("--ansible-only", action="store_true", default=_UNSET,
                   help="Skip cloud-init; re-run Ansible against an existing backing image.")
    p.add_argument("--mgmt-tap", action="store_true", default=_UNSET,
                   help="Use tap+bridge for management NIC instead of SLIRP.")
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


def _check_cmd(args: argparse.Namespace) -> None:
    check_system_run(args.env_file)


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
        args.env_file,
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
        clean_qcow2=args.clean_qcow2,
    )


def _compose_cmd(args: argparse.Namespace) -> None:
    compose_run(args.vm_name, args.images, args.compose_args,
                stack=args.stack, vm2_name=args.vm2_name,
                env_file=args.env_file, stack_dir=args.stack_dir)


def _inspect_cmd(args: argparse.Namespace) -> None:
    inspect_run(_one_vm_name(args), args.env_file, as_json=args.as_json)


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
        args.vm, args.env_file,
        strict_host_key=args.strict_host_key,
        extra=extra,
        dry_run=args.dry_run,
    )


def _shutdown_cmd(args: argparse.Namespace) -> None:
    shutdown_run(args.vm, timeout=args.timeout, dry_run=args.dry_run)


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
    if output_dir is None:
        try:
            output_dir = _compose_dir(args.stack)
        except FileNotFoundError:
            # First generation: the stack directory does not exist yet.
            output_dir = _SOURCE_COMPOSE_ROOT / args.stack
        if output_dir.is_relative_to(_INSTALLED_COMPOSE_ROOT):
            sys.exit(
                f"Error: {output_dir} is owned by the qemu-tool package.\n"
                "Pass --output-dir to generate somewhere writable, or name "
                "that path explicitly if you really mean to modify it."
            )
    gen_compose_run(args.env_file, output_dir, check=args.check,
                    dry_run=args.dry_run, stack=args.stack)


def _ernic_stats_cmd(args: argparse.Namespace) -> None:
    ernic_stats_run(args.stats_dir, args.port, args.addr)


def _lmcache_stats_cmd(args: argparse.Namespace) -> None:
    if args.once:
        from .lmcache_stats import collect
        sys.stdout.write(collect(args.coordinator_url, args.timeout))
        return
    lmcache_stats_run(args.coordinator_url, args.port, args.addr, args.timeout)


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
    # Lowest to highest: dataclass defaults, --domain XML, env file, CLI flags.
    # The env file outranks the XML because it is the user's own standing
    # configuration, where the XML is a description of somebody else's domain.
    cfg = _merge(xml_cfg, load_env_file(args.env_file))
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
        # env file names VM_BACKING_IMAGE for VM 1, and every other VM is then
        # created with --backing-file against the image that fetched -- so an
        # explicit flag has to clear its partner rather than collide with it,
        # the same way CLI outranks the env file everywhere else.
        if ns.get("backing_file", _UNSET) is not _UNSET:
            overrides["backing_image"] = None
        elif ns.get("backing_image", _UNSET) is not _UNSET:
            overrides["backing_file"] = None
        _take("ansible_playbook", "ansible_playbook")
        _take("ca_cert_file", "ca_cert")
        _take("ansible_only", "ansible_only")
        _take("mgmt_tap", "mgmt_tap")

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
