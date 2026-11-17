"""Generate a compose stack from fleet XML.

Every socket path, MAC, SSH port and qcow2 name is derived from <vm-count>
and a handful of bases, so one file describes a fleet of any size.

A text-valued per-VM setting (<vm-shm-size>, <rocjitsu-config>,
<vm-filesystem>) may be a comma-list. A bare value broadcasts to every VM; a
list of length <vm-count> applies positionally; any other length is an error
naming the element and both lengths.

The exception is the one that silently does the wrong thing if you get it
backwards: in <vm-extra-hostfwd> the commas mean "several values for ONE VM".
a,b there is two hostfwd rules for every guest, not one rule each for two
guests.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import itertools
import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .compose import _DEFAULT_STACK, _STACKS
from .fleet_xml import FleetConfig, FleetXml
from .resources import stack_fleet
from .vm_artifact import VM_COMMIT_TAG, VM_DATED_TAG

# An element present but empty means "the default", not "the empty string".
_FIELD_DEFAULTS = {f.name: f.default for f in dataclasses.fields(FleetConfig)}

# Absolute, and deliberately NOT "./prom-data". A relative path resolves
# against the STACK directory, and the stack directory is wherever qemu-tool
# found it -- for a pipx or .deb install that is inside site-packages or
# /usr/share, so the TSDB a fleet run produced lands somewhere nobody thinks
# to look and a reinstall deletes. Measured: a pipx-installed run put it under
# ~/.local/share/pipx/venvs/qemu-tool/.../share/compose/<stack>/prom-data,
# root-owned, and Prometheus then crash-looped on "mkdir data/: permission
# denied".
_PROM_DATA_DIR = "/var/lib/qemu-tool/prom-data"
# Grafana's own sqlite. The dashboards are provisioned from files so they
# survive without this, but annotations, starred dashboards and any ad-hoc
# exploration do not. Absolute for the same reason as the TSDB.
_GRAFANA_DATA_DIR = "/var/lib/qemu-tool/grafana-data"

# Third octet per service family. Static addressing keeps Docker's embedded
# DNS off the critical path: measured at 48 VMs, a fleet that resolves service
# names loses workers to "Temporary failure in name resolution" during the
# start burst, and each restart consumes a fresh mesh node id -- so a 48-VM
# fleet reached id 61 against a 64-node protocol ceiling. See the stack README.
_NET_ERNIC, _NET_ROCJITSU, _NET_QEMU, _NET_INFRA, _NET_SDXI = 1, 2, 3, 0, 4

# rocm-ernic's topology payload builder caps the mesh here
# (src/rdma/rdma_backend_tcp.c: `num_nodes < 64`).
_MESH_MAX = 64
# What the mesh has actually been measured to register. The protocol ceiling
# above is not reachable: the mesh is a full mesh, so registering node N+1
# costs N new connections through a single-threaded manager against a fixed
# timeout, and it stops completing around here. Asking for 64 settled at 40
# with max node id 30 (AGENTS.md issue 17).
_MESH_PRACTICAL = 40
_STAMP_RE = re.compile(r"\d{8}\.g[0-9a-f]+")
_TAP_IF = "tap0"
_TAPSETUP_IMAGE = "qemu-tool/ernic-tapsetup:1"

# Fourth octet within the infra family. Appending only: these are baked into a
# generated prometheus.yml and into the ansible role's controller address, so
# renumbering one silently repoints a scrape or a worker's registration.
# 12 and 14 were the LMCache coordinator and its exporter; left unused
# rather than renumbering the ones after them.
_INFRA_EXPORTER, _INFRA_PROM, _INFRA_GRAFANA = 10, 11, 13
_INFRA_LOKI, _INFRA_ALLOY = 15, 16

# CONTAINER_USER reaches the YAML as a compose variable reference, never as
# the value the generator read. The generated stack is committed and
# `gen-compose --check` compares it byte for byte in CI, so a uid baked into
# the render would fail that check on every machine but the one that ran the
# generator. The uid is resolved by compose at `up`.
#
# This reference is now emitted UNCONDITIONALLY. It used to be gated on the
# key being set, which made root the default for anyone who had not read far
# enough down the settings file -- and since the value cannot be committed
# (see above), "set it in an overlay" was the only on-ramp and almost nobody
# took it. Being a reference rather than a value is exactly what lets it be
# unconditional: the render stays machine-independent either way.
#
# `qemu-tool compose` fills it in from the caller's own uid:gid when the
# environment has not (see compose._caller_identity), so the supported path
# needs no configuration at all. Plain `docker compose` against a committed
# stack hits the :? below and is told what to set.
#
# The :? form, not :- , because the silent-failure spelling is the one people
# reach for. Measured on compose v5.3.1: an unset variable interpolates to the
# empty string, `user: ":"` is not an error to Docker, and the container runs
# as root -- a fleet that looks configured and is not, with one warning line
# as the only evidence.
_USER_VAR = "${CONTAINER_USER:?set CONTAINER_USER=<uid>:<gid>}"
_USER_REF = f'"{_USER_VAR}"'
# The host's kvm group. Only the qemu service needs it, and only to open
# /dev/kvm: a container given --user <uid>:<gid> holds no supplementary
# groups, so without this `accel=kvm` fails on a device node that is
# crw-rw---- root:kvm. The gid differs per host (136 here, 108 on other
# distributions), so like the uid it is resolved at `up`, not baked.
_KVM_GID_REF = '"${CONTAINER_KVM_GID:?set CONTAINER_KVM_GID to the host kvm gid}"'

# VM_IMAGES_DIR reaches the YAML as a compose variable reference for the same
# reason CONTAINER_USER does: a fleet file may spell it ${HOME}/..., which
# expands to a host-specific path. Baking that path into
# the render would fail `gen-compose --check` in CI (runner home != dev home).
# The :- default matches the fleet XML default so plain `docker compose up`
# against the committed stack uses the conventional path; qemu-tool compose
# supplies the real value from the fleet XML via the caller environment.
_VM_IMAGES_VAR = "VM_IMAGES_DIR"
_VM_IMAGES_DEFAULT = "/var/lib/qemu-tool/images"
_VM_IMAGES_REF = f"${{{_VM_IMAGES_VAR}:-{_VM_IMAGES_DEFAULT}}}"



def _hardening(indent: str = "    ") -> list[str]:
    """cap_drop + no-new-privileges, which every service in this stack takes.

    Nothing here is a daemon that needs a capability. The device servers open
    a unix socket and (with <ernic-tap>) an already-created tap; QEMU opens
    /dev/kvm and binds 2222; Prometheus, Loki, Grafana and the exporters bind
    a port above 1024 and write one directory. Docker nonetheless starts every
    container with CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID, NET_RAW,
    MKNOD and seven more in the permitted set, which is a default rather than
    a decision.

    Dropping ALL matters even with CONTAINER_USER set, because a uid is not a
    capability set: a container can be non-root and still hold NET_RAW. And it
    matters more when CONTAINER_USER has been pointed at 0:0 deliberately,
    which is the configuration where uid 0 keeps the capabilities and
    no-new-privileges keeps a setuid binary in the image from restoring
    anything that was dropped.

    The one exception is tapsetup-N, which adds NET_ADMIN back on top of this
    drop -- a far smaller set than the default it used to run with.
    """
    return [
        f"{indent}cap_drop:",
        f"{indent}  - ALL",
        f"{indent}security_opt:",
        f'{indent}  - "no-new-privileges:true"',
    ]

_PROVENANCE = "# xml-sha256: "

# Lines of header to scan for the digest. Generous, because a file whose
# digest is just past the window reads as "not generated" -- which silences
# the staleness warning instead of raising it.
_PROVENANCE_SCAN_LINES = 40


def xml_digest(paths: Path | Sequence[Path]) -> str:
    """One digest over every fleet XML layer, concatenated in order.

    Not a digest of the last layer or a set of per-file digests: an overlay is
    as much an input to the render as the golden file under it, so a change in
    ANY layer -- or a reordering of them -- has to invalidate the result. A
    single layer hashes to the bytes of that file.
    """
    layers = [paths] if isinstance(paths, Path) else list(paths)
    digest = hashlib.sha256()
    for layer in layers:
        digest.update(layer.read_bytes())
    return digest.hexdigest()


def recorded_digest(compose_file: Path) -> str | None:
    """Return the fleet XML hash a generated compose file was built from."""
    try:
        for line in compose_file.read_text().splitlines()[:_PROVENANCE_SCAN_LINES]:
            if line.startswith(_PROVENANCE):
                return line[len(_PROVENANCE):].split("#")[0].strip()
    except OSError:
        return None
    return None


def stack_for(xml: Path | Sequence[Path] | None) -> str:
    """The shipped stack a render belongs to when --stack does not say.

    A stack directory is named after the fleet file it is generated from, so
    the first layer names it: qemu/stacks/helios-tray.xml is the helios-tray
    stack. Anything else -- a fleet of your own, or no --xml at all -- is the
    default stack.
    """
    layers = [xml] if isinstance(xml, Path) else list(xml or [])
    if layers and layers[0].stem in _STACKS:
        return layers[0].stem
    return _DEFAULT_STACK


def _xml_args(layer_names: Sequence[str], stack: str = _DEFAULT_STACK) -> str:
    """The --xml (and optional --stack) arguments that reproduce this render."""
    parts = [f"--xml {name}" for name in layer_names]
    if stack != stack_for([Path(name) for name in layer_names]):
        parts.append(f"--stack {stack}")
    return " ".join(parts)


def _provenance_lines(digest: str) -> list[str]:
    """The digest over all fleet XML layers."""
    return [f"{_PROVENANCE}{digest}  # pre-commit: ignore"]


def _setting(cfg: FleetConfig, field: str) -> str:
    return getattr(cfg, field) or _FIELD_DEFAULTS[field]


def _split_per_vm(raw: str, count: int, element: str) -> list[str]:
    """Broadcast a scalar, or apply a length-<vm-count> list positionally."""
    items = [p.strip() for p in raw.split(",")]
    if len(items) == 1:
        return items * count
    if len(items) != count:
        sys.exit(
            f"Error: {element} has {len(items)} values but <vm-count> is "
            f"{count}. Give one value to apply to every VM, or exactly {count}."
        )
    return items


def _per_vm_flags(cfg: FleetConfig, count: int) -> list[list[tuple[str, str]]]:
    """Resolve the run-vm flags for each VM as (flag, value) pairs."""
    per_vm: list[list[tuple[str, str]]] = [[] for _ in range(count)]
    # <vm-nvme> is a count: run-vm creates the backing qcow2 per device itself
    # (_nvme_create), so unlike the root disk there is no gen-vm step to pair
    # with it. A negative count asks for null_blk.
    for flag, value in (
        ("--vcpus", cfg.vm_vcpus), ("--vmem", cfg.vm_vmem), ("--nvme", cfg.vm_nvme),
    ):
        if value:
            for vm in per_vm:
                vm.append((flag, str(value)))
    # A host directory shared into every guest over 9p (mount_tag=hostfs).
    # This is how a fleet gets a large artifact without copying it per VM: one
    # file on the host, read by all N guests. At 8 VMs that is 22 GB of
    # transfer saved, at 40 it is 112 GB. The directory must be readable by
    # the qemu container, so it is bind-mounted in below.
    if cfg.vm_filesystem:
        shares = _split_per_vm(cfg.vm_filesystem, count, "<vm-filesystem>")
        for vm, share in zip(per_vm, shares):
            vm.append(("--filesystem", share))
    # The commas are already spoken for: every rule goes to every VM.
    for rule in cfg.vm_extra_hostfwd.split(","):
        if rule.strip():
            for vm in per_vm:
                vm.append(("--extra-hostfwd", rule.strip()))
    return per_vm


def _warn_if_gpu_without_root_port(cfg: FleetConfig) -> None:
    """A GPU with no root port cannot bind amdgpu, and says so three layers up.

    Every stack this renders attaches a rocjitsu GPU per guest, so the
    combination is always wrong rather than merely unusual. Without the root
    port the device is a conventional endpoint with a 32-bit prefetchable
    BAR -- 16M measured on the emulated GPU, against 256M behind a port --
    which is too small for a VRAM aperture, so amdgpu's probe fails with -22
    and no KFD node is created. The module still loads, so nothing local
    looks broken, and the first complaint arrives from HIP as "no ROCm-capable
    device is detected". That is far enough from the cause to have produced a
    wrong diagnosis once, which is what this warning is for.

    A warning and not an error: the setting exists so a stack can deliberately
    keep today's enumeration while something else is being changed, and an
    ernic-only guest loses only its link-speed reporting.
    """
    if cfg.vm_vfio_user_root_port:
        return
    print(
        "Warning: <vm-vfio-user-root-port> is false and this stack gives every "
        "guest a rocjitsu GPU.\n"
        "  Without a root port the GPU gets a 16M 32-bit BAR, amdgpu's probe "
        "fails with -22, and\n"
        "  the guest reports 'no ROCm-capable device is detected' from HIP "
        "rather than naming the BAR.",
        file=sys.stderr,
    )


# A tag somebody meant as publish-qcow's dated one: digits, ".g", hex. The
# exact shape is VM_DATED_TAG; this is every near miss of it.
_NEAR_DATED_TAG = re.compile(r"[0-9]+\.g[0-9a-f]+", re.IGNORECASE)


def _image_repository(ref: str) -> str:
    """`ref` without its tag or digest, and without Docker Hub's default host."""
    head, sep, name = ref.rpartition("/")
    name = name.split("@", 1)[0].split(":", 1)[0]
    repository = f"{head}{sep}{name}"
    for host in ("docker.io/", "index.docker.io/"):
        if repository.startswith(host):
            return repository[len(host):]
    return repository


def _backing_ref_kind(ref: str, publish_ref: str = "") -> str:
    """How <backing-ref> pins the guest image it names.

    "none"       empty: nothing is pulled.
    "digest"     name@sha256:..., which cannot move.
    "rolling"    no tag, "latest" or qm.<sha7>: a name publish-qcow moves.
    "published"  exactly <YYYYMMDD>.g<sha7>, publish-qcow's dated tag.
    "malformed"  a tag that can only be a mistake for one of those: a near
                 miss of the dated tag (a digit short, a hex digit over,
                 upper case), "latest" in another case, qm. followed by
                 anything but seven hex digits -- and, when `publish_ref` is
                 known and names this repository, every tag that is not the
                 dated one. publish-qcow pushes nothing else there, so a
                 tag of any other shape in it names no guest a fleet pins.
    "other"      anything else, a batesste-ci-images qcow2 tag
                 (<stamp>-vm....-qcow2) and a local build among them.

    Without `publish_ref` a free-form tag ("stable") in the publish
    repository cannot be told from a local build under test and is "other".
    Only rocm-golden.xml sets <publish-ref>.
    """
    if not ref:
        return "none"
    # The last path component: a registry may carry a port.
    name = ref.rsplit("/", 1)[-1]
    if "@" in name:
        return "digest"
    if ":" not in name:
        return "rolling"
    tag = name.rsplit(":", 1)[1]
    if tag == "latest" or VM_COMMIT_TAG.fullmatch(tag):
        return "rolling"
    if VM_DATED_TAG.fullmatch(tag):
        return "published"
    low = tag.lower()
    if low == "latest" or low.startswith("qm.") or _NEAR_DATED_TAG.fullmatch(tag):
        return "malformed"
    if publish_ref and _image_repository(ref) == _image_repository(publish_ref):
        return "malformed"
    return "other"


def _check_image_coherence(cfg: FleetConfig) -> None:
    """Refuse an image set that cannot have been built together.

    batesste-ci-images tags carry a shared <date>.<ci-sha> build stamp, so
    for the three device-side images this is a string comparison. It is worth
    doing because the failure it prevents does not present as a version
    problem: QEMU, the ernic and rocjitsu speak vfio-user to each other, and
    a pair from different builds wedges at the vfio-user/DSR boundary,
    surfacing as the -ETIMEDOUT and "Failed to register ibdev" symptoms in
    AGENTS.md. Across a whole fleet that is an expensive afternoon.

    <backing-ref> is held to a different rule, by what its tag says it is
    (_backing_ref_kind):

    A published guest, tagged exactly <YYYYMMDD>.g<sha7>. qemu-minimal bakes
    and publishes the guest itself, and that stamp is the qemu-minimal commit
    it was baked from, on the day it was pushed. It is not a
    batesste-ci-images build stamp and never equals one, so it is not
    compared with the servers'. What ties that guest to the servers is the
    pins in vm-rocm-golden.yml AT that commit, which an installed package
    cannot read; tests/test_playbook_pins.py reads them out of git and
    compares them with the server images the fleet files name. What this
    function can and does hold is that the ref is pinned at all:

    A rolling ref -- no tag, "latest", or qm.<sha7> -- is refused. gen-vm
    re-pulls a backing image only when the text of the ref changes, so a tag
    that moves leaves every host on whichever image it happened to pull
    first, and the pin test above has no commit to read. A digest is
    accepted: it cannot move.

    A malformed tag is refused too, where the old code warned that the
    image had "no build stamp" and rendered: a near miss of the dated tag,
    "latest" in another case, qm. with anything but seven hex digits, and --
    in the repository <publish-ref> names, when a layer sets it -- any tag
    but the dated one. Each can only be a mistake for a published guest.

    Any other tag keeps the old rule. A batesste-ci-images qcow2
    (<stamp>-vm....-qcow2) was built in the same run as the containers, so a
    local fleet still pinning one must share their stamp, and a tag with no
    stamp is warned about and left out, like a server image with none.

    <image-allow-guest-lag> excuses <backing-ref> from the stamp
    comparison, and nothing else: not from the rolling-tag refusal or the
    malformed-tag one, and not
    the three server images, which genuinely must match. For a published
    guest there is no stamp comparison to excuse; there it is the waiver
    tests/test_playbook_pins.py asks for while the fleet's servers have
    moved to pins no published guest was baked at yet. Either way the render
    warns, every time, that the guest may be older than its servers.

    <sdxi-image> is not in the stamp comparison. Its pair is the guest's
    driver, by commit: the sdxi.<sha> in its tag against sdxi_source_pin in
    vm-rocm-golden.yml. An installed package cannot see that playbook, so
    tests/test_playbook_pins.py holds that check and this function has none.
    """
    refs = {
        "<qemu-ref>": cfg.qemu_ref,
        "<ernic-ref>": cfg.ernic_ref,
        "<rocjitsu-ref>": cfg.rocjitsu_ref,
    }
    # An empty ref renders `image: ""`, which only compose refuses, later.
    for key in ("<qemu-ref>", "<ernic-ref>", "<rocjitsu-ref>"):
        if not refs[key]:
            sys.exit(f"Error: {key} is empty; every stack runs that image.")
    if cfg.sdxi_per_vm and not cfg.sdxi_ref:
        sys.exit(
            "Error: <sdxi-per-vm> is non-zero but <sdxi-image> is empty; "
            "name the SDXI server image to run."
        )
    backing = _backing_ref_kind(cfg.backing_ref, cfg.publish_ref)
    if backing == "malformed":
        sys.exit(
            "Error: <backing-ref> does not name a published guest: "
            f"{cfg.backing_ref}\n"
            "  publish-qcow pushes a guest under its dated tag, "
            "<YYYYMMDD>.g<sha7> -- eight digits,\n"
            "  \".g\", seven lower-case hex digits and nothing after them "
            "-- and that is the tag a fleet\n"
            "  names. This one is not that shape"
            + (", in the repository <publish-ref> names" if cfg.publish_ref
               and _image_repository(cfg.backing_ref)
               == _image_repository(cfg.publish_ref) else "")
            + ". Name the dated tag,\n"
            "  or a digest."
        )
    if backing == "rolling":
        sys.exit(
            f"Error: <backing-ref> names a tag that moves: {cfg.backing_ref}\n"
            "  A fleet pins the guest image it pulls. gen-vm pulls again "
            "only when the ref changes,\n"
            "  so a moved tag leaves each host on whichever image it pulled "
            "first, and nothing can\n"
            "  say which commit that guest was baked from. Name the dated "
            "tag, <YYYYMMDD>.g<sha7>,\n"
            "  or a digest."
        )
    if backing == "other":
        refs["<backing-ref>"] = cfg.backing_ref
    if cfg.image_allow_guest_lag:
        refs.pop("<backing-ref>", None)
        print(
            "Warning: <image-allow-guest-lag> is set -- the guest image "
            "<backing-ref> names is not held\n"
            "  to the device servers' build. The guest image may be older "
            "than the device servers.\n"
            "  Verify by hand that it is old in ways you do not care about.",
            file=sys.stderr,
        )
    stamps: dict[str, str] = {}
    vfu: dict[str, str] = {}
    unstamped: list[str] = []
    for key, ref in refs.items():
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
            "\nAll of them must come from the same batesste-ci-images build."
            + ("\nA guest published by publish-qcow is named by its dated "
               "tag alone, <YYYYMMDD>.g<sha7>,\nwith nothing after it, and "
               "is not compared with the servers."
               if stamps.get("<backing-ref>") not in
               {v for k, v in stamps.items() if k != "<backing-ref>"}
               | {None} else "")
        )

    distinct_vfu = set(vfu.values())
    if len(distinct_vfu) > 1:
        detail = "\n".join(f"  {k} -> {v}" for k, v in sorted(vfu.items()))
        sys.exit(
            "Error: libvfio-user revisions differ between qemu and the device "
            "servers:\n" + detail +
            "\nThe vfio-user wire protocol is not compatible across revisions."
        )


def plan(cfg: FleetConfig) -> dict[str, Any]:
    """Resolve the fleet settings into everything the templates need."""
    count = cfg.count
    if count < 1:
        sys.exit(f"Error: <vm-count> must be at least 1, got {count}")

    ernic_per_vm = _positive(cfg.ernic_per_vm, "<ernic-per-vm>")
    rocjitsu_per_vm = _positive(cfg.rocjitsu_per_vm, "<rocjitsu-per-vm>")
    sdxi_per_vm = cfg.sdxi_per_vm

    # The mesh counts rocm-ernic PROCESSES, not VMs. At <ernic-per-vm> > 1 the
    # two differ by that factor, and a message naming only the VM count would
    # send someone looking at the wrong number.
    nic_count = count * ernic_per_vm
    if nic_count > _MESH_MAX:
        detail = (
            f"{count} VMs x {ernic_per_vm} ernics"
            if ernic_per_vm > 1 else f"{count} VMs"
        )
        sys.exit(
            f"Error: {detail} is {nic_count} mesh nodes, above the "
            f"{_MESH_MAX}-node ceiling in rocm-ernic's mesh topology payload "
            "(rdma_backend_tcp.c)."
        )
    if nic_count > _MESH_PRACTICAL:
        print(
            f"Warning: {nic_count} mesh nodes is above the ~{_MESH_PRACTICAL} "
            "the mesh has been measured to register (AGENTS.md issue 17). "
            "Workers past it die on 'TCP: Registration timeout' in a restart "
            "loop and take their guests with them.",
            file=sys.stderr,
        )

    _check_image_coherence(cfg)
    _warn_if_gpu_without_root_port(cfg)

    prefix = _setting(cfg, "name_prefix")
    names = [f"{prefix}-{n}" for n in range(1, count + 1)]
    ports = [str(cfg.ssh_port_base + n - 1) for n in range(1, count + 1)]

    # Keyed on the GLOBAL NIC ordinal, not the VM index. At one ernic per VM
    # the two are the same number, which is what keeps the existing stack
    # byte-identical; above that, every NIC in the fleet still gets its own
    # MAC, IP and GID.
    mac_prefix = _setting(cfg, "ernic_mac_prefix")
    macs = [f"{mac_prefix}:{o >> 8:02x}:{o & 0xff:02x}"
            for o in range(1, nic_count + 1)]

    subnet = _setting(cfg, "ernic_subnet")
    # Guests start at .11. Avoid .1: the mesh manager claims it as its DHCP
    # server_ip and intercepts ARP for it (AGENTS.md known issue 1).
    #
    # No /24 exhaustion check: the mesh ceiling above caps nic_count at 64,
    # so the highest address this can derive is .74. Anything that raises
    # _MESH_MAX past ~240 needs one.
    ips = [f"{subnet}.{10 + o}" for o in range(1, nic_count + 1)]

    shm_sizes = _split_per_vm(_setting(cfg, "shm_size"), count, "<vm-shm-size>")
    rocjitsu_configs = _split_per_vm(
        _setting(cfg, "rocjitsu_config"), count, "<rocjitsu-config>")

    flags = _per_vm_flags(cfg, count)

    net = _setting(cfg, "fleet_prefix")

    # Compute switch grouping ahead of the per-VM loop. When ernic_per_vm >
    # rocjitsu_per_vm (e.g. Helios: 2 RNICs per GPU) the vfio_socks list must
    # place nics_per_switch NICs before each GPU so _vfio_user_switch_args
    # slices the correct devices into each switch slot.
    _switch_width = cfg.vm_pcie_switch_width
    _nics_per_switch = (
        ernic_per_vm // rocjitsu_per_vm
        if _switch_width > 0 and rocjitsu_per_vm > 0
        else 1
    )

    vms = []
    nics: list[dict[str, Any]] = []
    gpus: list[dict[str, Any]] = []
    sdxis: list[dict[str, Any]] = []
    for index in range(count):
        n = index + 1
        vm_nics = []
        for d in range(1, ernic_per_vm + 1):
            o = index * ernic_per_vm + d
            suffix = _dev_suffix(n, d, ernic_per_vm)
            vm_nics.append({
                "n": n, "d": d, "o": o,
                "service": f"ernic-{suffix}",
                "addr": f"{net}.{_NET_ERNIC}.{o}",
                "mac": macs[o - 1],
                "ip": ips[o - 1],
                "guest_gids": f"{_link_local_gid(macs[o - 1])},{ips[o - 1]}",
                "sock": f"/run/vfu/ernic-{suffix}.sock",
                "stats_file": f"/run/ernic-stats/ernic-{suffix}.stats",
            })
        vm_gpus = []
        for d in range(1, rocjitsu_per_vm + 1):
            o = index * rocjitsu_per_vm + d
            suffix = _dev_suffix(n, d, rocjitsu_per_vm)
            vm_gpus.append({
                "n": n, "d": d, "o": o,
                "service": f"rocjitsu-{suffix}",
                "addr": f"{net}.{_NET_ROCJITSU}.{o}",
                "sock": f"/run/vfu/rocjitsu-{suffix}.sock",
                "config": rocjitsu_configs[index],
            })
        vm_sdxi = []
        for d in range(1, sdxi_per_vm + 1):
            o = index * sdxi_per_vm + d
            suffix = _dev_suffix(n, d, sdxi_per_vm)
            vm_sdxi.append({
                "n": n, "d": d, "o": o,
                "service": f"sdxi-{suffix}",
                "addr": f"{net}.{_NET_SDXI}.{o}",
                "sock": f"/run/vfu/sdxi-{suffix}.sock",
            })
        nics += vm_nics
        gpus += vm_gpus
        sdxis += vm_sdxi
        vms.append({
            "n": n,
            "qemu_addr": f"{net}.{_NET_QEMU}.{n}",
            "name": names[index],
            "ssh_port": ports[index],
            "nics": vm_nics,
            "gpus": vm_gpus,
            "sdxi": vm_sdxi,
            # The order the guest sees, and therefore what ends up paired under
            # each switch. When <ernic-per-vm> == <rocjitsu-per-vm> (e.g. MI355X
            # with switch_width=2) this is 1:1 round-robin. When there are
            # more ernics than GPUs (e.g. Helios: 2 RNICs per GPU,
            # switch_width=3) the groups are nics_per_switch NICs then one GPU.
            "vfio_socks": _group_socks_for_switches(
                [x["sock"] for x in vm_nics],
                [x["sock"] for x in vm_gpus],
                _nics_per_switch,
            ),
            "sdxi_socks": [x["sock"] for x in vm_sdxi],
            # Aliases to the first device of each kind. Everything that
            # predates multi-device VMs reads these, and at the default of one
            # each they are the only device there is.
            "ernic_addr": vm_nics[0]["addr"],
            "rocjitsu_addr": vm_gpus[0]["addr"] if vm_gpus else "",
            "mac": vm_nics[0]["mac"],
            "ip": vm_nics[0]["ip"],
            "guest_gids": vm_nics[0]["guest_gids"],
            "ernic_sock": vm_nics[0]["sock"],
            "rocjitsu_sock": vm_gpus[0]["sock"] if vm_gpus else "",
            "stats_file": vm_nics[0]["stats_file"],
            "shm_size": shm_sizes[index],
            "rocjitsu_config": rocjitsu_configs[index],
            "flags": flags[index],
        })

    return {
        "count": count,
        "vms": vms,
        "nics": nics,
        "gpus": gpus,
        "sdxis": sdxis,
        "ernic_per_vm": ernic_per_vm,
        "rocjitsu_per_vm": rocjitsu_per_vm,
        "sdxi_per_vm": sdxi_per_vm,
        "switch_width": _switch_width,
        "ernic_image": cfg.ernic_ref,
        "qemu_image": cfg.qemu_ref,
        "vm_images_dir": _setting(cfg, "vm_images_dir"),
        "cpu_sockets": cfg.vm_cpu_sockets,
        "cpu_phys_bits": cfg.vm_cpu_phys_bits,
        "pci_hole64_size": cfg.vm_pci_hole64_size,
        "net_prefix": net,
        "exporter_addr": f"{net}.{_NET_INFRA}.{_INFRA_EXPORTER}",
        "prometheus_addr": f"{net}.{_NET_INFRA}.{_INFRA_PROM}",
        "grafana_addr": f"{net}.{_NET_INFRA}.{_INFRA_GRAFANA}",
        "loki_addr": f"{net}.{_NET_INFRA}.{_INFRA_LOKI}",
        "alloy_addr": f"{net}.{_NET_INFRA}.{_INFRA_ALLOY}",
        "logs": cfg.logs_enable,
        "loki_image": _setting(cfg, "loki_image"),
        "loki_port": str(cfg.loki_port),
        "loki_data_dir": _setting(cfg, "loki_data_dir"),
        "alloy_image": _setting(cfg, "alloy_image"),
        # Carried into the inventory so the guest's ip_discovery.bin is generated
        # from the SAME rocjitsu build that serves the device. A blob from
        # another build does not match it.
        "rocjitsu_image": cfg.rocjitsu_ref,
        "grafana": cfg.grafana_enable,
        "grafana_image": _setting(cfg, "grafana_image"),
        "grafana_port": str(cfg.grafana_port),
        "grafana_password": _setting(cfg, "grafana_admin_password"),
        "subnet": subnet,
        "vm_username": _setting(cfg, "vm_username"),
        # Distinct shares, first seen first: one bind mount each.
        "vm_filesystem": list(dict.fromkeys(
            _split_per_vm(cfg.vm_filesystem, count, "<vm-filesystem>")
        )) if cfg.vm_filesystem else [],
        "vfio_user_root_port": cfg.vm_vfio_user_root_port,
        "tcp_port": str(cfg.ernic_tcp_port),
        "stats_port": str(cfg.ernic_stats_port),
        "ernic_nofile": str(cfg.ernic_nofile),
        "ernic_tap": cfg.ernic_tap,
        "ernic_p4": cfg.p4_enable,
        "ernic_p4_guest_scope": _setting(cfg, "p4_guest_scope"),
        "ernic_p4_program": cfg.p4_program,
        "sdxi_image": cfg.sdxi_ref,
        "stop_grace_period": _setting(cfg, "stop_grace_period"),
        "log_max_size": _setting(cfg, "log_max_size"),
        "log_max_file": str(cfg.log_max_files),
        "prom_data_dir": cfg.prometheus_data_dir or _PROM_DATA_DIR,
        "bind_addr": _setting(cfg, "publish_bind_addr"),
        "grafana_data_dir": cfg.grafana_data_dir or _GRAFANA_DATA_DIR,
        "prom_port": str(cfg.prom_port),
        "prom_interval": _setting(cfg, "prom_scrape_interval"),
        "prom_retention": _setting(cfg, "prom_retention"),
    }


def _positive(n: int, element: str) -> int:
    if n < 1:
        sys.exit(f"Error: {element} must be at least 1, got {n}")
    return n


def _dev_suffix(n: int, d: int, per_vm: int) -> str:
    """Service/socket suffix for device `d` of VM `n`.

    Single-device VMs keep the bare VM number. That is not cosmetic: it is
    what makes a one-device-per-VM stack regenerate byte-identical, so
    CI's `gen-compose --check` stays a real gate rather than a diff to
    re-baseline.
    """
    return f"{n}" if per_vm == 1 else f"{n}-{d}"


def _interleave(a: list[str], b: list[str]) -> list[str]:
    """Round-robin two lists, keeping any tail. Equal lengths pair 1:1."""
    out: list[str] = []
    for x, y in itertools.zip_longest(a, b):
        if x is not None:
            out.append(x)
        if y is not None:
            out.append(y)
    return out


def _group_socks_for_switches(
    nics: list[str], gpus: list[str], nics_per_switch: int
) -> list[str]:
    """Order sockets so each PCIe switch slot gets nics_per_switch NICs then one GPU.

    With nics_per_switch=1 this is equivalent to _interleave. Use when
    <ernic-per-vm> > <rocjitsu-per-vm> (e.g. Helios: 2 ernics per GPU, so each
    switch of width 3 receives [nic-A, nic-B, gpu-N]).
    """
    if nics_per_switch <= 1:
        return _interleave(nics, gpus)
    out: list[str] = []
    for i, gpu in enumerate(gpus):
        start = i * nics_per_switch
        out.extend(nics[start : start + nics_per_switch])
        out.append(gpu)
    out.extend(nics[len(gpus) * nics_per_switch :])
    return out


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
    cannot write /usr/local/bin; it installs under /tmp instead.

    When a source checkout is mounted, installs editable (pip install -e) so
    there is no build artifact to go stale: Python imports directly from the
    copied source tree. The binary is symlinked into bin_dir so callers need
    no path change.

    Falls back to 'pipx install qemu-tool' from PyPI when QEMU_TOOL_SRC is
    not set (no source checkout on the node). The volume default of /tmp means
    /qemu-tool-src/qemu/pyproject.toml will not exist in that case.
    """
    env = f"PIPX_BIN_DIR={bin_dir}"
    if not bin_dir.startswith("/usr"):
        env = f"HOME=/tmp PIPX_HOME=/tmp/pipx {env}"
    return [
        "        if [ -f /qemu-tool-src/qemu/pyproject.toml ]; then",
        "          cp -r /qemu-tool-src/qemu /tmp/qemu-tool-build",
        "          rm -rf /tmp/qemu-tool-build/build /tmp/qemu-tool-build/*.egg-info",
        "          python3 -m venv /tmp/qemu-tool-venv",
        "          /tmp/qemu-tool-venv/bin/pip install -q setuptools wheel",
        "          /tmp/qemu-tool-venv/bin/pip install -q --no-build-isolation -e /tmp/qemu-tool-build",
        f"          mkdir -p {bin_dir}",
        f"          ln -sf /tmp/qemu-tool-venv/bin/qemu-tool {bin_dir}/qemu-tool",
        "        else",
        "          for i in 1 2 3 4 5; do",
        f"            {env} pipx install --force qemu-tool && break",
        "            sleep 10",
        "          done",
        "        fi",
    ]


def render_compose(
    spec: dict[str, Any], layer_names: Sequence[str], digest: str,
    stack: str = _DEFAULT_STACK,
) -> str:
    out: list[str] = ["---"]
    out.append("# Generated by `qemu-tool gen-compose`. Do not edit.")
    out.append("#")
    out.append(f"# Regenerate after changing {' or '.join(layer_names)}:")
    out.append(f"#   qemu-tool gen-compose {_xml_args(layer_names, stack)}")
    out.append("#")
    out += _provenance_lines(digest)
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
    out.append(f'  image: "{spec["ernic_image"]}"')
    out.append("  logging: *logging")
    # rocm-ernic does not handle SIGTERM: measured, `docker stop` on one takes
    # 12.5 s and the container exits 137, which is the SIGKILL after Docker's
    # 10 s grace. The wait buys nothing.
    #
    # It costs a great deal, because the ernic services are chained
    # depends_on so registration order matches index order (see the service
    # comments), and compose therefore stops them STRICTLY ONE AT A TIME in
    # reverse. At 64 NICs that is 64 x 12.5 s -- a thirteen-minute teardown
    # spent entirely waiting to kill processes that were never going to exit.
    #
    # Safe to kill outright: these servers hold nothing durable. The sockets
    # and the stats files live on tmpfs volumes that `down -v` destroys, and
    # the stats file is rewritten every second, so the most that is lost is a
    # second of counters. Guests should already be down -- `qemu-tool
    # shutdown` is what makes that true -- and a guest still attached has
    # lost its device either way.
    out.append("  stop_signal: SIGKILL")
    # Both device servers only write into the two tmpfs volumes, which a
    # tmpfs mounts 1777, so an unprivileged uid creates its socket and its
    # stats file there with no pre-seeding and no init container.
    #
    # This anchor also carries rocjitsu, and that is not incidental:
    # rocjitsu does not chmod its vfio-user socket, so it stays 0755 owned
    # by whoever created it, and connect(2) on a unix socket needs write
    # on the inode. A qemu on a different uid than rocjitsu therefore
    # cannot attach the GPU at all. rocm-ernic hides this by chmodding its
    # own socket to 0666; do not generalise from it. The two anchors must
    # move together.
    out.append(f"  user: {_USER_REF}")
    # rocm-ernic attaches to a tap someone else created and owns; it needs
    # /dev/net/tun and no capability at all. Measured: an ernic at Docker's
    # default cap set reports "Ethernet attached to TAP tap0", and so does one
    # at none of them.
    out += _hardening("  ")
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
    out.append(f'  image: "{spec["qemu_image"]}"')
    out.append("  logging: *logging")
    out.append("  volumes:")
    out.append("    - vfu-sockets:/run/vfu:ro")
    out.append(f'    - "{_VM_IMAGES_REF}:{_VM_IMAGES_REF}"')
    for share in spec["vm_filesystem"]:
        # Same path inside the container as outside: run-vm passes the path
        # straight to -virtfs, so a remapped mount point would name a
        # directory the guest cannot see.
        out.append(f'    - "{share}:{share}:ro"')
    out.append('    - "${QEMU_TOOL_SRC:-../../..}:/qemu-tool-src:ro"')
    out.append("  devices:")
    out.append("    - /dev/kvm")
    # The point of the whole setting: with <vm-nvme> set, run-vm creates
    # <vm-images-dir>/<vm>-nvmeN.qcow2 from INSIDE this container, into a
    # directory that belongs to the person who started the fleet. As root
    # that lands 0644 root-owned in their own images/.
    #
    # Which is also the migration hazard from the root era: 0644 root-owned is
    # not writable by the uid, and QEMU opens a drive read-write, so an NVMe
    # qcow2 left behind by an earlier root run stops the guest booting rather
    # than merely having the wrong owner. `qemu-tool compose up` checks for
    # those and names them rather than letting the guest die at boot.
    out.append(f"  user: {_USER_REF}")
    # A container given --user <uid>:<gid> holds no supplementary groups, and
    # /dev/kvm is crw-rw---- root:kvm, so accel=kvm fails without this.
    out.append("  group_add:")
    out.append(f"    - {_KVM_GID_REF}")
    # QEMU needs no capability here. It does not configure the network (SLIRP
    # is userspace), does not mknod, and binds only 2222.
    out += _hardening("  ")
    # Docker's default is 10 s, then SIGKILL. QEMU is PID 1 here (run_vm
    # execs it) and exits on SIGTERM without ACPI-shutting-down the guest, so
    # a stop is a power cut however long the grace period is -- that is what
    # `qemu-tool shutdown` exists to fix. This buys the guest time to finish
    # powering off when something DOES ask it to, and stops a guest that is
    # mid-flush being killed partway.
    out.append(f"  stop_grace_period: {spec['stop_grace_period']}")
    out.append('  restart: "no"')
    out.append("")
    out.append("services:")

    for nic in spec["nics"]:
        n, o = nic["n"], nic["o"]
        # "NIC d of VM n" once there is more than one; the mesh role is what
        # the first line has to say, because it decides restart policy,
        # backend mode and who waits on whom.
        label = f"VM {n} NIC" if spec["ernic_per_vm"] == 1 \
            else f"VM {n} NIC {nic['d']}/{spec['ernic_per_vm']}"
        out.append("")
        if o == 1:
            out.append(f"  # ---- {label}: TCP mesh manager ----")
        elif spec["ernic_per_vm"] == 1:
            out.append(f"  # ---- {label}: TCP mesh worker ----")
        else:
            # Only worth saying when it is not just the VM number: the mesh
            # node ordinal is what the registration chain runs on.
            out.append(f"  # ---- {label}: TCP mesh worker (node {o}) ----")
        out.append(f"  {nic['service']}:")
        out.append("    <<: *ernic-common")
        # The node's hostname IS its mesh address: each rocm-ernic advertises
        # gethostname() to the manager and every peer then resolves that name
        # to dial it, so the default (the container id) puts Docker's embedded
        # DNS on every one of the fleet's N*(N-1) peer links. Naming the
        # container after its own address makes that a numeric getaddrinfo.
        out.append(f"    hostname: {nic['addr']}")
        if spec["ernic_tap"]:
            # Attaching to a pre-created, owned tap needs the device node and
            # nothing else -- no NET_ADMIN. That capability is the sidecar's.
            out.append("    devices:")
            out.append("      - /dev/net/tun")
        out.append("    networks:")
        if spec["ernic_tap"]:
            out.append("      l2: {}")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {nic['addr']}")
        if o == 1:
            backend = f"tcp:manager:listen:{spec['tcp_port']}"
            # A manager restart leaves every guest driver wedged on a DSR
            # timeout that only `down && up` clears (AGENTS.md issue 7), so
            # a crash must stay visible rather than be papered over.
            out.append('    restart: "no"')
        else:
            # The manager by address, not by name. Resolving "ernic-1" here
            # puts Docker's embedded DNS in the path of every worker's start,
            # and at fleet scale that is where the fleet breaks.
            backend = f"tcp:worker:{spec['nics'][0]['addr']}:{spec['tcp_port']}"
            out.append("    restart: on-failure")
        # Per node, not in the shared anchor: the GID set is what lets the
        # mesh resolve a destination GID to *this* node, so it is inherently
        # per-node. Getting it wrong is not a startup error -- the mesh
        # silently routes to some other node and the transfer fails with
        # "Completion with error at client".
        out.append("    environment:")
        out.append(f"      - ERNIC_TCP_GUEST_GIDS={nic['guest_gids']}")
        argv = ["rocm-ernic", "-s", nic["sock"], "-b", backend,
                "-m", nic["mac"], "-S", nic["stats_file"]]
        if spec["ernic_p4"]:
            argv += ["--p4"]
            if spec["ernic_p4_program"]:
                argv += ["--p4-program", spec["ernic_p4_program"]]
            scope = spec["ernic_p4_guest_scope"]
            if scope and scope != "none":
                argv += ["--p4-guest-scope", scope]
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
        if o != 1:
            # Chained, not all-depend-on-the-manager. The mesh assigns node
            # ids in *connection* order, but the GID->node resolver assumes
            # id == (last MAC byte - 1). Let workers race and whichever wins
            # takes node 1, so a guest addressing ernic-2 has its RDMA payload
            # routed to whichever ernic happened to register first -- the
            # transfer then fails with "Completion with error at client".
            # Starting them one at a time makes registration order match
            # index order, which is the only order the resolver gets right.
            #
            # The chain is over the GLOBAL ordinal, so at several NICs per VM
            # it runs across VM boundaries too. That is what registration
            # order has to be; it costs ~56 s of boot at 32 nodes.
            out.append("    depends_on:")
            out.append(f"      {spec['nics'][o - 2]['service']}:")
            out.append("        condition: service_healthy")
        out.append("    healthcheck:")
        if o == 1:
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
            # Check both /proc/net/tcp (IPv4) and /proc/net/tcp6 (IPv6):
            # rocm-ernic may bind on :: (IPv6 wildcard) on some hosts,
            # which appears only in tcp6 and would make an IPv4-only check
            # always fail. awk accepts multiple files and processes them in
            # sequence, so passing both costs nothing.
            out.append(
                '      test: ["CMD", "awk", '
                f'"$2 ~ /:{port_hex}$/ && $4 == \\"0A\\" {{found=1}} '
                'END {exit !found}", "/proc/net/tcp", "/proc/net/tcp6"]'
            )
        else:
            # Proving the socket exists is not enough to serialise
            # registration: rocm-ernic binds it before it has registered with
            # the manager. An ESTABLISHED connection to the manager's port is
            # the observable that registration actually happened.
            out.append(
                '      test: ["CMD", "awk", '
                f'"$3 ~ /:{port_hex}$/ && $4 == \\"01\\" {{found=1}} '
                'END {exit !found}", "/proc/net/tcp", "/proc/net/tcp6"]'
            )
        out.append("      interval: 2s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 5s")

    for gpu in spec["gpus"]:
        n = gpu["n"]
        label = f"VM {n} GPU" if spec["rocjitsu_per_vm"] == 1 \
            else f"VM {n} GPU {gpu['d']}/{spec['rocjitsu_per_vm']}"
        out.append("")
        out.append(f"  # ---- {label} ----")
        out.append(f"  {gpu['service']}:")
        out.append("    <<: *ernic-common")
        out.append(f'    image: "{spec["rocjitsu_image"]}"')
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {gpu['addr']}")
        out.append("    restart: on-failure")
        out.append("    command:")
        out.append("      - rocjitsu")
        out.append("      - --config")
        out.append(f"      - /usr/local/share/rocjitsu/configs/{gpu['config']}")
        out.append("      - --vfio-socket")
        out.append(f"      - {gpu['sock']}")
        out.append("    healthcheck:")
        sock_re = gpu["sock"].replace(".", "[.]")
        out.append(f"      test: [\"CMD\", \"sh\", \"-c\", \"grep -qE ' 01 [0-9]+ {sock_re}$' /proc/net/unix\"]")
        out.append("      interval: 2s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 5s")

    for sdxi in spec["sdxis"]:
        n = sdxi["n"]
        label = f"VM {n} SDXI" if spec["sdxi_per_vm"] == 1 \
            else f"VM {n} SDXI {sdxi['d']}/{spec['sdxi_per_vm']}"
        out.append("")
        out.append(f"  # ---- {label} ----")
        out.append(f"  {sdxi['service']}:")
        out.append("    <<: *ernic-common")
        out.append(f'    image: "{spec["sdxi_image"]}"')
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {sdxi['addr']}")
        out.append("    restart: on-failure")
        # The published image has no ENTRYPOINT and a shell CMD serving a
        # fixed socket name, so the binary has to be named here.
        out.append('    entrypoint: ["rocm-sdxi"]')
        out.append("    command:")
        out.append("      - -s")
        out.append(f"      - {sdxi['sock']}")
        out.append("    healthcheck:")
        sock_re = sdxi["sock"].replace(".", "[.]")
        out.append(f"      test: [\"CMD\", \"sh\", \"-c\", \"grep -qE ' 01 [0-9]+ {sock_re}$' /proc/net/unix\"]")
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
        # Every device this guest attaches, not just the first. QEMU exits if
        # any one vfio-user socket is missing, so a guest that starts while
        # its eighth NIC is still coming up dies outright.
        for dev in vm["nics"] + vm["gpus"] + vm["sdxi"]:
            out.append(f"      {dev['service']}:")
            out.append("        condition: service_healthy")
        out.append("    ports:")
        out.append(f'      - "{vm["ssh_port"]}:2222"')
        out.append("    entrypoint:")
        out.append("      - /bin/sh")
        out.append("      - -c")
        out.append("      - |")
        # pipx reaches pypi.org for the build backend, and a fleet's worth of
        # these racing at container start is enough to make Docker's embedded
        # resolver drop queries -- measured at 48 VMs, where 12 guests died on
        # "Failed to resolve 'pypi.org'" before QEMU ever started. Retry
        # rather than lose the guest.
        # /usr/local/bin is root-owned 0755 and the image's HOME is "/", also
        # unwritable, so the default install prefix fails before QEMU ever
        # starts -- and this service is restart: "no", so that is a lost
        # guest, not a retry. Same writable-prefix path the stats exporter has
        # always used. Unconditional now that the uid is: a prefix that works
        # for both root and a plain uid beats one that works for root only.
        out.extend(_pipx_install("/tmp/bin"))
        # /tmp/bin is not on PATH. Missing this is the easy one: the install
        # succeeds and the failure is a bare "qemu-tool: not found" at exec.
        out.append("        exec /tmp/bin/qemu-tool run-vm \\")
        out.append(f'          --images "{_VM_IMAGES_REF}" \\')
        out.append(f'          --vm-name "{vm["name"]}" \\')
        out.append("          --ssh-port 2222 \\")
        out.append("          --backing-shared \\")
        if spec["vfio_user_root_port"]:
            # A boolean, so it is emitted here rather than through _per_vm_flags,
            # which renders --flag "value" pairs.
            out.append("          --vfio-user-root-port \\")
        if spec["switch_width"]:
            out.append(
                f'          --vfio-user-switch-width {spec["switch_width"]} \\')
        if spec["cpu_sockets"]:
            out.append(f'          --cpu-sockets {spec["cpu_sockets"]} \\')
        if spec["cpu_phys_bits"]:
            out.append(f'          --cpu-phys-bits {spec["cpu_phys_bits"]} \\')
        if spec["pci_hole64_size"]:
            out.append(
                f'          --pci-hole64-size "{spec["pci_hole64_size"]}" \\')
        for flag, value in vm["flags"]:
            out.append(f'          {flag} "{value}" \\')
        # Sockets are passed explicitly rather than globbed: with a fleet's
        # worth of sockets on one shared tmpfs, the single-VM stack's
        # `ls /run/vfu/*.sock` would attach every device to every VM.
        #
        # ORDER MATTERS when switches are on: run-vm groups this list left to
        # right, so the NIC/GPU round-robin is what puts one of each below
        # every switch.
        all_vfio_socks = list(vm["vfio_socks"]) + list(vm["sdxi_socks"])
        out.append(f'          --vfio-userdev "{",".join(all_vfio_socks)}"')

    out.append("")
    out.append("  # ---- metrics (opt-in: --profile metrics) ----")
    out.append("  ernic-stats-exporter:")
    out.append("    <<: *qemu-common")
    out.append("    profiles: [metrics]")
    out.append("    networks:")
    out.append("      fleet:")
    out.append(f"        ipv4_address: {spec['exporter_addr']}")
    out.append("    devices: []")
    # Nulls out the kvm group the qemu anchor adds, beside the devices: []
    # that nulls out the device it is for. This service never touches
    # /dev/kvm.
    out.append("    group_add: []")
    # The exporter needs no privilege: it reads world-readable dumps off a
    # sticky tmpfs and binds an unprivileged port.
    #
    # Deliberately NOT CONTAINER_USER, which overrides this line if it is ever
    # emitted after the merge key: nobody is the right answer here. This
    # process creates nothing on the host, so there is no ownership to get
    # right, and 65534 is the stronger position of the two.
    out.append('    user: "65534:65534"')
    out.append("    stop_grace_period: 5s")
    out.append("    volumes:")
    out.append("      - ernic-stats:/run/ernic-stats:ro")
    out.append('      - "${QEMU_TOOL_SRC:-/tmp}:/qemu-tool-src:ro"')
    out.append("    entrypoint:")
    out.append("      - /bin/sh")
    out.append("      - -c")
    out.append("      - |")
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
    # This image already defaults to nobody, so the change here is not
    # root -> unprivileged, it is 65534 -> you: <prom-data-dir> is a host
    # bind mount, and the TSDB of a fleet run should belong to the person
    # who ran the fleet. It must be owned by the same uid, which
    # `qemu-tool compose up` arranges -- without that Prometheus
    # crash-loops on "mkdir data/: permission denied".
    out.append(f"    user: {_USER_REF}")
    out += _hardening()
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
        # Same as Prometheus: <loki-data-dir> is a host bind mount and has to
        # belong to this uid before the first run.
        out.append(f"    user: {_USER_REF}")
        out += _hardening()
        out.append("    volumes:")
        out.append("      - ./logging/loki-config.yml:/etc/loki/config.yml:ro")
        # Absolute, for the same reason as the TSDB: a relative path resolves
        # against the stack directory, which for an installed qemu-tool is
        # inside site-packages.
        out.append(f"      - {spec['loki_data_dir']}:/loki")
        out.append("    command:")
        out.append("      - -config.file=/etc/loki/config.yml")
        out.append("    healthcheck:")
        out.append('      test: ["CMD-SHELL", "wget -q -O- '
                   'http://127.0.0.1:3100/ready | grep -q ready"]')
        out.append("      interval: 10s")
        out.append("      timeout: 5s")
        out.append("      retries: 30")
        out.append("      start_period: 30s")
        out.append("")
        out.append("  # Ships every stack container's logs to Loki. The guests")
        out.append("  # run their own Alloy: a VM's journal is behind QEMU,")
        out.append("  # not on this Docker socket.")
        out.append("  #")
        out.append("  # Mounts the Docker socket directly. Anything that can")
        out.append("  # connect to that socket is root on the host, whatever")
        out.append("  # the mount flags say. Accepted here: this is a lab")
        out.append("  # stack and the socket is needed for container discovery")
        out.append("  # and log tailing. The container is otherwise locked")
        out.append("  # down (cap_drop ALL, no-new-privileges).")
        out.append("  alloy:")
        out.append(f'    image: "{spec["alloy_image"]}"')
        out.append("    logging: *logging")
        out.append("    profiles: [metrics]")
        out.append("    networks:")
        out.append("      fleet:")
        out.append(f"        ipv4_address: {spec['alloy_addr']}")
        out.append("    restart: on-failure")
        # Needs root to open /var/run/docker.sock (owned by root:docker).
        # The CONTAINER_USER uid is typically not in the docker group inside
        # the container, so we stay as root rather than failing silently.
        out.append("    cap_drop:")
        out.append("      - ALL")
        out.append("    security_opt:")
        out.append('      - "no-new-privileges:true"')
        out.append("    depends_on:")
        out.append("      loki:")
        out.append("        condition: service_healthy")
        out.append("    environment:")
        out.append(f"      - LOKI_HOST={spec['loki_addr']}:3100")
        out.append("    volumes:")
        out.append("      - /var/run/docker.sock:/var/run/docker.sock:ro")
        out.append("      - ./logging/alloy-host.alloy:/etc/alloy/config.alloy:ro")
        out.append("    command:")
        out.append("      - run")
        out.append("      - /etc/alloy/config.alloy")
        # /tmp, not a mount: a non-root uid has no writable HOME in this image
        # and the WAL here is per-run scratch, not something to keep.
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
        # Grafana's image defaults to uid 472 and <grafana-data-dir> does
        # not usually exist before the first `up`, so the daemon would create
        # it root-owned and Grafana could not write its sqlite. Owning it as
        # the invoking user is the same fix with a better owner, and
        # `qemu-tool compose up` creates it that way.
        out.append(f"    user: {_USER_REF}")
        out += _hardening()
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
        out.append("      - GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/fleet/quickstart.json")
        out.append("    volumes:")
        # Provisioned from files, not clicked in: a dashboard that exists only
        # in Grafana's own sqlite dies with the container, and the whole point
        # of the generated stack is that a fleet is reproducible from its
        # fleet XML. :ro because Grafana must not edit what the generator owns.
        out.append("      - ./grafana/provisioning:/etc/grafana/provisioning:ro")
        out.append("      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro")
        # Grafana's sqlite, on the host. The dashboards are provisioned from
        # files and survive without it, but annotations and anything explored
        # ad hoc do not.
        out.append(f"      - {spec['grafana_data_dir']}:/var/lib/grafana")
        out.append("    ports:")
        out.append(f'      - "{spec["bind_addr"]}:{spec["grafana_port"]}:3000"')

    if spec["ernic_tap"]:
        out.append("")
        out.append("  # ---- guest Ethernet wiring ----")
        out.append("  # One short-lived sidecar per ernic. It shares that")
        out.append("  # ernic's network namespace, creates the tap and the")
        out.append("  # bridge, and exits. NET_ADMIN lives here and nowhere")
        out.append("  # else.")
        out.append("  #")
        out.append("  # The one service CONTAINER_USER does not apply to, and")
        out.append("  # not by choice. cap_add raises only the bounding and")
        out.append("  # permitted sets; Docker sets no AMBIENT capabilities,")
        out.append("  # so a non-root uid holds nothing effective and")
        out.append("  # NET_ADMIN is dropped at the switch to it. Measured:")
        out.append("  # --user <uid> --cap-add NET_ADMIN gives 'RTNETLINK")
        out.append("  # answers: Operation not permitted' on the first `ip`")
        out.append("  # call. Compose cannot express ambient caps, so there is")
        out.append("  # no fix for the uid.")
        out.append("  #")
        out.append("  # What CAN be done is the rest of the cap set. This used")
        out.append("  # to run at Docker's default -- CHOWN, DAC_OVERRIDE,")
        out.append("  # FOWNER, SETUID, SETGID, NET_RAW, MKNOD and more -- plus")
        out.append("  # NET_ADMIN. It now runs at NET_ADMIN and nothing else,")
        out.append("  # on a read-only root filesystem, for the seconds it")
        out.append("  # takes to wire a bridge.")
        out.append("  #")
        out.append("  # TAP_OWNER below is how the ernic stays unprivileged")
        out.append("  # anyway: the tap is created persistent and owned by")
        out.append("  # the uid that will attach to it, because TUNSETIFF on")
        out.append("  # a persistent tap needs a matching owner or")
        out.append("  # NET_ADMIN. Left at the default of 0 it would hand a")
        out.append("  # non-root ernic EPERM and a guest with no Ethernet.")
        for nic in spec["nics"]:
            suffix = _dev_suffix(nic["n"], nic["d"], spec["ernic_per_vm"])
            out.append(f"  tapsetup-{suffix}:")
            out.append("    build:")
            out.append("      context: ./tapsetup")
            out.append("      args:")
            out.append("        - AMD_CERT")
            out.append(f'    image: "{_TAPSETUP_IMAGE}"')
            # Never try the registry. This image is built here, from the
            # ./tapsetup beside this file, and `qemu-tool/ernic-tapsetup`
            # exists in no registry anywhere -- so compose's default
            # pull-then-build greets every first run on a fresh host with
            #
            #   ! Image qemu-tool/ernic-tapsetup:1  pull access denied for
            #     qemu-tool/ernic-tapsetup, repository does not exist or may
            #     require 'docker login': denied
            #
            # which is harmless (the build follows) and reads exactly like a
            # missing credential. It cost a new user a support round trip.
            # Nobody who has run a fleet before ever sees it, because the
            # image is then already in their local store -- so it is
            # invisible precisely to the people who would recognise it.
            out.append("    pull_policy: build")
            out.append("    logging: *logging")
            out.append(f'    network_mode: "service:{nic["service"]}"')
            # Drop first, then add the one back. `cap_add` alone leaves
            # Docker's default set underneath it, which is what this ran with
            # before: NET_ADMIN was the only capability anyone had thought
            # about and thirteen others came along for free.
            out.append("    cap_drop: [ALL]")
            out.append("    cap_add: [NET_ADMIN]")
            out.append('    security_opt: ["no-new-privileges:true"]')
            # setup.sh writes nothing: it calls `ip` and exits. A read-only
            # rootfs costs this container nothing and takes a writable /usr
            # away from a NET_ADMIN process.
            out.append("    read_only: true")
            # The sidecar picks the l2 interface by address, because Docker
            # does not guarantee which network lands on which interface and
            # it genuinely differs between containers in one `up`. Bridging
            # the mesh interface by mistake breaks the fleet in a way that
            # looks like a worker bug -- see tapsetup/setup.sh.
            out.append("    environment:")
            out.append(f"      - FLEET_PREFIX={spec['net_prefix']}")
            out.append(f'      - "TAP_OWNER={_USER_VAR}"')
            out.append("    devices:")
            out.append("      - /dev/net/tun")
            out.append('    restart: "no"')
            out.append("    depends_on:")
            out.append(f"      {nic['service']}:")
            out.append("        condition: service_started")

    out.append("")
    out.append("networks:")
    if spec["ernic_tap"]:
        out.append("  # Dumb shared L2 carrying guest Ethernet only. No address")
        out.append("  # is assigned: each ernic bridges its tap onto it, and the")
        out.append("  # guests address each other over <ernic-subnet>.")
        out.append("  l2:")
        out.append("    driver: bridge")
    out.append("  # Every service has a fixed address and nothing resolves a")
    out.append("  # service name at run time. Docker's embedded DNS is a single")
    out.append("  # resolver per network, and a fleet start burst saturates it:")
    out.append("  # at 48 VMs, workers died with 'Temporary failure in name")
    out.append("  # resolution', and because the mesh allocates a NEW node id on")
    out.append("  # every reconnect, the restarts walked the fleet toward the")
    out.append("  # 64-node protocol ceiling. Third octet is the service family:")
    out.append(f"  #   {_NET_INFRA} infra   {_NET_ERNIC} ernic   {_NET_ROCJITSU} rocjitsu   {_NET_QEMU} qemu   {_NET_SDXI} sdxi")
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


def render_prometheus(spec: dict[str, Any], digest: str) -> str:
    out: list[str] = ["---"]
    out.append("# Generated by `qemu-tool gen-compose`. Do not edit.")
    out += _provenance_lines(digest)
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
    # No amd-gpu (:9488) and no hsa-snoop (:9490) job. Both were rendered
    # here for every stack, and both were down in every guest: every GPU this
    # generator attaches is rocjitsu's, and neither exporter can run against
    # it. amd-metrics-exporter's gpuagent assertion-fails where amdsmi
    # answers NOT_SUPPORTED and leaves /dev/kfd busy, so the guest image does
    # not install it. hsa-snoop's collector reads each queue's ring with
    # process_vm_readv, which its own packaging says deadlocks GPU dispatch
    # on a vfio-user device, and it holds trace_pipe for as long as it runs,
    # so the image disables it. The GPU's presence comes through fleet-node
    # instead: the guest writes amdgpu_kfd_present and amdgpu_devices_total
    # for node_exporter's textfile collector.
    out.append("  # RDMA metrics exporter inside each guest (port 9879).")
    out.append("  # Reads /sys/class/infiniband/*/counters; on ionic_rdma")
    out.append("  # these are empty today, but the target is wired so that")
    out.append("  # when the emulated device grows counters it starts reporting")
    out.append("  # with no further change. Same hostfwd pattern as fleet-node.")
    out.append("  - job_name: rdma-exporter")
    out.append("    static_configs:")
    for vm in spec["vms"]:
        out.append(f"      - targets: [\"{vm['qemu_addr']}:9879\"]")
        out.append("        labels:")
        out.append(f'          vm: "{vm["n"]}"')
        out.append(f'          vm_name: "{vm["name"]}"')
    out.append("")
    out.append("  - job_name: ernic-stats")
    out.append("    static_configs:")
    out.append(f"      - targets: [\"{spec['exporter_addr']}:{spec['stats_port']}\"]")

    return "\n".join(out) + "\n"


def render_inventory(spec: dict[str, Any], digest: str) -> str:
    """The fleet's ansible inventory, derived from the same <vm-count>.

    Generated rather than hand-kept because the failure mode of a stale one is
    quiet: ansible-playbook against an inventory listing six of eight guests
    configures six and reports success. The SSH ports and guest IPs here are
    the same values the compose file publishes, from the same `plan()`.
    """
    out: list[str] = ["---"]
    out.append("# Generated by `qemu-tool gen-compose`. Do not edit.")
    out += _provenance_lines(digest)
    out.append("")
    out.append("all:")
    out.append("  vars:")
    out.append("    ansible_host: 127.0.0.1")
    # From <vm-username>, which is a VMConfig field (config.py) and so already
    # understood by gen-vm and run-vm. The published guest images carry their
    # own user -- the rocm-golden qcow2 ships vm-info.json naming
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
    # The image the stack serves the device from, so that firmware generated
    # for a live guest comes from the same rocjitsu build.
    out.append(
        f'        fleet_guest_rocjitsu_image: "{spec["rocjitsu_image"]}"'
    )
    # /24 is implied by <ernic-subnet> being three octets; the generator
    # has no key for a prefix because the ernic L2 segment is flat by
    # construction.
    out.append("        fleet_guest_prefix: 24")
    out.append("      hosts:")
    for vm in spec["vms"]:
        # Keyed by VM name, the same identifier the vm_name label in
        # prometheus.yml carries.
        out.append(f"        {vm['name']}:")
        out.append(f"          ansible_port: {vm['ssh_port']}")
        out.append(f"          fleet_guest_ip: {vm['ip']}")
        # The guest finds its ernic by MAC, not by device name: the udev
        # rename comes from ernic_guest_setup and a guest without it keeps
        # the kernel name, so a name here is sometimes simply wrong.
        out.append(f"          fleet_guest_ernic_mac: {vm['mac']}")
        if len(vm["nics"]) > 1:
            # The remaining NICs, which the single-NIC vars above cannot
            # express. Nothing in ansible/ consumes this yet --
            # ernic_guest_setup's configure play addresses one device. It is
            # emitted so the per-NIC MAC/IP pairing is recorded where a role
            # that loops over them will look, rather than having to be
            # re-derived from the compose file.
            out.append("          fleet_guest_nics:")
            for nic in vm["nics"]:
                out.append(f"            - index: {nic['d']}")
                out.append(f"              mac: {nic['mac']}")
                out.append(f"              ip: {nic['ip']}")
    return "\n".join(out) + "\n"


# Every `./x` the rendered compose file names: a build context, a bind-mounted
# config file, a directory of them. Captured whole, not just the first
# segment -- `./logging/data` is a reference to a data directory, not to
# `logging` (see _protected_paths).
_RELATIVE_REF = re.compile(r"\./([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")


def _stack_source(stack: str = _DEFAULT_STACK) -> Path:
    """This repo's copy of the stack: the authority the mirror copies from.

    Named, not fixed, because each generated stack owns its own assets.
    Rendering mi355x-server while this answered "rocm-golden" would mirror
    that stack's grafana/, logging/ and tapsetup/ over it, authoritatively
    -- the deletion half of _copy_stack_assets, aimed at the wrong directory.
    """
    from .compose import _compose_dir

    return _compose_dir(stack)


def _owned_assets(
    compose_body: str, generated: set[str], output_dir: Path,
    stack: str = _DEFAULT_STACK,
) -> list[tuple[Path, Path]]:
    """The (source, destination) pairs this generator owns, in copy order.

    Ownership is deliberately narrow, because the destination is a live stack
    directory full of things that are not ours: in the 32-VM fleet's directory,
    2.4 GB of Prometheus TSDB under prom-data, grafana-data, loki-data,
    results/, ad-hoc scripts, tarballs and a docker-compose.override.yml. A
    name qualifies only if the rendered compose file names it as `./name` AND
    the repo's stack directory has something of that name to copy. A
    `./prom-data` bind mount therefore does not qualify -- the repo has no
    prom-data -- so no amount of compose-file editing can point the mirror at
    an operator's data.
    """
    source = _stack_source(stack)
    pairs = []
    # The first segment is what names an asset directory; the rest of the
    # reference is a path inside it, and _protected_paths is what reads that.
    names = {ref.split("/")[0] for ref in _RELATIVE_REF.findall(compose_body)}
    for name in sorted(names):
        # "." and ".." are references out of the stack, not assets of it: the
        # compose file mounts ./.. for the qemu-tool source tree. Copying one
        # means copying the whole compose root into the parent of the output
        # directory, which is both wrong and, mercifully, usually a permission
        # error rather than a silent mess.
        if name in {".", ".."} or name in generated:
            continue
        src = source / name
        dst = output_dir / name
        if not src.exists() or src.resolve() == dst.resolve():
            continue
        pairs.append((src, dst))
    return pairs


def _protected_paths(
    compose_body: str, source: Path, output_dir: Path
) -> set[Path]:
    """Relative bind-mount sources, in full, that the repo has nothing for.

    _owned_assets reasons about the first path segment only, which is right
    for deciding what is an asset directory and wrong for deciding what is in
    it. `<loki-data-dir>` set to ./logging/data -- Loki's data next to
    logging/loki-config.yml, a natural choice that plan() accepts without
    comment -- marks `logging` owned and contributes nothing to the wanted
    set, so the regeneration deleted the data directory the stack had just
    been told to use. A multi-segment reference the repo cannot supply is the
    operator's at any depth, exactly as `./prom-data` is at the top. One the
    repo CAN supply is an asset, and stays prunable: protecting
    `./grafana/provisioning` because it is bind-mounted would shield the
    superseded datasource this whole mirror exists to remove.
    """
    protected = set()
    for ref in set(_RELATIVE_REF.findall(compose_body)):
        if "/" not in ref or ref.startswith(".."):
            continue
        if (source / ref).exists():
            continue
        protected.add(output_dir / ref)
    return protected


def _walk_owned(dst: Path) -> tuple[list[Path], list[Path]]:
    """Every real path under dst, and every symlink the walk refused to cross.

    The invariant the mirror has to hold: it may never delete or write
    anything that is not inside the output directory itself. A symlink on an
    owned path breaks that in both directions, and pathlib hides it --
    rglob does not recurse into a symlinked subdirectory, but it does descend
    when the starting path is itself a symlink. An operator keeping dashboards
    on a big disk with `ln -s /srv/grafana-shared <stack>/grafana` had
    /srv/grafana-shared enumerated and emptied, with every `Removed` line
    naming the path through the link rather than the directory actually being
    emptied. Comparing resolved paths is not a fix: it rejects one alias out
    of many. So no symlink is followed and no symlink is deleted -- it is
    reported and its asset is left alone.
    """
    if dst.is_symlink():
        return [], [dst]
    if not dst.is_dir():
        return [], []
    paths: list[Path] = []
    links: list[Path] = []
    pending = [dst]
    while pending:
        for entry in sorted(pending.pop().iterdir()):
            if entry.is_symlink():
                links.append(entry)
                continue
            paths.append(entry)
            if entry.is_dir():
                pending.append(entry)
    return paths, links


def _diff_result(**kwargs: list[Path]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {
        key: []
        for key in ("missing", "modified", "retyped", "extra", "unowned")
    }
    result.update(kwargs)
    return result


def _asset_diff(
    src: Path, dst: Path, protected: frozenset[Path] | set[Path] = frozenset()
) -> dict[str, list[Path]]:
    """How dst differs from src, as lists of dst paths.

    Four classes, because --check has to answer "would a regeneration change
    anything" and not just "would it delete anything": `missing` is in the
    repo and not the stack, `modified` is in both and differs, `extra` is in
    the stack and not the repo, `unowned` is a symlink the mirror will not
    cross. `retyped` is the subset of `modified` whose file/directory type
    differs, which the copy cannot overwrite in place.
    """
    if dst.is_symlink():
        return _diff_result(unowned=[dst])
    if dst.exists() and dst.is_dir() != src.is_dir():
        # Docker creates a missing bind-mount source as a directory, so this
        # is the ordinary state of a stack that was started before its assets
        # were copied. The copy restores the right type immediately.
        return _diff_result(modified=[dst], retyped=[dst])

    present, unowned = _walk_owned(dst)
    if unowned:
        return _diff_result(unowned=unowned)
    if not src.is_dir():
        if not dst.exists():
            return _diff_result(missing=[dst])
        if dst.read_bytes() != src.read_bytes():
            return _diff_result(modified=[dst])
        return _diff_result()

    have = {path.relative_to(dst) for path in present}
    wanted = set()
    missing, modified, retyped = [], [], []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        wanted.add(rel)
        target = dst / rel
        if rel not in have:
            missing.append(target)
        elif path.is_dir() != target.is_dir():
            modified.append(target)
            retyped.append(target)
        elif path.is_file() and path.read_bytes() != target.read_bytes():
            modified.append(target)
    # Deepest first: a directory's contents have to go before the directory.
    #
    # A protected path shields its ANCESTORS as well as itself and everything
    # under it, and the deepest-first order is exactly why. With
    # <loki-data-dir> at ./logging/data/sub, `logging/data/sub` is skipped as
    # protected and then `logging/data` -- unprotected, unwanted, and holding
    # it -- is rmtree'd with the protected directory still inside. Measured:
    # one line of output, "Removed .../logging/data", and the operator's
    # chunk.db gone. Shielding only the path and its children reads as
    # sufficient and is not.
    extra = [
        path for path in sorted(present, reverse=True)
        if path.relative_to(dst) not in wanted
        and not any(
            path == keep or keep in path.parents or path in keep.parents
            for keep in protected
        )
    ]
    return _diff_result(
        missing=missing, modified=modified, retyped=retyped, extra=extra)


def _superseded(
    src: Path, dst: Path, protected: frozenset[Path] | set[Path] = frozenset()
) -> list[Path]:
    """Everything under dst that src does not have, deepest first."""
    return _asset_diff(src, dst, protected)["extra"]


def _copy_stack_assets(
    compose_body: str, generated: set[str], output_dir: Path,
    stack: str = _DEFAULT_STACK,
) -> None:
    """Mirror the stack's non-generated assets next to the generated files.

    Authoritative, not additive: after this runs each owned asset directory
    matches the repo. The additive version cost the 32-VM fleet two hours of
    metrics. A hand-made grafana/provisioning/datasources/prom.yml survived
    alongside the repo's prometheus.yml, both `isDefault: true`, and Grafana
    refuses to start on that:

        Only one datasource per organization can be marked as default

    It crash-looped unnoticed, because an asset that is merely *extra* breaks
    nothing until something downstream reads the whole directory. So extra
    files are removed -- and named on the way out, since a silent deletion of
    an operator's hand-added file is the next two-hour mystery. The deletion
    is scoped per asset directory, the same shape as publish-pages rsyncing
    each lane's subtree with its own --delete: nothing else in the stack
    directory is read, let alone written.

    The invariant that scoping rests on, and the one to preserve in any later
    change here: this function may delete nothing that is not inside
    output_dir itself. Two things can carry it out of there, and both are
    refused rather than resolved -- a symlink anywhere on an owned path (see
    _walk_owned) and a relative bind-mount source nested under an owned name
    (see _protected_paths).

    Only the three rendered files used to be written, which made the
    documented --output-dir workflow produce a stack that could not run. Two
    different failures, neither of which names a missing file:

        unable to prepare context: path ".../tapsetup" not found
        failed parsing config: read /etc/loki/config.yml: is a directory

    The second is the nastier one. A bind mount whose host path does not exist
    is not an error to Docker -- it CREATES it, as a directory -- so loki,
    alloy and grafana come up mounting empty directories over their configs
    and crash-loop on a config file that is a directory. Nothing reports the
    real cause, and the directories are left behind to confuse the next run.

    Generating to a scratch directory is the documented workflow
    for an overlay fleet, precisely because writing over the committed stack
    is what `gen-compose --check` exists to catch. The committed stack is
    unaffected: there source and destination are the same directory, and each
    copy is skipped.

    Two orderings matter, both because this code deletes files for a living.
    Copy first and prune second: the prune set is computed from the source
    before anything is written, so the order is free, and prune-first turned
    one unremovable entry into a stack emptier than it started -- the
    hand-made file deleted, the PermissionError propagating out, and not one
    asset copied. The only deletions that still precede a copy are type
    mismatches, each of which the very next copy recreates. And a failed
    removal is collected rather than raised, so the remaining assets are still
    mirrored before anything is reported.
    """
    source = _stack_source(stack)
    protected = _protected_paths(compose_body, source, output_dir)
    problems: list[str] = []
    for src, dst in _owned_assets(compose_body, generated, output_dir, stack):
        diff = _asset_diff(src, dst, protected)
        if diff["unowned"]:
            # Left strictly alone: following it would write outside the output
            # directory and pruning it would empty a directory the operator
            # put somewhere else on purpose. Say so and move on -- an operator
            # who symlinked grafana/ would rather be told than have /srv
            # emptied under the name of a stack asset.
            for path in diff["unowned"]:
                target = os.readlink(path)
                where = "it" if path == dst else dst
                problems.append(
                    f"{path} is a symlink to {target}; {where} was left alone"
                )
                print(f"Refused {path}: symlink to {target}, not this stack's")
            continue
        for path in diff["retyped"]:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        print(f"Copied {src} -> {dst}")
        for path in diff["extra"]:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as error:
                problems.append(f"{path}: {error.strerror}")
                continue
            print(f"Removed {path} (not in {src})")
    if problems:
        sys.exit(
            "Error: the stack directory was not fully mirrored:\n  "
            + "\n  ".join(problems)
        )


def run(
    xml: Path | Sequence[Path] | None,
    output_dir: Path,
    check: bool = False,
    dry_run: bool = False,
    stack: str | None = None,
) -> None:
    if stack is None:
        stack = stack_for(xml)
    paths = FleetXml.find(xml)
    shipped = None
    if not paths:
        # Nothing named and nothing on the search path: a shipped stack is
        # generated from the fleet file of its own name.
        shipped = stack_fleet(stack)
        if shipped is None:
            sys.exit("Error: no fleet file found. Pass --xml.")
        paths = [shipped]
    cfg = FleetXml.parse_layers(paths)
    spec = plan(cfg)
    digest = xml_digest(paths)
    # The names as the user wrote them, so the recorded header is a command
    # that can be pasted back. Falls back to the basename of what the search
    # path found, or to the repo's name for the stack's own file -- the one
    # spelling under which the committed stack regenerates byte-identical.
    if shipped is not None:
        layer_names = [f"qemu/stacks/{shipped.name}"]
    elif xml is None:
        layer_names = [paths[0].name]
    elif isinstance(xml, Path):
        layer_names = [str(xml)]
    else:
        layer_names = [str(layer) for layer in xml]

    artifacts = {
        "docker-compose.yml": render_compose(spec, layer_names, digest, stack),
        "prometheus.yml": render_prometheus(spec, digest),
        # Alongside the other two rather than under ansible/inventory/, so all
        # three carry one xml-sha256 and `--check` stays a single comparison
        # against one directory. Point ansible-playbook at it with -i.
        "fleet-inventory.yml": render_inventory(spec, digest),
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
        # --check is the gate that is meant to notice exactly the two-hour
        # Grafana outage, and a superseded file is invisible to a file-by-file
        # comparison of the generated artifacts: nothing is out of date, there
        # is simply one file too many. The asset trees are therefore compared
        # too -- and compared symmetrically, because enumerating only the
        # destination answers "would a regeneration DELETE anything", not
        # "would it CHANGE anything". `rm stack/logging/loki-config.yml` used
        # to leave --check printing "Up to date" for a stack that cannot boot:
        # Docker recreates that bind-mount source as a directory and loki
        # crash-loops on `read /etc/loki/config.yml: is a directory`, the exact
        # failure _copy_stack_assets exists to prevent. Each class is worded
        # separately so a CI log says which of the three it found.
        compose_body = artifacts["docker-compose.yml"]
        protected = _protected_paths(
            compose_body, _stack_source(stack), output_dir)
        drift: dict[str, list[str]] = {
            "missing": [], "modified": [], "extra": [], "unowned": []}
        owned = _owned_assets(
            compose_body, set(artifacts), output_dir, stack)
        for src, dst in owned:
            diff = _asset_diff(src, dst, protected)
            for key in drift:
                drift[key] += [
                    str(path.relative_to(output_dir)) for path in diff[key]
                ]
        if stale or any(drift.values()):
            message = []
            if stale:
                message.append(
                    "Error: generated files are out of date: "
                    + ", ".join(sorted(stale))
                )
            if drift["missing"]:
                message.append(
                    "Error: stack assets missing from the output directory: "
                    + ", ".join(sorted(drift["missing"]))
                )
            if drift["modified"]:
                message.append(
                    "Error: stack assets modified from the repo's: "
                    + ", ".join(sorted(drift["modified"]))
                )
            if drift["extra"]:
                message.append(
                    "Error: extra stack assets the repo does not have: "
                    + ", ".join(sorted(drift["extra"]))
                )
            if drift["unowned"]:
                message.append(
                    "Error: stack assets are symlinks and will not be "
                    "mirrored: " + ", ".join(sorted(drift["unowned"]))
                )
            message.append(
                f"Regenerate with: qemu-tool gen-compose {_xml_args(layer_names, stack)}"
            )
            sys.exit("\n".join(message))
        print(f"Up to date: {output_dir}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, body in artifacts.items():
        (output_dir / name).write_text(body)
        print(f"Wrote {output_dir / name}")

    _copy_stack_assets(
        artifacts["docker-compose.yml"], set(artifacts), output_dir, stack
    )

    _print_next_steps(spec, cfg)


def _print_next_steps(spec: dict[str, Any], cfg: FleetConfig) -> None:
    backing = cfg.backing_ref
    first = spec["vms"][0]["name"]
    images = _setting(cfg, "vm_images_dir")
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
