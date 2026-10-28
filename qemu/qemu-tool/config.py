from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .resources import PACKAGES_D as _PACKAGES_D_PACKAGE, packaged_path

# Created by the .deb. A pipx or source install has neither, so both defaults
# below are resolved per-instance rather than at import time -- the answer
# depends on what is on disk, and that can differ between processes.
_INSTALLED_IMAGES = Path("/var/lib/qemu-tool/images")
_INSTALLED_PACKAGES = Path("/usr/share/qemu-tool/packages-default")


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
    # ---- shared (run-vm + gen-vm) ----
    vm_name: str = "qemu-minimal"
    arch: str = "amd64"        # amd64 | arm64 | riscv64
    vcpus: int = 2
    vmem: int = 4096           # MiB
    images: Path = field(default_factory=default_images)
    ssh_port: int = 2222
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
    # Size of the 64-bit PCI hole, as a QEMU size string ("512G"). None leaves
    # the machine default, which does not fit eight emulated GPUs' BARs.
    #
    # Bounded by the CPU's physical address width, and the failure is a hard
    # refusal at startup, not a degraded boot:
    #   Address space limit 0xffffffffff < 0x1ffffffffff phys-bits too low (40)
    # -cpu EPYC gives 40 bits (1 TiB). Measured on QEMU 10.2.1: 512G is
    # accepted, 1T is not. Going above that needs cpu_phys_bits=41.
    pci_hole64_size: str | None = None
    nvme: str | None = None
    nvme_trace: str | None = None   # doorbell | all | event-name
    nvme_trace_file: Path | None = None
    nvme_lbaf_mask: str | None = None
    nvme_recreate: bool = False
    pci_testdev: bool = False
    pci_hostdev: list[str] = field(default_factory=list)
    vram_dev_index: int | None = None  # 1-based index into pci_hostdev
    vram_bar: int = 0
    vfio_userdev: list[str] = field(default_factory=list)
    pci_mmio_bridge: bool = False
    data_nic_queues: int = 0
    mcast_group: str | None = None
    # qmp_socket: None=off, "true"=auto path, else literal socket path
    qmp_socket: str | None = None
    qemu_guest_agent: bool = True
    backing_shared: bool = False
    extra_hostfwd: list[str] = field(default_factory=list)
    dry_run: bool = False
    mgmt_tap: bool = False

    # ---- gen-vm ----
    size: int = 64
    release: str = "resolute"
    ssh_key_file: Path = field(default_factory=lambda: Path("~/.ssh/id_rsa.pub"))
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
    ansible_only: bool = False
