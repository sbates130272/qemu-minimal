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

_ARCH_MACHINE = {
    "amd64": "q35",
    "arm64": "virt",
    "riscv64": "virt",
}

NVME_SIZE = "1024G"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def build_command(cfg: VMConfig, caps: QemuCaps) -> list[str]:
    cmd: list[str] = [qemu_binary(cfg)]
    cmd += _arch_args(cfg)
    cmd += _trace_args(cfg)
    cmd += ["-smp", f"cpus={cfg.vcpus}", "-m", str(cfg.vmem)]
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
    return cmd


def run(cfg: VMConfig, caps: QemuCaps) -> None:
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
                "-device", f"nvme,serial={name},drive=nvme-{i}",
            ]
        return args
    # literal string — split and pass through
    return shlex.split(cfg.nvme)


def _nvme_create(
    name: str, size: str, idx: int, cfg: VMConfig, caps: QemuCaps
) -> list[str]:
    img = Path(cfg.images) / f"{name}.qcow2"
    if cfg.nvme_recreate and img.exists():
        img.unlink()
    if not img.exists():
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(img), size],
            check=True, capture_output=True,
        )

    drv = (
        f"file={img},format=qcow2,if=none,id=nvme-{idx}"
        f",aio={caps.aio_mode},cache.direct=on"
        f",discard=unmap,detect-zeroes=unmap"
    )

    dev = f"nvme,serial={name},id=nvme-{idx}-dev"
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
    for j, sock in enumerate(cfg.vfio_userdev, start=1):
        if not cfg.dry_run and not Path(sock).is_socket():
            sys.exit(f"ERROR: Socket {sock} does not exist.")
        # No PCIe root port — add the device directly. rombar=0 suppresses
        # the ROM BAR that vfio-user-pci would otherwise advertise.
        dev_json = (
            f'{{"driver":"vfio-user-pci","rombar":0,'
            f'"socket":{{"path":"{sock}","type":"unix"}}}}'
        )
        args += ["-device", dev_json]
    return args


def _root_drive_args(cfg: VMConfig) -> list[str]:
    img = Path(cfg.images) / f"{cfg.vm_name}.qcow2"
    drv = f"if=virtio,format=qcow2,file={img}"
    if cfg.backing_shared:
        drv += ",file.locking=off,backing.file.locking=off"
    return ["-drive", drv]


def _netdev_args(cfg: VMConfig) -> list[str]:
    if cfg.mgmt_tap:
        tap = _mgmt_tap_name(cfg.ssh_port)
        mac = _mgmt_mac(cfg.ssh_port)
        return [
            "-netdev", f"tap,id=net0,ifname={tap},script=no,downscript=no",
            "-device", f"virtio-net-pci,netdev=net0,mac={mac}",
        ]
    hostfwd = f"hostfwd=tcp::{cfg.ssh_port}-:22"
    for rule in cfg.extra_hostfwd:
        hostfwd += f",hostfwd={rule}"
    return [
        "-netdev", f"user,id=net0,{hostfwd}",
        "-device", "virtio-net-pci,netdev=net0",
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
        f"virtio-net-pci,netdev=data0,mq=on,vectors={vectors}",
    ]


def _mcast_args(cfg: VMConfig) -> list[str]:
    if cfg.mcast_group is None:
        return []
    hi = cfg.ssh_port // 256
    lo = cfg.ssh_port % 256
    mac = f"52:54:00:00:{hi:02x}:{lo:02x}"
    return [
        "-netdev", f"socket,id=net1,mcast={cfg.mcast_group}",
        "-device", f"virtio-net-pci,netdev=net1,mac={mac}",
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
    result = subprocess.run(
        ["lspci", "-k", "-s", bdf], capture_output=True, text=True
    )
    if "vfio-pci" not in result.stdout:
        sys.exit(f"ERROR: Device {bdf} is not bound to vfio-pci driver.")
    sysbdf = _pci_sys_bdf(bdf)
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


def _mgmt_mac(port: int) -> str:
    hi = (port >> 8) & 0xFF
    lo = port & 0xFF
    return f"52:54:00:00:{hi:02x}:{lo:02x}"


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

    subprocess.run(
        ["sudo", "sysctl", "-qw", "net.ipv4.ip_forward=1"], check=True
    )

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
