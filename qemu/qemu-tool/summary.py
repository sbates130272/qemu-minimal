"""Fleet-level aggregate of `list`, plus a measured load picture.

`list` prints one row per VM. On the scale-out stacks that is 32 rows of
mostly identical text, and the questions an operator actually has -- how much
of this node is left, is anything on fire, is anyone logged in -- are not
answerable by reading it. This is the aggregate of the same bytes, with four
cheap host-side collectors and one opt-out guest-side one bolted on.

Every number here has a defined unavailable state and renders it. A zero that
means "could not measure" is the specific defect this module exists to avoid:
an idle fleet and a fleet whose stats files could not be read look identical
if the unreadable case contributes 0.

Cost, measured on the live 32-VM mi100-fleet (100 containers, 192 cores):

    list's own collect()                      ~1.3 s
    docker ps -a (compose labels, roll-call)  ~0.2 s
    cgroup cpu/memory, 98 scopes, x2 samples  ~0.1 s
    ernic stats, one docker exec for all 32   ~0.2 s
    images: stat + qemu-img info 8-wide       ~0.2 s
    the sample window itself                   5.0 s
    guest sessions, 8-wide, overlapped         (hidden under the window)
    ------------------------------------------------
    qemu-tool list --summary                   ~6.9 s
    qemu-tool list --summary --no-guests       ~6.8 s

The window dominates, and it cannot shrink much: the ernic servers rewrite
their stats files at 1 Hz, so a window under ~2 s samples noise rather than a
rate. `docker stats --no-stream` is deliberately not used anywhere -- it costs
4.2 s for 100 containers against 45 ms for the same numbers read out of
cgroup v2 sysfs.
"""

from __future__ import annotations

import json
import os
import re
import socket as _socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import ernic_stats
from . import inspect as inspect_mod
from . import list_vms

# Long enough that the ernic 1 Hz stats rewrite has happened at least twice
# inside it; shorter windows report the quantisation of that rewrite rather
# than a rate.
SAMPLE_SECONDS = 5.0

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC = Path("/proc")
_CLK_TCK = os.sysconf("SC_CLK_TCK")

# AGENTS.md issue 17: the mesh stops registering new workers at about 40
# nodes. The 64-node topology payload cap in rdma_backend_tcp.c is NOT the
# ceiling -- a full mesh's per-registration cost grows with the mesh until
# registration cannot finish against a fixed timeout, measured at ~40 on a
# 128-core host. Expect it to move with host speed, hence the "~".
MESH_NODE_WALL = 40

# AGENTS.md issue 14: the manager's 20 s heartbeat timeout is not tunable, so
# CPU oversubscription evicts live nodes and kills their guests. Measured
# budget is nproc plus about 25%.
VCPU_OVERCOMMIT = 1.25

# The three services that exist once per VM on every stack this repo ships.
# Everything else in the project is rolled up as "infra" -- naming each of
# prometheus/grafana/exporter in a one-line header would crowd out the
# roll-call that matters.
_VM_SERVICE_CLASSES = ("qemu", "ernic", "rocjitsu")

_SERVICE_INDEX_RE = re.compile(r"-\d+$")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(as_json: bool = False, qemu_tool_only: bool = False,
        guests: bool = True, sample_seconds: float = SAMPLE_SECONDS) -> None:
    try:
        vms = list_vms.collect()
    except OSError as exc:
        sys.exit(f"Error: cannot read {_PROC}: {exc}")
    if qemu_tool_only:
        vms = [v for v in vms if v["source"] == "qemu-tool"]
    data = collect(vms, guests=guests, sample_seconds=sample_seconds)
    if as_json:
        print(json.dumps(data, indent=2))
        return
    for line in render(data):
        print(line)


def collect(vms: list[dict[str, Any]], *, guests: bool = True,
            sample_seconds: float = SAMPLE_SECONDS) -> dict[str, Any]:
    """One object, whatever the VM count.

    Deliberately not shaped like `inspect --json`, which prints a dict for one
    VM and a list for many: a consumer there has to sniff the type before it
    can read anything.
    """
    containers = _compose_containers()
    by_name = {c["name"]: c for c in containers.values()}
    groups = _group(vms, by_name)

    # The guest probe is the only collector that contacts a guest and the only
    # one that costs seconds rather than milliseconds, so it runs underneath
    # the sample window instead of after it. Started first, joined last.
    pool = None
    futures: dict[int, Any] = {}
    if guests and vms:
        pool = ThreadPoolExecutor(max_workers=min(8, len(vms)))
        futures = {id(v): pool.submit(_vm_sessions, v) for v in vms}

    try:
        ernic = {p: _ernic_fleet(p, containers) for p in groups}
        storage = {p: _storage(g) for p, g in groups.items()}
        first = _cgroup_sample(containers)
        proc_first = {v["pid"]: _proc_cpu(v["pid"]) for v in vms}
        t0 = time.monotonic()
        time.sleep(sample_seconds)
        elapsed = time.monotonic() - t0
        second = _cgroup_sample(containers)
        proc_second = {v["pid"]: _proc_cpu(v["pid"]) for v in vms}
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

    sessions = {id(v): (futures[id(v)].result() if futures else None)
                for v in vms}

    host = _host()
    fleets = []
    for project, members in sorted(groups.items()):
        fleets.append(_fleet(
            project, members, containers, host, ernic[project],
            storage[project], first, second, proc_first, proc_second,
            elapsed, sessions, guests,
        ))
    return {
        "schema": 1,
        "sampled_seconds": round(elapsed, 2),
        "guests_probed": guests,
        "host": host,
        "fleets": fleets,
    }


# ---------------------------------------------------------------------------
# Host facts
# ---------------------------------------------------------------------------

def _host() -> dict[str, Any]:
    return {
        # First label only: the FQDN doubles the width of the header line and
        # the domain is the same for every node someone is comparing.
        "hostname": _socket.gethostname().split(".")[0],
        "cores": os.cpu_count(),
        "memory_mib": _meminfo_mib("MemTotal"),
        "memory_available_mib": _meminfo_mib("MemAvailable"),
        "loadavg": _loadavg(),
        "loadavg_scope": "host",
        "source": "proc",
    }


def _meminfo_mib(key: str) -> int | None:
    try:
        text = (_PROC / "meminfo").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(key + ":"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024
    return None


def _loadavg() -> list[float] | None:
    try:
        return [float(x) for x in (_PROC / "loadavg").read_text().split()[:3]]
    except (OSError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Container inventory and grouping
# ---------------------------------------------------------------------------

def _service_class(service: str) -> str:
    """"ernic-17" -> "ernic"; "ernic-stats-exporter" is left alone.

    The generated stacks number every per-VM service, so the compose service
    label is unique per container and useless as a grouping key until the
    index comes off. Only a trailing -<digits> is stripped, which is why the
    exporter survives intact.
    """
    return _SERVICE_INDEX_RE.sub("", service)


def _compose_containers() -> dict[str, dict[str, Any]]:
    """Full container id -> compose identity and state, including stopped ones.

    `docker ps -a`, not the `docker ps` list_vms already makes: the roll-call
    needs the denominator, and a container that died is exactly the one worth
    reporting. 0.2 s for 100 containers.
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format",
             '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}'
             '\t{{.Label "com.docker.compose.project"}}'
             '\t{{.Label "com.docker.compose.service"}}'],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6 or not parts[0]:
            continue
        cid, name, state, status, project, service = parts
        found[cid] = {
            "id": cid,
            "name": name,
            "state": state,
            "status": status,
            "project": project or None,
            "service": service or None,
            "class": _service_class(service) if service else None,
        }
    return found


def _group(vms: list[dict[str, Any]],
           by_name: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """VMs keyed by compose project; uncontainerised ones land under "-".

    Grouping on the compose project label rather than a name prefix: a prefix
    rule guesses, and two stacks whose VM names share a prefix would merge
    into one fleet that does not exist.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for vm in vms:
        c = by_name.get(vm.get("container") or "")
        groups.setdefault((c or {}).get("project") or "-", []).append(vm)
    return groups


def _roll_call(project: str,
               containers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """running/total per service class for one project.

    A class whose every container has exited 0 is a one-shot that completed --
    the tapsetup sidecars that hold CAP_NET_ADMIN just long enough to make the
    TAP are 32 Exited(0) containers by design. Counting them as 0/32 down
    would make a healthy fleet read as half dead, so they are reported
    separately as completed rather than against the roll-call.
    """
    classes: dict[str, dict[str, int]] = {}
    completed: list[str] = []
    for c in containers.values():
        if c["project"] != project or not c["class"]:
            continue
        slot = classes.setdefault(c["class"], {"running": 0, "total": 0,
                                               "exited_ok": 0})
        slot["total"] += 1
        if c["state"] == "running":
            slot["running"] += 1
        elif c["state"] == "exited" and "(0)" in c["status"]:
            slot["exited_ok"] += 1
    for name, slot in list(classes.items()):
        if slot["total"] and slot["exited_ok"] == slot["total"]:
            completed.append(name)
            del classes[name]
    services = {k: {"running": v["running"], "total": v["total"]}
                for k, v in classes.items()}
    infra = {"running": 0, "total": 0}
    for name, slot in services.items():
        if name not in _VM_SERVICE_CLASSES:
            infra["running"] += slot["running"]
            infra["total"] += slot["total"]
    return {
        "services": services,
        "infra": infra,
        "completed": sorted(completed),
        "source": "docker-ps" if containers else "unavailable",
    }


# ---------------------------------------------------------------------------
# CPU and memory, from cgroup v2 and /proc
# ---------------------------------------------------------------------------

def _scope_paths() -> dict[str, Path]:
    """container id -> its cgroup directory, for both Docker cgroup drivers.

    systemd driver puts a scope under system.slice; cgroupfs driver puts a
    plain directory under /sys/fs/cgroup/docker. Globbing the two known
    layouts is 1 ms; walking the whole hierarchy to find them is not.
    """
    found: dict[str, Path] = {}
    for p in _CGROUP_ROOT.glob("system.slice/docker-*.scope"):
        found[p.name[len("docker-"):-len(".scope")]] = p
    for p in _CGROUP_ROOT.glob("docker/*"):
        if p.is_dir():
            found[p.name] = p
    return found


def _read_kv(path: Path) -> dict[str, int]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, val = line.partition(" ")
        try:
            out[key] = int(val)
        except ValueError:
            continue
    return out


def _cgroup_sample(containers: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-container cpu usage and memory, one pass over the cgroup files.

    cpu.stat usage_usec is cumulative, so a rate needs two of these. Memory is
    instantaneous and the second sample simply wins.
    """
    scopes = _scope_paths()
    out: dict[str, dict[str, int]] = {}
    for cid in containers:
        scope = scopes.get(cid)
        if scope is None:
            continue
        cpu = _read_kv(scope / "cpu.stat")
        if "usage_usec" not in cpu:
            continue
        mem = _read_kv(scope / "memory.stat")
        try:
            current = int((scope / "memory.current").read_text().strip())
        except (OSError, ValueError):
            current = 0
        out[cid] = {
            "usage_usec": cpu["usage_usec"],
            # shmem, not anon. Guest RAM on these stacks is a
            # memory-backend-memfd with share=on, so it lands in the cgroup as
            # shmem; anon across all 32 qemu containers is 2.0 GiB against
            # 41.9 GiB of shmem. Reporting anon as "guest RAM in use" is 20x
            # low.
            "shmem": mem.get("shmem", 0),
            "current": current,
        }
    return out


def _proc_cpu(pid: int) -> int | None:
    """utime+stime in clock ticks, or None if the process is gone.

    Same parse as list_vms._uptime: comm can contain spaces and parens, so
    everything is taken positionally after the final ')'.
    """
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    tail = stat.rpartition(")")[2].split()
    try:
        return int(tail[11]) + int(tail[12])
    except (IndexError, ValueError):
        return None


def _cores(delta_usec: int, elapsed: float) -> float:
    return delta_usec / 1e6 / elapsed if elapsed > 0 else 0.0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _vm_images(vm: dict[str, Any]) -> list[str]:
    paths = []
    if vm.get("image"):
        paths.append(vm["image"])
    for dev in vm.get("nvme") or []:
        if dev.get("image"):
            paths.append(dev["image"])
    return paths


def _qemu_img_info(path: str) -> dict[str, Any] | None:
    """virtual size and backing file for one image.

    --force-share is not optional: these files are open by a running QEMU and
    qemu-img refuses to look at them otherwise.

    Run per distinct path rather than once per guessed "image shape": nothing
    in a filename tells you its virtual size, so a representative-per-pattern
    heuristic would be inventing the number for every file it skipped. 64
    forks 8-wide is ~0.2 s, which is cheaper than being wrong.
    """
    try:
        r = subprocess.run(
            ["qemu-img", "info", "--force-share", "--output=json", path],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _storage(vms: list[dict[str, Any]]) -> dict[str, Any]:
    """Host-side disk figures for a fleet's images.

    HOST-side, and the distinction is load-bearing. Asking the guests instead
    gives 301.5 GiB used across 32 guests against the 25.9 GiB this node
    actually holds -- every guest counts the whole shared backing file as its
    own. Both answers are correct; this one is the one that decides whether
    another VM fits.
    """
    paths = sorted({p for vm in vms for p in _vm_images(vm)})
    if not paths:
        return {"allocated_bytes": None, "virtual_bytes": None,
                "free_bytes": None, "images": 0, "source": "unavailable"}

    allocated = 0
    counted: set[tuple[int, int]] = set()
    filesystems: dict[int, int] = {}

    def account(path: str) -> None:
        # Dedupe by (device, inode), not by path: the shared backing file is
        # reached once per overlay and counting it 32 times would turn 15.8 G
        # into 506 G.
        nonlocal allocated
        try:
            st = os.stat(path)
        except OSError:
            return
        key = (st.st_dev, st.st_ino)
        if key in counted:
            return
        counted.add(key)
        allocated += st.st_blocks * 512
        if st.st_dev not in filesystems:
            try:
                v = os.statvfs(path)
                filesystems[st.st_dev] = v.f_bavail * v.f_frsize
            except OSError:
                pass

    for path in paths:
        account(path)

    with ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
        infos = list(pool.map(_qemu_img_info, paths))

    virtual = 0
    measured = 0
    for info in infos:
        if not info:
            continue
        size = info.get("virtual-size")
        if isinstance(size, int):
            virtual += size
            measured += 1
        backing = info.get("full-backing-filename")
        if backing:
            account(backing)

    return {
        "allocated_bytes": allocated,
        # None rather than 0 when not one image could be read: 0 virtual bytes
        # would render as an infinitely safe fleet.
        "virtual_bytes": virtual if measured else None,
        "images": len(paths),
        "images_measured": measured,
        "free_bytes": sum(filesystems.values()) if filesystems else None,
        "source": "stat+qemu-img" if measured else "stat",
    }


# ---------------------------------------------------------------------------
# ernic mesh
# ---------------------------------------------------------------------------

def _ernic_dumps(container: str) -> dict[str, str]:
    """Every ernic stats file in the shared volume, read in one docker exec.

    The volume is mounted by every ernic, qemu and rocjitsu container in the
    stack, so one exec into any of them reads all N files for ~0.2 s. The host
    path under /var/lib/docker/volumes is root-only, and the metrics-profile
    exporter publishes no host port -- neither is a usable source for a CLI.

    The exporter's HTTP endpoint is reachable at the container's fleet address
    and is faster (35 ms), but it is deliberately not used: it emits counters
    only, having dropped the `connection` and `backend` strings it parsed, and
    those are the whole of the mesh-liveness answer.
    """
    sep = "\x1e"
    script = (f'for f in /run/ernic-stats/*.stats; do printf "{sep}%s\\n" "$f"; '
              'cat "$f" 2>/dev/null; done')
    try:
        r = subprocess.run(["docker", "exec", container, "sh", "-c", script],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    dumps: dict[str, str] = {}
    for record in r.stdout.split(sep):
        name, _, body = record.partition("\n")
        name = name.strip()
        if name.endswith(".stats"):
            dumps[name] = body
    return dumps


def _ernic_fleet(project: str,
                 containers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    unavailable = {"instances": 0, "expected": 0, "connected": None,
                   "rdma_bytes": None, "ip_bytes_tx": None,
                   "resets": None, "source": "unavailable"}
    hosts = [c for c in containers.values()
             if c["project"] == project and c["class"] == "ernic"
             and c["state"] == "running"]
    if not hosts:
        return unavailable
    expected = len(hosts)
    dumps = _ernic_dumps(sorted(hosts, key=lambda c: c["name"])[0]["name"])
    if not dumps:
        out = dict(unavailable)
        out["expected"] = expected
        return out

    connected = 0
    rdma = 0
    tx = 0
    resets = 0
    read = 0
    for text in dumps.values():
        device, _qps = ernic_stats.parse(text)
        if not device:
            # The server rewrites in place with no atomic rename, so a read
            # can land mid-write. An unreadable instance shrinks the
            # denominator; it must never contribute a zero that reads as an
            # idle node.
            continue
        read += 1
        if str(device.get("connection", "")).startswith("connected"):
            connected += 1
        for key in ("total_bytes_sent", "total_bytes_received",
                    "total_bytes_rdma_read", "total_bytes_rdma_write"):
            val = device.get(key)
            if isinstance(val, int):
                rdma += val
        # tx only. The l2 bridge floods every frame to all N guests, so
        # summing rx counts the same ~0.9 MB once per guest and reports 31 MB
        # of fleet Ethernet traffic on a fleet that offered 1 MB.
        if isinstance(device.get("total_ip_bytes_tx"), int):
            tx += device["total_ip_bytes_tx"]
        if isinstance(device.get("reset_count"), int):
            resets += device["reset_count"]
    if not read:
        out = dict(unavailable)
        out["expected"] = expected
        return out
    return {
        "instances": read,
        "expected": expected,
        "connected": connected,
        "rdma_bytes": rdma,
        "ip_bytes_tx": tx,
        "resets": resets,
        "source": "ernic-stats",
    }


# ---------------------------------------------------------------------------
# Guest sessions
# ---------------------------------------------------------------------------

def _vm_sessions(vm: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Login sessions on one guest, or None when we could not tell.

    Straight through inspect's machinery, which already knows that this repo's
    Ubuntu 26.04 guests ship no utmp (guest-get-users returns [] with someone
    logged in) and that the two CLASS=manager linger sessions every fleet
    guest carries are not logins. A naive count reports 64 users on an idle
    32-VM fleet.
    """
    if not vm.get("ga_available"):
        return None
    ga = inspect_mod._ga_query(vm.get("ga_socket"), vm.get("container"))
    if not ga:
        return None
    return inspect_mod._ssh_sessions(ga)


def _sessions_summary(members: list[dict[str, Any]],
                      sessions: dict[int, Any],
                      guests: bool) -> dict[str, Any]:
    if not guests:
        return {"sessions": None, "occupied": None, "unknown": None,
                "users": None, "guests": len(members),
                "source": "not-collected"}
    total = 0
    occupied = []
    unknown = 0
    users: dict[str, int] = {}
    for vm in members:
        found = sessions.get(id(vm))
        if found is None:
            # Kept separate from zero all the way to the renderer: "nobody is
            # logged in" and "no agent answered" are different answers and
            # folding them loses the one that matters.
            unknown += 1
            continue
        if not found:
            continue
        total += len(found)
        occupied.append({"vm": vm["name"], "sessions": found})
        for s in found:
            users[s["user"]] = users.get(s["user"], 0) + 1
    return {
        "sessions": total,
        "occupied": occupied,
        "unknown": unknown,
        "users": users,
        "guests": len(members),
        "source": "guest-agent",
    }


# ---------------------------------------------------------------------------
# Per-fleet assembly
# ---------------------------------------------------------------------------

def _fleet(project: str, members: list[dict[str, Any]],
           containers: dict[str, dict[str, Any]], host: dict[str, Any],
           ernic: dict[str, Any], storage: dict[str, Any],
           first: dict[str, dict[str, int]], second: dict[str, dict[str, int]],
           proc_first: dict[int, int | None], proc_second: dict[int, int | None],
           elapsed: float, sessions: dict[int, Any],
           guests: bool) -> dict[str, Any]:
    by_name = {c["name"]: c for c in containers.values()}
    member_cids = {by_name[vm["container"]]["id"]: vm
                   for vm in members
                   if vm.get("container") and vm["container"] in by_name}

    cpu = _cpu(project, members, containers, member_cids, first, second,
               proc_first, proc_second, elapsed)
    memory = _memory(members, member_cids, second)
    headroom = _headroom(members, host, ernic, storage, memory)
    return {
        "project": project if project != "-" else None,
        "vms": len(members),
        "uptime_seconds": _uptime_range(members),
        "roll_call": _roll_call(project, containers),
        "headroom": headroom,
        "cpu": cpu,
        "memory": memory,
        "storage": storage,
        "network": ernic,
        "gpu": _gpu(members),
        "logins": _sessions_summary(members, sessions, guests),
        "outliers": _outliers(cpu, containers, project),
    }


def _uptime_range(members: list[dict[str, Any]]) -> dict[str, int | None]:
    vals = [v["uptime_seconds"] for v in members
            if v.get("uptime_seconds") is not None]
    if not vals:
        return {"min": None, "max": None}
    return {"min": min(vals), "max": max(vals)}


def _cpu(project: str, members: list[dict[str, Any]],
         containers: dict[str, dict[str, Any]],
         member_cids: dict[str, dict[str, Any]],
         first: dict[str, dict[str, int]], second: dict[str, dict[str, int]],
         proc_first: dict[int, int | None], proc_second: dict[int, int | None],
         elapsed: float) -> dict[str, Any]:
    """Cores busy over the window, split by service class, plus per-VM.

    The split is the point. A QEMU process burning CPU is not the guest being
    busy, and on an idle fleet the ernic mesh sidecars burn MORE than the VMs
    they serve -- measured 5.4 ernic cores against 6.6 qemu. A summary that
    reported only QEMU would understate the fleet's cost on this node by over
    2x.
    """
    by_class: dict[str, float] = {}
    for cid, cur in second.items():
        prev = first.get(cid)
        meta = containers.get(cid)
        if not prev or not meta or meta["project"] != project or not meta["class"]:
            continue
        delta = cur["usage_usec"] - prev["usage_usec"]
        if delta < 0:
            continue  # the container restarted inside the window
        by_class[meta["class"]] = by_class.get(meta["class"], 0.0) + _cores(
            delta, elapsed)

    per_vm = []
    for vm in members:
        a, b = proc_first.get(vm["pid"]), proc_second.get(vm["pid"])
        cores = None
        if a is not None and b is not None and b >= a:
            cores = (b - a) / _CLK_TCK / elapsed if elapsed > 0 else 0.0
        per_vm.append({"vm": vm["name"], "container": vm.get("container"),
                       "cores_busy": None if cores is None else round(cores, 2)})
        # An uncontainerised VM has no cgroup scope, so its cost would vanish
        # from the class split that the containerised ones dominate.
        if cores is not None and not vm.get("container"):
            by_class["qemu"] = by_class.get("qemu", 0.0) + cores

    return {
        "cores_busy": round(sum(by_class.values()), 2) if by_class else None,
        "cores_total": os.cpu_count(),
        "by_service": {k: round(v, 2) for k, v in sorted(by_class.items())},
        "per_vm": per_vm,
        "source": "cgroup" if by_class else "unavailable",
    }


def _memory(members: list[dict[str, Any]],
            member_cids: dict[str, dict[str, Any]],
            sample: dict[str, dict[str, int]]) -> dict[str, Any]:
    allocated = sum(v["memory_mib"] or 0 for v in members)
    shmem = 0
    current = 0
    seen = 0
    for cid in member_cids:
        cur = sample.get(cid)
        if not cur:
            continue
        seen += 1
        shmem += cur["shmem"]
        current += cur["current"]
    return {
        "allocated_mib": allocated or None,
        # Two different questions, never one column: shmem is guest RAM the
        # guests have touched, memory.current additionally folds in qcow2 page
        # cache and is "what the host cannot reclaim cheaply".
        "guest_resident_bytes": shmem if seen else None,
        "unreclaimable_bytes": current if seen else None,
        "containers_measured": seen,
        "source": "cgroup" if seen else "unavailable",
    }


def _gpu(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Count and target. Not utilisation, which does not exist here.

    rocjitsu exposes no stats interface of any kind -- its entire option set
    is --config/--cpu-thread-budget/--wait-checking/--vfio-socket/
    --thread-budget-table/--check-vfio-user/--version/--help -- and these
    guests have no amdgpu, so /sys/class/kfd does not exist to read either. A
    fabricated busy% would be strictly worse than saying so.
    """
    targets: dict[str, int] = {}
    total = 0
    for vm in members:
        for gfx in vm.get("gpus") or []:
            targets[gfx] = targets.get(gfx, 0) + 1
            total += 1
    return {
        "count": total,
        "targets": targets,
        "origin": "emulated" if total else None,
        "utilisation": None,
        "utilisation_source": "unmeasurable",
    }


def _outliers(cpu: dict[str, Any], containers: dict[str, dict[str, Any]],
              project: str) -> dict[str, Any]:
    """VMs far off the fleet's own median, and containers that are not up.

    Against the median rather than a fixed threshold: what counts as busy on
    an idle fleet and on a loaded one are different numbers, and the question
    "is one of these unlike the others" is the one a 32-row table cannot
    answer at a glance.
    """
    vals = sorted(v["cores_busy"] for v in cpu["per_vm"]
                  if v["cores_busy"] is not None)
    hot: list[dict[str, Any]] = []
    median = None
    if vals:
        median = vals[len(vals) // 2]
        floor = max(median * 4, 0.5)
        hot = sorted(
            (v for v in cpu["per_vm"]
             if v["cores_busy"] is not None and v["cores_busy"] >= floor),
            key=lambda v: -v["cores_busy"],
        )[:4]
    return {
        "cpu_median_cores": median,
        "cpu_hot": hot,
        "containers": [
            {"name": c["name"], "status": c["status"]}
            for c in sorted(containers.values(), key=lambda c: c["name"])
            if c["project"] == project and c["class"]
            and c["state"] != "running"
            and not (c["state"] == "exited" and "(0)" in c["status"])
        ],
    }


# ---------------------------------------------------------------------------
# Headroom
# ---------------------------------------------------------------------------

def _headroom(members: list[dict[str, Any]], host: dict[str, Any],
              ernic: dict[str, Any], storage: dict[str, Any],
              memory: dict[str, Any]) -> dict[str, Any]:
    """How many more VMs fit, by each ceiling, and which one binds.

    Four rows because the binding one is not the one people expect. Memory
    never binds in practice (48 VMs used 107G of 503G on the host AGENTS.md
    records); the mesh registration wall usually does, well below the vCPU
    budget and far below anything RAM says.
    """
    per_vcpu = max((v["vcpus"] or 0) for v in members) if members else 0
    per_mib = max((v["memory_mib"] or 0) for v in members) if members else 0
    rows: list[dict[str, Any]] = []

    # Nearest ceiling first: the mesh wall is the one that usually binds, and
    # it is the one nobody expects, so it leads.
    registered = ernic["instances"] if ernic["source"] != "unavailable" else None
    rows.append({
        "name": "mesh",
        "used": registered,
        "limit": MESH_NODE_WALL if registered is not None else None,
        # One ernic per VM on every stack this repo ships, so a free mesh slot
        # is a VM one for one.
        "vms": None if registered is None else max(
            0, MESH_NODE_WALL - registered),
        "note": "issue 17",
    })

    allocated_vcpu = sum(v["vcpus"] or 0 for v in members)
    cores = host.get("cores")
    ceiling = int(cores * VCPU_OVERCOMMIT) if cores else None
    rows.append({
        "name": "vcpu",
        "used": allocated_vcpu,
        "limit": ceiling,
        "vms": _fits(ceiling - allocated_vcpu, per_vcpu)
        if ceiling is not None else None,
        "note": "issue 14",
    })

    avail_mib = host.get("memory_available_mib")
    rows.append({
        "name": "memory",
        "used": memory["allocated_mib"],
        "limit": avail_mib,
        "vms": _fits(avail_mib, per_mib) if avail_mib is not None else None,
        "note": None,
    })

    virtual = storage.get("virtual_bytes")
    free = storage.get("free_bytes")
    rows.append({
        "name": "disk",
        "used": storage.get("allocated_bytes"),
        "limit": free,
        # No VM count: thin images grow with what the guests write, not with
        # how many guests there are, so any figure here would be a guess
        # dressed as a ceiling. The overcommit ratio is the honest signal.
        "vms": None,
        "note": (f"{virtual / free:.0f}x overcommit"
                 if virtual and free else None),
    })

    binding = None
    counted = [r for r in rows if r["vms"] is not None]
    if counted:
        binding = min(counted, key=lambda r: r["vms"])["name"]
    return {"rows": rows, "binding": binding,
            "vms": next((r["vms"] for r in rows if r["name"] == binding), None)}


def _fits(capacity: int | None, per_vm: int) -> int | None:
    if capacity is None or per_vm <= 0:
        return None
    return max(0, capacity // per_vm)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_size(n: int | None, unit: str = "B") -> str:
    """Compact binary size with a one-letter suffix: 25.9G, 48.0T."""
    if n is None:
        return "-"
    size = float(n)
    if unit == "MiB":
        size *= 1048576
    for suffix in ("B", "K", "M", "G", "T", "P"):
        if size < 1024 or suffix == "P":
            if suffix == "B":
                return f"{size:.0f}B"
            return f"{size:.1f}{suffix}"
        size /= 1024
    return "-"


def _fmt_num(x: float | None, places: int = 1) -> str:
    return "-" if x is None else f"{x:.{places}f}"


def render(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    host = data["host"]
    if not data["fleets"]:
        return ["No QEMU VMs running."]
    for fleet in data["fleets"]:
        lines.append("")
        lines.extend(_render_fleet(fleet, host, data))
    return lines


def _render_fleet(f: dict[str, Any], host: dict[str, Any],
                  data: dict[str, Any]) -> list[str]:
    name = f["project"] or "(uncontainerised)"
    roll = f["roll_call"]
    parts = [f"{f['vms']} VM" + ("" if f["vms"] == 1 else "s"),
             f"up {_fmt_uptime_range(f['uptime_seconds'])}"]
    bits = []
    for cls in _VM_SERVICE_CLASSES:
        slot = roll["services"].get(cls, {"running": 0, "total": 0})
        bits.append(f"{cls} {slot['running']}/{slot['total']}")
    bits.append(f"infra {roll['infra']['running']}/{roll['infra']['total']}")
    head = f"{name}  {'  '.join(parts)}   {'  '.join(bits)}"
    pad = " " * min(len(name), 20)
    lines = [head, f"{pad}  host {host['hostname']}  {host['cores']} cores  "
                   f"{_fmt_size(host['memory_mib'], 'MiB')}", ""]

    lines.extend(_render_headroom(f["headroom"], f, host))
    lines.extend(_render_load(f, host, data))
    lines.extend(_render_outliers(f))
    return lines


def _fmt_uptime_range(span: dict[str, int | None]) -> str:
    lo, hi = span["min"], span["max"]
    if lo is None or hi is None:
        return "-"
    low, high = list_vms._fmt_uptime(lo), list_vms._fmt_uptime(hi)
    return low if low == high else f"{low}-{high}"


def _render_headroom(h: dict[str, Any], f: dict[str, Any],
                     host: dict[str, Any]) -> list[str]:
    if h["binding"] is None:
        head = "HEADROOM   unknown (no ceiling could be measured)"
    else:
        head = f"HEADROOM   room for +{h['vms']} VMs  (binding: {h['binding']})"
    lines = [head]
    for row in h["rows"]:
        label = {"vcpu": "vCPU"}.get(row["name"], row["name"])
        right = "-" if row["vms"] is None else f"+{row['vms']} VMs"
        note = "<-- binding" if row["name"] == h["binding"] else (row["note"] or "")
        detail = _headroom_detail(row, f["storage"], host)
        lines.append(f"  {label:<9} {detail:<45} {right:>8}  {note}".rstrip())
    return lines


def _headroom_detail(row: dict[str, Any], storage: dict[str, Any],
                     host: dict[str, Any]) -> str:
    name = row["name"]
    if name == "mesh":
        if row["used"] is None:
            return "ernic stats unreadable - mesh size unknown"
        return f"{row['used']} of ~{MESH_NODE_WALL} registered nodes"
    if name == "vcpu":
        return f"{row['used'] or 0} of {host['cores']}, ceiling {row['limit']}"
    if name == "memory":
        return (f"{_fmt_size(row['used'], 'MiB')} allocated / "
                f"{_fmt_size(row['limit'], 'MiB')} host available")
    if storage["source"] == "unavailable":
        return "no images found"
    return (f"{_fmt_size(storage['allocated_bytes'])} real, "
            f"{_fmt_size(storage['virtual_bytes'])} virtual, "
            f"{_fmt_size(storage['free_bytes'])} free")


def _render_load(f: dict[str, Any], host: dict[str, Any],
                 data: dict[str, Any]) -> list[str]:
    net = f["network"]
    stats_read = (f"{net['instances']}/{net['expected']} ernic stats read"
                  if net["expected"] else "no ernic")
    # Sampled, not configured: a VM whose /proc entry went away mid-window
    # contributes to neither the CPU figure nor this denominator.
    sampled = sum(1 for v in f["cpu"]["per_vm"] if v["cores_busy"] is not None)
    lines = [f"LOAD   {data['sampled_seconds']:.1f}s sample, host-side, "
             f"{sampled}/{f['vms']} VMs, {stats_read}"]

    cpu = f["cpu"]
    if cpu["source"] == "unavailable":
        lines.append("  cpu       unavailable (no readable cgroup or /proc)")
    else:
        lines.append(f"  cpu       {cpu['cores_busy']:.1f} cores busy of "
                     f"{cpu['cores_total']}   {_cpu_split(cpu['by_service'])}")

    load = host["loadavg"]
    lines.append("  hostload  " + (
        "unavailable" if load is None else
        f"{load[0]:.1f} {load[1]:.1f} {load[2]:.1f}   "
        "(HOST, not fleet -- this node is shared)"))

    mem = f["memory"]
    if mem["source"] == "unavailable":
        lines.append("  memory    unavailable (no readable cgroup)")
    else:
        lines.append(
            f"  memory    {_fmt_size(mem['guest_resident_bytes'])} guest RAM "
            f"resident (memfd=shmem)   "
            f"{_fmt_size(mem['unreclaimable_bytes'])} unreclaimable")

    if net["source"] == "unavailable":
        lines.append("  net       unavailable (ernic stats not readable)"
                     if net["expected"] else "  net       no ernic devices")
    else:
        lines.append(
            f"  net       RDMA {_fmt_size(net['rdma_bytes'])} moved, mesh "
            f"{net['connected']}/{net['instances']} connected   "
            f"Ethernet {_fmt_size(net['ip_bytes_tx'])} offered (tx)")

    gpu = f["gpu"]
    if not gpu["count"]:
        lines.append("  gpu       none configured")
    else:
        # No "*N" when the whole fleet is one target: the count is already the
        # first word of the line.
        targets = ",".join(
            k if len(gpu["targets"]) == 1 or v == 1 else f"{k}*{v}"
            for k, v in sorted(gpu["targets"].items()))
        lines.append(f"  gpu       {gpu['count']} emulated {targets} -- "
                     "utilisation not measurable")

    lines.append("  users     " + _login_line(f["logins"]))
    return lines


def _cpu_split(by_service: dict[str, float]) -> str:
    """The three per-VM classes by name, everything else rolled up as infra.

    Matching the header's roll-up, and for the same reason: naming the
    exporter and two dashboards individually at 0.0 cores each crowds out the
    ernic-versus-qemu comparison, which is the one that surprises. --json
    keeps the full per-class breakdown.
    """
    parts = []
    for cls in _VM_SERVICE_CLASSES:
        if cls in by_service:
            parts.append(f"{cls} {by_service[cls]:.1f}")
    other = sum(v for k, v in by_service.items()
                if k not in _VM_SERVICE_CLASSES)
    if round(other, 1) > 0:
        parts.append(f"infra {other:.1f}")
    return "  ".join(parts)


def _login_line(logins: dict[str, Any]) -> str:
    if logins["source"] == "not-collected":
        return "not collected (drop --no-guests)"
    occupied = len(logins["occupied"])
    word = "session" if logins["sessions"] == 1 else "sessions"
    return (f"{logins['sessions']} {word} on {occupied} of "
            f"{logins['guests']} guests, {logins['unknown']} unknown")


def _render_outliers(f: dict[str, Any]) -> list[str]:
    out = f["outliers"]
    rows: list[str] = []
    if out["cpu_hot"]:
        hot = " | ".join(f"{v['container'] or v['vm']} {v['cores_busy']:.2f} cores"
                         for v in out["cpu_hot"])
        rows.append(f"  cpu       {hot}   (fleet median "
                    f"{_fmt_num(out['cpu_median_cores'], 2)})")
    for entry in (f["logins"]["occupied"] or []):
        for s in entry["sessions"]:
            where = f" from {s['remote_host']}" if s.get("remote_host") else ""
            rows.append(f"  users     {entry['vm']}   {s['user']}  "
                        f"{s.get('service') or '-'}{where}")
    if out["containers"]:
        rows.append("  infra     " + ", ".join(
            f"{c['name']} {c['status']}" for c in out["containers"]))
    if not rows:
        return ["OUTLIERS   none"]
    return ["OUTLIERS"] + rows
