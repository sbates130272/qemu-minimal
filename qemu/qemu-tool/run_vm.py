from __future__ import annotations

import atexit
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .caps import QemuCaps, qemu_binary
from .config import VMConfig
from .identity import identity_args

_ARCH_MACHINE = {
    "amd64": "q35",
    "arm64": "virt",
    "riscv64": "virt",
}

NVME_SIZE = "1024G"

# Let a guest give clusters back. Without it a qcow2 only ever grows: a
# deletion inside the guest frees nothing at the image level, so a bake that
# installs a toolchain and then removes it costs the same as one that keeps it,
# and a long-lived VM's overlay grows monotonically for the life of the VM.
# detect-zeroes=unmap additionally punches out an all-zero write rather than
# allocating a cluster of zeros, which is what makes a guest-side `fstrim`
# actually shrink the file.
#
# This is the same pair the NVMe scratch drives have always used; it was simply
# never applied to a root disk, including the ones gen-vm boots. Appending it is
# safe for list_vms._drive_file(), which splits the -drive spec on commas and
# matches the `file=` part, not the whole string.
DISCARD_OPTS = ",discard=unmap,detect-zeroes=unmap"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def build_command(cfg: VMConfig, caps: QemuCaps) -> list[str]:
    cmd: list[str] = [qemu_binary(cfg)]
    cmd += _arch_args(cfg)
    cmd += _trace_args(cfg)
    cmd += _smp_args(cfg) + ["-m", str(cfg.vmem)]
    cmd += _filesystem_args(cfg)
    cmd += ["-nographic"]
    cmd += _pci_testdev_args(cfg)
    cmd += _pci_hostdev_args(cfg)
    cmd += _nvme_args(cfg, caps)
    cmd += _pci_mmio_bridge_args(cfg, caps)
    cmd += _vfio_userdev_args(cfg)
    cmd += _root_drive_args(cfg)
    cmd += _netdev_args(cfg)
    cmd += _data_nic_args(cfg)
    cmd += _mcast_args(cfg)
    cmd += _guest_agent_args(cfg)
    cmd += _qmp_args(cfg)
    # Last: qemu and the listing parser both take the last -name/-uuid, so
    # stamping here stops literal --nvme passthrough from un-identifying the VM.
    cmd += identity_args("run-vm", cfg.vm_name)
    return cmd


def _owner(path: Path) -> str:
    """"uid N (name)" for a path, or just the uid when it has no passwd entry.

    The name matters because "uid 0" and "root" are the same fact but only one
    of them is the word someone will search for; the number matters because an
    LDAP or container uid often has no name at all on this host.
    """
    try:
        uid = path.stat().st_uid
    except OSError:
        return "unknown owner"
    try:
        import pwd

        return f"uid {uid} ({pwd.getpwuid(uid).pw_name})"
    except (KeyError, ImportError):
        return f"uid {uid}"


def _check_image_access(cfg: VMConfig) -> None:
    """Refuse early, by name, when a disk or its directory is not ours.

    This is the root-era migration landing, and every way it lands is
    unhelpful on its own:

    * QEMU opens a drive READ-WRITE even for a guest that only reads it, so a
      0644 root-owned qcow2 left by an earlier root-era run does not degrade
      -- the guest does not boot at all. QEMU's own message is
      "Could not open '<path>': Permission denied", buried in a container log
      among the vfio-user chatter, and it does not say who owns the file or
      why it changed.
    * An images directory this uid cannot write fails LATER and somewhere
      else: `qemu-img create` for an NVMe drive exits non-zero with its
      stderr captured, so the operator gets a CalledProcessError traceback
      naming a qemu-img command rather than a permissions problem.

    Both are one check, and both have the same one-line fix, so they are
    reported together with the owner named. See AGENTS.md, "Container
    privilege": the containers now run as the caller, so files from before
    that belong to root and are exactly what this catches.
    """
    uid = os.getuid()
    images = Path(cfg.images)

    if not images.is_dir():
        # Not a permissions problem, but it arrives at the same moment and
        # the next thing to fail would be an opaque qemu-img error.
        sys.exit(f"ERROR: images directory {images} does not exist.")

    # (path, why) per problem. The two causes want different fixes and
    # conflating them sends people to `chown` for a file they already own:
    # "owned by uid 1000, not read-write for uid 1000" is a true sentence
    # that reads like a contradiction and names the wrong remedy.
    foreign: list[Path] = []   # someone else's -- chown, or delete if scratch
    unwritable: list[Path] = []  # ours, but the mode says no -- chmod

    def classify(path: Path) -> None:
        try:
            mine = path.stat().st_uid == uid
        except OSError:
            mine = False
        (unwritable if mine else foreign).append(path)

    # W_OK|X_OK, not W_OK: creating a file needs write AND search on the
    # directory, and a directory that is writable but not searchable fails on
    # the open rather than on the create.
    if not os.access(images, os.W_OK | os.X_OK):
        classify(images)

    # Only files QEMU will actually open. A backing image is opened read-only
    # and is legitimately shared, so it is not checked for write -- demanding
    # that on a backing file every guest in a fleet shares would reject a
    # correct setup.
    disks = [images / f"{cfg.vm_name}.qcow2"]
    if cfg.nvme and re.match(r"^[0-9]+$", cfg.nvme):
        disks += [images / f"{cfg.vm_name}-nvme{i}.qcow2"
                  for i in range(1, int(cfg.nvme) + 1)]
    for disk in disks:
        if not disk.exists():
            continue  # it will be created; the directory check above covers that
        if not os.access(disk, os.R_OK | os.W_OK):
            classify(disk)

    if not foreign and not unwritable:
        return

    lines = [f"ERROR: this VM's storage is not usable by the user it runs as "
             f"(uid {uid}).", ""]
    for path in foreign:
        what = "the images directory" if path == images else "disk image"
        lines.append(f"  {path}")
        lines.append(f"      {what}, owned by {_owner(path)} -- not you")
    for path in unwritable:
        what = "the images directory" if path == images else "disk image"
        lines.append(f"  {path}")
        lines.append(f"      {what}, yours but mode "
                     f"{oct(path.stat().st_mode & 0o777)[2:]} denies write")
    lines += [
        "",
        "       QEMU opens every drive read-write, so this stops the guest "
        "booting",
        "       rather than merely owning the wrong file.",
        "",
        "       The fix:",
    ]
    # NVMe drives are scratch -- recreated on the next start -- so deleting
    # one is strictly better advice than chowning it, and does not need sudo.
    scratch = [p for p in foreign if "-nvme" in p.name]
    if scratch:
        lines.append(f"         rm {' '.join(str(p) for p in scratch)}")
        lines.append("           # NVMe drives are scratch and are recreated")
    keep = [p for p in foreign if p not in scratch]
    if keep:
        lines.append(f"         sudo chown {uid}:{os.getgid()} "
                     f"{' '.join(str(p) for p in keep)}")
    if unwritable:
        lines.append(f"         chmod u+rw {' '.join(str(p) for p in unwritable)}")
    if foreign:
        lines += [
            "",
            "       Files you do not own here are usually left over from "
            "before the",
            "       containers ran as you -- see AGENTS.md, \"Container "
            "privilege\".",
        ]
    sys.exit("\n".join(lines))


def run(cfg: VMConfig, caps: QemuCaps) -> None:
    # Before build_command, which creates the NVMe qcow2s as a side effect and
    # would hit the same permission wall first -- with qemu-img's stderr
    # captured, so the operator would see a traceback rather than the cause.
    if not cfg.dry_run:
        _check_image_access(cfg)
    cmd = build_command(cfg, caps)
    if cfg.dry_run:
        print(shlex.join(cmd))
        return
    if cfg.arch == "amd64" and cfg.qemu_guest_agent:
        qga_sock = Path(f"/tmp/qga-{cfg.vm_name}-{cfg.ssh_port}.sock")
        qga_sock.unlink(missing_ok=True)
    if cfg.mgmt_tap:
        _ensure_mgmt_bridge(cfg.ssh_port)
    if cfg.data_nic_queues > 0:
        _ensure_tap(f"dt{cfg.ssh_port}", cfg.data_nic_queues)
    os.execvp(cmd[0], cmd)


# ---------------------------------------------------------------------------
# Argument-building helpers
# ---------------------------------------------------------------------------

def _arch_args(cfg: VMConfig) -> list[str]:
    kvm_suffix = ",accel=kvm" if cfg.kvm else ""
    if cfg.arch == "amd64":
        # When vfio-user devices are present, _vfio_userdev_args emits its own
        # -machine with the shared memfd bound, so skip the machine arg here.
        if cfg.vfio_userdev:
            return ["-cpu", "EPYC"]
        return ["-machine", f"q35{kvm_suffix}", "-cpu", "EPYC"]
    if cfg.arch == "arm64":
        return [
            "-machine", f"virt,gic-version=max{kvm_suffix}",
            "-cpu", "max",
            "-bios", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
        ]
    if cfg.arch == "riscv64":
        return [
            "-machine", f"virt,{kvm_suffix}",
            "-kernel", "/usr/lib/u-boot/qemu-riscv64_smode/uboot.elf",
        ]
    sys.exit(f"Error: no ARCH mapping for '{cfg.arch}'")


def _smp_args(cfg: VMConfig) -> list[str]:
    """`-smp`, with the socket count pinned when asked for.

    Bare `cpus=N` leaves QEMU to infer the rest, and what it infers is
    sockets=N, cores=1: a guest given 8 vCPUs reports EIGHT single-core
    sockets. Nothing real looks like that, and anything in the guest that
    reads socket count or per-socket topology -- ROCm affinity, NUMA
    heuristics -- reads it wrong. Off by default so no existing stack
    changes shape.
    """
    if cfg.cpu_sockets <= 0:
        return ["-smp", f"cpus={cfg.vcpus}"]
    if cfg.vcpus % cfg.cpu_sockets:
        sys.exit(
            f"Error: --cpu-sockets ({cfg.cpu_sockets}) does not divide "
            f"--vcpus ({cfg.vcpus})."
        )
    cores = cfg.vcpus // cfg.cpu_sockets
    return [
        "-smp",
        f"cpus={cfg.vcpus},sockets={cfg.cpu_sockets},cores={cores},threads=1",
    ]


def _trace_args(cfg: VMConfig) -> list[str]:
    if cfg.nvme_trace is None:
        return []
    args: list[str] = []
    if cfg.nvme_trace == "doorbell":
        args += ["-trace", "enable=pci_nvme_mmio_doorbell_sq"]
        args += ["-trace", "enable=pci_nvme_mmio_doorbell_cq"]
    elif cfg.nvme_trace == "all":
        args += ["-trace", "enable=pci_nvme*"]
    else:
        args += ["-trace", f"enable={cfg.nvme_trace}"]
    if cfg.nvme_trace_file is not None:
        args += ["-trace", f"file={cfg.nvme_trace_file}"]
    return args


def _filesystem_args(cfg: VMConfig) -> list[str]:
    if cfg.filesystem is None:
        return []
    return [
        "-object", "memory-backend-memfd,id=mem0,size=2G",
        "-virtfs",
        f"local,path={cfg.filesystem},security_model=passthrough,mount_tag=hostfs",
    ]


def _pci_testdev_args(cfg: VMConfig) -> list[str]:
    if not cfg.pci_testdev:
        return []
    return ["-device", "pci-testdev,membar=16G,membar-backed=true"]


def _pci_hostdev_args(cfg: VMConfig) -> list[str]:
    if not cfg.pci_hostdev:
        return []
    args: list[str] = []
    for i, bdf in enumerate(cfg.pci_hostdev, start=1):
        _pci_check(bdf, cfg.dry_run)
        args += ["-device", f"pcie-root-port,id=pcie.{i},chassis={i}"]
        args += ["-device", f"vfio-pci,id=vfio-host{i},bus=pcie.{i},host={bdf}"]
    return args


def _nvme_serial(name: str) -> str:
    # QEMU rejects an nvme serial longer than 20 characters outright, so a VM
    # name of any length would fail to start. Keep the tail rather than the
    # head: the "-nvmeN" suffix is what distinguishes one drive from another.
    return name[-20:]


def _nvme_args(cfg: VMConfig, caps: QemuCaps) -> list[str]:
    if cfg.nvme is None:
        return []
    if re.match(r"^[0-9]+$", cfg.nvme):
        count = int(cfg.nvme)
        args: list[str] = []
        for i in range(1, count + 1):
            args += _nvme_create(f"{cfg.vm_name}-nvme{i}", NVME_SIZE, i, cfg, caps)
        return args
    if re.match(r"^-[0-9]+$", cfg.nvme):
        count = abs(int(cfg.nvme))
        args = []
        for i in range(count):
            name = f"{cfg.vm_name}-nvme{i}"
            args += [
                "-drive", f"file=/dev/nullb{i},format=raw,if=none,id=nvme-{i}",
                "-device", f"nvme,serial={_nvme_serial(name)},drive=nvme-{i}",
            ]
        return args
    # literal string — split and pass through
    return shlex.split(cfg.nvme)


def _nvme_create(
    name: str, size: str, idx: int, cfg: VMConfig, caps: QemuCaps
) -> list[str]:
    img = Path(cfg.images) / f"{name}.qcow2"
    if cfg.nvme_recreate and img.exists():
        try:
            img.unlink()
        except OSError as exc:
            # Unlinking needs write on the DIRECTORY, not on the file, so
            # this fires on a images/ that belongs to someone else even when
            # the qcow2 itself looks fine.
            sys.exit(
                f"ERROR: cannot remove {img} to recreate it: {exc.strerror}.\n"
                f"       It is owned by {_owner(img)} and "
                f"{img.parent} by {_owner(img.parent)}; "
                f"this process is uid {os.getuid()}."
            )
    if not img.exists():
        created = subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(img), size],
            capture_output=True, text=True,
        )
        if created.returncode != 0:
            # NOT check=True: that raises CalledProcessError with the stderr
            # inside the exception, so the operator gets a traceback naming a
            # qemu-img command line and has to go read it to find "Permission
            # denied". _check_image_access catches the usual cause before we
            # get here; this is the backstop that still says what happened.
            sys.exit(
                f"ERROR: could not create the NVMe image {img}.\n"
                f"       qemu-img said: {created.stderr.strip()}\n"
                f"       {img.parent} is owned by {_owner(img.parent)}; "
                f"this process is uid {os.getuid()}."
            )

    drv = (
        f"file={img},format=qcow2,if=none,id=nvme-{idx}"
        f",aio={caps.aio_mode},cache.direct=on"
        f"{DISCARD_OPTS}"
    )

    dev = f"nvme,serial={_nvme_serial(name)},id=nvme-{idx}-dev"
    if cfg.pci_mmio_bridge:
        if caps.has_ioeventfd:
            dev += ",ioeventfd=off"
        if caps.has_dbcs:
            dev += ",dbcs=off"
    if cfg.vram_dev_index is not None and caps.has_vram_dev:
        _validate_vram(cfg)
        dev += f",vram-dev=vfio-host{cfg.vram_dev_index},vram-bar={cfg.vram_bar}"

    ns = f"nvme-ns,drive=nvme-{idx},bus=nvme-{idx}-dev,nsid=1"
    if cfg.nvme_lbaf_mask is not None:
        _validate_lbaf_mask(cfg.nvme_lbaf_mask)
        mask = cfg.nvme_lbaf_mask.lower()
        if caps.has_lbaf_mask:
            ns += f",lbaf-mask={mask}"

    return [
        "-drive", drv,
        "-device", dev,
        "-device", ns,
    ]


def _pci_mmio_bridge_args(cfg: VMConfig, caps: QemuCaps) -> list[str]:
    if not cfg.pci_mmio_bridge:
        return []
    if not caps.has_pci_mmio_bridge:
        sys.exit(
            f"Error: --pci-mmio-bridge requested but '{qemu_binary(cfg)}' "
            f"does not support 'pci-mmio-bridge'."
        )
    print("NOTE: pci-mmio-bridge active; nvme_create forces ioeventfd=off,dbcs=off.")
    return [
        "-device",
        "pci-mmio-bridge,id=mmio-bridge,shadow-gpa=0x80000000,"
        "shadow-size=8192,poll-interval-ns=1000000,addr=8.0",
        "-trace", "enable=pci_mmio_*",
    ]


def _vfio_user_dev_json(sock: str, bus: str | None) -> str:
    # rombar=0 suppresses the ROM BAR that vfio-user-pci would otherwise
    # advertise.
    bus_part = f'"bus":"{bus}",' if bus else ""
    return (
        f'{{"driver":"vfio-user-pci",{bus_part}"rombar":0,'
        f'"socket":{{"path":"{sock}","type":"unix"}}}}'
    )


def _vfio_user_switch_args(cfg: VMConfig) -> list[str]:
    """Group the vfio-user devices under PCIe switches, in socket order.

    One `pcie-root-port` per switch off the root complex, an
    `x3130-upstream` on it, and a `xio3130-downstream` per device. This is
    the MI355X shape in miniature: with width=2 and an interleaved socket
    list, each switch carries one NIC and one GPU, so their traffic has a
    path that does not cross the root complex.

    ORDER IS THE PAIRING. The caller decides what ends up together by the
    order it passes --vfio-userdev; nothing here looks at what a socket
    serves.
    """
    width = cfg.vfio_user_switch_width
    socks = cfg.vfio_userdev
    args: list[str] = []
    # Unique across every port in the machine, root and downstream alike --
    # NOT per switch. QEMU uses (chassis, slot) to identify a slot, so a
    # repeated pair silently collapses two slots into one and the second
    # device never appears in the guest.
    chassis = 0
    for s, start in enumerate(range(0, len(socks), width)):
        chassis += 1
        # hotplug=off on every port is load-bearing, not tidiness. A port
        # that accepts hotplug reserves an MMIO/IO window for the device
        # that might arrive later, and eight switches' worth of those
        # reservations exhaust the 32-bit hole -- at which point the guest
        # fails BAR assignment for devices that are already present. None of
        # these ports is a hotplug target.
        args += [
            "-device",
            f"pcie-root-port,id=vfurp{s},chassis={chassis},slot={s},hotplug=off",
        ]
        args += ["-device", f"x3130-upstream,id=vfusw{s},bus=vfurp{s}"]
        for k, sock in enumerate(socks[start:start + width]):
            chassis += 1
            port = f"vfudp{s}.{k}"
            args += [
                "-device",
                f"xio3130-downstream,id={port},bus=vfusw{s}"
                f",chassis={chassis},slot={k},hotplug=off",
            ]
            args += ["-device", _vfio_user_dev_json(sock, port)]
    return args


def _vfio_userdev_args(cfg: VMConfig) -> list[str]:
    if not cfg.vfio_userdev:
        return []
    # Bind the shared memfd directly to the machine so the vfio-user server
    # can map DMA windows into it. Per PR rocm-systems#11397, using a NUMA
    # node memdev instead prevents the server from accessing guest RAM.
    kvm_suffix = ",accel=kvm" if cfg.kvm else ""
    args: list[str] = [
        "-object",
        f"memory-backend-memfd,id=mem-vfio-user,size={cfg.vmem}M,share=on",
        "-machine", f"q35{kvm_suffix},memory-backend=mem-vfio-user",
    ]
    if cfg.pci_hole64_size:
        # The default 64-bit hole does not fit many large BARs at once --
        # eight emulated GPUs will not enumerate without this.
        args += [
            "-global",
            f"q35-pcihost.pci-hole64-size={cfg.pci_hole64_size}",
        ]
    for sock in cfg.vfio_userdev:
        if not cfg.dry_run and not Path(sock).is_socket():
            sys.exit(f"ERROR: Socket {sock} does not exist.")

    if cfg.vfio_user_switch_width < 0:
        # Not silently "off": a negative width is a typo, and treating it as
        # off hands back a flat tree that looks like it worked.
        sys.exit(
            "Error: --vfio-user-switch-width must be 0 (off) or positive, "
            f"got {cfg.vfio_user_switch_width}."
        )
    if cfg.vfio_user_switch_width > 0:
        return args + _vfio_user_switch_args(cfg)

    for j, sock in enumerate(cfg.vfio_userdev, start=1):
        # On the root complex by default. q35 IS a PCIe machine, but a device
        # with no bus= lands on pcie.0 as a CONVENTIONAL PCI endpoint, with
        # no Express capability and therefore no link speed or width -- the
        # guest's ionic then logs "0.000 Gb/s available PCIe bandwidth
        # (Unknown x255 link)". A root port gives it real PCIe semantics,
        # which is what BAR-to-BAR DMA between two vfio-user devices needs.
        bus = None
        if cfg.vfio_user_root_port:
            args += [
                "-device",
                f"pcie-root-port,id=vfur{j},chassis={j},slot={j}",
            ]
            bus = f"vfur{j}"
        args += ["-device", _vfio_user_dev_json(sock, bus)]
    return args


def _root_drive_args(cfg: VMConfig) -> list[str]:
    img = Path(cfg.images) / f"{cfg.vm_name}.qcow2"
    drv = f"if=virtio,format=qcow2,file={img}{DISCARD_OPTS}"
    if cfg.backing_shared:
        drv += ",file.locking=off,backing.file.locking=off"
    return ["-drive", drv]


def _effective_mac(cfg: VMConfig) -> str:
    return cfg.mac or _mgmt_mac(cfg.ssh_port)


def _netdev_args(cfg: VMConfig, mac: str | None = None) -> list[str]:
    if cfg.mgmt_tap:
        tap = _mgmt_tap_name(cfg.ssh_port)
        m = mac or _effective_mac(cfg)
        return [
            "-netdev", f"tap,id=net0,ifname={tap},script=no,downscript=no",
            "-device", f"virtio-net-pci,netdev=net0,mac={m}",
        ]
    hostfwd = f"hostfwd=tcp::{cfg.ssh_port}-:22"
    for rule in cfg.extra_hostfwd:
        hostfwd += f",hostfwd={rule}"
    # Deterministic, so a guest that pins its netplan to the MAC it saw at
    # first boot still matches on later boots. Images this repo's gen-vm
    # builds do not pin -- they match `name: "en*"` -- but an image built
    # elsewhere may, and then the MAC has to be whatever that image expects:
    # --mac / VM_MAC is the way to say so.
    m = mac or _effective_mac(cfg)
    return [
        "-netdev", f"user,id=net0,{hostfwd}",
        "-device", f"virtio-net-pci,netdev=net0,mac={m}",
    ]


def _data_nic_args(cfg: VMConfig) -> list[str]:
    if cfg.data_nic_queues <= 0:
        return []
    tap = f"dt{cfg.ssh_port}"
    if not cfg.dry_run:
        _ensure_tap(tap, cfg.data_nic_queues)
    vectors = 2 * cfg.data_nic_queues + 2
    return [
        "-netdev",
        f"tap,id=data0,ifname={tap},queues={cfg.data_nic_queues}"
        f",vhost=on,script=no,downscript=no",
        "-device",
        f"virtio-net-pci,netdev=data0,mq=on,vectors={vectors}"
        f",mac={_nic_mac(cfg.ssh_port, 2)}",
    ]


def _mcast_args(cfg: VMConfig) -> list[str]:
    if cfg.mcast_group is None:
        return []
    return [
        "-netdev", f"socket,id=net1,mcast={cfg.mcast_group}",
        "-device", f"virtio-net-pci,netdev=net1,mac={_nic_mac(cfg.ssh_port, 1)}",
    ]


def _guest_agent_args(cfg: VMConfig) -> list[str]:
    if cfg.arch != "amd64" or not cfg.qemu_guest_agent:
        return []
    sock = f"/tmp/qga-{cfg.vm_name}-{cfg.ssh_port}.sock"
    return [
        "-chardev", f"socket,id=qga0,path={sock},server=on,wait=off",
        "-device", "virtio-serial-pci,id=virtio-serial0",
        "-device",
        "virtserialport,chardev=qga0,bus=virtio-serial0.0"
        ",name=org.qemu.guest_agent.0",
    ]


def _qmp_args(cfg: VMConfig) -> list[str]:
    if cfg.qmp_socket is None:
        return []
    if cfg.qmp_socket == "true":
        path = f"/tmp/qmp-{cfg.vm_name}-{cfg.ssh_port}.sock"
    else:
        path = cfg.qmp_socket
    return ["-qmp", f"unix:{path},server,nowait"]


# ---------------------------------------------------------------------------
# PCI / TAP helpers
# ---------------------------------------------------------------------------

def _pci_sys_bdf(bdf: str) -> str:
    if re.match(r"^[0-9a-fA-F]{4}:", bdf):
        return bdf
    return f"0000:{bdf}"


def _pci_check(bdf: str, dry_run: bool) -> None:
    if not re.match(
        r"^([0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$", bdf
    ):
        sys.exit(f"ERROR: PCIe bus address is invalid ({bdf}).")
    if dry_run:
        return
    sysbdf = _pci_sys_bdf(bdf)
    # Read the binding out of sysfs rather than shelling out to lspci. The
    # container that runs this carries QEMU and little else -- no pciutils --
    # so the lspci call this replaces died with FileNotFoundError before any
    # of the checks below could run, which reads as a qemu-tool crash rather
    # than as a missing package.
    driver_link = Path(f"/sys/bus/pci/devices/{sysbdf}/driver")
    if not driver_link.exists():
        sys.exit(f"ERROR: Device {bdf} is not bound to any driver "
                 f"(expected vfio-pci).")
    driver = driver_link.resolve().name
    if driver != "vfio-pci":
        sys.exit(f"ERROR: Device {bdf} is bound to {driver}, not vfio-pci.")
    iommu = Path(f"/sys/bus/pci/devices/{sysbdf}/iommu_group")
    if iommu.exists():
        group = Path(iommu).resolve().name
        vfio_dev = Path(f"/dev/vfio/{group}")
        if not os.access(vfio_dev, os.R_OK | os.W_OK):
            sys.exit(
                f"ERROR: Cannot access {vfio_dev} (IOMMU group for {bdf}).\n"
                f"       Fix: sudo chmod 660 {vfio_dev}\n"
                f"       Persistent: ../udev/install-vfio-rules"
            )


def _ensure_tap(tap: str, queues: int) -> None:
    result = subprocess.run(
        ["ip", "link", "show", tap], capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["sudo", "ip", "tuntap", "add", "dev", tap, "mode", "tap",
             "multi_queue", "user", os.getlogin()],
            check=True,
        )
        subprocess.run(["sudo", "ip", "link", "set", tap, "up"], check=True)
        atexit.register(_teardown_tap, tap)


def _teardown_tap(tap: str) -> None:
    subprocess.run(
        ["sudo", "ip", "tuntap", "del", "dev", tap, "mode", "tap"],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Management-NIC tap+bridge helpers
# ---------------------------------------------------------------------------

def _mgmt_bridge_name(port: int) -> str:
    return f"qemu-br{port}"


def _mgmt_tap_name(port: int) -> str:
    return f"mt{port}"


def _mgmt_subnet(port: int) -> str:
    octet = (port - 2000) % 254 + 1
    return f"172.16.{octet}"


_IP_FORWARD = Path("/proc/sys/net/ipv4/ip_forward")

# Whether THIS process turned ip_forward on. Only then is it ours to turn
# back off: the setting is global and host-wide, so a run that found it
# already enabled must leave it that way -- it may be carrying the host's
# own routing, a libvirt network or someone else's fleet.
_ip_forward_was_ours = False


def _ip_forward_enabled() -> bool:
    try:
        return _IP_FORWARD.read_text().strip() == "1"
    except OSError:
        # Unreadable means we cannot know, so we must not claim it. Returning
        # True here makes the caller skip both the set and the restore, which
        # is the safe direction: at worst the bridge does not route and that
        # is visible immediately.
        return True


def _enable_ip_forward() -> None:
    """Turn on ip_forward if it is off, and remember that we did.

    This used to be an unconditional `sysctl -w net.ipv4.ip_forward=1` with no
    matching restore, so running one VM once left the host forwarding packets
    forever. It is a global switch with no owner and no reference count, which
    is why the only safe rule is "put back exactly what you changed".
    """
    global _ip_forward_was_ours
    if _ip_forward_enabled():
        return
    subprocess.run(["sudo", "sysctl", "-qw", "net.ipv4.ip_forward=1"], check=True)
    _ip_forward_was_ours = True


def _restore_ip_forward(port: int) -> None:
    """Turn ip_forward back off, unless something else still needs it.

    The guard is the other qemu-br* bridges: this process may have enabled
    forwarding for VM A and then a second run-vm, seeing it already on, will
    not have recorded it as theirs. Switching it off from under them would
    break a running guest's networking with nothing to connect it to. So the
    restore happens only when no other management bridge is left.
    """
    global _ip_forward_was_ours
    if not _ip_forward_was_ours:
        return
    mine = _mgmt_bridge_name(port)
    try:
        others = [
            p.name for p in Path("/sys/class/net").iterdir()
            if p.name.startswith("qemu-br") and p.name != mine
        ]
    except OSError:
        others = []
    if others:
        print(
            f"Leaving net.ipv4.ip_forward=1: still in use by {', '.join(others)}",
            file=sys.stderr,
        )
        return
    subprocess.run(["sudo", "sysctl", "-qw", "net.ipv4.ip_forward=0"],
                   capture_output=True)
    _ip_forward_was_ours = False


def _nic_mac(port: int, index: int) -> str:
    """Deterministic MAC for NIC `index` of the VM on `port`.

    The index is a whole byte of its own so the NICs of one guest can never
    collide: they used to, because the mcast NIC derived its MAC from the
    port with the same arithmetic as the management NIC and landed on the
    identical address. A guest with two interfaces sharing a MAC gets ARP
    answers for the wrong one.
    """
    hi = (port >> 8) & 0xFF
    lo = port & 0xFF
    return f"52:54:00:{index:02x}:{hi:02x}:{lo:02x}"


def _mgmt_mac(port: int) -> str:
    return _nic_mac(port, 0)


def _ensure_mgmt_bridge(port: int) -> None:
    br = _mgmt_bridge_name(port)
    tap = _mgmt_tap_name(port)
    subnet = _mgmt_subnet(port)

    if subprocess.run(["ip", "link", "show", br], capture_output=True).returncode != 0:
        subprocess.run(["sudo", "ip", "link", "add", br, "type", "bridge",
                        "stp_state", "0"], check=True)
        subprocess.run(
            ["sudo", "ip", "addr", "add", f"{subnet}.1/24", "dev", br], check=True
        )
        subprocess.run(["sudo", "ip", "link", "set", br, "up"], check=True)

    _enable_ip_forward()

    def _ipt_ensure(table_args: list[str], rule: list[str]) -> None:
        """Add rule only if it doesn't already exist."""
        if subprocess.run(
            ["sudo", "iptables"] + table_args + ["-C"] + rule, capture_output=True
        ).returncode != 0:
            subprocess.run(
                ["sudo", "iptables"] + table_args + ["-A"] + rule, check=True
            )

    _ipt_ensure(["-t", "nat"], ["POSTROUTING",
                "-s", f"{subnet}.0/24", "!", "-d", f"{subnet}.0/24", "-j", "MASQUERADE"])
    _ipt_ensure([], ["FORWARD", "-i", br, "-j", "ACCEPT"])
    _ipt_ensure([], ["FORWARD", "-o", br,
                     "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"])

    pid_file = f"/tmp/qemu-dnsmasq-{port}.pid"
    dnsmasq_running = False
    try:
        pid = int(Path(pid_file).read_text().strip())
        dnsmasq_running = subprocess.run(
            ["sudo", "kill", "-0", str(pid)], capture_output=True
        ).returncode == 0
    except Exception:
        pass
    if not dnsmasq_running:
        subprocess.run(
            [
                "sudo", "dnsmasq",
                f"--pid-file={pid_file}",
                f"--interface={br}",
                "--bind-interface",
                "--except-interface=lo",
                f"--dhcp-range={subnet}.100,{subnet}.200,1h",
                "--log-dhcp",
            ],
            check=True,
        )
        print("dnsmasq DHCP log: sudo journalctl -t dnsmasq -f")

    if subprocess.run(["ip", "link", "show", tap], capture_output=True).returncode != 0:
        subprocess.run(
            ["sudo", "ip", "tuntap", "add", "dev", tap, "mode", "tap",
             "user", os.getlogin()],
            check=True,
        )
    subprocess.run(["sudo", "ip", "link", "set", tap, "master", br], check=True)
    subprocess.run(["sudo", "ip", "link", "set", tap, "up"], check=True)

    atexit.register(_teardown_mgmt_bridge, port)


def _teardown_mgmt_bridge(port: int) -> None:
    br = _mgmt_bridge_name(port)
    tap = _mgmt_tap_name(port)
    subnet = _mgmt_subnet(port)
    pid_file = f"/tmp/qemu-dnsmasq-{port}.pid"

    try:
        pid = int(Path(pid_file).read_text().strip())
        subprocess.run(["sudo", "kill", str(pid)], capture_output=True)
        Path(pid_file).unlink(missing_ok=True)
    except Exception:
        pass

    subprocess.run(
        ["sudo", "iptables", "-t", "nat", "-D", "POSTROUTING",
         "-s", f"{subnet}.0/24", "!", "-d", f"{subnet}.0/24", "-j", "MASQUERADE"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "iptables", "-D", "FORWARD", "-i", br, "-j", "ACCEPT"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "iptables", "-D", "FORWARD", "-o", br,
         "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "ip", "tuntap", "del", "dev", tap, "mode", "tap"],
        capture_output=True,
    )
    subprocess.run(["sudo", "ip", "link", "del", br], capture_output=True)
    # After the bridge is gone, so the "is anyone else still using it" check
    # does not count our own.
    _restore_ip_forward(port)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_vram(cfg: VMConfig) -> None:
    if cfg.vram_dev_index is None or cfg.vram_dev_index < 1:
        sys.exit(
            f"Error: --vram-dev-index must be a 1-based integer index into --pci-hostdev."
        )
    if not cfg.pci_hostdev:
        sys.exit("Error: --vram-dev-index set but --pci-hostdev is empty.")
    if cfg.vram_dev_index > len(cfg.pci_hostdev):
        sys.exit(
            f"Error: --vram-dev-index ({cfg.vram_dev_index}) exceeds "
            f"number of --pci-hostdev entries ({len(cfg.pci_hostdev)})."
        )


def _validate_lbaf_mask(mask: str) -> None:
    if not re.match(r"^0x[0-9a-fA-F]{1,4}$", mask):
        sys.exit(
            f"Error: --nvme-lbaf-mask ('{mask}') must be a 16-bit hex mask "
            f"e.g. 0x1f"
        )
