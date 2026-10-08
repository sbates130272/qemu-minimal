"""Generate the scale-out compose stack from the env file.

The 2-VM stack hand-writes every socket path, MAC, SSH port and qcow2 name.
That does not survive 8 VMs, let alone the mesh's 64, so this derives all of
them from VM_COUNT and a handful of bases.

Per-VM values are comma-lists, not numbered keys. A bare value broadcasts to
every VM; a list of length VM_COUNT applies positionally; any other length is
an error naming the key and both lengths.

The exception is the one that silently does the wrong thing if you get it
backwards: three VMConfig fields are ALREADY list[str] (pci_hostdev,
vfio_userdev, extra_hostfwd), where the commas mean "several values for ONE
VM". VM_EXTRA_HOSTFWD=a,b is two hostfwd rules for every guest, not one rule
each for two guests. Which case applies is read off VMConfig's own type hints
rather than a hand-kept list of key names, so a scalar field added to VMConfig
later becomes per-VM-overridable for free.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import sys
import typing
from pathlib import Path
from typing import Any

from .config import VMConfig
from .envfile import _coerce, env_key, find as find_env_file, parse as parse_env

# Fields the generated qemu entrypoint forwards to run-vm, in emit order.
# Everything here is per-VM overridable by the rules above.
_VM_FLAGS = [
    ("vcpus", "--vcpus"),
    ("vmem", "--vmem"),
    ("extra_hostfwd", "--extra-hostfwd"),
    # Like extra_hostfwd this is list[str], so the commas are several devices
    # for ONE VM and the whole value reaches every VM. That is the right
    # meaning for a field of this type and the wrong thing to ask for at
    # VM_COUNT > 1: a PCI function cannot be assigned to two guests, so the
    # second QEMU to claim it fails to start. Passing through one device per
    # guest needs per-VM hostdev support, which does not exist yet.
    ("pci_hostdev", "--pci-hostdev"),
]

# Per-VM keys that name no VMConfig field, so they need their own split.
_EXTRA_PER_VM = ["VM_SHM_SIZE", "ROCJITSU_CONFIG"]

# Derived per-VM, each with an optional override list.
_DEFAULTS = {
    "VM_NAME_PREFIX": "qemu-fleet",
    "VM_SSH_PORT_BASE": "2222",
    "ERNIC_MAC_PREFIX": "72:6f:63:6d",
    "ERNIC_GUEST_SUBNET": "192.168.100",
    "ERNIC_TCP_PORT": "6320",
    "ERNIC_STATS_PORT": "9840",
    "ERNIC_NOFILE": "65536",
    "ERNIC_TAP": "false",
    "LOG_MAX_SIZE": "50m",
    "LOG_MAX_FILE": "3",
    "PROM_DATA_DIR": "./prom-data",
    "PROM_PORT": "9090",
    "PROM_SCRAPE_INTERVAL": "15s",
    "PROM_RETENTION": "7d",
    "VM_SHM_SIZE": "8g",
    "ROCJITSU_CONFIG": "gfx1250_mi455x.json",
    "VM_IMAGES_DIR": "/var/lib/qemu-tool/images",
    "QEMU_TOOL_SRC": "../../..",
    "FLEET_NET_PREFIX": "172.31",
}

# Third octet per service family. Static addressing keeps Docker's embedded
# DNS off the critical path: measured at 48 VMs, a fleet that resolves service
# names loses workers to "Temporary failure in name resolution" during the
# start burst, and each restart consumes a fresh mesh node id -- so a 48-VM
# fleet reached id 61 against a 64-node protocol ceiling. See the stack README.
_NET_ERNIC, _NET_ROCJITSU, _NET_QEMU, _NET_INFRA = 1, 2, 3, 0

# rocm-ernic's topology payload builder caps the mesh here
# (src/rdma/rdma_backend_tcp.c: `num_nodes < 64`).
_MESH_MAX = 64
_STAMP_RE = re.compile(r"\d{8}\.g[0-9a-f]+")
_TAP_IF = "tap0"
_TAPSETUP_IMAGE = "qemu-tool/ernic-tapsetup:1"

_PROVENANCE = "# env-sha256: "


def env_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded_digest(compose_file: Path) -> str | None:
    """Return the env hash a generated compose file was built from."""
    try:
        for line in compose_file.read_text().splitlines()[:20]:
            if line.startswith(_PROVENANCE):
                return line[len(_PROVENANCE):].strip()
    except OSError:
        return None
    return None


def _get(values: dict[str, str], key: str) -> str:
    raw = values.get(key, "")
    return raw if raw else _DEFAULTS.get(key, "")


def _split_per_vm(raw: str, count: int, key: str) -> list[str]:
    """Broadcast a scalar, or apply a length-VM_COUNT list positionally."""
    items = [p.strip() for p in raw.split(",")]
    if len(items) == 1:
        return items * count
    if len(items) != count:
        sys.exit(
            f"Error: {key} has {len(items)} values but VM_COUNT is {count}. "
            f"Give one value to apply to every VM, or exactly {count}."
        )
    return items


def _per_vm_flags(values: dict[str, str], count: int) -> list[list[tuple[str, str]]]:
    """Resolve the run-vm flags for each VM as (flag, value) pairs."""
    hints = typing.get_type_hints(VMConfig)
    per_vm: list[list[tuple[str, str]]] = [[] for _ in range(count)]
    for field, flag in _VM_FLAGS:
        key = env_key(field)
        raw = values.get(key, "")
        if not raw:
            continue
        if typing.get_origin(hints[field]) is list:
            # A list[str] field: the commas are already spoken for. The whole
            # value goes to every VM, once per element.
            for item in _coerce(hints[field], raw, key):
                for vm in per_vm:
                    vm.append((flag, item))
            continue
        for index, item in enumerate(_split_per_vm(raw, count, key)):
            # Coerce per element so a per-VM value is type-checked exactly as
            # a broadcast one is.
            _coerce(hints[field], item, key)
            per_vm[index].append((flag, item))
    return per_vm


def _check_image_coherence(values: dict[str, str]) -> None:
    """Refuse an image set that cannot have been built together.

    batesste-ci-images tags carry a shared <date>.<ci-sha> build stamp across
    the containers and the qcow2, so this is a string comparison. It is worth
    doing because the failure it prevents does not present as a version
    problem: a guest driver and a host server from different SHAs wedge at the
    vfio-user/DSR boundary, surfacing as the -ETIMEDOUT and "Failed to register
    ibdev" symptoms in AGENTS.md. Across a whole fleet that is an expensive
    afternoon.
    """
    keys = ["QEMU_IMAGE", "ERNIC_IMAGE", "ROCJITSU_IMAGE", "VM_BACKING_IMAGE"]
    stamps: dict[str, str] = {}
    vfu: dict[str, str] = {}
    unstamped: list[str] = []
    for key in keys:
        ref = values.get(key, "")
        if not ref or ":" not in ref:
            continue
        tag = ref.rsplit(":", 1)[1]
        stamp = tag.split("-", 1)[0]
        # A tag with no <date>.<ci-sha> stamp did not come from
        # batesste-ci-images, so there is nothing to compare it against. That
        # is the shape of a locally built image carrying a fix under test --
        # exactly what you want to run a fleet against before it ships. Warn
        # and exclude it rather than refusing: the check still catches the
        # dangerous case, which is two *different* real builds mixed together.
        if not _STAMP_RE.fullmatch(stamp):
            unstamped.append(f"  {key} -> {ref}")
            continue
        stamps[key] = stamp
        for part in tag.split("-"):
            if part.startswith("vfu."):
                vfu[key] = part

    if unstamped:
        print("Warning: image(s) with no build stamp, excluded from the "
              "coherence check:", file=sys.stderr)
        print("\n".join(unstamped), file=sys.stderr)
        print("  A mismatched driver/server pair wedges at the vfio-user/DSR "
              "boundary; verify by hand.", file=sys.stderr)

    distinct = set(stamps.values())
    if len(distinct) > 1:
        detail = "\n".join(f"  {k} -> {v}" for k, v in sorted(stamps.items()))
        sys.exit(
            "Error: image tags do not share one build stamp:\n" + detail +
            "\nAll four must come from the same batesste-ci-images build."
        )

    distinct_vfu = set(vfu.values())
    if len(distinct_vfu) > 1:
        detail = "\n".join(f"  {k} -> {v}" for k, v in sorted(vfu.items()))
        sys.exit(
            "Error: libvfio-user revisions differ between qemu and the device "
            "servers:\n" + detail +
            "\nThe vfio-user wire protocol is not compatible across revisions."
        )


def plan(values: dict[str, str]) -> dict[str, Any]:
    """Resolve the env file into everything the templates need."""
    raw_count = _get(values, "VM_COUNT")
    if not raw_count:
        sys.exit("Error: VM_COUNT is required in the env file for this stack.")
    try:
        count = int(raw_count)
    except ValueError:
        sys.exit(f"Error: VM_COUNT must be an integer, got {raw_count!r}")
    if count < 1:
        sys.exit(f"Error: VM_COUNT must be at least 1, got {count}")
    if count > _MESH_MAX:
        sys.exit(
            f"Error: VM_COUNT is {count}, above the {_MESH_MAX}-node ceiling "
            "in rocm-ernic's mesh topology payload (rdma_backend_tcp.c)."
        )

    _check_image_coherence(values)

    prefix = _get(values, "VM_NAME_PREFIX")
    names = _override_list(values, "VM_NAMES", count) or \
        [f"{prefix}-{n}" for n in range(1, count + 1)]

    port_base = int(_get(values, "VM_SSH_PORT_BASE"))
    ports = _override_list(values, "VM_SSH_PORTS", count) or \
        [str(port_base + n - 1) for n in range(1, count + 1)]

    mac_prefix = _get(values, "ERNIC_MAC_PREFIX")
    macs = _override_list(values, "ERNIC_MACS", count) or \
        [f"{mac_prefix}:{n >> 8:02x}:{n & 0xff:02x}" for n in range(1, count + 1)]

    subnet = _get(values, "ERNIC_GUEST_SUBNET")
    # Guests start at .11. Avoid .1: the mesh manager claims it as its DHCP
    # server_ip and intercepts ARP for it (AGENTS.md known issue 1).
    ips = _override_list(values, "ERNIC_GUEST_IPS", count) or \
        [f"{subnet}.{10 + n}" for n in range(1, count + 1)]

    for label, seq in (("VM name", names), ("SSH port", ports), ("ernic MAC", macs)):
        if len(set(seq)) != len(seq):
            sys.exit(f"Error: {label} values are not unique across the fleet: {seq}")

    extras = {
        key: _split_per_vm(_get(values, key), count, key)
        for key in _EXTRA_PER_VM
    }

    flags = _per_vm_flags(values, count)

    net = _get(values, "FLEET_NET_PREFIX")

    vms = []
    for index in range(count):
        n = index + 1
        vms.append({
            "n": n,
            "ernic_addr": f"{net}.{_NET_ERNIC}.{n}",
            "rocjitsu_addr": f"{net}.{_NET_ROCJITSU}.{n}",
            "qemu_addr": f"{net}.{_NET_QEMU}.{n}",
            "name": names[index],
            "ssh_port": ports[index],
            "mac": macs[index],
            "guest_gids": f"{_link_local_gid(macs[index])},{ips[index]}",
            "ip": ips[index],
            "ernic_sock": f"/run/vfu/ernic-{n}.sock",
            "rocjitsu_sock": f"/run/vfu/rocjitsu-{n}.sock",
            "stats_file": f"/run/ernic-stats/ernic-{n}.stats",
            "shm_size": extras["VM_SHM_SIZE"][index],
            "rocjitsu_config": extras["ROCJITSU_CONFIG"][index],
            "flags": flags[index],
        })

    return {
        "count": count,
        "vms": vms,
        "net_prefix": net,
        "exporter_addr": f"{net}.{_NET_INFRA}.10",
        "prometheus_addr": f"{net}.{_NET_INFRA}.11",
        "tcp_port": _get(values, "ERNIC_TCP_PORT"),
        "stats_port": _get(values, "ERNIC_STATS_PORT"),
        "ernic_nofile": _get(values, "ERNIC_NOFILE"),
        "ernic_tap": _get(values, "ERNIC_TAP").strip().lower()
        in ("1", "true", "yes", "on"),
        "log_max_size": _get(values, "LOG_MAX_SIZE"),
        "log_max_file": _get(values, "LOG_MAX_FILE"),
        "prom_data_dir": _get(values, "PROM_DATA_DIR"),
        "prom_port": _get(values, "PROM_PORT"),
        "prom_interval": _get(values, "PROM_SCRAPE_INTERVAL"),
        "prom_retention": _get(values, "PROM_RETENTION"),
    }


def _override_list(values: dict[str, str], key: str, count: int) -> list[str] | None:
    raw = values.get(key, "")
    if not raw:
        return None
    items = [p.strip() for p in raw.split(",") if p.strip()]
    if len(items) != count:
        sys.exit(
            f"Error: {key} has {len(items)} values but VM_COUNT is {count}."
        )
    return items


def _link_local_gid(mac: str) -> str:
    """The IPv6 link-local address a guest autoconfigures from `mac`.

    EUI-64: flip the universal/local bit of the first octet and insert
    ff:fe in the middle. The guests really do come up on this -- verified
    against a running fleet -- so deriving it is better than asking the
    settings file to repeat what the MAC already says.
    """
    o = [int(x, 16) for x in mac.split(":")]
    eui = bytes([o[0] ^ 0x02, o[1], o[2], 0xFF, 0xFE, o[3], o[4], o[5]])
    # Let ipaddress do the zero-compression: hand-rolling it produces
    # non-canonical forms like "fe80::0:ff:fe00:1" for MACs whose first
    # three octets are small, and a GID the mesh cannot match is exactly
    # the silent-misroute this setting exists to prevent.
    return str(ipaddress.IPv6Address(bytes.fromhex("fe80") + bytes(6) + eui))


def _pipx_install(bin_dir: str = "/usr/local/bin") -> list[str]:
    """The entrypoint's install step, as lines of an 8-space block scalar.

    bin_dir is a parameter because the stats exporter runs unprivileged and
    cannot write /usr/local/bin; it installs under /tmp instead. pipx also
    wants a writable HOME, which nobody does not otherwise have.
    """
    env = f"PIPX_BIN_DIR={bin_dir}"
    if not bin_dir.startswith("/usr"):
        env = f"HOME=/tmp PIPX_HOME=/tmp/pipx {env}"
    return [
        "        for i in 1 2 3 4 5; do",
        f"          {env} pipx install --force \\",
        "            /tmp/qemu-tool-build && break",
        "          sleep 10",
        "        done",
    ]


def render_compose(spec: dict[str, Any], env_name: str, digest: str) -> str:
    out: list[str] = ["---"]
    out.append("# Generated by `qemu-tool gen-compose`. Do not edit.")
    out.append("#")
    out.append(f"# Regenerate after changing {env_name}:")
    out.append(f"#   qemu-tool gen-compose --env-file {env_name}")
    out.append("#")
    out.append(f"# env-file: {env_name}")
    out.append(f"{_PROVENANCE}{digest}")
    out.append("")
    # Bound every container's log. Docker's json-file driver is unlimited by
    # default, and an ernic manager that reaches EMFILE spins in an accept()
    # error loop with no backoff: one measured here wrote **295 GB** in two
    # days before anyone looked. Rotation keeps the tail -- which is the part
    # worth reading -- without letting a wedged service fill the disk.
    out.append("x-logging: &logging")
    out.append("  driver: json-file")
    out.append("  options:")
    out.append(f'    max-size: "{spec["log_max_size"]}"')
    out.append(f'    max-file: "{spec["log_max_file"]}"')
    out.append("")
    out.append("x-ernic-common: &ernic-common")
    out.append('  image: "${ERNIC_IMAGE}"')
    out.append("  logging: *logging")
    # Docker's default soft nofile limit is 1024, and the mesh manager does not
    # close the socket of a node it evicts -- a reconnecting worker gets a new
    # node id and a new fd, and the old one is never reclaimed. At fleet scale
    # the manager therefore leaks roughly one fd per eviction until accept()
    # starts returning EMFILE, after which it spins in a tight error loop (3.3
    # GB of log in 12 hours, measured at 40 VMs) and the guest attached to it
    # dies. Raising the limit does not fix the leak, it just moves the wall far
    # enough out that a fleet-length run finishes first.
    out.append("  ulimits:")
    out.append("    nofile:")
    out.append(f"      soft: {spec['ernic_nofile']}")
    out.append(f"      hard: {spec['ernic_nofile']}")
    out.append("  volumes:")
    out.append("    - vfu-sockets:/run/vfu")
    out.append("    - ernic-stats:/run/ernic-stats")
    out.append("")
    out.append("x-qemu-common: &qemu-common")
    out.append('  image: "${QEMU_IMAGE}"')
    out.append("  logging: *logging")
    out.append("  volumes:")
    out.append("    - vfu-sockets:/run/vfu:ro")
    out.append('    - "${VM_IMAGES_DIR}:${VM_IMAGES_DIR}"')
    out.append('    - "${QEMU_TOOL_SRC:-../../..}:/qemu-tool-src:ro"')
    out.append("  devices:")
    out.append("    - /dev/kvm")
    out.append('  restart: "no"')
    out.append("")
    out.append("services:")

    for vm in spec["vms"]:
        n = vm["n"]
        out.append("")
        if n == 1:
            out.append(f"  # ---- VM {n} NIC: TCP mesh manager ----")
        else:
            out.append(f"  # ---- VM {n} NIC: TCP mesh worker ----")
        out.append(f"  ernic-{n}:")
        out.append("    <<: *ernic-common")
        # The node's hostname IS its mesh address: each rocm-ernic advertises
        # gethostname() to the manager and every peer then resolves that name
        # to dial it, so the default (the container id) puts Docker's embedded
        # DNS on every one of the fleet's N*(N-1) peer links. Naming the
        # container after its own address makes that a numeric getaddrinfo.
        out.append(f"    hostname: {vm['ernic_addr']}")
        if spec["ernic_tap"]:
            # Attaching to a pre-created, owned tap needs the device node and
            # nothing else -- no NET_ADMIN. That capability is the sidecar's.
            out.append("    devices:")
            out.append("      - /dev/net/tun")
        out.append("    networks:")
        if spec["ernic_tap"]:
            out.append("      l2: {}")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {vm['ernic_addr']}")
        if n == 1:
            backend = f"tcp:manager:listen:{spec['tcp_port']}"
            # A manager restart leaves every guest driver wedged on a DSR
            # timeout that only `down && up` clears (AGENTS.md issue 7), so
            # a crash must stay visible rather than be papered over.
            out.append('    restart: "no"')
        else:
            # The manager by address, not by name. Resolving "ernic-1" here
            # puts Docker's embedded DNS in the path of every worker's start,
            # and at fleet scale that is where the fleet breaks.
            backend = f"tcp:worker:{spec['vms'][0]['ernic_addr']}:{spec['tcp_port']}"
            out.append("    restart: on-failure")
        # Per node, not in the shared anchor: the GID set is what lets the
        # mesh resolve a destination GID to *this* node, so it is inherently
        # per-node. Getting it wrong is not a startup error -- the mesh
        # silently routes to some other node and the transfer fails with
        # "Completion with error at client".
        out.append("    environment:")
        out.append(f"      - ERNIC_TCP_GUEST_GIDS={vm['guest_gids']}")
        argv = ["rocm-ernic", "-s", vm["ernic_sock"], "-b", backend,
                "-m", vm["mac"], "-S", vm["stats_file"]]
        if spec["ernic_tap"]:
            argv += ["-T", _TAP_IF]
            # The sidecar can only run once this container exists, because it
            # shares its netns -- so the tap does not exist at the instant
            # rocm-ernic starts. Wait for it rather than racing and dying.
            quoted = " ".join(f"'{a}'" for a in argv[1:])
            out.append("    entrypoint:")
            out.append("      - /bin/sh")
            out.append("      - -c")
            out.append("      - |")
            out.append(f"        n=0; until [ -e /sys/class/net/{_TAP_IF} ]; do")
            out.append("          n=$$((n+1))")
            out.append(f"          [ $$n -gt 240 ] && echo '{_TAP_IF} never appeared' && exit 1")
            out.append("          sleep 0.5")
            out.append("        done")
            out.append(f"        exec {argv[0]} {quoted}")
        else:
            out.append("    command:")
            for a in argv:
                out.append(f'      - "{a}"')
        if n != 1:
            # Chained, not all-depend-on-the-manager. The mesh assigns node
            # ids in *connection* order, but the GID->node resolver assumes
            # id == (last MAC byte - 1). Let workers race and whichever wins
            # takes node 1, so a guest addressing ernic-2 has its RDMA payload
            # routed to whichever ernic happened to register first -- the
            # transfer then fails with "Completion with error at client".
            # Starting them one at a time makes registration order match
            # index order, which is the only order the resolver gets right.
            out.append("    depends_on:")
            out.append(f"      ernic-{n - 1}:")
            out.append("        condition: service_healthy")
        out.append("    healthcheck:")
        if n == 1:
            # The socket file alone is not enough for the manager: workers
            # gate on this condition and then dial its TCP port, so the check
            # has to prove that port is listening, not just that the process
            # got as far as binding a unix socket.
            #
            # It reads /proc/net/tcp rather than connecting, because the
            # manager never closes a connection that disconnects without
            # registering -- it leaves it in CLOSE_WAIT forever. A connecting
            # check at this interval therefore leaks a manager fd every time
            # it runs: measured at 28/min on an otherwise perfectly healthy
            # 32-VM fleet with zero evictions and zero restarts, which alone
            # reaches Docker's 1024 default in about 37 minutes. The leak is
            # upstream's; handing it a connection every 2 s was ours.
            #
            # Fields are hex and space-separated: local_address is $2 as
            # <addr>:<port>, st is $4, and 0A is TCP_LISTEN.
            port_hex = f"{int(spec['tcp_port']):04X}"
            out.append(
                '      test: ["CMD", "awk", '
                f'"$2 ~ /:{port_hex}$/ && $4 == \\"0A\\" {{found=1}} '
                'END {exit !found}", "/proc/net/tcp"]'
            )
        else:
            # Proving the socket exists is not enough to serialise
            # registration: rocm-ernic binds it before it has registered with
            # the manager. An ESTABLISHED connection to the manager's port is
            # the observable that registration actually happened.
            out.append(
                '      test: ["CMD", "awk", '
                f'"$3 ~ /:{port_hex}$/ && $4 == \\"01\\" {{found=1}} '
                'END {exit !found}", "/proc/net/tcp"]'
            )
        out.append("      interval: 2s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 5s")

    for vm in spec["vms"]:
        n = vm["n"]
        out.append("")
        out.append(f"  # ---- VM {n} GPU ----")
        out.append(f"  rocjitsu-{n}:")
        out.append("    <<: *ernic-common")
        out.append('    image: "${ROCJITSU_IMAGE}"')
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {vm['rocjitsu_addr']}")
        out.append("    restart: on-failure")
        out.append("    command:")
        out.append("      - rocjitsu")
        out.append("      - --config")
        out.append(f"      - /usr/local/share/rocjitsu/configs/{vm['rocjitsu_config']}")
        out.append("      - --vfio-socket")
        out.append(f"      - {vm['rocjitsu_sock']}")
        out.append("    healthcheck:")
        out.append(f'      test: ["CMD", "sh", "-c", "test -S {vm["rocjitsu_sock"]}"]')
        out.append("      interval: 2s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 5s")

    for vm in spec["vms"]:
        n = vm["n"]
        out.append("")
        out.append(f"  # ---- VM {n}: guest ernic IP {vm['ip']} ----")
        out.append(f"  qemu-{n}:")
        out.append("    <<: *qemu-common")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {vm['qemu_addr']}")
        out.append(f'    shm_size: "{vm["shm_size"]}"')
        out.append("    depends_on:")
        out.append(f"      ernic-{n}:")
        out.append("        condition: service_healthy")
        out.append(f"      rocjitsu-{n}:")
        out.append("        condition: service_healthy")
        out.append("    ports:")
        out.append(f'      - "{vm["ssh_port"]}:2222"')
        out.append("    entrypoint:")
        out.append("      - /bin/sh")
        out.append("      - -c")
        out.append("      - |")
        out.append("        cp -r /qemu-tool-src/qemu /tmp/qemu-tool-build")
        # pipx reaches pypi.org for the build backend, and a fleet's worth of
        # these racing at container start is enough to make Docker's embedded
        # resolver drop queries -- measured at 48 VMs, where 12 guests died on
        # "Failed to resolve 'pypi.org'" before QEMU ever started. Retry
        # rather than lose the guest.
        out.extend(_pipx_install())
        out.append("        exec qemu-tool run-vm \\")
        out.append('          --images "${VM_IMAGES_DIR}" \\')
        out.append(f'          --vm-name "{vm["name"]}" \\')
        out.append("          --ssh-port 2222 \\")
        out.append("          --backing-shared \\")
        for flag, value in vm["flags"]:
            out.append(f'          {flag} "{value}" \\')
        # Sockets are passed explicitly rather than globbed: with a fleet's
        # worth of sockets on one shared tmpfs, the single-VM stack's
        # `ls /run/vfu/*.sock` would attach every device to every VM.
        out.append(f'          --vfio-userdev "{vm["ernic_sock"]},{vm["rocjitsu_sock"]}"')

    out.append("")
    out.append("  # ---- metrics (opt-in: --profile metrics) ----")
    out.append("  ernic-stats-exporter:")
    out.append("    <<: *qemu-common")
    out.append("    profiles: [metrics]")
    out.append("    networks:")
    out.append("      fleet:")
    out.append(f"        ipv4_address: {spec['exporter_addr']}")
    out.append("    devices: []")
    # The exporter needs no privilege: it reads world-readable dumps off a
    # sticky tmpfs and binds an unprivileged port. Every other service in the
    # stack is root only because its base image declares no USER, which is a
    # default rather than a decision; this one is cheap to fix, so it is.
    out.append('    user: "65534:65534"')
    out.append("    volumes:")
    out.append("      - ernic-stats:/run/ernic-stats:ro")
    out.append('      - "${QEMU_TOOL_SRC:-../../..}:/qemu-tool-src:ro"')
    out.append("    entrypoint:")
    out.append("      - /bin/sh")
    out.append("      - -c")
    out.append("      - |")
    out.append("        cp -r /qemu-tool-src/qemu /tmp/qemu-tool-build")
    out.extend(_pipx_install("/tmp/bin"))
    out.append("        exec /tmp/bin/qemu-tool ernic-stats \\")
    out.append("          --stats-dir /run/ernic-stats \\")
    out.append(f"          --port {spec['stats_port']}")
    out.append("")
    out.append("  prometheus:")
    out.append("    image: docker.io/prom/prometheus:latest")
    out.append("    logging: *logging")
    out.append("    profiles: [metrics]")
    out.append("    networks:")
    out.append("      fleet:")
    out.append(f"        ipv4_address: {spec['prometheus_addr']}")
    out.append("    restart: on-failure")
    out.append("    volumes:")
    out.append("      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro")
    # A bind mount, not a named volume: the TSDB is the record of a fleet run,
    # and a named volume puts it under /var/lib/docker/volumes where it is
    # root-owned, invisible to the person who ran the fleet, and silently
    # shared with the next run of a different size. A path they chose is one
    # they can archive next to the rest of the run's artifacts.
    out.append(f"      - {spec['prom_data_dir']}:/prometheus")
    out.append("    command:")
    out.append("      - --config.file=/etc/prometheus/prometheus.yml")
    out.append(f"      - --storage.tsdb.retention.time={spec['prom_retention']}")
    out.append("    ports:")
    out.append(f'      - "{spec["prom_port"]}:9090"')

    if spec["ernic_tap"]:
        out.append("")
        out.append("  # ---- guest Ethernet wiring ----")
        out.append("  # One short-lived privileged sidecar per ernic. It shares")
        out.append("  # that ernic's network namespace, creates the tap and the")
        out.append("  # bridge, and exits. NET_ADMIN lives here and nowhere else.")
        for vm in spec["vms"]:
            n = vm["n"]
            out.append(f"  tapsetup-{n}:")
            out.append("    build: ./tapsetup")
            out.append(f'    image: "{_TAPSETUP_IMAGE}"')
            out.append("    logging: *logging")
            out.append(f'    network_mode: "service:ernic-{n}"')
            out.append("    cap_add: [NET_ADMIN]")
            out.append("    devices:")
            out.append("      - /dev/net/tun")
            out.append('    restart: "no"')
            out.append("    depends_on:")
            out.append(f"      ernic-{n}:")
            out.append("        condition: service_started")

    out.append("")
    out.append("networks:")
    if spec["ernic_tap"]:
        out.append("  # Dumb shared L2 carrying guest Ethernet only. No address")
        out.append("  # is assigned: each ernic bridges its tap onto it, and the")
        out.append("  # guests address each other over ERNIC_GUEST_SUBNET.")
        out.append("  l2:")
        out.append("    driver: bridge")
    out.append("  # Every service has a fixed address and nothing resolves a")
    out.append("  # service name at run time. Docker's embedded DNS is a single")
    out.append("  # resolver per network, and a fleet start burst saturates it:")
    out.append("  # at 48 VMs, workers died with 'Temporary failure in name")
    out.append("  # resolution', and because the mesh allocates a NEW node id on")
    out.append("  # every reconnect, the restarts walked the fleet toward the")
    out.append("  # 64-node protocol ceiling. Third octet is the service family:")
    out.append(f"  #   {_NET_INFRA} infra   {_NET_ERNIC} ernic   {_NET_ROCJITSU} rocjitsu   {_NET_QEMU} qemu")
    out.append("  fleet:")
    out.append("    driver: bridge")
    out.append("    ipam:")
    out.append("      config:")
    out.append(f"        - subnet: {spec['net_prefix']}.0.0/16")
    out.append("")
    out.append("volumes:")
    for name in ("vfu-sockets", "ernic-stats"):
        out.append(f"  {name}:")
        out.append("    driver: local")
        out.append("    driver_opts:")
        out.append("      type: tmpfs")
        out.append("      device: tmpfs")
    return "\n".join(out) + "\n"


def render_prometheus(spec: dict[str, Any], env_name: str, digest: str) -> str:
    out: list[str] = ["---"]
    out.append("# Generated by `qemu-tool gen-compose`. Do not edit.")
    out.append(f"# env-file: {env_name}")
    out.append(f"{_PROVENANCE}{digest}")
    out.append("")
    out.append("global:")
    out.append(f"  scrape_interval: {spec['prom_interval']}")
    out.append("")
    out.append("scrape_configs:")
    out.append("  # The guests sit behind QEMU SLIRP inside their qemu")
    out.append("  # containers and have no address on this network. The")
    out.append("  # container is the target; --extra-hostfwd forwards :9100")
    out.append("  # through to the guest's node-exporter. Each container has")
    out.append("  # its own netns, so every VM uses the same port.")
    out.append("  #")
    out.append("  # Targets are addresses, not names: the compose file pins one")
    out.append("  # per service so nothing in the fleet depends on Docker's")
    out.append("  # embedded DNS, and a scrape loop is no exception.")
    out.append("  - job_name: fleet-node")
    out.append("    static_configs:")
    for vm in spec["vms"]:
        out.append(f"      - targets: [\"{vm['qemu_addr']}:9100\"]")
        out.append("        labels:")
        out.append(f'          vm: "{vm["n"]}"')
        out.append(f'          vm_name: "{vm["name"]}"')
    out.append("")
    out.append("  - job_name: ernic-stats")
    out.append("    static_configs:")
    out.append(f"      - targets: [\"{spec['exporter_addr']}:{spec['stats_port']}\"]")
    return "\n".join(out) + "\n"


def run(
    env_file: Path | None,
    output_dir: Path,
    check: bool = False,
    dry_run: bool = False,
) -> None:
    path = find_env_file(env_file)
    if path is None:
        sys.exit("Error: no env file found. Pass --env-file.")
    values = parse_env(path)
    spec = plan(values)
    digest = env_digest(path)
    env_name = str(env_file) if env_file is not None else path.name

    artifacts = {
        "docker-compose.yml": render_compose(spec, env_name, digest),
        "prometheus.yml": render_prometheus(spec, env_name, digest),
    }

    if dry_run:
        for name, body in artifacts.items():
            print(f"===== {name} =====")
            print(body, end="")
        return

    if check:
        stale = [
            name for name, body in artifacts.items()
            if not (output_dir / name).is_file()
            or (output_dir / name).read_text() != body
        ]
        if stale:
            sys.exit(
                "Error: generated files are out of date: " + ", ".join(sorted(stale)) +
                f"\nRegenerate with: qemu-tool gen-compose --env-file {env_name}"
            )
        print(f"Up to date: {output_dir}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, body in artifacts.items():
        (output_dir / name).write_text(body)
        print(f"Wrote {output_dir / name}")

    _print_next_steps(spec, values)


def _print_next_steps(spec: dict[str, Any], values: dict[str, str]) -> None:
    backing = values.get("VM_BACKING_IMAGE", "")
    first = spec["vms"][0]["name"]
    images = _get(values, "VM_IMAGES_DIR")
    print()
    print(f"{spec['count']} VMs. If the images do not exist yet:")
    print()
    if backing:
        print(f"  qemu-tool gen-vm --vm-name {first} \\")
        print(f'      --backing-image "{backing}"')
    else:
        print(f"  qemu-tool gen-vm --vm-name {first}")
    for vm in spec["vms"][1:]:
        print(f"  qemu-tool gen-vm --vm-name {vm['name']} \\")
        print(f"      --backing-file {images}/{first}-backing.qcow2")
