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
    # Emulated NVMe, as LMCache's L2 tier. run-vm creates the backing qcow2
    # per device itself (_nvme_create), so unlike the root disk there is no
    # gen-vm step to pair with this. A bare count ("1") is the common case,
    # and a negative count for null_blk works too.
    #
    # run-vm's --nvme also accepts a literal QEMU argument string, but NOT
    # through here: this names a str field, so commas are the per-VM split.
    # "4096,logical_block_size=4096" is two values, which at VM_COUNT=8 is a
    # loud count mismatch and at VM_COUNT=2 is a silent one -- guest 1 gets
    # "4096", guest 2 gets "logical_block_size=4096". Pinned by
    # TestNVMe in tests/test_gen_compose.py.
    ("nvme", "--nvme"),
    # A host directory shared into every guest over 9p (mount_tag=hostfs).
    # This is how a fleet gets a large artifact -- the LMCache image tarball,
    # most obviously -- without copying it per VM: one file on the host,
    # read by all N guests. At VM_COUNT=8 that is 22 GB of transfer saved,
    # at 40 it is 112 GB. The directory must be readable by the qemu
    # container, so it is bind-mounted in below.
    ("filesystem", "--filesystem"),
    ("extra_hostfwd", "--extra-hostfwd"),
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
    # Absolute, and deliberately NOT "./prom-data". A relative path resolves
    # against the STACK directory, and the stack directory is wherever
    # qemu-tool found it -- for a pipx or .deb install that is inside
    # site-packages or /usr/share, so the TSDB a fleet run produced lands
    # somewhere nobody thinks to look and a reinstall deletes. Measured: a
    # pipx-installed run put it under
    # ~/.local/share/pipx/venvs/qemu-tool/.../share/compose/<stack>/prom-data,
    # root-owned, and Prometheus then crash-looped on "mkdir data/:
    # permission denied".
    "PROM_DATA_DIR": "/var/lib/qemu-tool/prom-data",
    "PROM_PORT": "9090",
    # Host interface the published ports bind to. 0.0.0.0 so a fleet running
    # on a lab box is reachable from a workstation; set to 127.0.0.1 to keep
    # it local. Grafana has anonymous admin enabled, so this is the control
    # that decides who can reach it.
    "PUBLISH_BIND_ADDR": "0.0.0.0",
    "PROM_SCRAPE_INTERVAL": "15s",
    "PROM_RETENTION": "7d",
    "VM_SHM_SIZE": "8g",
    "ROCJITSU_CONFIG": "gfx1250_mi455x.json",
    "VM_IMAGES_DIR": "/var/lib/qemu-tool/images",
    "QEMU_TOOL_SRC": "../../..",
    "FLEET_NET_PREFIX": "172.31",
    "VM_USERNAME": "ubuntu",
    "VM_FILESYSTEM": "",
    "VM_VFIO_USER_ROOT_PORT": "false",
    # ---- LMCache -----------------------------------------------------------
    # Off by default so an env file that predates this feature regenerates
    # byte-identical output. qemu/env.scale-out turns it on.
    #
    # Two switches, not one, because they answer different questions.
    # LMCACHE_ENABLE decides whether the services and their scrape jobs are
    # EMITTED; `--profile lmcache` decides whether they RUN. The scrape jobs
    # are the reason the first one has to exist: prometheus.yml has no notion
    # of a profile, so jobs emitted for a profile nobody selected would just
    # be permanently-down targets.
    "LMCACHE_ENABLE": "false",
    "LMCACHE_IMAGE": "qemu-tool/lmcache-rocm:0.5.5-gfx1250",
    # The MP coordinator's HTTP port (`lmcache coordinator`, default 9300).
    # This is the MP-mode coordinator -- membership, the key directory, L2
    # quota and eviction -- and NOT `lmcache_controller`, which is the legacy
    # in-process mode's controller and tracks a different thing entirely.
    "LMCACHE_COORD_PORT": "9300",
    # Seconds without a heartbeat before an instance is evicted, and the sweep
    # interval. Upstream's defaults; restated because a fleet boot burst can
    # outrun a 30 s timeout the same way the ernic mesh's 20 s heartbeat does.
    "LMCACHE_INSTANCE_TIMEOUT": "30",
    "LMCACHE_HEALTH_CHECK_INTERVAL": "10",
    # Must be identical on the coordinator and every server: the coordinator
    # resolves pin token_ids to keys with them, so a mismatch silently fails
    # to match rather than erroring.
    "LMCACHE_CHUNK_SIZE": "256",
    "LMCACHE_HASH_ALGORITHM": "blake3",
    # In-guest ports, reached through the qemu container's hostfwd rules.
    # LMCACHE_SERVER_HTTP_PORT is where /metrics lives -- NOT the server's
    # --prometheus-port, which is only used when the HTTP server is off.
    "LMCACHE_SERVER_HTTP_PORT": "9500",
    "LMCACHE_SERVER_ZMQ_PORT": "5555",
    # The P2P transfer-channel endpoint each guest advertises to its peers.
    "LMCACHE_P2P_PORT": "9400",
    # nixl or mooncake_te. nixl here, and not by preference: mooncake's wheel
    # is a CUDA build and fails on a ROCm image with "libcudart.so.12: cannot
    # open shared object file".
    "LMCACHE_P2P_TRANSFER_ENGINE": "nixl",
    "LMCACHE_L1_SIZE_GB": "2",
    "LMCACHE_EVICTION_POLICY": "LRU",
    "LMCACHE_STATS_PORT": "9841",
    # ---- Logs --------------------------------------------------------------
    # Loki plus an Alloy on the host; the guests get their own Alloy from the
    # lmcache_guest role. Rides the `metrics` profile: a log panel beside a
    # metric panel is the whole point, and a third switch for "observability
    # but only half of it" helps nobody.
    "LOGS_ENABLE": "true",
    "LOKI_IMAGE": "docker.io/grafana/loki:3.3.2",
    "LOKI_PORT": "3100",
    "LOKI_DATA_DIR": "/var/lib/qemu-tool/loki-data",
    "ALLOY_IMAGE": "docker.io/grafana/alloy:v1.5.1",
    # ---- Grafana -----------------------------------------------------------
    "GRAFANA_ENABLE": "true",
    "GRAFANA_IMAGE": "docker.io/grafana/grafana:latest",
    "GRAFANA_PORT": "3000",
    "GRAFANA_ADMIN_PASSWORD": "admin",
    # Grafana's own sqlite. The dashboards are provisioned from files so they
    # survive without this, but annotations, starred dashboards and any
    # ad-hoc exploration do not. Absolute for the same reason as the TSDB.
    "GRAFANA_DATA_DIR": "/var/lib/qemu-tool/grafana-data",
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

# Fourth octet within the infra family. Appending only: these are baked into a
# generated prometheus.yml and into the ansible role's controller address, so
# renumbering one silently repoints a scrape or a worker's registration.
_INFRA_EXPORTER, _INFRA_PROM, _INFRA_LMCACHE, _INFRA_GRAFANA = 10, 11, 12, 13
_INFRA_LMCACHE_STATS, _INFRA_LOKI, _INFRA_ALLOY = 14, 15, 16

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


def _flag(values: dict[str, str], key: str) -> bool:
    return _get(values, key).strip().lower() in ("1", "true", "yes", "on")


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
        "exporter_addr": f"{net}.{_NET_INFRA}.{_INFRA_EXPORTER}",
        "prometheus_addr": f"{net}.{_NET_INFRA}.{_INFRA_PROM}",
        "lmcache_addr": f"{net}.{_NET_INFRA}.{_INFRA_LMCACHE}",
        "grafana_addr": f"{net}.{_NET_INFRA}.{_INFRA_GRAFANA}",
        "lmcache_stats_addr": f"{net}.{_NET_INFRA}.{_INFRA_LMCACHE_STATS}",
        "loki_addr": f"{net}.{_NET_INFRA}.{_INFRA_LOKI}",
        "alloy_addr": f"{net}.{_NET_INFRA}.{_INFRA_ALLOY}",
        "logs": _flag(values, "LOGS_ENABLE"),
        "loki_image": _get(values, "LOKI_IMAGE"),
        "loki_port": _get(values, "LOKI_PORT"),
        "loki_data_dir": _get(values, "LOKI_DATA_DIR"),
        "alloy_image": _get(values, "ALLOY_IMAGE"),
        "lmcache": _flag(values, "LMCACHE_ENABLE"),
        "lmcache_image": _get(values, "LMCACHE_IMAGE"),
        "lmcache_coord_port": _get(values, "LMCACHE_COORD_PORT"),
        "lmcache_instance_timeout": _get(values, "LMCACHE_INSTANCE_TIMEOUT"),
        "lmcache_health_interval": _get(values, "LMCACHE_HEALTH_CHECK_INTERVAL"),
        "lmcache_chunk_size": _get(values, "LMCACHE_CHUNK_SIZE"),
        "lmcache_hash_algorithm": _get(values, "LMCACHE_HASH_ALGORITHM"),
        "lmcache_http_port": _get(values, "LMCACHE_SERVER_HTTP_PORT"),
        "lmcache_zmq_port": _get(values, "LMCACHE_SERVER_ZMQ_PORT"),
        "lmcache_p2p_port": _get(values, "LMCACHE_P2P_PORT"),
        "lmcache_p2p_engine": _get(values, "LMCACHE_P2P_TRANSFER_ENGINE"),
        "lmcache_l1_size_gb": _get(values, "LMCACHE_L1_SIZE_GB"),
        "lmcache_eviction_policy": _get(values, "LMCACHE_EVICTION_POLICY"),
        "lmcache_stats_port": _get(values, "LMCACHE_STATS_PORT"),
        "grafana": _flag(values, "GRAFANA_ENABLE"),
        "grafana_image": _get(values, "GRAFANA_IMAGE"),
        "grafana_port": _get(values, "GRAFANA_PORT"),
        "grafana_password": _get(values, "GRAFANA_ADMIN_PASSWORD"),
        "subnet": subnet,
        "vm_username": _get(values, "VM_USERNAME"),
        "vm_filesystem": _get(values, "VM_FILESYSTEM"),
        "vfio_user_root_port": _flag(values, "VM_VFIO_USER_ROOT_PORT"),
        "tcp_port": _get(values, "ERNIC_TCP_PORT"),
        "stats_port": _get(values, "ERNIC_STATS_PORT"),
        "ernic_nofile": _get(values, "ERNIC_NOFILE"),
        "ernic_tap": _flag(values, "ERNIC_TAP"),
        "log_max_size": _get(values, "LOG_MAX_SIZE"),
        "log_max_file": _get(values, "LOG_MAX_FILE"),
        "prom_data_dir": _get(values, "PROM_DATA_DIR"),
        "bind_addr": _get(values, "PUBLISH_BIND_ADDR"),
        "grafana_data_dir": _get(values, "GRAFANA_DATA_DIR"),
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
    if spec["vm_filesystem"]:
        # Same path inside the container as outside: run-vm passes the path
        # straight to -virtfs, so a remapped mount point would name a
        # directory the guest cannot see.
        out.append(f'    - "{spec["vm_filesystem"]}:{spec["vm_filesystem"]}:ro"')
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
        if spec["vfio_user_root_port"]:
            # A boolean, so it is emitted here rather than through _VM_FLAGS,
            # which renders --flag "value" pairs.
            out.append("          --vfio-user-root-port \\")
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
    out.append(f'      - "{spec["bind_addr"]}:{spec["prom_port"]}:9090"')

    if spec["logs"]:
        out.append("")
        out.append("  # ---- logs (opt-in: --profile metrics) ----")
        out.append("  loki:")
        out.append(f'    image: "{spec["loki_image"]}"')
        out.append("    logging: *logging")
        out.append("    profiles: [metrics]")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {spec['loki_addr']}")
        out.append("    restart: on-failure")
        out.append("    volumes:")
        out.append("      - ./logging/loki-config.yml:/etc/loki/config.yml:ro")
        # Absolute, for the same reason as the TSDB: a relative path resolves
        # against the stack directory, which for an installed qemu-tool is
        # inside site-packages.
        out.append(f"      - {spec['loki_data_dir']}:/loki")
        out.append("    command:")
        out.append("      - -config.file=/etc/loki/config.yml")
        out.append("    ports:")
        out.append(f'      - "{spec["bind_addr"]}:{spec["loki_port"]}:3100"')
        out.append("    healthcheck:")
        out.append('      test: ["CMD-SHELL", "wget -q -O- '
                   'http://127.0.0.1:3100/ready | grep -q ready"]')
        out.append("      interval: 10s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 30s")
        out.append("")
        out.append("  # Ships every stack container's logs. The guests run")
        out.append("  # their own Alloy: a VM's journal is behind QEMU, not")
        out.append("  # on this Docker socket.")
        out.append("  alloy:")
        out.append(f'    image: "{spec["alloy_image"]}"')
        out.append("    logging: *logging")
        out.append("    profiles: [metrics]")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {spec['alloy_addr']}")
        out.append("    restart: on-failure")
        out.append("    depends_on:")
        out.append("      loki:")
        out.append("        condition: service_healthy")
        out.append("    environment:")
        out.append(f"      - LOKI_HOST={spec['loki_addr']}:3100")
        out.append("    volumes:")
        out.append("      - ./logging/alloy-host.alloy:/etc/alloy/config.alloy:ro")
        # Read-only: this collector only ever reads logs and container
        # metadata. A writable socket here would be root on the host.
        out.append("      - /var/run/docker.sock:/var/run/docker.sock:ro")
        out.append("    command:")
        out.append("      - run")
        out.append("      - /etc/alloy/config.alloy")
        out.append("      - --storage.path=/tmp/alloy")

    if spec["grafana"]:
        out.append("")
        out.append("  grafana:")
        out.append(f'    image: "{spec["grafana_image"]}"')
        out.append("    logging: *logging")
        out.append("    profiles: [metrics]")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {spec['grafana_addr']}")
        out.append("    restart: on-failure")
        out.append("    depends_on:")
        out.append("      - prometheus")
        out.append("    environment:")
        out.append(f"      - GF_SECURITY_ADMIN_PASSWORD={spec['grafana_password']}")
        # The fleet is a disposable lab behind a host port, and a login prompt
        # between the operator and the only dashboard is pure friction. It is
        # still a published port, so do not point this at anything shared.
        out.append("      - GF_AUTH_ANONYMOUS_ENABLED=true")
        out.append("      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin")
        out.append("      - GF_USERS_DEFAULT_THEME=dark")
        out.append("    volumes:")
        # Provisioned from files, not clicked in: a dashboard that exists only
        # in Grafana's own sqlite dies with the container, and the whole point
        # of the generated stack is that a fleet is reproducible from the env
        # file. :ro because Grafana must not edit what the generator owns.
        out.append("      - ./grafana/provisioning:/etc/grafana/provisioning:ro")
        out.append("      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro")
        # Grafana's sqlite, on the host. The dashboards are provisioned from
        # files and survive without it, but annotations and anything explored
        # ad hoc do not.
        out.append(f"      - {spec['grafana_data_dir']}:/var/lib/grafana")
        out.append("    ports:")
        out.append(f'      - "{spec["bind_addr"]}:{spec["grafana_port"]}:3000"')

    if spec["lmcache"]:
        out.append("")
        out.append("  # ---- LMCache (opt-in: --profile lmcache) ----")
        out.append("  # The MP coordinator, beside the vfio-user device")
        out.append("  # servers rather than inside a guest. The per-VM MP")
        out.append("  # servers are containers INSIDE the guests, started by a")
        out.append("  # systemd unit the lmcache_guest ansible role installs --")
        out.append("  # compose has no reach into a VM, so they cannot live")
        out.append("  # here.")
        out.append("  #")
        out.append("  # Membership only. The coordinator answers 'who are my")
        out.append("  # live peers?'; it never sees KV data and is not in the")
        out.append("  # lookup path. The guests read each other's KV directly")
        out.append("  # over one-sided RDMA, which is what the ernic NIC each")
        out.append("  # guest already has is for.")
        out.append("  lmcache-coordinator:")
        out.append(f'    image: "{spec["lmcache_image"]}"')
        out.append("    logging: *logging")
        out.append("    profiles: [lmcache]")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {spec['lmcache_addr']}")
        out.append("    restart: on-failure")
        # Reachable from the guests without publishing anything: QEMU's SLIRP
        # NATs guest egress out through the qemu container's own stack, and
        # that container is on this network. So a guest dials the
        # coordinator's fleet address directly. Published anyway, because the
        # REST API is the thing an operator actually queries, and 0.0.0.0 on
        # the coordinator side is what makes both paths work.
        out.append("    ports:")
        out.append(
            f'      - "{spec["bind_addr"]}:{spec["lmcache_coord_port"]}'
            f':{spec["lmcache_coord_port"]}"')
        out.append("    entrypoint: []")
        out.append("    command:")
        # `lmcache coordinator`, the MP coordinator. NOT lmcache_controller,
        # which is the legacy in-process mode's controller: it listens on a
        # ZMQ pull/reply pair for LMCacheWorker registrations that an MP
        # server never sends, so pairing the two leaves every guest
        # registered with nothing.
        out.append("      - lmcache")
        out.append("      - coordinator")
        out.append("      - --host")
        out.append('      - "0.0.0.0"')
        out.append("      - --port")
        out.append(f'      - "{spec["lmcache_coord_port"]}"')
        out.append("      - --instance-timeout")
        out.append(f'      - "{spec["lmcache_instance_timeout"]}"')
        out.append("      - --health-check-interval")
        out.append(f'      - "{spec["lmcache_health_interval"]}"')
        # Both must equal the servers'. The coordinator resolves pin
        # token_ids to keys with them, so a mismatch does not error -- it
        # silently stops matching.
        out.append("      - --chunk-size")
        out.append(f'      - "{spec["lmcache_chunk_size"]}"')
        out.append("      - --hash-algorithm")
        out.append(f'      - "{spec["lmcache_hash_algorithm"]}"')
        out.append("    healthcheck:")
        # /instances, not /metrics and not /. It is the endpoint whose answer
        # the fleet actually depends on, and /health does not exist on this
        # service (404).
        out.append(
            '      test: ["CMD", "python3", "-c", "import urllib.request; '
            f"urllib.request.urlopen('http://127.0.0.1:{spec['lmcache_coord_port']}"
            '/instances\').read()"]'
        )
        out.append("      interval: 5s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 20s")

        out.append("")
        out.append("  # The coordinator's directory, as Prometheus metrics.")
        out.append("  # Its own /metrics carries only the OpenTelemetry")
        out.append("  # event-bus series -- every directory, membership and")
        out.append("  # per-tier usage figure is HTTP-only, so a dashboard")
        out.append("  # cannot see which node holds what without this.")
        out.append("  lmcache-stats-exporter:")
        out.append("    <<: *qemu-common")
        out.append("    profiles: [lmcache]")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {spec['lmcache_stats_addr']}")
        out.append("    devices: []")
        # Same reasoning as the ernic exporter: it only reads an HTTP API and
        # binds an unprivileged port, so it has no business running as root.
        out.append('    user: "65534:65534"')
        out.append("    volumes:")
        out.append('      - "${QEMU_TOOL_SRC:-../../..}:/qemu-tool-src:ro"')
        out.append("    depends_on:")
        out.append("      lmcache-coordinator:")
        out.append("        condition: service_healthy")
        out.append("    entrypoint:")
        out.append("      - /bin/sh")
        out.append("      - -c")
        out.append("      - |")
        out.append("        cp -r /qemu-tool-src/qemu /tmp/qemu-tool-build")
        out.extend(_pipx_install("/tmp/bin"))
        out.append("        exec /tmp/bin/qemu-tool lmcache-stats \\")
        out.append(
            f"          --coordinator-url http://{spec['lmcache_addr']}"
            f":{spec['lmcache_coord_port']} \\")
        out.append(f"          --port {spec['lmcache_stats_port']}")

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
            # The sidecar picks the l2 interface by address, because Docker
            # does not guarantee which network lands on which interface and
            # it genuinely differs between containers in one `up`. Bridging
            # the mesh interface by mistake breaks the fleet in a way that
            # looks like a worker bug -- see tapsetup/setup.sh.
            out.append("    environment:")
            out.append(f"      - FLEET_PREFIX={spec['net_prefix']}")
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

    if spec["lmcache"]:
        out.append("")
        out.append("  # The LMCache MP server inside each guest. Same shape as")
        out.append("  # fleet-node above and for the same reason -- the target")
        out.append("  # is the qemu CONTAINER, and VM_EXTRA_HOSTFWD forwards")
        out.append("  # the port through to the server. Add")
        out.append(f"  #   tcp::{spec['lmcache_http_port']}"
                   f"-:{spec['lmcache_http_port']}")
        out.append("  # to VM_EXTRA_HOSTFWD or every target here stays down.")
        out.append("  #")
        out.append("  # This is the server's --http-port, which is where")
        out.append("  # /metrics actually lives. Its --prometheus-port is a")
        out.append("  # decoy here: it only binds when the HTTP server is off,")
        out.append("  # and `lmcache server` always runs it.")
        out.append("  - job_name: lmcache-server")
        out.append("    static_configs:")
        for vm in spec["vms"]:
            out.append(
                f"      - targets: [\"{vm['qemu_addr']}:"
                f"{spec['lmcache_http_port']}\"]"
            )
            out.append("        labels:")
            out.append(f'          vm: "{vm["n"]}"')
            out.append(f'          vm_name: "{vm["name"]}"')
            # The id the controller knows this worker by, so a Grafana panel
            # can join a scraped series to a controller query without a
            # lookup table.
            out.append(f'          lmcache_instance_id: "{vm["name"]}"')
        out.append("")
        out.append("  # The coordinator's directory, via qemu-tool")
        out.append("  # lmcache-stats. Separate from the job below because")
        out.append("  # the coordinator's own /metrics does not carry it.")
        out.append("  - job_name: lmcache-directory")
        out.append("    static_configs:")
        out.append(
            f"      - targets: [\"{spec['lmcache_stats_addr']}:"
            f"{spec['lmcache_stats_port']}\"]")
        out.append("")
        out.append("  - job_name: lmcache-coordinator")
        out.append("    static_configs:")
        out.append(
            f"      - targets: [\"{spec['lmcache_addr']}:"
            f"{spec['lmcache_coord_port']}\"]"
        )
    return "\n".join(out) + "\n"


def render_inventory(spec: dict[str, Any], env_name: str, digest: str) -> str:
    """The fleet's ansible inventory, derived from the same VM_COUNT.

    Generated rather than hand-kept because the failure mode of a stale one is
    quiet: ansible-playbook against an inventory listing six of eight guests
    configures six and reports success. The SSH ports and guest IPs here are
    the same values the compose file publishes, from the same `plan()`.
    """
    out: list[str] = ["---"]
    out.append("# Generated by `qemu-tool gen-compose`. Do not edit.")
    out.append(f"# env-file: {env_name}")
    out.append(f"{_PROVENANCE}{digest}")
    out.append("")
    out.append("all:")
    out.append("  vars:")
    out.append("    ansible_host: 127.0.0.1")
    # From VM_USERNAME, which is a VMConfig field (config.py) and so already
    # understood by gen-vm and run-vm. The published guest images carry their
    # own user -- the ernic-rocjitsu qcow2 ships vm-info.json naming
    # "batesste" -- so the ubuntu default is right only for a guest this repo
    # built itself.
    out.append(f"    ansible_user: {spec['vm_username']}")
    out.append("    ansible_ssh_common_args: >-")
    out.append("      -o StrictHostKeyChecking=no")
    out.append("      -o UserKnownHostsFile=/dev/null")
    out.append("")
    out.append("  children:")
    out.append("    fleet:")
    out.append("      vars:")
    # The coordinator by its fleet address, not 10.0.2.2 and not a service
    # name. SLIRP NATs guest egress through the qemu container, which is on
    # the fleet network, so this address resolves from inside a guest without
    # anything being published -- and unlike 10.0.2.2 it names the
    # coordinator rather than whatever else the container might be serving.
    out.append(
        f"        lmcache_guest_coordinator_url: "
        f"http://{spec['lmcache_addr']}:{spec['lmcache_coord_port']}"
    )
    out.append(
        f"        lmcache_guest_loki_url: http://{spec['loki_addr']}"
        f":{spec['loki_port']}")
    out.append(f"        lmcache_guest_http_port: {spec['lmcache_http_port']}")
    out.append(f"        lmcache_guest_zmq_port: {spec['lmcache_zmq_port']}")
    out.append(f"        lmcache_guest_p2p_port: {spec['lmcache_p2p_port']}")
    out.append(
        f'        lmcache_guest_p2p_transfer_engine: "{spec["lmcache_p2p_engine"]}"'
    )
    out.append(f"        lmcache_guest_chunk_size: {spec['lmcache_chunk_size']}")
    out.append(
        f'        lmcache_guest_hash_algorithm: "{spec["lmcache_hash_algorithm"]}"'
    )
    out.append(f"        lmcache_guest_l1_size_gb: {spec['lmcache_l1_size_gb']}")
    out.append(
        f'        lmcache_guest_eviction_policy: "{spec["lmcache_eviction_policy"]}"'
    )
    out.append(f'        lmcache_guest_image: "{spec["lmcache_image"]}"')
    # /24 is implied by ERNIC_GUEST_SUBNET being three octets; the generator
    # has no key for a prefix because the ernic L2 segment is flat by
    # construction.
    out.append("        fleet_guest_prefix: 24")
    out.append("      hosts:")
    for vm in spec["vms"]:
        # Keyed by VM name, which is also lmcache_instance_id in
        # fleet-lmcache.yml and the lmcache_instance_id label in
        # prometheus.yml -- one identifier across all three.
        out.append(f"        {vm['name']}:")
        out.append(f"          ansible_port: {vm['ssh_port']}")
        out.append(f"          fleet_guest_ip: {vm['ip']}")
        # The guest finds its ernic by MAC, not by device name: the udev
        # rename comes from ernic_guest_setup and a guest without it keeps
        # the kernel name, so a name here is sometimes simply wrong.
        out.append(f"          lmcache_guest_ernic_mac: {vm['mac']}")
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
        # Alongside the other two rather than under ansible/inventory/, so all
        # three carry one env-sha256 and `--check` stays a single comparison
        # against one directory. Point ansible-playbook at it with -i.
        "fleet-inventory.yml": render_inventory(spec, env_name, digest),
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
