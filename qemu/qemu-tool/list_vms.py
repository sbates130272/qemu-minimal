from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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

_QMP_RE = re.compile(r"unix:([^,]+)")
# PCI class 01h subclass 08h is NVM Express. sysfs spells the full 24-bit
# class as 0x0108xx, the last byte being the programming interface (02 = NVMe
# I/O controller), so the prefix is what to match on.
_NVME_CLASS_PREFIX = "0x0108"
_SYS_PCI = Path("/sys/bus/pci/devices")
_BDF_RE = re.compile(r"^(?:([0-9a-f]{4}):)?([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$", re.I)
_GA_CHARDEV_RE = re.compile(r"socket,(?:[^,]*,)*path=([^,]+)")

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
    _upgrade_nvme_bdfs(vms)
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
    qmp_sock = _qmp_socket(argv)
    ga_sock = _ga_socket(argv)
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
        "nvme": _nvme_devices(argv),
        "qmp_socket": qmp_sock,
        "qmp_available": _socket_exists(pid, qmp_sock) or (bool(container_name) and bool(qmp_sock)),
        "ga_socket": ga_sock,
        "ga_available": _socket_exists(pid, ga_sock) or (bool(container_name) and bool(ga_sock)),
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


def _qmp_socket(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "-qmp" and i + 1 < len(argv):
            m = _QMP_RE.match(argv[i + 1])
            if m:
                return m.group(1)
    return None


def _ga_socket(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "-chardev" and i + 1 < len(argv):
            spec = argv[i + 1]
            if "qga" not in spec and "guest_agent" not in spec:
                continue
            m = _GA_CHARDEV_RE.search(spec)
            if m:
                return m.group(1)
    return None


def _socket_exists(pid: int, path: str | None) -> bool:
    """Return True if the socket file is present.

    For non-containerised VMs the path is on the host — check directly.
    For containerised VMs /proc/<pid>/root requires root, so fall back to
    checking the path directly (usually absent on the host) and then trusting
    that a configured socket path means the socket exists inside the container.
    The list column shows 'configured'; inspect's live GA query is authoritative.
    """
    if not path:
        return False
    try:
        if Path(path).exists():
            return True
    except OSError:
        pass
    # Try the container's root via /proc (works when running as root).
    try:
        container_path = _PROC / str(pid) / "root" / Path(path).relative_to("/")
        if container_path.exists():
            return True
    except (OSError, ValueError):
        pass
    return False


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


# ---------------------------------------------------------------------------
# NVMe
# ---------------------------------------------------------------------------

def _device_opts(spec: str) -> tuple[str, dict[str, str]]:
    """(driver, options) for a -device argument in key=value form.

    Returns ("", {}) for the JSON form, which vfio-user-pci uses and no NVMe
    does -- splitting a JSON object on commas would produce nonsense keys.
    """
    if spec.startswith("{"):
        return "", {}
    parts = spec.split(",")
    opts = {}
    for p in parts[1:]:
        k, sep, v = p.partition("=")
        if sep:
            opts[k] = v
    return parts[0], opts


def _normalise_bdf(bdf: str) -> str | None:
    """Canonical dddd:bb:dd.f, or None if this is not a BDF at all.

    Callers feed this straight into a /sys path, so anything that does not
    parse must not become a path component.
    """
    m = _BDF_RE.match(bdf.strip())
    if not m:
        return None
    dom, bus, dev, fn = m.groups()
    return f"{(dom or '0000').lower()}:{bus.lower()}:{dev.lower()}.{fn}"


def _pci_class(bdf: str) -> str | None:
    """Host PCI class of a BDF, or None if this host has no such device.

    /sys is the host's view, which is the right question for a passthrough
    BDF (run_vm's -device vfio-pci,host=... names a device on this host) and
    meaningless for a guest one.
    """
    try:
        return (_SYS_PCI / bdf / "class").read_text().strip().lower()
    except OSError:
        return None


def _drive_files(argv: list[str]) -> dict[str, str]:
    """-drive id -> file, for the if=none drives an NVMe controller is backed by.

    The scratch qcow2s are named only here. _drive_file above deliberately
    skips if=none because it is hunting the root disk, so without this map the
    path of every --nvme image is parsed and thrown away -- and those images
    carry the bulk of a fleet's virtual size (1 TiB each against the root
    overlay's 512 GiB).
    """
    found: dict[str, str] = {}
    for i, a in enumerate(argv):
        if a != "-drive" or i + 1 >= len(argv):
            continue
        opts = {}
        for part in argv[i + 1].split(","):
            k, sep, v = part.partition("=")
            if sep:
                opts[k] = v
        if opts.get("id") and opts.get("file"):
            found[opts["id"]] = opts["file"]
    return found


def _nvme_image(opts: dict[str, str], argv: list[str],
                drives: dict[str, str]) -> str | None:
    """The backing file of one -device nvme, or None for a passthrough one.

    Two spellings, because run_vm emits both: the null_blk form puts
    drive=nvme-N on the controller itself, while the qcow2 form hangs an
    -device nvme-ns off it and the drive= lives there, joined back by
    bus=<controller id>.
    """
    direct = opts.get("drive")
    if direct:
        return drives.get(direct)
    qdev_id = opts.get("id")
    if not qdev_id:
        return None
    for i, a in enumerate(argv):
        if a != "-device" or i + 1 >= len(argv):
            continue
        driver, ns = _device_opts(argv[i + 1])
        if driver == "nvme-ns" and ns.get("bus") == qdev_id and ns.get("drive"):
            return drives.get(ns["drive"])
    return None


def _nvme_devices(argv: list[str]) -> list[dict[str, Any]]:
    """NVMe devices attached to a VM, derived from the QEMU command line.

    Two shapes, and the host can say much less about the first than it would
    like:

    - emulated: run_vm emits "-device nvme,serial=...,id=nvme-N-dev" with no
      addr=, so QEMU picks the slot at realize time and the guest BDF is
      simply not in argv. Recorded with bdf None; _upgrade_nvme_bdfs fills it
      in over QMP where a monitor socket exists.
    - passthrough: "-device vfio-pci,...,host=0000:03:00.0" names a HOST BDF.
      A GPU and a NIC are passed through by exactly the same argument, so the
      class is checked in sysfs and only 0x0108xx is claimed as NVMe.

    A BDF with no sysfs entry (unbound since boot, or a VM whose container is
    served by another host's device) is reported with class_verified False
    rather than dropped: dropping would silently hide a real NVMe, and the
    renderer marks the uncertainty with a trailing "?" so an unverified entry
    is never mistaken for a confirmed one.
    """
    out: list[dict[str, Any]] = []
    drives = _drive_files(argv)
    for i, a in enumerate(argv):
        if a != "-device" or i + 1 >= len(argv):
            continue
        driver, opts = _device_opts(argv[i + 1])
        if driver == "nvme":
            # Exact match: "nvme-ns" is a namespace hanging off a controller
            # already counted here, not a second drive.
            out.append({
                "bdf": None,
                "kind": "emulated",
                "source": "argv",
                "qdev_id": opts.get("id"),
                "serial": opts.get("serial"),
                "image": _nvme_image(opts, argv, drives),
            })
        elif driver == "vfio-pci":
            bdf = _normalise_bdf(opts.get("host", ""))
            if not bdf:
                continue
            cls = _pci_class(bdf)
            if cls is not None and not cls.startswith(_NVME_CLASS_PREFIX):
                continue  # a GPU or NIC passed through to the same guest
            out.append({
                "bdf": bdf,
                "kind": "passthrough",
                "source": "argv",
                "qdev_id": opts.get("id"),
                "class_verified": cls is not None,
                # A passed-through controller has no host-side image file at
                # all; None here means "no such thing", not "unknown".
                "image": None,
            })
    return out


# Runs under `docker exec python3 -c` for a containerised VM, and under this
# interpreter as a subprocess otherwise -- one implementation, and in both
# cases subprocess.run's timeout is a hard bound, which an in-process
# connect() to a wedged socket would not be. A listing must never hang.
_QMP_SCRIPT = r"""
import json, socket, sys

def query(path, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        return []
    # recv-only timeout: connect must be blocking to avoid EAGAIN on Unix sockets
    s.settimeout(timeout)
    f = s.makefile("rwb")

    def recv():
        line = f.readline()
        return json.loads(line) if line else {}

    def send(cmd):
        f.write(json.dumps({"execute": cmd}).encode() + b"\n")
        f.flush()
        # Flush events and any stale response: QMP interleaves asynchronous
        # events with command replies, and only a return/error is ours.
        for _ in range(20):
            r = recv()
            if "return" in r or "error" in r:
                return r
        return {}

    try:
        greeting = recv()           # QMP announces itself before accepting
        if "QMP" not in greeting:   # anything else is not a QMP monitor
            return []
        if "return" not in send("qmp_capabilities"):
            return []
        reply = send("query-pci")
    except (OSError, ValueError):
        return []
    found = []
    def walk(devices):
        for d in devices:
            # class_info.class is the 16-bit class/subclass pair: 0x0108 is
            # NVM Express, whatever the programming interface byte says.
            if (d.get("class_info") or {}).get("class") == 0x0108:
                found.append({
                    "bdf": "0000:%02x:%02x.%d" % (
                        d.get("bus", 0), d.get("slot", 0), d.get("function", 0)),
                    "qdev_id": d.get("qdev_id") or None,
                })
            # An NVMe behind a pcie-root-port is one level down.
            walk((d.get("pci_bridge") or {}).get("devices") or [])
    for bus in (reply.get("return") or []):
        walk(bus.get("devices") or [])
    return found

print(json.dumps(query(__SOCKET__, __TIMEOUT__)))
"""


def _qmp_nvme(socket_path: str, container: str | None,
              timeout: float = 3.0) -> list[dict[str, Any]]:
    """NVMe controllers a VM's QMP monitor reports, as [{bdf, qdev_id}].

    Empty on any failure at all -- a wedged monitor on one VM must cost the
    listing that VM's guest BDFs and nothing else.
    """
    script = (_QMP_SCRIPT
              .replace("__SOCKET__", repr(socket_path))
              .replace("__TIMEOUT__", repr(float(timeout))))
    if container:
        # The socket lives in the container's filesystem namespace, so it has
        # to be opened from inside it (same reason inspect.py proxies the
        # guest agent).
        cmd = ["docker", "exec", container, "python3", "-c", script]
    else:
        cmd = [sys.executable, "-I", "-c", script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout + 2)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        out = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return out if isinstance(out, list) else []


def _upgrade_nvme_bdfs(vms: list[dict[str, Any]]) -> None:
    """Fill in guest BDFs for emulated NVMes from each VM's QMP monitor.

    Only VMs that have both a monitor and an unresolved emulated NVMe are
    probed -- on the fleet stacks no guest has either, so the common case
    costs nothing. Those that qualify are probed in parallel because the
    containerised path is a `docker exec` of a few hundred ms and 32 of them
    in series is a visible regression on a command whose job is to print one
    table.
    """
    todo = [v for v in vms
            if v.get("qmp_available") and v.get("qmp_socket")
            and any(d["bdf"] is None for d in v["nvme"])]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
        results = pool.map(
            lambda v: _qmp_nvme(v["qmp_socket"], v["container"]), todo)
        for vm, found in zip(todo, results):
            _merge_nvme(vm["nvme"], found)


def _merge_nvme(devices: list[dict[str, Any]],
                found: list[dict[str, Any]]) -> None:
    by_id = {f["qdev_id"]: f for f in found if f.get("qdev_id")}
    spare = [f for f in found if not f.get("qdev_id")]
    for d in devices:
        if d["bdf"] is not None:
            continue
        hit = by_id.pop(d.get("qdev_id"), None)
        if hit is None:
            # No qdev id to correlate on (the nullb form of --nvme sets none):
            # take controllers in the order QMP reported them.
            hit = spare.pop(0) if spare else None
        if hit is None:
            continue
        d["bdf"] = hit["bdf"]
        d["source"] = "qmp"


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


def _fmt_mgmt(v: dict[str, Any]) -> str:
    """Compact MGMT column: which management sockets are available."""
    parts = []
    if v.get("qmp_available"):
        parts.append("qmp")
    if v.get("ga_available"):
        parts.append("ga")
    return ",".join(parts) or "-"


def _short_bdf(bdf: str) -> str:
    """Drop a 0000 domain for the table; --json keeps the canonical form."""
    return bdf[5:] if bdf.startswith("0000:") else bdf


def _fmt_nvme(v: dict[str, Any]) -> str:
    """The NVME column.

    Four cases, kept short enough to survive a 32-row table:

      00:05.0        guest BDF, read from the VM's QMP monitor
      emul           an emulated NVMe whose guest BDF is not knowable
      host:03:00.0   a passed-through host BDF -- a different address space
                     from the guest's, so it is labelled rather than printed
                     bare, because sending someone to the wrong namespace's
                     device is worse than telling them nothing
      host:03:00.0?  the same, with no sysfs entry on this host to confirm it
                     is an NVMe at all

    Repeats collapse ("emul*2"): VM_NVME=2 is ordinary and two identical
    labels say no more than one plus a count.
    """
    labels: list[str] = []
    for d in v.get("nvme") or []:
        if d["kind"] == "emulated":
            labels.append(_short_bdf(d["bdf"]) if d["bdf"] else "emul")
        else:
            mark = "" if d.get("class_verified") else "?"
            labels.append(f"host:{_short_bdf(d['bdf'])}{mark}")
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return ",".join(f"{k}*{n}" if n > 1 else k for k, n in counts.items()) or "-"


def _print_table(vms: list[dict[str, Any]], qemu_tool_only: bool = False) -> None:
    if not vms:
        print("No qemu-tool VMs running." if qemu_tool_only
              else "No QEMU VMs running.")
        return

    headers = ["NAME", "PID", "SOURCE", "ARCH", "VCPU", "MEM", "SSH",
               "KVM", "UPTIME", "CONTAINER", "NETWORK", "GPU", "RDMA",
               "NVME", "VFIO-USER", "MGMT"]
    show_split = any(v["ssh_port_host"] != v["ssh_port"] for v in vms)
    rows = []
    for v in vms:
        source = v["source"]
        if v["role"]:
            source = f"qemu-tool/{v['role']}"
        rows.append([
            v["name"] or "-",
            str(v["pid"]),
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
            _fmt_nvme(v),
            ",".join(Path(s).name for s in v["vfio_user_sockets"]) or "-",
            _fmt_mgmt(v),
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
