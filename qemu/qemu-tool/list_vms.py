from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .identity import match_identity

_PROC = Path("/proc")
_CONTAINER_ID_RE = re.compile(r"\b([0-9a-f]{64})\b")
_HOSTFWD_RE = re.compile(r"hostfwd=tcp::(\d+)-:22")
_PUBLISHED_RE = re.compile(r":(\d+)->(\d+)/tcp")
# Any .sock path a device server was told to serve. The socket is what joins a
# guest's QEMU command line to the server process behind it: both name it, and
# nothing else they say is in common.
_VFU_SOCK_RE = re.compile(r"(/\S+\.sock)")
# rocjitsu's --config filename carries the target it emulates
# (gfx1250_mi455x.json). Matching the filename rather than reading the JSON
# keeps this to the argv we already have.
# No trailing \b: the filename is gfx1250_mi455x.json and "_" is a word
# character, so a trailing boundary never matches and the column silently
# stays empty.
_GFX_RE = re.compile(r"\b(gfx[0-9a-f]+)")
# The ernic server is given its guest MAC with -m; it is the one identifier
# visible from the host that the guest also sees.
_MAC_RE = re.compile(r"\b((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\b", re.I)

# socket path -> argv of the container serving it, filled in by
# _container_addresses (one docker inspect for the whole host) and read back
# per VM. Module state rather than a parameter because _describe is called per
# PID and threading a second lookup table through it buys nothing.
_DEVICE_ARGV: dict[str, str] = {}
_CLK_TCK = os.sysconf("SC_CLK_TCK")

_ARCH_BY_BINARY = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "riscv64": "riscv64",
}

_MEM_SCALE = {"b": 1 / 1048576, "k": 1 / 1024, "m": 1, "g": 1024, "t": 1048576}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(as_json: bool = False, qemu_tool_only: bool = False) -> None:
    try:
        vms = collect()
    except OSError as exc:
        sys.exit(f"Error: cannot read {_PROC}: {exc}")
    if qemu_tool_only:
        vms = [v for v in vms if v["source"] == "qemu-tool"]
    if as_json:
        print(json.dumps(vms, indent=2))
        return
    _print_table(vms, qemu_tool_only=qemu_tool_only)


def collect() -> list[dict[str, Any]]:
    containers = _container_names()
    vms = [_describe(pid, argv, containers) for pid, argv in _iter_qemu()]
    return sorted(vms, key=lambda v: v["pid"])


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def _iter_qemu():
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, ValueError):
            continue  # process exited, or /proc hidepid
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv or not Path(argv[0]).name.startswith("qemu-system-"):
            continue
        yield int(entry.name), argv


def _describe(pid: int, argv: list[str], containers: dict[str, str]) -> dict[str, Any]:
    name = _guest_name(argv)
    role = match_identity(name, _opt(argv, "-uuid"))
    image = _drive_file(argv)
    if name is None and image:
        name = Path(image).stem  # pre-marker VMs: best-effort from the disk
    machine = _opt(argv, "-machine") or ""
    ssh = _HOSTFWD_RE.search(" ".join(argv))
    guest_port = int(ssh.group(1)) if ssh else None
    container_name, published, networks = containers.get(
        _container_id(pid) or "", (None, {}, []))
    # Uncontainerised, the guest's hostfwd port *is* the host port. Inside a
    # container it is only the port in that netns, and the fleet stacks give
    # every guest 2222 there -- what a user can actually ssh to is whatever
    # docker published it as.
    host_port = published.get(guest_port) if container_name else guest_port
    return {
        "pid": pid,
        "name": name,
        "source": "qemu-tool" if role else "external",
        "role": role,
        "arch": _arch(argv[0]),
        "vcpus": _vcpus(argv),
        "memory_mib": _memory(argv),
        "ssh_port": guest_port,
        "ssh_port_host": host_port,
        "kvm": "accel=kvm" in machine,
        "image": image,
        "vfio_user_sockets": _vfio_sockets(argv),
        "gpus": _gpus(_vfio_sockets(argv)),
        "rdma_devices": _rdma_devices(_vfio_sockets(argv)),
        "container": container_name,
        # "<network>=<ip>" per attachment. Empty for an uncontainerised VM,
        # which has no Docker network at all rather than an unknown one.
        "networks": networks,
        "user": _owner(pid),
        "uptime_seconds": _uptime(pid),
    }


# ---------------------------------------------------------------------------
# Command-line parsing
# ---------------------------------------------------------------------------

def _opt(argv: list[str], flag: str) -> str | None:
    """Value following the last occurrence of flag (qemu takes the last -machine)."""
    for i in range(len(argv) - 2, -1, -1):
        if argv[i] == flag:
            return argv[i + 1]
    return None


def _arch(binary: str) -> str:
    # Distro builds add suffixes (qemu-system-x86_64-spice), so take the first
    # component after the prefix rather than the text after the last dash.
    stem = Path(binary).name[len("qemu-system-"):]
    return _ARCH_BY_BINARY.get(stem.split("-", 1)[0], "?")


def _guest_name(argv: list[str]) -> str | None:
    raw = _opt(argv, "-name")
    if not raw:
        return None
    for part in raw.split(","):
        if part.startswith("guest="):
            return part[len("guest="):]
    # -name accepts a bare string too; ignore the key=value suffixes.
    first = raw.split(",")[0]
    return first if "=" not in first else None


def _vcpus(argv: list[str]) -> int | None:
    raw = _opt(argv, "-smp")
    if not raw:
        return None
    for part in raw.split(","):
        if part.startswith("cpus="):
            part = part[len("cpus="):]
        if part.isdigit():
            return int(part)
    return None


def _memory(argv: list[str]) -> int | None:
    raw = _opt(argv, "-m")
    if not raw:
        return None
    # -m is a QemuOpts list with free key order: size= may appear anywhere.
    size = None
    for part in raw.split(","):
        if part.startswith("size="):
            size = part[len("size="):]
            break
    if size is None:
        first = raw.split(",")[0]
        size = first if "=" not in first else None
    if size is None:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([BbKkMmGgTt]?)", size.strip())
    if not m:
        return None
    return int(float(m.group(1)) * _MEM_SCALE[(m.group(2) or "m").lower()])


def _drive_file(argv: list[str]) -> str | None:
    """Root disk path. NVMe scratch drives (if=none) are emitted first, so an
    unqualified first-match would report the wrong image for --nvme VMs."""
    fallback = None
    for i, a in enumerate(argv):
        if a != "-drive" or i + 1 >= len(argv):
            continue
        parts = argv[i + 1].split(",")
        for part in parts:
            if not (part.startswith("file=") and part.endswith(".qcow2")):
                continue
            path = part[len("file="):]
            if "if=none" not in parts:
                return path
            fallback = fallback or path
    return fallback


def _vfio_sockets(argv: list[str]) -> list[str]:
    socks = []
    for i, a in enumerate(argv):
        if a != "-device" or i + 1 >= len(argv):
            continue
        spec = argv[i + 1]
        if "vfio-user-pci" not in spec:
            continue
        try:
            dev = json.loads(spec)
        except ValueError:
            continue
        path = (dev.get("socket") or {}).get("path")
        if path:
            socks.append(path)
    return socks


# ---------------------------------------------------------------------------
# /proc and container attribution
# ---------------------------------------------------------------------------

def _container_id(pid: int) -> str | None:
    try:
        # cgroup path components are arbitrary kernel bytes, not always UTF-8.
        cgroup = (_PROC / str(pid) / "cgroup").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    m = _CONTAINER_ID_RE.search(cgroup)
    return m.group(1) if m else None


def _container_names() -> dict[str, tuple[str, dict[int, int], list[str]]]:
    """Full container id -> (name, {container port: host port}, networks).

    The published map matters because a containerised guest's -hostfwd port is
    the port inside that container's netns, and the fleet stacks give every
    guest the same one -- distinguishing them is the job of the published
    mapping, not of the QEMU command line.

    Networks come from the same call rather than a `docker inspect` per
    container: `docker ps` already carries them, and `list` runs against a
    fleet where that would be one subprocess per VM.

    Each entry is "<network>=<ip>". The address is the useful half on the
    generated stacks -- every service there has a fixed one and nothing
    resolves a service name at run time, so the address is what Prometheus
    scrapes and what a worker dials.
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "--no-trunc",
             "--format", "{{.ID}}\t{{.Names}}\t{{.Ports}}\t{{.Networks}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    names: dict[str, tuple[str, dict[int, int], list[str]]] = {}
    ids: list[str] = []
    raw: dict[str, tuple[str, str, str]] = {}
    for line in out.stdout.splitlines():
        cid, _, rest = line.partition("\t")
        name, _, rest2 = rest.partition("\t")
        ports, _, nets = rest2.partition("\t")
        if cid and name:
            raw[cid] = (name, ports, nets)
            ids.append(cid)
    addrs = _container_addresses(ids)
    for cid, (name, ports, nets) in raw.items():
        found = addrs.get(cid, {})
        joined = [
            f"{_short_net(n)}={found[n]}" if found.get(n) else _short_net(n)
            for n in (p.strip() for p in nets.split(",")) if n
        ]
        names[cid] = (name, _published_ports(ports), joined)
    return names


def _short_net(name: str) -> str:
    """Drop compose's "<project>_" prefix from a network name.

    Purely for table width: the project is already spelled out in the
    CONTAINER column, so repeating it once per network crowds out the address,
    which is the part worth reading. `--json` keeps the full name.
    """
    head, sep, tail = name.rpartition("_")
    return tail if sep and tail else name


def _container_addresses(ids: list[str]) -> dict[str, dict[str, str]]:
    """container id -> {network name: ipv4}, for every id in one call.

    `docker ps` names a container's networks but not its addresses, so this is
    the one extra subprocess. It is batched over every id rather than run per
    VM: a 40-VM fleet would otherwise pay 40 docker round-trips to print one
    table.
    """
    if not ids:
        return {}
    # \x1e starts each record. Line-based parsing is not safe here: an
    # ernic running with ERNIC_TAP has a multi-line shell script as its
    # argv, so its record spans lines and the socket -- which appears after
    # the first newline -- was being dropped, leaving the RDMA column empty
    # for exactly the stacks that have RDMA.
    fmt = ("\x1e{{.Id}}\t{{range $k, $v := .NetworkSettings.Networks}}"
           # `range`, not `join`: Args is []interface{} on some daemon
           # versions and `join` type-errors the whole template, which takes
           # the addresses down with it.
           "{{$k}}={{$v.IPAddress}} {{end}}\t{{.Path}} "
           "{{range .Args}}{{.}} {{end}}")
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", fmt, *ids],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    # Not checking returncode: inspect exits non-zero when ANY id is gone
    # (a container can stop between the ps and here) while still printing the
    # ones that resolved. Parse what we got.
    found: dict[str, dict[str, str]] = {}
    for record in out.stdout.split("\x1e"):
        cid, _, rest = record.partition("\t")
        cid = cid.strip()
        if not cid:
            continue
        nets, _, argv = rest.partition("\t")
        argv = argv.replace("\n", " ")
        per: dict[str, str] = {}
        for pair in nets.split():
            net, _, ip = pair.partition("=")
            if net and ip:
                per[net] = ip
        found[cid] = per
        # Side table, keyed by the vfio-user socket each device server was
        # told to serve. That socket path is the only thing the guest's QEMU
        # command line and the server's own command line have in common, so
        # it is what joins "this VM has a device" to "that device is a
        # gfx1250" -- neither of which the guest tells us from outside.
        for sock in _VFU_SOCK_RE.findall(argv):
            _DEVICE_ARGV[sock] = argv
    return found


def _published_ports(ports: str) -> dict[int, int]:
    """Parse docker's Ports column into {container port: host port}.

    A container publishing on both stacks is listed once per address family
    ("0.0.0.0:2231->2222/tcp, [::]:2231->2222/tcp"); the host port is the same
    in both, so last-wins is fine.
    """
    published: dict[int, int] = {}
    for m in _PUBLISHED_RE.finditer(ports):
        published[int(m.group(2))] = int(m.group(1))
    return published


def _device_servers() -> dict[str, str]:
    """vfio-user socket path -> the argv of the container serving it."""
    return _DEVICE_ARGV


def _gpus(sockets: list[str]) -> list[str]:
    """gfx targets of the rocjitsu devices attached to a VM.

    Read off the server's --config, whose filename carries the target
    (gfx1250_mi455x.json). That is the only place it appears: the guest sees
    an emulated device and `rocm-smi` inside it would need the guest booted
    and ROCm working, which is exactly what someone running `list` is often
    trying to find out.
    """
    out: list[str] = []
    for sock in sockets:
        argv = _DEVICE_ARGV.get(sock)
        if not argv:
            continue
        m = _GFX_RE.search(argv)
        if m:
            out.append(m.group(1))
    return out


def _rdma_devices(sockets: list[str]) -> list[str]:
    """MAC addresses of the ernic devices attached to a VM.

    The MAC and NOT a guest device name, which this cannot know. An earlier
    version of this column printed "rocm-rdma-ernic0", derived from the udev
    rename in 99-rocm-ernic.rules -- but those rules ship with
    ernic_guest_setup, and on a guest without them the device comes up under
    its kernel name instead. Measured on a guest where ionic had just bound:

        ionic 0000:00:07.0 enp0s7np0: renamed from eth0
        ionic 0000:00:07.0 rocep0s7: Port: 1 Link ACTIVE

    so the column confidently named a device that did not exist, which is
    worse than naming none. The MAC comes off the ernic server's own -m flag,
    is what the guest's netdev actually carries, and is therefore the one
    identifier that holds whether or not udev renamed anything -- match it
    with `ip -br link` in the guest.
    """
    out: list[str] = []
    for sock in sockets:
        argv = _DEVICE_ARGV.get(sock)
        if not argv or "rocm-ernic" not in argv:
            continue
        m = _MAC_RE.search(argv)
        out.append(m.group(1) if m else "ernic")
    return out


def _owner(pid: int) -> str | None:
    try:
        # The pid directory keeps the effective uid; files under it revert to
        # root once the task becomes non-dumpable (qemu -runas, setuid drop).
        uid = (_PROC / str(pid)).stat().st_uid
        return pwd.getpwuid(uid).pw_name
    except (OSError, KeyError):
        return None


def _uptime(pid: int) -> int | None:
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
        boot = float((_PROC / "uptime").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return None
    # comm (field 2) may contain spaces and parens; everything after the final
    # ')' is positionally stable.
    tail = stat.rpartition(")")[2].split()
    try:
        starttime = int(tail[19])  # field 22 overall
    except (IndexError, ValueError):
        return None
    return max(0, int(boot - starttime / _CLK_TCK))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fmt_ssh(vm: dict[str, Any], show_split: bool) -> str:
    """The SSH column: "host->guest" where they differ, else the one port.

    Only splits when some VM in the listing actually differs, so an
    uncontainerised listing is unchanged.
    """
    host, guest = vm["ssh_port_host"], vm["ssh_port"]
    if not show_split:
        return str(guest or "-")
    return f"{host or '-'}->{guest or '-'}"


def _print_table(vms: list[dict[str, Any]], qemu_tool_only: bool = False) -> None:
    if not vms:
        print("No qemu-tool VMs running." if qemu_tool_only
              else "No QEMU VMs running.")
        return

    headers = ["PID", "NAME", "SOURCE", "ARCH", "VCPU", "MEM", "SSH",
               "KVM", "UPTIME", "CONTAINER", "NETWORK", "GPU", "RDMA",
               "VFIO-USER"]
    show_split = any(v["ssh_port_host"] != v["ssh_port"] for v in vms)
    rows = []
    for v in vms:
        source = v["source"]
        if v["role"]:
            source = f"qemu-tool/{v['role']}"
        rows.append([
            str(v["pid"]),
            v["name"] or "-",
            source,
            v["arch"],
            str(v["vcpus"] or "-"),
            f"{v['memory_mib']}M" if v["memory_mib"] else "-",
            _fmt_ssh(v, show_split),
            "yes" if v["kvm"] else "no",
            _fmt_uptime(v["uptime_seconds"]),
            v["container"] or "-",
            ",".join(v["networks"]) or "-",
            ",".join(v["gpus"]) or "-",
            ",".join(v["rdma_devices"]) or "-",
            ",".join(Path(s).name for s in v["vfio_user_sockets"]) or "-",
        ])

    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line.rstrip())
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())

    if any(v["source"] == "external" for v in vms):
        # stdout is block-buffered when redirected, stderr is not: without this
        # the footnote would land above the table it annotates.
        sys.stdout.flush()
        print(
            "\nVMs marked 'external' were not started by this qemu-tool, or were "
            "started before\nidentity markers were added (restart them to be "
            "identified).",
            file=sys.stderr,
        )
