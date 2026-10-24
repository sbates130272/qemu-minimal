"""`qemu-tool check-system` -- is this host able to run the stacks?

Every failure this reports has been seen in the wild, and most of them
present somewhere other than where they are caused: a missing kvm group looks
like slow guests, an unreadable /dev/net/tun looks like a worker bug, a
CONTAINER_USER typo looks like a stack that will not start for reasons the
error does not name. The point of this command is to ask all of it up front,
cheaply, before anything has been pulled or written.

It is the same code `qemu-tool quick-start` runs in its preflight, not a copy
of it -- quick_start imports from here. The repo has been bitten before by a
documented path and its CI equivalent drifting apart, so there is one
implementation and two callers.

Nothing here writes anything or needs sudo. Read-only by construction, so it
is safe to run on a host you have not decided to use yet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .compose import resolve_identity
from .envfile import find as find_env_file, parse_layers

# Exit codes. A WARN does not fail the command: a host that is merely tight
# still works, and a check-system that cried wolf would stop being run.
_OK, _WARN, _FAIL, _SKIP = "ok", "warn", "FAIL", "skip"

_MARK = {_OK: "  ok  ", _WARN: " warn ", _FAIL: " FAIL ", _SKIP: " skip "}


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    # What to do about it. Only read for warn/FAIL, where "this is broken" is
    # worth much less than "this is broken, here is the package".
    hint: str = ""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# Split by what actually needs them, because a host that only ever runs
# `qemu-tool compose` against a published image does not need oras or
# ansible, and failing it for their absence would be wrong.
_REQUIRED_TOOLS = {
    "docker": "install Docker Engine with the compose plugin "
              "(https://docs.docker.com/engine/install/)",
}
_OPTIONAL_TOOLS = {
    "oras": ("pulls the published guest qcow2 for gen-vm and quick-start",
             "https://oras.land/docs/installation"),
    "ansible-playbook": ("provisions guests (vm-ernic.yml, fleet-quickstart.yml)",
                         "apt install ansible, then ansible-galaxy collection "
                         "install -r ansible/requirements.yml"),
    "ssh": ("reaches guests; qemu-tool ssh and every playbook need it",
            "apt install openssh-client"),
    "qemu-img": ("creates and compacts the overlay qcow2s in gen-vm",
                 "apt install qemu-utils"),
    "sudo": ("only run-vm's --mgmt-nic path, which configures a host "
             "bridge and dnsmasq",
             "apt install sudo"),
}


def _check_tools() -> list[Result]:
    out: list[Result] = []
    for tool, hint in _REQUIRED_TOOLS.items():
        found = shutil.which(tool)
        out.append(Result(f"{tool} on PATH",
                          _OK if found else _FAIL,
                          found or "not found", "" if found else hint))
    for tool, (what, hint) in _OPTIONAL_TOOLS.items():
        found = shutil.which(tool)
        out.append(Result(
            f"{tool} on PATH", _OK if found else _WARN,
            found or f"not found -- {what}", "" if found else hint))
    return out


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

def docker_compose_ok() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _check_docker() -> list[Result]:
    if shutil.which("docker") is None:
        return [Result("docker compose plugin", _SKIP, "docker not on PATH")]
    out = [Result(
        "docker compose plugin",
        _OK if docker_compose_ok() else _FAIL,
        "", "`docker compose version` failed: the plugin is missing, or this "
            "user cannot reach the daemon")]
    # Separated from the plugin check because the fix is different and the
    # symptom is identical -- "permission denied while trying to connect to
    # the Docker daemon socket" is what both look like from here.
    try:
        import grp

        docker_gid = grp.getgrnam("docker").gr_gid
        in_group = docker_gid in os.getgroups() or os.getuid() == 0
        out.append(Result(
            "docker group membership",
            _OK if in_group else _WARN,
            f"gid {docker_gid}" + ("" if in_group else " -- you are not in it"),
            "" if in_group else
            f"sudo usermod -aG docker {os.environ.get('USER', '$USER')}, "
            "then log out and back in"))
    except KeyError:
        # No docker group at all. Rootless Docker and a socket owned another
        # way are both legitimate, so this is only worth a line if the daemon
        # is also unreachable -- which the check above already said.
        out.append(Result("docker group membership", _SKIP,
                          "no docker group on this host"))
    return out


# ---------------------------------------------------------------------------
# KVM and the container identity
# ---------------------------------------------------------------------------

def kvm_usable() -> tuple[bool, str]:
    """The ioctl, not the node.

    A device node can exist and still be non-functional, and QEMU falls back
    to TCG without saying so. Guests under TCG will not finish inside any
    timeout worth setting, so this presents as "the fleet is hanging".
    """
    if not Path("/dev/kvm").exists():
        return False, "/dev/kvm is missing"
    try:
        import fcntl

        fd = os.open("/dev/kvm", os.O_RDWR)
    except OSError as exc:
        return False, (f"/dev/kvm cannot be opened ({exc.strerror}) -- "
                       "are you in the kvm group?")
    try:
        fcntl.ioctl(fd, 0xAE00, 0)
    except OSError as exc:
        return False, (f"/dev/kvm is present but KVM_GET_API_VERSION failed "
                       f"({exc.strerror})")
    finally:
        os.close(fd)
    return True, ""


def _check_kvm() -> list[Result]:
    ok, why = kvm_usable()
    return [Result("/dev/kvm usable", _OK if ok else _FAIL, why,
                   "" if ok else "without KVM the guests run under TCG and "
                                 "never finish; check virtualisation is "
                                 "enabled in firmware and that you are in the "
                                 "kvm group")]


def _check_identity(settings: list[Path]) -> list[Result]:
    """Who the containers will run as, stated outright.

    Worth a line of its own even when it is fine, because "which uid did my
    fleet run as" used to have a surprising answer: every stack ran as root
    until the reference became unconditional. Printing it makes the new
    default visible rather than merely true.
    """
    try:
        ident, source = resolve_identity(settings)
    except SystemExit as exc:
        return [Result("container identity", _FAIL, str(exc).splitlines()[0],
                       "CONTAINER_USER must be numeric <uid>:<gid>, or unset")]
    out = [Result("container identity", _OK,
                  f"{ident['CONTAINER_USER']} (from {source})")]
    gid = ident.get("CONTAINER_KVM_GID")
    out.append(Result(
        "kvm gid for containers", _OK if gid else _FAIL,
        gid or "could not determine the gid that owns /dev/kvm",
        "" if gid else "a container given a uid holds no supplementary "
                       "groups, so QEMU could not open /dev/kvm. Set "
                       "CONTAINER_KVM_GID explicitly."))
    return out


def _check_tun() -> list[Result]:
    """/dev/net/tun, which only an ERNIC_TAP fleet needs.

    A warning and not a failure: the single-VM stacks and any fleet with
    ERNIC_TAP off never touch it. But without it a tap fleet has no guest
    Ethernet at all -- ARP goes unanswered and ib_send_bw cannot do its
    out-of-band exchange -- and that presents as an RDMA failure, nowhere
    near the missing device node.
    """
    path = Path("/dev/net/tun")
    if not path.exists():
        return [Result("/dev/net/tun", _WARN, "missing",
                       "needed only by ERNIC_TAP fleets, which cannot do "
                       "inter-node RDMA without it: modprobe tun")]
    return [Result("/dev/net/tun", _OK, "")]


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

def _mem_total_mb() -> int:
    try:
        kb = next(int(line.split()[1])
                  for line in Path("/proc/meminfo").read_text().splitlines()
                  if line.startswith("MemTotal:"))
        return kb // 1024
    except (OSError, StopIteration):
        return 0


def free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1024 ** 3


def _check_capacity(values: dict[str, str]) -> list[Result]:
    cores = os.cpu_count() or 1
    mem_mb = _mem_total_mb()
    out = [Result("cpu", _OK, f"{cores} cores"),
           Result("memory", _OK, f"{mem_mb // 1024} GiB")]

    # The vCPU budget, against the fleet this host's settings describe. Not a
    # failure: an oversubscribed host still boots, it just loses guests to the
    # mesh manager's untunable 20 s heartbeat, which presents as
    # "vfio-user: timed out waiting for reply" (AGENTS.md issue 14).
    try:
        vcpus = int(values.get("VM_VCPUS") or 0)
        count = int(values.get("VM_COUNT") or 0)
    except ValueError:
        vcpus = count = 0
    if vcpus and count:
        budget = cores * 5 // 4
        over = vcpus * count > budget
        out.append(Result(
            "vCPU budget", _WARN if over else _OK,
            f"{count} x {vcpus} vCPU = {vcpus * count} against {cores} cores",
            "expect heartbeat evictions and lost guests (AGENTS.md issue 14); "
            "reduce VM_COUNT or VM_VCPUS" if over else ""))
        if vcpus < 4:
            out.append(Result(
                "VM_VCPUS floor", _FAIL, f"{vcpus}",
                "below 4 vCPU ionic_rdma never probes, and the only symptom "
                "is a line in a guest log nobody is reading"))

    # /dev/shm is NOT where guest RAM is accounted -- a memfd is anonymous and
    # charged to the cgroup as shmem -- but shm_size must still be >= VM_VMEM
    # or QEMU refuses to allocate the backend, so the mount has to be big
    # enough to cap it.
    try:
        shm_gb = shutil.disk_usage("/dev/shm").total / 1024 ** 3
        out.append(Result("/dev/shm", _OK, f"{shm_gb:.0f} GiB total"))
    except OSError:
        out.append(Result("/dev/shm", _WARN, "not readable"))
    return out


def _check_images_dir(values: dict[str, str]) -> list[Result]:
    raw = values.get("VM_IMAGES_DIR")
    if not raw:
        return [Result("VM_IMAGES_DIR", _SKIP, "not set in any settings file")]
    path = Path(raw)
    if not path.is_dir():
        return [Result("VM_IMAGES_DIR", _WARN, f"{path} does not exist",
                       "gen-vm will create it, or point VM_IMAGES_DIR "
                       "somewhere that exists")]
    out = [Result("VM_IMAGES_DIR", _OK, str(path))]
    writable = os.access(path, os.W_OK | os.X_OK)
    out.append(Result("  writable by you", _OK if writable else _FAIL,
                      "" if writable else f"{path} is not writable",
                      "" if writable else "gen-vm writes the overlay qcow2s "
                                          "here"))
    gb = free_gb(path)
    # A backing qcow2 is ~2.5 GB compressed and lands ~16 GB unpacked, plus an
    # overlay per guest. 30 is a floor for one guest, not a recommendation.
    out.append(Result("  free space", _OK if gb >= 30 else _WARN,
                      f"{gb:.0f} GiB free",
                      "" if gb >= 30 else "a published backing image unpacks "
                                          "to ~16 GiB before any overlay"))

    # The root-era artifacts. Same check `qemu-tool compose up` makes, said
    # here before anything has been started: a root-owned NVMe qcow2 is 0644
    # and QEMU opens a drive read-write, so the guest does not boot at all
    # rather than merely owning the wrong file.
    uid = os.getuid()
    try:
        stale = [p for p in sorted(path.glob("*-nvme*.qcow2"))
                 if p.stat().st_uid != uid]
    except OSError:
        stale = []
    if stale:
        out.append(Result(
            "  ownership", _WARN,
            f"{len(stale)} NVMe qcow2 not owned by uid {uid} "
            f"(e.g. {stale[0].name})",
            "left by a root-era run; delete them, they are scratch"))
    else:
        out.append(Result("  ownership", _OK, f"everything is uid {uid}"))
    return out


def _check_settings(settings: list[Path]) -> list[Result]:
    """Which settings file qemu-tool would actually read.

    Printed because the search path has four entries and the answer surprises
    people -- a stale qemu/.env in a checkout outranks the system one, and a
    fleet then runs with half-remembered values.
    """
    if not settings:
        return [Result("settings file", _WARN, "none found on the search path",
                       "cp qemu/env/example qemu/.env, or pass --env-file")]
    return [Result("settings file", _OK, str(settings[0]))] + [
        Result("  overlay", _OK, str(p)) for p in settings[1:]]


def _check_qemu_tool() -> list[Result]:
    """Which qemu-tool is on PATH, which is not always this one.

    The CI images ship a qemu-tool of their own, and a pipx install does not
    replace it unless PIPX_BIN_DIR says so. Running a check from one copy and
    a fleet from another is a confusing afternoon.
    """
    found = shutil.which("qemu-tool")
    if not found:
        return [Result("qemu-tool on PATH", _WARN, "not found",
                       "this is fine when running as `python3 -m qemu_tool`")]
    return [Result("qemu-tool on PATH", _OK, found)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(env_file: list[Path] | None = None) -> None:
    settings = find_env_file(env_file)
    try:
        values = parse_layers(settings) if settings else {}
    except SystemExit:
        # A settings file this tool cannot parse is itself the finding, and a
        # better one than dying halfway through the report.
        values = {}

    groups: list[tuple[str, list[Result]]] = [
        ("Tools", _check_tools()),
        ("Docker", _check_docker()),
        ("Virtualisation", _check_kvm() + _check_tun()),
        ("Container identity", _check_identity(settings)),
        ("Settings", _check_settings(settings) + _check_qemu_tool()),
        ("Capacity", _check_capacity(values)),
        ("Images", _check_images_dir(values)),
    ]

    failed = warned = 0
    for title, results in groups:
        print(f"\n{title}")
        for r in results:
            if r.status == _FAIL:
                failed += 1
            elif r.status == _WARN:
                warned += 1
            line = f"  [{_MARK[r.status]}] {r.name}"
            if r.detail:
                line += f": {r.detail}"
            print(line)
            if r.hint and r.status in (_FAIL, _WARN):
                print(f"            {r.hint}")

    print()
    if failed:
        print(f"{failed} blocking problem(s), {warned} warning(s). "
              "The stacks will not run until the failures are fixed.")
        sys.exit(1)
    if warned:
        print(f"No blocking problems, {warned} warning(s). "
              "Everything above marked 'warn' limits what this host can do, "
              "but nothing stops a stack starting.")
        return
    print("This host can run the stacks.")
