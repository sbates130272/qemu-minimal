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
    return {
        "pid": pid,
        "name": name,
        "source": "qemu-tool" if role else "external",
        "role": role,
        "arch": _arch(argv[0]),
        "vcpus": _vcpus(argv),
        "memory_mib": _memory(argv),
        "ssh_port": int(ssh.group(1)) if ssh else None,
        "kvm": "accel=kvm" in machine,
        "image": image,
        "vfio_user_sockets": _vfio_sockets(argv),
        "container": containers.get(_container_id(pid) or "", None),
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


def _container_names() -> dict[str, str]:
    """Full container id -> name. Best effort; docker may be absent."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    names = {}
    for line in out.stdout.splitlines():
        cid, _, name = line.partition("\t")
        if cid and name:
            names[cid] = name
    return names


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


def _print_table(vms: list[dict[str, Any]], qemu_tool_only: bool = False) -> None:
    if not vms:
        print("No qemu-tool VMs running." if qemu_tool_only
              else "No QEMU VMs running.")
        return

    headers = ["PID", "NAME", "SOURCE", "ARCH", "VCPU", "MEM", "SSH",
               "KVM", "UPTIME", "CONTAINER", "VFIO-USER"]
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
            str(v["ssh_port"] or "-"),
            "yes" if v["kvm"] else "no",
            _fmt_uptime(v["uptime_seconds"]),
            v["container"] or "-",
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
