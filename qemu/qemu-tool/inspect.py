from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import VMConfig
from .envfile import (
    find as _find_env,
    overrides as _env_overrides,
    parse_layers as _parse_env_layers,
)
from .list_vms import (
    collect as _collect_vms,
    _device_opts,
    _device_servers,
    _fmt_uptime,
    _gpus as _rocjitsu_gpus,
    _normalise_bdf,
    _pci_class,
    _short_bdf,
)

# A passed-through accelerator is "VGA compatible" (0x0300xx) on a consumer
# part and "Processing accelerator" (0x1200xx) on an Instinct one -- the
# emulated rocjitsu device itself reports class 120000 in the guest, so a
# 0x0300-only filter would miss every MI-class GPU this repo cares about.
_GPU_CLASS_PREFIXES = ("0x0300", "0x1200")

# Only buses QEMU emulates in this repo's launchers (-drive if=virtio, the
# default SATA CD-ROM). A real device is always handed over as vfio-pci and
# reaches the guest as nvme, so nothing real can arrive on these.
_EMULATED_BUS_TYPES = ("virtio", "ide", "sata")

_QEMU_NVME_NQN_PREFIX = "nqn.2019-08.org.qemu:"


def run(
    vm_name: str | None,
    env_file: list[Path] | None,
    as_json: bool = False,
) -> None:
    try:
        vms = _collect_vms()
    except OSError as exc:
        sys.exit(f"Error: cannot read /proc: {exc}")

    if vm_name is not None:
        vms = [v for v in vms if v["name"] == vm_name]
        if not vms:
            sys.exit(f"Error: no running VM named {vm_name!r}")

    cfg = _load_config(env_file)

    # One docker exec per VM at ~300 ms, serial, was 10.9 s over the 32-VM
    # fleet before this query grew any commands. executor.map keeps collect()
    # order so --json stays diffable between runs.
    if len(vms) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(vms))) as pool:
            records = list(pool.map(lambda v: _record(v, cfg), vms))
    else:
        records = [_record(v, cfg) for v in vms]

    if as_json:
        if vm_name is not None and len(records) == 1:
            print(json.dumps(records[0], indent=2))
        else:
            print(json.dumps(records, indent=2))
    else:
        _print_human(records)


def _load_config(env_file: list[Path] | None) -> VMConfig:
    paths = _find_env(env_file)
    env_overrides = _env_overrides(_parse_env_layers(paths)) if paths else {}
    import dataclasses
    d = dataclasses.asdict(VMConfig())
    d.update(env_overrides)
    return VMConfig(**d)


def _read_vm_info(image: str | None) -> dict[str, Any]:
    """Read VM metadata from a sidecar JSON file, if present.

    Checks two locations in order:
    1. <images-dir>/vm-info.json  — the batesste-ci-images schema (schema_version key)
    2. <vm-name>.json             — the sidecar written by gen-vm

    Returns a normalised dict with keys: username, ssh_key_file (public key path).
    """
    if not image:
        return {}
    image_path = Path(image)
    images_dir = image_path.parent

    # batesste-ci-images schema: shared vm-info.json in the images directory.
    ci_info_path = images_dir / "vm-info.json"
    if ci_info_path.exists():
        try:
            raw = json.loads(ci_info_path.read_text())
            if "schema_version" in raw:
                result: dict[str, Any] = {}
                if "username" in raw:
                    result["username"] = raw["username"]
                ssh = raw.get("ssh_keys", {})
                pub = ssh.get("public_key_path", "")
                if pub:
                    # Path is relative to the images dir when it starts with /output/
                    pub_path = images_dir / Path(pub).name
                    if pub_path.exists():
                        result["ssh_key_file"] = str(pub_path)
                return result
        except (OSError, json.JSONDecodeError):
            pass

    # gen-vm sidecar: <vm-name>.json next to the qcow2.
    sidecar = image_path.with_suffix(".json")
    try:
        return json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _ssh_key_for_image(
    image: str | None, info: dict[str, Any], cfg: VMConfig
) -> tuple[list[str], Path | None]:
    """Return (public_key_lines, private_key_path).

    Priority: sidecar ssh_key_file > image-adjacent .pub > config default.
    """
    candidates: list[Path] = []

    sidecar_key = info.get("ssh_key_file")
    if sidecar_key:
        candidates.append(Path(sidecar_key))

    if image:
        candidates.append(Path(image).with_suffix(".pub"))

    candidates.append(cfg.ssh_key_file.expanduser())

    for pub in candidates:
        try:
            text = pub.read_text()
        except OSError:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        private = pub.parent / pub.stem
        return lines, (private if private.exists() else None)

    return [], None


# Runs under `docker exec python3 -c` for a containerised VM and under this
# interpreter as a subprocess otherwise, so subprocess.run's timeout is a hard
# bound in both cases -- an in-process connect() to a wedged guest's socket
# would not be, and inspect must never hang on one VM of a 32-VM fleet.
_GA_SCRIPT = r"""
import base64, socket, json, sys, random, time

def _ga_run(path, timeout=2.0, budget=8.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        return {}
    # recv-only timeout: connect must be blocking to avoid EAGAIN on Unix sockets
    s.settimeout(timeout)

    def send_recv(cmd, args=None):
        req = {"execute": cmd}
        if args is not None:
            req["arguments"] = args
        s.sendall(json.dumps(req).encode() + b"\n")
        raw = b""
        while b"\n" not in raw:
            chunk = s.recv(65536)
            if not chunk:
                break
            raw += chunk
        return json.loads(raw.split(b"\n")[0])

    # Flush stale responses from any previous connection on this socket.
    sync_id = random.randint(1, 2**31)
    s.sendall(json.dumps({"execute": "guest-sync",
                           "arguments": {"id": sync_id}}).encode() + b"\n")
    for _ in range(20):
        try:
            raw = b""
            while b"\n" not in raw:
                chunk = s.recv(65536)
                if not chunk:
                    break
                raw += chunk
            if json.loads(raw.split(b"\n")[0]).get("return") == sync_id:
                break
        except socket.timeout:
            return {}

    deadline = time.monotonic() + budget

    result = {}
    for cmd, key in [("guest-get-host-name", "hostname"),
                      ("guest-network-get-interfaces", "interfaces"),
                      ("guest-get-disks", "disks"),
                      ("guest-get-fsinfo", "fsinfo"),
                      ("guest-get-users", "users")]:
        if time.monotonic() > deadline:
            break
        try:
            r = send_recv(cmd)
            if "return" in r:
                result[key] = r["return"]
            # CommandNotFound: older agent or guest agent compiled without this command.
        except (socket.timeout, ValueError):
            pass

    def read_file(path, count=65536):
        # The agent has no readdir and no command for disk capacity or for
        # GPUs at all, so sysfs/procfs reads are the only read-only source.
        try:
            r = send_recv("guest-file-open", {"path": path, "mode": "r"})
        except (socket.timeout, ValueError):
            return None
        handle = r.get("return")
        if not isinstance(handle, int):
            return None
        try:
            rr = send_recv("guest-file-read", {"handle": handle, "count": count})
            buf = (rr.get("return") or {}).get("buf-b64") or ""
            return base64.b64decode(buf).decode("utf-8", "replace")
        except Exception:
            return None
        finally:
            # An abandoned handle leaks inside someone's live guest.
            try:
                send_recv("guest-file-close", {"handle": handle})
            except Exception:
                pass

    files = {}

    def grab(path):
        if time.monotonic() > deadline:
            return None
        text = read_file(path)
        if text is not None:
            files[path] = text
        return text

    # One read gives every block device's size; cheaper than N sysfs reads.
    grab("/proc/partitions")
    if result.get("users") == []:
        # guest-get-users reads utmp and nothing else, and Ubuntu 26.04 has
        # dropped utmp -- it returns [] there even with someone logged in over
        # ssh. Whether the file exists is what separates "nobody is logged in"
        # from "this agent cannot tell", and an empty answer must not be
        # reported as the first when it is the second.
        if time.monotonic() <= deadline:
            r = None
            try:
                r = send_recv("guest-file-open", {"path": "/var/run/utmp",
                                                  "mode": "r"})
            except (socket.timeout, ValueError):
                pass
            handle = (r or {}).get("return")
            result["utmp"] = isinstance(handle, int)
            if isinstance(handle, int):
                try:
                    send_recv("guest-file-close", {"handle": handle})
                except Exception:
                    pass
    # Node 0 is the CPU node. Probing stops at the first ENOENT because the
    # agent cannot list a directory.
    for n in range(1, 17):
        if grab("/sys/class/kfd/kfd/topology/nodes/%d/properties" % n) is None:
            break
    for d in result.get("disks") or []:
        name = d.get("name") or ""
        if d.get("partition") or not name.startswith("/dev/nvme"):
            continue
        grab("/sys/block/%s/device/subsysnqn" % name.rsplit("/", 1)[-1])

    if files:
        result["files"] = files
    return result

print(json.dumps(_ga_run(__SOCKET__)))
"""


def _ga_query(ga_socket: str | None, container: str | None) -> dict[str, Any]:
    """Query the QEMU guest agent for live guest information.

    For containerised VMs, proxies the query through 'docker exec' since the
    socket lives inside the container's filesystem namespace. For bare VMs,
    runs the same script under this interpreter. Returns an empty dict on any
    error.
    """
    if not ga_socket:
        return {}
    script = _GA_SCRIPT.replace("__SOCKET__", repr(ga_socket))
    if container:
        cmd = ["docker", "exec", container, "python3", "-c", script]
    else:
        # The same script as a subprocess rather than a second inline copy:
        # the old inline version had an unbounded recv loop that spun on EOF
        # and no wall-clock bound at all, and two copies of the command list
        # drifted apart (the hostname bug below lived in both).
        cmd = [sys.executable, "-I", "-c", script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


def _proc_partitions(text: str | None) -> dict[str, int]:
    """Block device base name -> capacity in bytes, from /proc/partitions.

    No guest-get-* command returns a device's capacity: guest-get-disks has no
    size field at all and guest-get-fsinfo reports the size of a mounted
    *filesystem*, which says nothing about an unmounted or raw passed-through
    device. One read of /proc/partitions covers every device in 1 KiB blocks.
    """
    out: dict[str, int] = {}
    for line in (text or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            out[fields[3]] = int(fields[2]) * 1024
        except ValueError:
            continue
    return out


def _gfx_from_kfd(text: str) -> str | None:
    """gfx target of one KFD topology node, or None if it is not a GPU.

    gfx_target_version packs major/minor/step as decimal MMmmss and the minor
    and step digits are read as hex: 120500 -> gfx1250, 90402 -> gfx942. That
    is the spelling the rocjitsu --config filename uses, so the guest's answer
    and the host's can be compared directly.
    """
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "gfx_target_version":
            try:
                v = int(fields[1])
            except ValueError:
                return None
            if v <= 0:
                return None  # node 0 is the CPU node
            return f"gfx{v // 10000}{(v // 100) % 100:x}{v % 100:x}"
    return None


def _guest_gfx(ga_info: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for path, text in (ga_info.get("files") or {}).items():
        if "/kfd/" not in path:
            continue
        gfx = _gfx_from_kfd(text)
        if gfx:
            out.append(gfx)
    return sorted(out)


def _passthrough_gpus(argv: list[str]) -> list[dict[str, Any]]:
    """Real GPUs handed to a VM, as host BDFs off '-device vfio-pci,host=...'.

    A GPU, a NIC and an NVMe are all passed through by that identical
    argument, so the host PCI class decides which is which -- list_vms._gpus
    covers only the emulated rocjitsu sockets and _nvme_devices deliberately
    drops everything that is not NVMe class, so neither reports this.
    """
    out: list[dict[str, Any]] = []
    for i, a in enumerate(argv):
        if a != "-device" or i + 1 >= len(argv):
            continue
        driver, opts = _device_opts(argv[i + 1])
        if driver != "vfio-pci":
            continue
        bdf = _normalise_bdf(opts.get("host", ""))
        if not bdf:
            continue
        cls = _pci_class(bdf)
        if cls is None or not cls.startswith(_GPU_CLASS_PREFIXES):
            continue
        out.append({"gfx": None, "origin": "passthrough", "source": _short_bdf(bdf)})
    return out


def _gpu_devices(vm: dict[str, Any], ga_info: dict[str, Any],
                 argv: list[str]) -> list[dict[str, Any]]:
    """GPUs attached to a VM, each with an origin the host can stand behind.

    Origin is never a guest fact: the guest sees a real-looking KFD node for
    an emulated rocjitsu just as it would for a passed-through card. Only the
    command line distinguishes them -- a vfio-user socket served by rocjitsu
    is emulated by construction, a vfio-pci host BDF of GPU class is real.
    The guest supplies the gfx target for the real case, where no --config
    filename exists to read it off.
    """
    out: list[dict[str, Any]] = []
    for sock in vm.get("vfio_user_sockets") or []:
        for gfx in _rocjitsu_gpus([sock]):
            out.append({"gfx": gfx, "origin": "emulated", "source": Path(sock).name})

    passthrough = _passthrough_gpus(argv)
    out.extend(passthrough)

    unclaimed = list(_guest_gfx(ga_info))
    for entry in out:
        if entry["gfx"] in unclaimed:
            unclaimed.remove(entry["gfx"])

    # A single real GPU and a single gfx the emulated ones did not account for
    # is the only pairing that is not a guess. Anything else stays unknown.
    if len(passthrough) == 1 and len(unclaimed) == 1 and passthrough[0]["gfx"] is None:
        passthrough[0]["gfx"] = unclaimed.pop()

    for gfx in unclaimed:
        out.append({"gfx": gfx, "origin": "unknown", "source": "guest"})
    return out


def _block_origin(disk: dict[str, Any], subsysnqn: str | None,
                  nvme: list[dict[str, Any]]) -> tuple[str, str | None]:
    """(origin, host_bdf) for one whole disk the guest reported."""
    bus = (disk.get("address") or {}).get("bus-type")
    if bus in _EMULATED_BUS_TYPES:
        return "emulated", None
    if bus != "nvme":
        return "unknown", None

    nqn = (subsysnqn or "").strip()
    serial = (disk.get("address") or {}).get("serial") or ""
    for d in nvme:
        argv_serial = d.get("serial")
        if d.get("kind") != "emulated" or not argv_serial:
            continue
        # QEMU space-pads the serial into the 20-byte identify field and the
        # guest's udev folds it into ID_SERIAL as "QEMU_NVMe_Ctrl_<serial>..._1",
        # so equality can never match -- but the subsystem NQN carries it
        # unpadded.
        if nqn.endswith(":" + argv_serial) or argv_serial in serial:
            return "emulated", None
    if nqn.startswith(_QEMU_NVME_NQN_PREFIX):
        return "emulated", None

    hostdevs = [d for d in nvme if d.get("kind") == "passthrough"]
    if len(hostdevs) == 1:
        return "passthrough", hostdevs[0].get("bdf")
    if hostdevs:
        return "passthrough", None
    return "unknown", None


def _block_devices(vm: dict[str, Any],
                   ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Whole block devices in the guest, or None if the agent could not say.

    None and [] are different answers: no agent to ask is not the same fact as
    a guest with no disks, and only the guest knows the /dev names at all.
    """
    disks = ga_info.get("disks")
    if disks is None:
        return None
    files = ga_info.get("files") or {}
    sizes = _proc_partitions(files.get("/proc/partitions"))
    mounts = _mountpoints(ga_info.get("fsinfo"))
    nvme = vm.get("nvme") or []

    out: list[dict[str, Any]] = []
    for disk in disks:
        # Partitions carry no address at all; they are folded into the parent
        # device they depend on.
        if disk.get("partition"):
            continue
        name = disk.get("name") or ""
        base = name.rsplit("/", 1)[-1]
        if not base:
            continue
        size = sizes.get(base)
        if base.startswith("loop") and not size:
            continue  # an unbacked loop device is not a disk anyone asked about
        subsysnqn = files.get(f"/sys/block/{base}/device/subsysnqn")
        origin, bdf = _block_origin(disk, subsysnqn, nvme)
        out.append({
            "dev": name,
            "bytes": size,
            "size": _fmt_bytes(size),
            "bus": (disk.get("address") or {}).get("bus-type"),
            "origin": origin,
            "host_bdf": bdf,
            "mountpoints": mounts.get(base, []),
        })
    return out


def _mountpoints(fsinfo: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Parent device base name -> mountpoints of its filesystems."""
    out: dict[str, list[str]] = {}
    for fs in fsinfo or []:
        mount = fs.get("mountpoint")
        if not mount:
            continue
        for disk in fs.get("disk") or []:
            dev = (disk.get("dev") or "").rsplit("/", 1)[-1]
            if not dev:
                continue
            # vda1 belongs to vda; a 9p mount has an empty disk list and so
            # never reaches here, which is right -- it is not a block device.
            parent = dev.rstrip("0123456789") or dev
            out.setdefault(parent, []).append(mount)
    return out


def _ssh_sessions(ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Logged-in users, or None when the agent could not answer.

    guest-get-users dedupes by username and reads utmp, so it cannot tell an
    ssh login from a console one and reports users, not sessions.

    An empty answer from a guest with no utmp is unknown, not none: verified
    on this repo's Ubuntu 26.04 guests, where guest-get-users returns [] with
    an ssh session open because /var/run/utmp does not exist. The agent has
    no logind-backed command, so there is nothing to fall back to -- only
    read-only guest-exec of loginctl would answer, and running code inside
    someone's live guest is not worth a session list.
    """
    users = ga_info.get("users")
    if users is None:
        return None
    if not users and ga_info.get("utmp") is False:
        return None
    out: list[dict[str, Any]] = []
    for u in users:
        name = u.get("user")
        if not name:
            continue
        login = u.get("login-time")
        out.append({
            "user": name,
            "login_time": float(login) if isinstance(login, (int, float)) else None,
        })
    return out


def _vm_argv(pid: int) -> list[str]:
    """The VM's command line. list_vms.collect() parses and discards it."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _record(vm: dict[str, Any], cfg: VMConfig) -> dict[str, Any]:
    info = _read_vm_info(vm["image"])
    pub_keys, private_key = _ssh_key_for_image(vm["image"], info, cfg)
    username = info.get("username") or cfg.username
    ga_info = _ga_query(vm.get("ga_socket"), vm["container"])

    port = vm["ssh_port_host"]
    identity = f" -i {private_key}" if private_key is not None else ""
    ssh_cmd = (
        f"ssh{identity} -p {port} {username}@localhost"
        if port is not None
        else None
    )

    record: dict[str, Any] = {
        "name": vm["name"],
        "pid": vm["pid"],
        "arch": vm["arch"],
        "vcpus": vm["vcpus"],
        "memory_mib": vm["memory_mib"],
        "ssh_host": "localhost",
        "ssh_port": port,
        "username": username,
        "ssh_public_keys": pub_keys,
        "ssh_cmd": ssh_cmd,
        "image": vm["image"],
        "container": vm["container"],
        "uptime_seconds": vm["uptime_seconds"],
        "vfio_user_sockets": vm["vfio_user_sockets"],
        "qmp_socket": vm.get("qmp_socket"),
        "qmp_available": vm.get("qmp_available", False),
        "ga_socket": vm.get("ga_socket"),
        "ga_available": vm.get("ga_available", False),
        "gpus": _gpu_devices(vm, ga_info, _vm_argv(vm["pid"])),
        "block_devices": _block_devices(vm, ga_info),
        "ssh_sessions": _ssh_sessions(ga_info),
    }
    if ga_info:
        # The raw sysfs and procfs text is an input to the three collectors
        # above, not an answer: several KiB per VM of it would bury --json,
        # which already takes 60 lines to get through one VM's interfaces.
        ga_info.pop("files", None)
        record["guest"] = ga_info
    return record


def _print_human(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No QEMU VMs running.")
        return
    for r in records:
        name = r["name"] or "-"
        print(f"VM: {name}  (pid {r['pid']})")
        ssh = r["ssh_cmd"] or f"port {r['ssh_port'] or '-'}"
        print(f"  SSH:       {ssh}")
        print(f"  User:      {r['username']}")
        for key in r["ssh_public_keys"]:
            print(f"  Key:       {key}")
        vcpus = r["vcpus"] or "-"
        mem = f"{r['memory_mib']} MiB" if r["memory_mib"] else "-"
        print(f"  Arch:      {r['arch']}  vCPUs: {vcpus}  Memory: {mem}")
        for gpu in r.get("gpus") or []:
            gfx = gpu["gfx"] or "unknown gfx"
            source = f", {gpu['source']}" if gpu.get("source") else ""
            print(f"  GPU:       {gfx} ({gpu['origin']}{source})")
        print(f"  Image:     {r['image'] or '-'}")
        if r["container"]:
            print(f"  Container: {r['container']}")
        print(f"  Uptime:    {_fmt_uptime(r['uptime_seconds'])}")
        if r["vfio_user_sockets"]:
            socks = ", ".join(Path(s).name for s in r["vfio_user_sockets"])
            print(f"  VFIO:      {socks}")
        if r.get("qmp_socket"):
            avail = " (available)" if r.get("qmp_available") else " (unavailable)"
            print(f"  QMP:       {r['qmp_socket']}{avail}")
        if r.get("ga_socket"):
            avail = " (available)" if r.get("ga_available") else " (unavailable)"
            print(f"  GA:        {r['ga_socket']}{avail}")
        guest = r.get("guest", {})
        if guest.get("hostname"):
            print(f"  Hostname:  {guest['hostname'].get('host-name', '-')}")
        if guest.get("interfaces"):
            for iface in guest["interfaces"]:
                for addr in iface.get("ip-addresses", []):
                    if addr.get("ip-address-type") == "ipv4":
                        print(f"  IP:        {addr['ip-address']} ({iface['name']})")
        for line in _disk_lines(r.get("block_devices")):
            print(f"  Disk:      {line}")
        login = _login_line(r)
        if login is not None:
            print(f"  Login:     {login}")
        print()


_DISK_LINE_LIMIT = 6


def _disk_lines(devices: list[dict[str, Any]] | None) -> list[str]:
    """One line per disk, in the repeated-label style of Key: and IP:.

    Nothing at all when the agent could not answer, matching how a failed
    hostname or interfaces query leaves no trace in the record.
    """
    if not devices:
        return []
    lines = []
    for d in devices[:_DISK_LINE_LIMIT]:
        origin = d["origin"]
        if origin == "passthrough" and d.get("host_bdf"):
            # A host BDF is a different address space from the guest's, so it
            # is labelled rather than printed bare.
            origin = f"passthrough host:{_short_bdf(d['host_bdf'])}"
        bus = f" ({d['bus']})" if d.get("bus") else ""
        mounts = "  " + ",".join(d["mountpoints"]) if d.get("mountpoints") else ""
        lines.append(f"{d['dev']:<14} {d['size']:>10}  {origin}{bus}{mounts}")
    extra = len(devices) - _DISK_LINE_LIMIT
    if extra > 0:
        lines.append(f"+{extra} more")
    return lines


def _login_line(record: dict[str, Any]) -> str | None:
    """"none", "unknown", or the logged-in users.

    Nobody logged in and nobody to ask are different facts, so an agent that
    is there but did not answer says unknown rather than claiming the guest
    is empty. A VM with no agent socket at all prints no line, like the rest
    of the guest-derived output.
    """
    sessions = record.get("ssh_sessions")
    if sessions is None:
        return "unknown" if record.get("ga_socket") else None
    if not sessions:
        return "none"
    out = []
    for s in sessions:
        since = ""
        if s.get("login_time"):
            since = time.strftime(" (since %H:%M)", time.localtime(s["login_time"]))
        out.append(f"{s['user']}{since}")
    return ", ".join(out)
