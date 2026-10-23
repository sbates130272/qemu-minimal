"""Two GPU + RDMA guests on this host, sized for it, from one command.

This is the path a 558-line shell script under scripts/ used to be, moved
into the tool so
that the thing a new user is told to run and the thing the daily
report-for-quickstart lane runs are the same code -- which was the whole point
of that script, and is why it is gone rather than kept beside this.

What moved with it, because every line of it was paid for:

  * the guest account comes from the IMAGE, not from the settings file
    (_adopt_login). VM_USERNAME=ubuntu against a qcow2 baked with `batesste`
    leaves every guest healthy and every login refused, and the error names
    the key, not the account.
  * the keypair ships as an ORAS REFERRER beside the qcow2, selected by
    artifactType rather than by position, and its public half is published
    into <images>/keys/ so `qemu-tool inspect` and `qemu-tool ssh` can find
    it.
  * --in-network, for a caller that is itself a container on the fleet
    network and so cannot reach the guests on published host ports.
  * the stack directory falls back to XDG state when /var/lib/qemu-tool is
    root-owned, and an EARLIER run's fallback wins over an empty default.

What is new here:

  * the guests are sized from this host by default. The script did it only
    under --size-from-host, which meant the documented path asked for a fixed
    4 vCPU / 4 GiB whatever it was run on.
  * Prometheus and Grafana come up with the stack, with their data
    directories under the stack directory so neither needs root or a chown.
  * it asks. A new user finds out what this wants in disk and a writable
    images directory from a prompt, not from a failure part way into the
    pull. Every prompt has an answer it takes on its own when stdin is
    not a terminal, so CI is unaffected -- see _ask.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import VMConfig
from .envfile import overrides as env_overrides, parse_layers
from .gen_compose import run as gen_compose_run
from .gen_vm import run as gen_vm_run

VERBS = ("all", "preflight", "up", "test", "report", "down")

# The settings file this subcommand is about. Shipped in the wheel, so a pipx
# install has it without a checkout.
_ENV_NAME = "quickstart"

# Where the guest keypair referrer is pulled to, and what it carries.
_VM_INFO_TYPE = "application/vnd.batesste.vm-info.v1"

# Rough, and deliberately so. Measured against the pinned tag rather than
# guessed: the registry reports 2.5 GB compressed and the image's own
# annotation says 9.2 GB on disk, then each guest takes an overlay that grows
# as it is written and `--nvme 1` adds another file per guest. 30 leaves room
# for that without being the ~60 this claimed for years -- a figure inherited
# from a "17 GB compressed, 17 GB decompressed" reading that was about 7x the
# real download and turned the check into a warning nobody could satisfy on a
# laptop. Warned about rather than enforced, because `down` and `test` do not
# need it.
_DISK_NEEDED_GB = 30

# ionic_lif_size() derives its EQ count from num_online_cpus() and
# ionic_create_rdma_admin() rejects fewer than IONIC_EQ_COUNT_MIN, so below
# this ionic_rdma never probes and the only symptom is "Failed to register
# ibdev" in the guest log.
_VCPU_FLOOR = 4
# Not a hardware limit -- a judgement. Past about this the guest gains
# nothing and the host loses: the mesh manager's heartbeat timeout is 20 s of
# wall clock and is not tunable, so vCPU this stack cannot use is vCPU that
# makes it evict live nodes (AGENTS.md issue 14).
_VCPU_CEILING = 8
_VMEM_FLOOR_MB = 4096
_VMEM_CEILING_MB = 8192


# ---------------------------------------------------------------------------
# Resolved state
# ---------------------------------------------------------------------------

@dataclass
class _State:
    env_file: Path
    stack_dir: Path
    key_dir: Path
    report_dir: Path | None
    repo_root: Path | None
    in_network: bool
    metrics: bool
    assume_yes: bool
    size_from_host: bool
    artifacts: Path

    # What the user named: the settings file and any --env-file layers. The
    # overlays this subcommand generates are appended to these on every
    # rescan, never folded into them.
    base_layers: list[Path] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    layers: list[Path] = field(default_factory=list)
    username: str = ""
    rdma_delta: int | None = None

    # ---- convenience readers -------------------------------------------
    def get(self, key: str) -> str:
        """A settings value, falling back to the generator's own default.

        PROM_PORT, GRAFANA_PORT and PUBLISH_BIND_ADDR are not in
        qemu/env/quickstart -- they do not need to be, because gen_compose
        defaults them. Reading them from the file alone meant this printed a
        URL for a port the stack was not publishing, or raised. Taking the
        same dict the generator takes them from is what keeps the two from
        disagreeing.
        """
        from .gen_compose import _DEFAULTS

        value = self.values.get(key)
        if value:
            return value
        return _DEFAULTS[key]

    @property
    def vm_count(self) -> int:
        return int(self.values.get("VM_COUNT", "2"))

    @property
    def prefix(self) -> str:
        return self.values.get("VM_NAME_PREFIX", "quickstart")

    @property
    def stack_name(self) -> str:
        return self.stack_dir.name

    @property
    def images_dir(self) -> Path:
        return Path(self.values["VM_IMAGES_DIR"]).expanduser()

    @property
    def key(self) -> Path:
        return self.key_dir / "id_rsa"

    def vm(self, index: int) -> str:
        return f"{self.prefix}-{index}"

    def guest_host(self, index: int) -> str:
        if self.in_network:
            # Third octet 3 is the qemu family. By address, not by service
            # name: this stack resolves no service names at run time, on
            # purpose (AGENTS.md issue 13).
            return f"{self.values['FLEET_NET_PREFIX']}.3.{index}"
        return "localhost"

    def guest_port(self, index: int) -> int:
        if self.in_network:
            return 2222
        return int(self.values["VM_SSH_PORT_BASE"]) + index - 1


# ---------------------------------------------------------------------------
# Output and prompting
# ---------------------------------------------------------------------------

def _say(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def _die(message: str) -> None:
    sys.exit(f"error: {message}")


def _interactive(state: _State) -> bool:
    return not state.assume_yes and sys.stdin.isatty()


def _ask(state: _State, question: str, default: str) -> str:
    """Prompt, or take `default` without one when nobody can answer.

    The default is PRINTED in the non-interactive case rather than applied
    silently: a CI log that does not say which images directory the run chose
    cannot be read back when the run fails on it.
    """
    if not _interactive(state):
        print(f"{question} [{default}]")
        return default
    try:
        reply = input(f"{question} [{default}]: ").strip()
    except EOFError:
        print()
        return default
    return reply or default


def _confirm(state: _State, question: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    reply = _ask(state, f"{question} ({suffix})", "y" if default else "n")
    return reply.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _repo_root(explicit: Path | None) -> Path | None:
    """The checkout, or None when this is an installed tool with no source.

    Two things still live only in a checkout: ansible/playbooks/
    fleet-quickstart.yml, which configures the guests and runs the tests, and
    the qemu/ source tree the stack bind-mounts into its containers. Neither
    ships in the wheel, so this returns None rather than guessing and the
    caller degrades with a named reason.

    Two places are searched, and the second is not optional. An editable
    install resolves __file__ into the checkout, so walking up from this
    module finds it -- but `pipx install ./qemu` COPIES the package into its
    own venv, and the CI lane installs exactly that way while running from
    the checkout. Searching only this file's parents therefore found no repo
    on the one machine that definitely has one, and the lane would have
    brought two guests up and skipped every test it exists to run.
    """
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / "ansible" / "playbooks" / "fleet-quickstart.yml").is_file():
            _die(f"--repo-root {root} has no ansible/playbooks/fleet-quickstart.yml")
        return root
    here = Path(__file__).resolve()
    for start in (here, Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "ansible" / "playbooks" / "fleet-quickstart.yml").is_file() \
                    and (candidate / "qemu" / "pyproject.toml").is_file():
                return candidate
    return None


def _packaged_env() -> Path | None:
    """qemu/env/quickstart, from the checkout or from inside the wheel."""
    source = Path(__file__).resolve().parent.parent / "env" / _ENV_NAME
    if source.is_file():
        return source
    from .resources import ENV as _ENV_PACKAGE, packaged_path

    packaged = packaged_path(_ENV_PACKAGE, _ENV_NAME)
    if packaged is not None and packaged.is_file():
        return packaged
    return None


def _resolve_stack_dir(requested: Path, explicit: bool) -> Path:
    """Where the generated stack lives, falling back off an unwritable root.

    /var/lib/qemu-tool/stacks is created by whichever run got there first,
    and a root one leaves it root-owned 755 -- so the default landed on
    "mkdir: Permission denied" three minutes into a 20 GB pull, naming
    neither the path nor the fix. An EARLIER run's fallback wins over an
    empty default, or a later verb would create the default, find no compose
    file in it, and report that against a stack sitting in the fallback
    perfectly intact.
    """
    if requested.is_dir():
        return requested
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    fallback = base / "qemu-tool" / "stacks" / requested.name
    if not explicit and (fallback / "docker-compose.yml").is_file():
        return fallback
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        if explicit:
            _die(f"cannot create {requested}")
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            _die(f"cannot create {requested} or {fallback}")
        print(f"note: {requested} is not writable, using {fallback}")
        return fallback


# ---------------------------------------------------------------------------
# Settings layers
# ---------------------------------------------------------------------------

def _reload(state: _State) -> None:
    state.values = parse_layers(state.layers)


# The overlays this subcommand writes beside the stack, in the order they
# layer. Last wins, so the account the image reports goes last: it is the one
# answer that beats everything, including a VM_USERNAME typed into an
# --env-file.
#
# The ORDER is fixed here rather than taken from the order they happen to be
# written, because they are not all written in one process. `up` and `test`
# are separate invocations in CI, and before this list existed the second one
# started from the base settings file alone -- so a run whose Grafana port
# had been moved, or whose images directory had been relocated, went back to
# the original values half way through and waited for SSH on a port no guest
# was published on.
_LAYER_ORDER = ("env.sized", "env.images", "env.ports", "env.metrics",
                "env.image-login")


def _rescan_layers(state: _State) -> None:
    """Rebuild state.layers as the base files plus whatever is on disk."""
    generated = [state.stack_dir / name for name in _LAYER_ORDER]
    state.layers = state.base_layers + [p for p in generated if p.is_file()]
    _reload(state)


def _write_layer(state: _State, name: str, body: dict[str, str], why: str) -> None:
    """Write (or rewrite) one generated overlay on top of the settings file.

    An overlay rather than an edit of qemu/env/quickstart, for the reason
    --env-file is repeatable at all: the committed file stays the thing a
    reader diffs, and a run on a small host does not look like a settings
    change.
    """
    if name not in _LAYER_ORDER:
        raise AssertionError(f"{name} is not in _LAYER_ORDER")
    path = state.stack_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Written by `qemu-tool quick-start`. {why}",
             "# Do not edit; it is rewritten on every run."]
    lines += [f"{key}={value}" for key, value in body.items()]
    path.write_text("\n".join(lines) + "\n")
    _rescan_layers(state)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

_TOOL_HINTS = {
    "docker": "install Docker Engine with the compose plugin "
              "(https://docs.docker.com/engine/install/)",
    "oras": "install oras (https://oras.land/docs/installation) -- it is what "
            "pulls the published guest image",
    "ansible-playbook": "apt install ansible, then "
                        "`ansible-galaxy collection install -r ansible/requirements.yml`",
    "ssh": "apt install openssh-client",
    "qemu-img": "apt install qemu-utils",
}


def _check_tools() -> list[str]:
    return [tool for tool in _TOOL_HINTS if shutil.which(tool) is None]


def _kvm_usable() -> tuple[bool, str]:
    """The ioctl, not the node.

    A device node can exist and still be non-functional, and QEMU falls back
    to TCG without saying so. Two guests under TCG will not finish inside any
    timeout worth setting.
    """
    if not Path("/dev/kvm").exists():
        return False, "/dev/kvm is missing"
    try:
        import fcntl

        fd = os.open("/dev/kvm", os.O_RDWR)
    except OSError as exc:
        return False, f"/dev/kvm cannot be opened ({exc.strerror}) -- are you in the kvm group?"
    try:
        fcntl.ioctl(fd, 0xAE00, 0)
    except OSError as exc:
        return False, f"/dev/kvm is present but KVM_GET_API_VERSION failed ({exc.strerror})"
    finally:
        os.close(fd)
    return True, ""


def _docker_compose_ok() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1024 ** 3


def _shm_free_mb() -> int | None:
    try:
        return int(shutil.disk_usage("/dev/shm").free / 1024 ** 2)
    except OSError:
        return None


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _next_free_port(start: int) -> int:
    for port in range(start, start + 200):
        if _port_free(port):
            return port
    return start


def _size_for_host(state: _State) -> dict[str, str]:
    """vCPU, RAM and shm per guest, derived from this machine.

    The script did this only under --size-from-host, for a CI runner. It is
    the default here: a fixed 4 vCPU / 4 GiB is the wrong answer on every
    host except the one it was written for.
    """
    count = state.vm_count
    cores = os.cpu_count() or 1
    try:
        mem_kb = next(int(line.split()[1])
                      for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemTotal:"))
    except (OSError, StopIteration):
        mem_kb = 0
    mem_mb = mem_kb // 1024

    # One core left for the host, which is what everything else on it --
    # dockerd, the mesh manager, this process -- has to run on.
    vcpus = max(_VCPU_FLOOR, min(_VCPU_CEILING, (cores - 1) // count))
    vmem = _VMEM_FLOOR_MB if not mem_mb else max(
        _VMEM_FLOOR_MB, min(_VMEM_CEILING_MB, mem_mb * 3 // 4 // count))
    # VM_VMEM plus 1 GiB, not equal to it. The rule is "VM_SHM_SIZE >=
    # VM_VMEM, per VM", so equal is the boundary and leaves the vfio-user
    # memfd backend no slack. shm_size caps a tmpfs rather than reserving it,
    # so the headroom costs nothing until it is touched.
    return {
        "VM_VCPUS": str(vcpus),
        "VM_VMEM": str(vmem),
        "VM_SHM_SIZE": f"{vmem + 1024}m",
    }


def preflight(state: _State) -> None:
    _say("Checking this machine")

    missing = _check_tools()
    if missing:
        # Report and exit, with the fix for each. Not a prompt that offers to
        # install: this runs as an ordinary user and guessing a package
        # manager is how a quickstart starts modifying the host.
        print("\nThese are needed and not on PATH:\n", file=sys.stderr)
        for tool in missing:
            print(f"  {tool:<18} {_TOOL_HINTS[tool]}", file=sys.stderr)
        _die("install the above and run this again")
    if not _docker_compose_ok():
        _die("`docker compose version` failed -- the compose plugin is missing, "
             "or this user cannot reach the Docker daemon (try: sudo usermod "
             f"-aG docker {os.environ.get('USER', '$USER')}, then log out and back in)")

    ok, why = _kvm_usable()
    if not ok:
        _die(f"{why}; the guests would run under TCG and never finish")

    if state.size_from_host:
        sized = _size_for_host(state)
        _write_layer(state, "env.sized", sized,
                     f"Guest size derived from this host "
                     f"({os.cpu_count()} cores).")

    vcpus = int(state.values["VM_VCPUS"])
    vmem = int(state.values["VM_VMEM"])
    shm = state.values["VM_SHM_SIZE"]
    count = state.vm_count
    cores = os.cpu_count() or 1

    # Not fatal, because a host that is merely tight still finishes. Warned
    # about because the symptom is not "slow": the mesh manager's heartbeat
    # timeout is 20 s of wall clock and is not tunable, so an oversubscribed
    # host mistakes live nodes for dead ones and the guests attached to them
    # die with "vfio-user: timed out waiting for reply" (AGENTS.md issue 14).
    if vcpus * count > cores * 5 // 4:
        print(f"warning: {count} x {vcpus} vCPU on {cores} cores is "
              f"oversubscribed; expect heartbeat evictions "
              f"(AGENTS.md issue 14).", file=sys.stderr)

    # Skipped under --in-network, where this process is itself a container:
    # the /dev/shm it can see is its own 64 MiB one, not the host's, and each
    # guest container gets a tmpfs of its own from shm_size regardless.
    if not state.in_network:
        free_mb = _shm_free_mb()
        want_mb = _shm_mb(shm) * count
        if free_mb is not None and want_mb > free_mb:
            print(f"warning: {count} x {shm} does not fit /dev/shm "
                  f"({free_mb} MiB free).", file=sys.stderr)

    _resolve_images_dir(state)

    print()
    print(f"  settings    {' + '.join(str(p) for p in state.layers)}")
    print(f"  guests      {count} x {vcpus} vCPU, {vmem} MiB RAM, {shm} shm")
    print(f"  images      {state.images_dir}  ({_free_gb(state.images_dir):.0f} GB free)")
    print(f"  stack       {state.stack_dir}")
    if state.metrics:
        print(f"  monitoring  Prometheus :{state.get('PROM_PORT')}, "
              f"Grafana :{state.get('GRAFANA_PORT')}")
    print(f"  guest image {state.values['VM_BACKING_IMAGE'].split(':')[-1]}")
    print()


def _shm_mb(value: str) -> int:
    raw = value.strip().lower()
    if raw.endswith("g"):
        return int(float(raw[:-1]) * 1024)
    if raw.endswith("m"):
        return int(float(raw[:-1]))
    return 0


def _resolve_images_dir(state: _State) -> None:
    """Make sure the images directory exists and this user can write it.

    /var/lib/qemu-tool/images needs root on a fresh box, and the failure
    without this lands part way into the pull.
    """
    images = state.images_dir
    probe = images
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = images.is_dir() and os.access(images, os.W_OK)
    if not images.exists():
        try:
            images.mkdir(parents=True, exist_ok=True)
            writable = True
        except OSError:
            writable = False

    if not writable:
        default = str(Path.home() / ".local" / "share" / "qemu-tool" / "images")
        print(f"\n{images} is not writable by this user.", file=sys.stderr)
        chosen = _ask(state, "Where should the guest images go?", default)
        path = Path(chosen).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _die(f"cannot create {path}: {exc.strerror}")
        _write_layer(state, "env.images", {"VM_IMAGES_DIR": str(path.resolve())},
                     "The images directory this run was given.")
        images = state.images_dir

    free = _free_gb(images)
    if free < _DISK_NEEDED_GB:
        print(f"warning: {images} has {free:.0f} GB free; this wants about "
              f"{_DISK_NEEDED_GB} GB (a ~2.5 GB pull that unpacks to ~9 GB, "
              f"plus an overlay and an NVMe image per guest).",
              file=sys.stderr)


def _resolve_ports(state: _State) -> None:
    """Move Prometheus, Grafana and the guest SSH ports off anything in use.

    9090 and 3000 are the two most commonly occupied ports on a developer's
    machine, and compose does not fail on a clash until `up` -- by which
    point the guests are already booting.
    """
    changed: dict[str, str] = {}
    wanted = []
    if state.metrics:
        wanted += [("PROM_PORT", "Prometheus"), ("GRAFANA_PORT", "Grafana")]
    for key, label in wanted:
        port = int(state.get(key))
        if _port_free(port):
            continue
        free = _next_free_port(port + 1)
        answer = _ask(state, f"{label} wants port {port}, which is in use. "
                             f"Use which port instead?", str(free))
        changed[key] = answer
        print(f"note: {label} will be published on {answer}, not {port}")

    base = int(state.values["VM_SSH_PORT_BASE"])
    taken = [base + i for i in range(state.vm_count) if not _port_free(base + i)]
    if taken and not state.in_network:
        free = _next_free_port(base + state.vm_count)
        answer = _ask(state, f"Guest SSH wants ports {base}..{base + state.vm_count - 1}, "
                             f"and {', '.join(str(p) for p in taken)} "
                             f"{'are' if len(taken) > 1 else 'is'} in use. "
                             f"Start the guest ports at?", str(free))
        changed["VM_SSH_PORT_BASE"] = answer
        print(f"note: guests will answer SSH from port {answer} up")

    if changed:
        _write_layer(state, "env.ports", changed,
                     "Ports moved off something already listening here.")


# ---------------------------------------------------------------------------
# The guest image, its keypair and the account baked into it
# ---------------------------------------------------------------------------

def _adopt_login(state: _State) -> None:
    """The image is the authority on which accounts exist in it.

    The settings file names only the one we expect, and a wrong guess is
    expensive: when VM_USERNAME became `ubuntu` against a qcow2 built with
    `batesste`, every guest was healthy and the wait still spent ten minutes
    retrying before dying with "did not answer SSH", which says nothing about
    the account.
    """
    state.username = state.values.get("VM_USERNAME", "ubuntu")
    info = state.key_dir / "vm-info.json"
    if not info.is_file():
        return
    try:
        from_image = (json.loads(info.read_text()).get("username") or "").strip()
    except (OSError, ValueError):
        return
    if not from_image or from_image == state.username:
        return
    _say(f"Guest account is {from_image} per the image, not {state.username} "
         f"per {state.env_file}; using {from_image}")
    state.username = from_image
    # Handed to every qemu-tool call that BAKES the account, not just to the
    # ones that use it: gen-vm records the username in the per-VM sidecar and
    # `inspect` prefers that sidecar over the settings layers, so an overlay
    # given only to gen-compose left inspect reading /home/ubuntu on a
    # batesste guest and reporting its key as unverified.
    _write_layer(state, "env.image-login", {"VM_USERNAME": from_image},
                 "The account the image reports, which beats the settings file.")


def fetch_key(state: _State) -> None:
    """Pull the guest keypair, which ships beside the qcow2 as a referrer.

    gen-vm pulls only the disk, so without this the generated inventory
    authenticates as a user that exists with a key that does not.
    """
    _say("Fetching the guest keypair")
    if state.key.is_file():
        print(f"already at {state.key}")
        _adopt_login(state)
        _publish_keypair(state)
        return

    state.key_dir.mkdir(parents=True, exist_ok=True)
    backing = state.values["VM_BACKING_IMAGE"]
    discovered = subprocess.run(["oras", "discover", backing, "--format", "json"],
                                capture_output=True, text=True)
    if discovered.returncode != 0:
        _die(f"oras discover failed on {backing}:\n{discovered.stderr.strip()}")
    # Selected by artifactType rather than by position: oras 1.3.4 calls the
    # list "referrers", and [0] is a guess waiting to break the day a
    # signature or an SBOM referrer is attached ahead of this one.
    referrers = json.loads(discovered.stdout).get("referrers", [])
    hits = [r["reference"] for r in referrers
            if r.get("artifactType") == _VM_INFO_TYPE]
    if not hits:
        _die(f"no {_VM_INFO_TYPE} referrer on the qcow2; found "
             f"{[r.get('artifactType') for r in referrers]!r}")
    subprocess.run(["oras", "pull", hits[0], "-o", str(state.key_dir)], check=True)
    # Named explicitly: a silent miss here surfaces much later as a permission
    # denial against a guest that is perfectly healthy.
    if not state.key.is_file():
        _die("the referrer carried no id_rsa")
    state.key.chmod(0o600)
    _adopt_login(state)
    _publish_keypair(state)


def _publish_keypair(state: _State) -> None:
    """Put the keypair where `inspect` and `ssh` look for it.

    Those two search <images>/keys/ and ~/.ssh, never the key directory this
    pulls to -- so without it they report the guest's key as "accepted by
    guest, not found locally" and the SSH line they print carries no -i.

    BOTH halves, and the private one is the whole point. inspect's
    _local_key_index derives the private path from the public one by
    stripping ".pub" and offers an identity only when that file exists
    (inspect.py:546-555). Publishing the public half alone therefore bought
    exactly nothing: `qemu-tool ssh quickstart-1` found the key, matched it
    against the guest, and still connected with no -i -- which lands on a
    password prompt, and reads as a broken guest rather than a missing file.
    The README tells people to use that command, so it has to work.

    This is not a secret being spread around: the key ships in a public OCI
    referrer beside a public image, and every guest built from that image
    accepts it. It is written 0600 regardless, because ssh refuses a private
    key the group can read and the failure names permissions rather than
    this.
    """
    pub = state.images_dir / "keys" / "quickstart_id_rsa.pub"
    private = pub.with_suffix("")
    pub.parent.mkdir(parents=True, exist_ok=True)
    if not pub.is_file():
        derived = subprocess.run(["ssh-keygen", "-y", "-f", str(state.key)],
                                 capture_output=True, text=True)
        if derived.returncode != 0:
            _die(f"could not derive the public key from {state.key}")
        pub.write_text(derived.stdout)
    # Not gated on the public half existing: an install from before this
    # published the private one has the .pub already, and would otherwise
    # never gain the half that makes it useful.
    if not private.is_file():
        shutil.copyfile(state.key, private)
        private.chmod(0o600)


def make_overlays(state: _State) -> None:
    """Two overlays of one backing file. This is the long step: ~2.5 GB
    pulled and unpacked to ~9 GB, once, however many guests there are."""
    _say("Creating the guest overlays")
    images = state.images_dir
    base_cfg = env_overrides(state.values)

    for index in range(1, state.vm_count + 1):
        name = state.vm(index)
        if (images / f"{name}.qcow2").is_file():
            print(f"{name} already exists, leaving it alone")
            continue
        fields = dict(base_cfg)
        fields["vm_name"] = name
        if index == 1:
            fields["backing_image"] = state.values["VM_BACKING_IMAGE"]
            fields["backing_file"] = None
        else:
            # Every guest after the first overlays the file the first one
            # fetched, which is what VM_BACKING_SHARED is for.
            fields["backing_image"] = None
            fields["backing_file"] = images / f"{state.vm(1)}-backing.qcow2"
        gen_vm_run(VMConfig(**fields))


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------

def generate_stack(state: _State) -> None:
    """Render the stack to the scratch directory, never over the committed one.

    qemu/compose/vfio-user-ernic-rocjitsu-scale-out/docker-compose.yml is
    generated from qemu/env/scale-out and `gen-compose --check` compares the
    two in CI.
    """
    _say("Generating the stack")
    state.stack_dir.mkdir(parents=True, exist_ok=True)

    # Under the stack directory, and absolute. The defaults are
    # /var/lib/qemu-tool/{prom,grafana}-data, which on a fresh host neither
    # exists nor is creatable by this user -- and the containers would then
    # need a chown to uid 65534 and 472 respectively, which needs root. Here
    # they sit beside the stack that produced them, which is also where you
    # would look for them.
    if state.metrics:
        data = {"PROM_DATA_DIR": str(state.stack_dir / "prom-data"),
                "GRAFANA_DATA_DIR": str(state.stack_dir / "grafana-data")}
        for path in data.values():
            Path(path).mkdir(parents=True, exist_ok=True)
        _write_layer(state, "env.metrics", data,
                     "Prometheus and Grafana state, beside the stack.")

    gen_compose_run(list(state.layers), state.stack_dir)
    print(f"wrote {state.stack_dir / 'docker-compose.yml'}")

    # The stack bind-mounts a qemu-tool source tree into its containers and
    # builds the tool inside it. Copied here rather than mounted from the
    # checkout because compose bind mounts are resolved by the HOST docker
    # daemon, and under --in-network the checkout path is container-internal
    # and does not exist there.
    if state.repo_root is not None:
        dest = state.stack_dir / "qemu"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(state.repo_root / "qemu", dest)


def _compose(state: _State, *args: str, check: bool = True,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "--project-name", state.stack_name]
    for layer in state.layers:
        cmd += ["--env-file", str(layer.resolve())]
    if state.metrics:
        cmd += ["--profile", "metrics"]
    cmd += list(args)
    full_env = os.environ.copy()
    full_env.update(env or {})
    result = subprocess.run(cmd, cwd=state.stack_dir, env=full_env)
    if check and result.returncode != 0:
        _die(f"`docker compose {' '.join(args)}` failed")
    return result


def up(state: _State) -> None:
    _say("Bringing the stack up")
    _compose(state, "up", "--detach",
             env={"QEMU_TOOL_SRC": str(state.stack_dir)})

    _say("Waiting for the NIC servers")
    deadline = time.monotonic() + 120
    while True:
        states = [_health(f"{state.stack_name}-ernic-{i}-1")
                  for i in range(1, state.vm_count + 1)]
        if all(s == "healthy" for s in states):
            break
        if time.monotonic() > deadline:
            _compose(state, "logs", *[f"ernic-{i}" for i in range(1, state.vm_count + 1)],
                     check=False)
            _die(f"the NIC servers never became healthy (last: {states})")
        time.sleep(2)
    print("NIC servers healthy")


def _health(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Health.Status}}", container],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "missing"


def wait_for_guests(state: _State) -> None:
    """Separate from up(), because a caller using --in-network can only reach
    the guests once it has joined the network that up() just created."""
    _say("Waiting for the guests")
    for index in range(1, state.vm_count + 1):
        host, port = state.guest_host(index), state.guest_port(index)
        deadline = time.monotonic() + 600
        while True:
            probe = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=5", "-i", str(state.key),
                 "-p", str(port), f"{state.username}@{host}", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                break
            if time.monotonic() > deadline:
                _die(f"guest {index} did not answer SSH at {host}:{port} "
                     f"as {state.username}")
            time.sleep(5)
        print(f"guest {index} up at {host}:{port}")


def _write_network_inventory(state: _State) -> Path:
    """A second inventory source overriding where each guest answers.

    Ansible merges inventories and the later source wins, so the generated
    one stays authoritative for everything else -- the MACs, the ernic
    addresses and the rocjitsu image the firmware is generated from.
    """
    path = state.stack_dir / "inventory-in-network.yml"
    lines = ["---",
             "# Written by `qemu-tool quick-start --in-network`. Do not edit.",
             "all:", "  hosts:"]
    for index in range(1, state.vm_count + 1):
        lines += [f"    {state.vm(index)}:",
                  f"      ansible_host: {state.guest_host(index)}",
                  f"      ansible_port: {state.guest_port(index)}"]
    path.write_text("\n".join(lines) + "\n")
    return path


def configure_and_test(state: _State) -> None:
    wait_for_guests(state)
    if state.repo_root is None:
        print("\nnote: no checkout found, so the guests are up but not "
              "configured and no test ran.\n"
              "      The playbook lives at ansible/playbooks/fleet-quickstart.yml "
              "in the repo;\n"
              "      clone it and re-run `qemu-tool quick-start test`, or pass "
              "--repo-root.", file=sys.stderr)
        return

    _say("Configuring the guests and running the tests")
    shutil.rmtree(state.artifacts, ignore_errors=True)
    before = _ernic_bytes(state, 1)

    cmd = ["ansible-playbook",
           "-i", str(state.stack_dir / "fleet-inventory.yml")]
    if state.in_network:
        cmd += ["-i", str(_write_network_inventory(state))]
    cmd += ["playbooks/fleet-quickstart.yml",
            "--private-key", str(state.key),
            "-e", f"quickstart_artifacts_dir={state.artifacts}"]
    result = subprocess.run(cmd, cwd=state.repo_root / "ansible")
    if result.returncode != 0:
        _die("the configure-and-test playbook failed")

    state.rdma_delta = _ernic_bytes(state, 1) - before
    _say(f"ernic 1 moved {state.rdma_delta} bytes")
    # A passing ib_send_bw proves the verbs worked; only the NIC's own
    # counter proves the bytes crossed the mesh. rocm-ernic writes this dump
    # unconditionally under -S, so it needs no metrics profile.
    if state.rdma_delta <= 0:
        _die("the ernic byte counter did not move")


def _ernic_bytes(state: _State, index: int) -> int:
    """The NIC's own counter, from the dump rocm-ernic writes under -S.

    A miss is an error rather than a 0: an unreadable counter and a transfer
    that moved nothing produce the same delta, and reporting the second when
    it was the first is a test that cannot fail.
    """
    result = subprocess.run(
        ["docker", "exec", f"{state.stack_name}-ernic-{index}-1", "sh", "-c",
         f"awk '/total_bytes_sent/ {{print $NF}}' /run/ernic-stats/ernic-{index}.stats"],
        capture_output=True, text=True)
    digits = "".join(c for c in result.stdout.splitlines()[0] if c.isdigit()) \
        if result.stdout.strip() else ""
    if not digits:
        _die(f"could not read the byte counter from ernic {index}")
    return int(digits)


# ---------------------------------------------------------------------------
# Report and teardown
# ---------------------------------------------------------------------------

def report(state: _State) -> None:
    if state.report_dir is None:
        return
    _say(f"Writing the report to {state.report_dir}")
    state.report_dir.mkdir(parents=True, exist_ok=True)
    # The site treats a report with no `Status: **pass**` as unknown rather
    # than as a pass, which is what a `report` run on its own -- with no test
    # behind it -- should produce.
    status = "pass" if (state.rdma_delta or 0) > 0 else "unknown"
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            cwd=state.repo_root or Path.cwd(),
                            capture_output=True, text=True)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    out = [
        "# Two-VM quickstart report",
        "",
        "Two guests from the published `ernic-rocjitsu` image, each with an",
        "emulated gfx1250, an emulated NVMe and an emulated RDMA NIC, booted",
        "by `qemu-tool quick-start`.",
        "",
        f"- Guest image: `{state.values['VM_BACKING_IMAGE'].split(':')[-1]}`",
        f"- Guest size: {state.vm_count} x {state.values['VM_VCPUS']} vCPU, "
        f"{state.values['VM_VMEM']} MiB",
        f"- RDMA bytes across the mesh: {state.rdma_delta if state.rdma_delta is not None else 'unknown'}",
        f"- Generated: **{stamp}** &middot; Commit: "
        f"`{commit.stdout.strip() or 'unknown'}` &middot; Status: **{status}**",
        "",
    ]
    if state.artifacts.is_dir():
        for path in sorted(state.artifacts.glob("*.md")):
            out += [path.read_text(), ""]
    out += ["## ernic counters", "```"]
    for index in range(1, state.vm_count + 1):
        result = subprocess.run(
            ["docker", "exec", f"{state.stack_name}-ernic-{index}-1", "sh", "-c",
             f"grep -E 'total_bytes' /run/ernic-stats/ernic-{index}.stats"],
            capture_output=True, text=True)
        for line in (result.stdout + result.stderr).splitlines():
            out.append(f"ernic-{index}: {line}")
    out.append("```")
    (state.report_dir / "index.md").write_text("\n".join(out) + "\n")


def down(state: _State) -> None:
    _say("Taking the stack down")
    # Power the guests off first. run-vm execs QEMU, so PID 1 in a qemu
    # container IS qemu-system-x86_64, and QEMU exits on SIGTERM rather than
    # ACPI-shutting-down the guest -- `compose down` on its own is a power
    # cut, which leaves guest filesystems needing journal replay and has been
    # observed to abort one outright.
    #
    # BY NAME, one guest at a time. `shutdown` with no VM named shuts down
    # every QEMU process on the node, and the hosts this runs on are exactly
    # the ones with a 32-VM fleet alongside -- so the convenient spelling
    # would have made `quick-start down` power off somebody's soak. A guest
    # that is already gone is not an error here: `down` has to work against a
    # half-dead stack, which is the state you most want it in.
    from .shutdown import run as shutdown_run

    for index in range(1, state.vm_count + 1):
        try:
            shutdown_run(state.vm(index), timeout=120, dry_run=False)
        except SystemExit as exc:
            print(f"note: {state.vm(index)}: {exc}")
    _compose(state, "down", check=False)
    print(f"the overlays under {state.images_dir} are left in place; "
          "delete them to start over")


# ---------------------------------------------------------------------------
# What a new user is actually here for
# ---------------------------------------------------------------------------

def _summary(state: _State) -> None:
    host = state.get("PUBLISH_BIND_ADDR")
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print()
    print("=" * 70)
    print(f"  {state.vm_count} guests are up, each with a GPU, an NVMe and an RDMA NIC.")
    print("=" * 70)
    print()
    print("  Log in -- qemu-tool resolves the port, the account and the key:")
    print()
    for index in range(1, state.vm_count + 1):
        print(f"      qemu-tool ssh {state.vm(index)}")
    print()
    print(f"      qemu-tool ssh {state.vm(1)} -- ibv_devinfo      # or any command")
    print()
    print("  Or with plain ssh, which is the same thing spelled out:")
    print()
    for index in range(1, state.vm_count + 1):
        print(f"      ssh -i {state.key} \\")
        print(f"          -p {state.guest_port(index)} {state.username}@{shown}")
    print()
    if state.metrics:
        print("  Monitoring (Grafana has anonymous admin on, so no login):")
        print()
        print(f"      Prometheus   http://{shown}:{state.get('PROM_PORT')}")
        print(f"      Grafana      http://{shown}:{state.get('GRAFANA_PORT')}")
        print()
        print("  The ernic dashboard is provisioned from files, under")
        print("  Dashboards in the Grafana sidebar. Its data comes from the")
        print("  stats dump each rocm-ernic writes, scraped by the exporter.")
        print()
    print("  What else is here:")
    print()
    print("      qemu-tool list --summary          # what the fleet is doing")
    print(f"      qemu-tool inspect {state.vm(1)}")
    print("      qemu-tool quick-start test        # re-run the GPU and RDMA tests")
    print("      qemu-tool quick-start down        # stop it, keep the overlays")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    verb: str,
    env_file: list[Path] | None,
    stack_dir: Path | None,
    key_dir: Path,
    report_dir: Path | None,
    repo_root: Path | None,
    images: Path | None,
    in_network: bool,
    metrics: bool,
    size_from_host: bool,
    assume_yes: bool,
) -> None:
    if env_file:
        layers = [p.expanduser().resolve() for p in env_file]
        for layer in layers:
            if not layer.is_file():
                _die(f"settings file not found: {layer}")
        base = layers[0]
    else:
        packaged = _packaged_env()
        if packaged is None:
            _die("qemu/env/quickstart is missing from this install; "
                 "pass --env-file")
        layers = [packaged]
        base = packaged

    requested = stack_dir or Path("/var/lib/qemu-tool/stacks/quickstart")
    state = _State(
        env_file=base,
        stack_dir=_resolve_stack_dir(requested.expanduser(), stack_dir is not None),
        key_dir=key_dir.expanduser().resolve(),
        report_dir=report_dir.expanduser().resolve() if report_dir else None,
        repo_root=_repo_root(repo_root),
        in_network=in_network,
        metrics=metrics,
        assume_yes=assume_yes,
        size_from_host=size_from_host,
        artifacts=Path("/var/tmp/quickstart-artifacts"),
        base_layers=layers,
    )
    # Picks up the overlays an earlier verb left beside the stack, which is
    # what makes `up` then `test` -- two processes, as the CI lane runs them
    # -- agree about the ports, the images directory and the account.
    _rescan_layers(state)

    if images is not None:
        _write_layer(state, "env.images",
                     {"VM_IMAGES_DIR": str(images.expanduser().resolve())},
                     "The images directory this run was given.")

    # Every verb, not just the ones that fetch. `up` and `test` are separate
    # processes in CI, so adopting the account inside fetch_key alone died
    # with the `up` process and the wait went back to the settings file's
    # name. The referrer is already on disk by then, so this is a local read
    # and a no-op before the first fetch.
    _adopt_login(state)

    if verb == "down":
        down(state)
        return
    if verb == "report":
        report(state)
        return
    if verb == "preflight":
        preflight(state)
        return

    if verb in ("all", "up"):
        preflight(state)
        _resolve_ports(state)
        if not _confirm(state, "Bring this up now?"):
            print("nothing done")
            return
        fetch_key(state)
        make_overlays(state)
        generate_stack(state)
        up(state)
        if verb == "up":
            return

    if verb in ("all", "test"):
        if verb == "test":
            preflight(state)
        configure_and_test(state)
        report(state)

    if verb == "all":
        _summary(state)
