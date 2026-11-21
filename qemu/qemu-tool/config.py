"""`VMConfig`, the settings of one guest, and where its defaults come from.

Every field is something `run-vm` or `gen-vm` can be told. The command line
fills them in `cli`, and `settings.vm_overrides()` fills them from fleet
XML. The default images directory and package manifest depend on how
qemu-tool was installed, so they are worked out for each instance and not
when the module is imported.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .identity import AMD_CPU_VENDOR
from .ports import DEFAULT_SSH_PORT
from .resources import PACKAGES_D as _PACKAGES_D_PACKAGE, packaged_path

# Created by the .deb. A pipx or source install has neither, so both defaults
# below are resolved per-instance rather than at import time -- the answer
# depends on what is on disk, and that can differ between processes.
_INSTALLED_IMAGES = Path("/var/lib/qemu-tool/images")
_INSTALLED_PACKAGES = Path("/usr/share/qemu-tool/packages-default")


# The block sizes an emulated NVMe namespace may be given, logical and
# physical alike: --nvme-block-size and <vm-nvme-block-size>. The first is
# QEMU's default and the one that renders nothing.
NVME_BLOCK_SIZES: tuple[int, ...] = (512, 4096)
DEFAULT_NVME_BLOCK_SIZE = NVME_BLOCK_SIZES[0]


def nvme_block_size(raw: str, what: str) -> int:
    """RAW as one of NVME_BLOCK_SIZES; ValueError names WHAT and both.

    Exact on purpose: ASCII digits with white space round them and nothing
    else, so the generated schema can state the same form.
    """
    text = raw.strip()
    for size in NVME_BLOCK_SIZES:
        if text == str(size):
            return size
    allowed = " or ".join(str(size) for size in NVME_BLOCK_SIZES)
    raise ValueError(f"{what} must be {allowed}, got {raw!r}")


# <ernic-mtu>: the MTU of every emulated ernic NIC in a guest, and of the
# Ethernet path between guests when the fleet has one. 1500 is what the
# device comes up with. 9198 is the most the pinned rocm-ernic server
# advertises (its default profile's mtu_max; the guest's `ip link` refuses
# 9199 as "greater than device maximum").
DEFAULT_ERNIC_MTU = 1500
MAX_ERNIC_MTU = 9198
# What the guest's RoCE stack takes off a netdev MTU before it rounds down
# to an InfiniBand MTU. Measured on the pinned driver: 2143 gives 1024 and
# 2144 gives 2048; 4191 gives 2048 and 4192 gives 4096.
_ROCE_HEADER_BYTES = 96
_IB_MTUS = (4096, 2048, 1024, 512, 256)


def ernic_mtu(raw: str, what: str) -> int:
    """RAW as an MTU from DEFAULT_ERNIC_MTU to MAX_ERNIC_MTU.

    Exact on purpose, like nvme_block_size: four ASCII digits with white
    space round them and nothing else, so the generated schema can state
    the same form. ValueError names WHAT and the range.
    """
    text = raw.strip()
    if re.fullmatch(r"[1-9][0-9][0-9][0-9]", text):
        value = int(text)
        if DEFAULT_ERNIC_MTU <= value <= MAX_ERNIC_MTU:
            return value
    raise ValueError(
        f"{what} must be a number of bytes from {DEFAULT_ERNIC_MTU} to "
        f"{MAX_ERNIC_MTU}, got {raw!r}")


def rdma_active_mtu(netdev_mtu: int) -> int:
    """The RDMA active_mtu a guest reports for a NIC at NETDEV_MTU."""
    room = netdev_mtu - _ROCE_HEADER_BYTES
    return next((size for size in _IB_MTUS if size <= room), _IB_MTUS[-1])


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "share"


def default_images() -> Path:
    """Where to keep VM images when nothing overrides it.

    /var/lib/qemu-tool/images is the shared, kvm-group-writable location the
    .deb sets up. Only use it if it is actually writable: a rootless pipx
    install has no such directory, and a host whose postinst could not find a
    kvm group has one that root alone can write. Falling back to XDG keeps
    gen-vm working for an unprivileged user instead of failing on the default.
    """
    if os.access(_INSTALLED_IMAGES, os.W_OK):
        return _INSTALLED_IMAGES
    return _xdg_data_home() / "qemu-tool" / "images"


def default_packages() -> str:
    """Path to the default cloud-init package manifest.

    System copy first so an admin's edits to /usr/share/qemu-tool win, then
    the copy bundled in the wheel. Returns the system path even when absent,
    so the error gen-vm raises names the location a user would expect.
    """
    if _INSTALLED_PACKAGES.is_file():
        return str(_INSTALLED_PACKAGES)
    packaged = packaged_path(_PACKAGES_D_PACKAGE, "packages-default")
    if packaged is not None:
        return str(packaged)
    return str(_INSTALLED_PACKAGES)


@dataclass
class VMConfig:
    """The settings of one guest: what `run-vm` and `gen-vm` are told.

    The fields are in three groups, in this order: those both commands
    read, those of `run-vm` and those of `gen-vm`. `run_vm.run()` and
    `gen_vm.run()` each take one. `LibvirtXml` converts one to and from a
    libvirt domain file.
    """

    # ---- shared (run-vm + gen-vm) ----
    vm_name: str = "qemu-minimal"
    arch: str = "amd64"        # amd64 | arm64 | riscv64
    vcpus: int = 2
    vmem: int = 4096           # MiB
    images: Path = field(default_factory=default_images)
    ssh_port: int = DEFAULT_SSH_PORT
    # --ssh-port auto: ssh_port is where the search starts, and each QEMU
    # start gets a free port from run_vm.resolve_ssh_port.
    ssh_port_auto: bool = False
    # None means derive from ssh_port; see run_vm._mgmt_mac.
    mac: str | None = None
    kvm: bool = True
    qemu_path: str = ""

    # ---- run-vm ----
    filesystem: str | None = None
    # nvme: None=off, digit-str=count, negative-digit-str=null_blk, else literal args
    # Put each vfio-user device behind its own pcie-root-port instead of on
    # the root complex. ON by default, because off is not merely cosmetic.
    #
    # Without a root port the device is a conventional PCI endpoint with no
    # Express capability, and it gets a 32-bit prefetchable BAR. Measured on
    # the emulated rocjitsu GPU, same guest and image, only this changing:
    #
    #   off: Region 0: fd000000   (32-bit, prefetchable) [size=16M]
    #   on:  Region 0: c040000000 (64-bit, prefetchable) [size=256M]
    #        Capabilities: [40] Express (v2) Endpoint
    #
    # 16M is far too small for a VRAM aperture, so amdgpu's memory controller
    # cannot initialise and the probe unwinds with -22. The module still
    # loads, so lsmod looks fine and no KFD node appears -- and the symptom
    # surfaces three layers up as "hipSetDevice failed: no ROCm-capable
    # device is detected", which is a wrong diagnosis waiting to happen. It
    # cost exactly that once.
    #
    # The ionic path only loses its link speed ("0.000 Gb/s available PCIe
    # bandwidth (Unknown x255 link)"), and BAR-to-BAR DMA between two
    # vfio-user devices needs the real PCIe semantics either way.
    #
    # 256M is still short of what a gfx1250 aperture wants, so this is
    # necessary and not sufficient: the remaining limit is the rocjitsu
    # device model's BAR size, which is not ours.
    vfio_user_root_port: bool = True
    # Group vfio-user devices under PCIe switches, this many per switch, in
    # the order --vfio-userdev gives them. 0 is off. A real MI355X node pairs
    # each GPU with a NIC below a switch so their traffic never crosses the
    # root complex, and software that reads the PCIe tree -- ROCm topology
    # detection, P2P path selection -- decides differently on a flat tree.
    # Implies vfio_user_root_port: each switch needs one to hang off.
    vfio_user_switch_width: int = 0
    # Pin the guest's socket count. 0 leaves QEMU to infer it, which means
    # `-smp cpus=N` yields N single-core sockets -- so every guest this repo
    # has ever booted sees N EPYC sockets rather than one. Set to 1 to look
    # like a single-socket server.
    cpu_sockets: int = 0
    # Physical address bits for the EPYC CPU. 0 leaves QEMU's default (40 bits,
    # 1 TiB). Set to 41 when a large 64-bit PCI hole is needed (e.g. a 256 GiB
    # rocjitsu BAR); must be raised together with pci_hole64_size, since QEMU
    # refuses a hole larger than the phys-bits address space allows.
    cpu_phys_bits: int = 0
    # The CPUID vendor stated on -cpu, amd64 only. Empty states none, which
    # under KVM is the host's. A guest is an AMD machine unless very clearly
    # told otherwise: only --cpu-vendor and <vm-cpu-vendor> change this.
    cpu_vendor: str = AMD_CPU_VENDOR
    # Size of the 64-bit PCI hole, as a QEMU size string ("512G"). None leaves
    # the machine default, which does not fit eight emulated GPUs' BARs.
    #
    # Bounded by the CPU's physical address width, and the failure is a hard
    # refusal at startup, not a degraded boot:
    #   Address space limit 0xffffffffff < 0x1ffffffffff phys-bits too low (40)
    # The EPYC model gives 40 bits (1 TiB). Measured on QEMU 10.2.1: 512G is
    # accepted, 1T is not. Going above that needs cpu_phys_bits=41.
    pci_hole64_size: str | None = None
    nvme: str | None = None
    nvme_trace: str | None = None   # doorbell | all | event-name
    nvme_trace_file: Path | None = None
    nvme_lbaf_mask: str | None = None
    nvme_recreate: bool = False
    nvme_dbbuf_poll_ns: int | None = None   # None = do not emit; 0 = disable
    nvme_dbbuf_poll_eventidx: str | None = None  # "on" | "off" | None
    nvme_vram_dev: str | None = None  # qdev id of vfio-user device for P2P
    nvme_vram_bar: int | None = None  # BAR index for vfio-user P2P (None=0)
    # Logical and physical block size, in bytes, of every namespace run-vm
    # itself creates: one of NVME_BLOCK_SIZES. 512 is QEMU's default and
    # adds nothing to the command line. A literal --nvme string is not
    # touched.
    nvme_block_size: int = DEFAULT_NVME_BLOCK_SIZE
    pci_testdev: bool = False
    pci_hostdev: list[str] = field(default_factory=list)
    vram_dev_index: int | None = None  # 1-based index into pci_hostdev
    vram_bar: int = 0
    vfio_userdev: list[str] = field(default_factory=list)
    # Devices placed on plain CPU root ports regardless of switch_width.
    # Uses chassis 100+ to avoid colliding with switched-device chassis ids.
    pci_mmio_bridge: bool = False
    data_nic_queues: int = 0
    mcast_group: str | None = None
    # qmp_socket: None=off, "true"=auto path, else literal socket path
    qmp_socket: str | None = None
    qemu_guest_agent: bool = True
    backing_shared: bool = False
    extra_hostfwd: list[str] = field(default_factory=list)
    # Words appended to the QEMU command line as they are: --qemu-arg.
    qemu_args: list[str] = field(default_factory=list)
    dry_run: bool = False
    mgmt_tap: bool = False

    # ---- gen-vm ----
    size: int = 64
    release: str = "resolute"
    # None: gen-vm generates a keypair for the image it bakes. Never a path
    # under ~/.ssh by default -- an image's key can end up published.
    ssh_key_file: Path | None = None
    username: str = "ubuntu"
    user_id: int = 1000
    password: str = "password"
    # packages: path to manifest file, or None meaning no extra packages
    packages: str | None = field(default_factory=default_packages)
    force: bool = False
    compact: bool = True
    no_backing: bool = False
    restore_image: bool = False
    backing_file: Path | None = None
    # OCI reference for a published backing qcow2, pulled with oras. Same job
    # as the cloud-image download, different source.
    backing_image: str | None = None
    ansible_playbook: Path | None = None
    ca_cert_file: Path | None = None
    # Handed to the playbook as bake_cache_dir; None is a bake from source.
    cache_dir: Path | None = None
    # The cache in cache_dir was saved for other pins: save it again.
    cache_replace: bool = False
    ansible_only: bool = False
