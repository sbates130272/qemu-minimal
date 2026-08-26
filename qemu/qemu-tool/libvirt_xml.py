"""Bidirectional libvirt domain XML <-> VMConfig converter.

Standard libvirt elements cover the common fields. QEMU-specific features
that libvirt cannot natively represent (NVMe emulation, libvfio-user,
pci-mmio-bridge, NVME_TRACE, backing_shared, etc.) are stored as
<qemu:commandline> entries. Round-trip fidelity is maintained via
<!-- qemu-tool:KEY=VALUE --> hint comments placed immediately before
the <qemu:commandline> element; these are parsed back on ingestion.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .caps import QemuCaps
from .config import VMConfig

NS_QEMU = "http://libvirt.org/schemas/domain/qemu/1.0"
_Q = f"{{{NS_QEMU}}}"

ET.register_namespace("qemu", NS_QEMU)

_ARCH_TO_LIBVIRT: dict[str, tuple[str, str]] = {
    "amd64":   ("x86_64",  "q35"),
    "arm64":   ("aarch64", "virt"),
    "riscv64": ("riscv64", "virt"),
}
_LIBVIRT_TO_ARCH: dict[str, str] = {v[0]: k for k, v in _ARCH_TO_LIBVIRT.items()}


class LibvirtXml:

    # -------------------------------------------------------------------------
    # Parse: XML -> VMConfig
    # -------------------------------------------------------------------------

    @classmethod
    def parse(cls, source: str | Path) -> VMConfig:
        if str(source) == "-":
            import sys as _sys
            text = _sys.stdin.read()
        else:
            text = Path(str(source)).read_text()
        # ET.fromstring rejects an XML declaration that appears mid-string.
        # Find the start of <domain (skipping any leading preamble/comments)
        # and parse just from there so we get a clean fragment.
        start = text.find("<domain")
        if start == -1:
            # Fall back: try the whole thing (may work if caller gave bare XML)
            start = 0
        root = ET.fromstring(text[start:])

        hints = cls._parse_hints(root)
        cfg = VMConfig()

        # domain type -> kvm
        cfg.kvm = root.get("type", "").lower() == "kvm"

        # name
        name_el = root.find("name")
        if name_el is not None and name_el.text:
            cfg.vm_name = name_el.text.strip()

        # memory
        mem_el = root.find("memory")
        if mem_el is not None and mem_el.text:
            unit = mem_el.get("unit", "KiB").lower()
            val = int(mem_el.text.strip())
            cfg.vmem = _to_mib(val, unit)

        # vcpu
        vcpu_el = root.find("vcpu")
        if vcpu_el is not None and vcpu_el.text:
            cfg.vcpus = int(vcpu_el.text.strip())

        # arch from <os><type arch="...">
        os_el = root.find("os")
        if os_el is not None:
            type_el = os_el.find("type")
            if type_el is not None:
                libvirt_arch = type_el.get("arch", "x86_64")
                cfg.arch = _LIBVIRT_TO_ARCH.get(libvirt_arch, "amd64")

        # devices
        devices = root.find("devices")
        if devices is not None:
            cls._parse_devices(devices, cfg, hints)

        # qemu:commandline hints override where relevant
        cls._apply_hints(hints, cfg)

        return cfg

    @classmethod
    def _parse_hints(cls, root: ET.Element) -> dict[str, str]:
        """Extract <!-- qemu-tool:KEY=VALUE --> comments from the XML text."""
        hints: dict[str, str] = {}
        # ET doesn't expose comments, so we re-parse the raw text
        # using a regex over the serialised form of this element.
        raw = ET.tostring(root, encoding="unicode")
        for m in re.finditer(r"<!--\s*qemu-tool:(\w+)=(.+?)\s*-->", raw):
            hints[m.group(1)] = m.group(2).strip()
        return hints

    @classmethod
    def _parse_devices(
        cls, devices: ET.Element, cfg: VMConfig, hints: dict[str, str]
    ) -> None:
        # root disk -> images + vm_name
        for disk in devices.findall("disk"):
            target = disk.find("target")
            source = disk.find("source")
            if target is None or source is None:
                continue
            dev = target.get("dev", "")
            if dev in ("vda", "sda") or target.get("bus") == "virtio":
                file_path = source.get("file", "")
                if file_path:
                    p = Path(file_path)
                    # strip .qcow2 suffix and derive vm_name + images dir
                    stem = p.stem
                    cfg.images = p.parent
                    cfg.vm_name = stem
                break

        # interfaces
        for iface in devices.findall("interface"):
            itype = iface.get("type", "")
            if itype == "user":
                # SSH port forwarding
                for pf in iface.findall("portForwarding"):
                    if pf.get("guest") == "22":
                        try:
                            cfg.ssh_port = int(pf.get("host", "2222"))
                        except ValueError:
                            pass
                    else:
                        g = pf.get("guest", "")
                        h = pf.get("host", "")
                        proto = pf.get("proto", "tcp")
                        if g and h:
                            cfg.extra_hostfwd.append(f"{proto}::{h}-:{g}")
            elif itype == "ethernet":
                # TAP data NIC -> data_nic_queues
                driver = iface.find("driver")
                if driver is not None:
                    try:
                        cfg.data_nic_queues = int(driver.get("queues", "0"))
                    except ValueError:
                        pass

        # filesystem
        for fs in devices.findall("filesystem"):
            src = fs.find("source")
            if src is not None:
                cfg.filesystem = src.get("dir")
                break

        # hostdev -> pci_hostdev
        for hd in devices.findall("hostdev"):
            if hd.get("type") == "pci":
                src = hd.find("source")
                if src is not None:
                    addr = src.find("address")
                    if addr is not None:
                        bus  = int(addr.get("bus",  "0"), 16)
                        slot = int(addr.get("slot", "0"), 16)
                        func = int(addr.get("function", "0"), 16)
                        dom  = int(addr.get("domain", "0"), 16)
                        cfg.pci_hostdev.append(
                            f"{dom:04x}:{bus:02x}:{slot:02x}.{func}"
                        )

        # guest agent channel
        for ch in devices.findall("channel"):
            tgt = ch.find("target")
            if tgt is not None and tgt.get("name") == "org.qemu.guest_agent.0":
                cfg.qemu_guest_agent = True
                break
        else:
            if hints.get("qemu_guest_agent") == "false":
                cfg.qemu_guest_agent = False

    @classmethod
    def _apply_hints(cls, hints: dict[str, str], cfg: VMConfig) -> None:
        """Overwrite VMConfig fields from qemu-tool hint comments."""
        if "nvme_count" in hints:
            cfg.nvme = hints["nvme_count"]
        if "nvme_literal" in hints:
            cfg.nvme = hints["nvme_literal"]
        if "nvme_nullblk" in hints:
            cfg.nvme = hints["nvme_nullblk"]
        if "nvme_trace" in hints:
            cfg.nvme_trace = hints["nvme_trace"]
        if "nvme_trace_file" in hints:
            cfg.nvme_trace_file = Path(hints["nvme_trace_file"])
        if "nvme_lbaf_mask" in hints:
            cfg.nvme_lbaf_mask = hints["nvme_lbaf_mask"]
        if "nvme_recreate" in hints:
            cfg.nvme_recreate = hints["nvme_recreate"] == "true"
        if "pci_mmio_bridge" in hints:
            cfg.pci_mmio_bridge = hints["pci_mmio_bridge"] == "true"
        if "vram_dev_index" in hints:
            cfg.vram_dev_index = int(hints["vram_dev_index"])
        if "vram_bar" in hints:
            cfg.vram_bar = int(hints["vram_bar"])
        if "mcast_group" in hints:
            cfg.mcast_group = hints["mcast_group"]
        if "backing_shared" in hints:
            cfg.backing_shared = hints["backing_shared"] == "true"
        if "vfio_userdev" in hints:
            cfg.vfio_userdev = [s for s in hints["vfio_userdev"].split(",") if s]
        if "pci_testdev" in hints:
            cfg.pci_testdev = hints["pci_testdev"] == "true"
        if "qmp_socket" in hints:
            cfg.qmp_socket = hints["qmp_socket"]
        if "extra_hostfwd" in hints:
            cfg.extra_hostfwd = [s for s in hints["extra_hostfwd"].split(",") if s]

    # -------------------------------------------------------------------------
    # Emit: VMConfig -> XML string
    # -------------------------------------------------------------------------

    @classmethod
    def emit(cls, cfg: VMConfig, caps: QemuCaps | None = None) -> str:
        if caps is None:
            caps = QemuCaps()

        libvirt_arch, machine = _ARCH_TO_LIBVIRT[cfg.arch]
        domain_type = "kvm" if cfg.kvm else "qemu"

        root = ET.Element("domain")
        root.set("type", domain_type)

        _sub(root, "name").text = cfg.vm_name
        mem = _sub(root, "memory")
        mem.set("unit", "MiB")
        mem.text = str(cfg.vmem)
        cmem = _sub(root, "currentMemory")
        cmem.set("unit", "MiB")
        cmem.text = str(cfg.vmem)
        _sub(root, "vcpu").text = str(cfg.vcpus)

        cls._emit_os(root, cfg, libvirt_arch, machine)
        cls._emit_features(root, cfg)
        cls._emit_cpu(root, cfg)
        cls._emit_devices(root, cfg)
        cls._emit_qemu_commandline(root, cfg, caps)

        indent(root)
        xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        preamble = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!--\n"
            "  Generated by qemu-tool --convert-to-libvirt\n"
            f"  VM: {cfg.vm_name}  arch: {cfg.arch}\n"
            f"  Date: {now}\n"
            "\n"
            "  QEMU-specific features are encoded as <qemu:commandline> entries\n"
            "  and qemu-tool hint comments. Round-trip with:\n"
            f"    qemu-tool run-vm --domain {cfg.vm_name}.xml\n"
            "-->\n"
        )
        return preamble + xml_bytes + "\n"

    @classmethod
    def _emit_os(
        cls, root: ET.Element, cfg: VMConfig,
        libvirt_arch: str, machine: str
    ) -> None:
        os_el = _sub(root, "os")
        type_el = _sub(os_el, "type")
        type_el.set("arch", libvirt_arch)
        type_el.set("machine", machine)
        type_el.text = "hvm"
        if cfg.arch == "arm64":
            loader = _sub(os_el, "loader")
            loader.set("readonly", "yes")
            loader.set("type", "pflash")
            loader.text = "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"
        elif cfg.arch == "riscv64":
            _sub(os_el, "kernel").text = (
                "/usr/lib/u-boot/qemu-riscv64_smode/uboot.elf"
            )

    @classmethod
    def _emit_features(cls, root: ET.Element, cfg: VMConfig) -> None:
        if cfg.arch == "amd64":
            features = _sub(root, "features")
            _sub(features, "acpi")
            _sub(features, "apic")

    @classmethod
    def _emit_cpu(cls, root: ET.Element, cfg: VMConfig) -> None:
        if cfg.arch == "amd64":
            cpu = _sub(root, "cpu")
            cpu.set("mode", "host-passthrough")

    @classmethod
    def _emit_devices(cls, root: ET.Element, cfg: VMConfig) -> None:
        devices = _sub(root, "devices")

        # serial console
        serial = _sub(devices, "serial")
        serial.set("type", "pty")
        st = _sub(serial, "target")
        st.set("type", "isa-serial")
        st.set("port", "0")
        console = _sub(devices, "console")
        console.set("type", "pty")
        ct = _sub(console, "target")
        ct.set("type", "serial")
        ct.set("port", "0")

        # root disk (skip when backing_shared — goes to qemu:commandline)
        if not cfg.backing_shared:
            cls._emit_root_disk(devices, cfg)

        # SSH user-mode NIC
        cls._emit_user_nic(devices, cfg)

        # TAP data NIC
        if cfg.data_nic_queues > 0:
            cls._emit_tap_nic(devices, cfg)

        # VirtFS filesystem share
        if cfg.filesystem:
            cls._emit_filesystem(devices, cfg)

        # VFIO PCI passthrough
        for i, bdf in enumerate(cfg.pci_hostdev, start=1):
            cls._emit_hostdev(devices, bdf)

        # QEMU guest agent
        if cfg.arch == "amd64" and cfg.qemu_guest_agent:
            cls._emit_guest_agent(devices, cfg)

    @classmethod
    def _emit_root_disk(cls, devices: ET.Element, cfg: VMConfig) -> None:
        disk = _sub(devices, "disk")
        disk.set("type", "file")
        disk.set("device", "disk")
        driver = _sub(disk, "driver")
        driver.set("name", "qemu")
        driver.set("type", "qcow2")
        driver.set("cache", "none")
        driver.set("discard", "unmap")
        src = _sub(disk, "source")
        img = Path(cfg.images) / f"{cfg.vm_name}.qcow2"
        src.set("file", str(img))
        tgt = _sub(disk, "target")
        tgt.set("dev", "vda")
        tgt.set("bus", "virtio")
        boot = _sub(disk, "boot")
        boot.set("order", "1")

    @classmethod
    def _emit_user_nic(cls, devices: ET.Element, cfg: VMConfig) -> None:
        iface = _sub(devices, "interface")
        iface.set("type", "user")
        model = _sub(iface, "model")
        model.set("type", "virtio")
        pf = _sub(iface, "portForwarding")
        pf.set("proto", "tcp")
        pf.set("guest", "22")
        pf.set("host", str(cfg.ssh_port))
        for rule in cfg.extra_hostfwd:
            # rule: "tcp::9150-:9100" or "9150-:9100"
            m = re.match(
                r"^(?:([a-z]+)::)?(\d+)-:(\d+)$", rule
            )
            if m:
                proto = m.group(1) or "tcp"
                host_port = m.group(2)
                guest_port = m.group(3)
                extra_pf = _sub(iface, "portForwarding")
                extra_pf.set("proto", proto)
                extra_pf.set("guest", guest_port)
                extra_pf.set("host", host_port)

    @classmethod
    def _emit_tap_nic(cls, devices: ET.Element, cfg: VMConfig) -> None:
        iface = _sub(devices, "interface")
        iface.set("type", "ethernet")
        tgt = _sub(iface, "target")
        tgt.set("dev", f"dt{cfg.ssh_port}")
        model = _sub(iface, "model")
        model.set("type", "virtio")
        driver = _sub(iface, "driver")
        driver.set("queues", str(cfg.data_nic_queues))

    @classmethod
    def _emit_filesystem(cls, devices: ET.Element, cfg: VMConfig) -> None:
        fs = _sub(devices, "filesystem")
        fs.set("type", "mount")
        fs.set("accessmode", "passthrough")
        drv = _sub(fs, "driver")
        drv.set("type", "path")
        src = _sub(fs, "source")
        src.set("dir", cfg.filesystem)
        tgt = _sub(fs, "target")
        tgt.set("dir", "hostfs")

    @classmethod
    def _emit_hostdev(cls, devices: ET.Element, bdf: str) -> None:
        # bdf: DDDD:BB:SS.F or BB:SS.F
        if not re.match(r"^[0-9a-fA-F]{4}:", bdf):
            bdf = f"0000:{bdf}"
        parts = bdf.split(":")
        domain_hex = parts[0]
        bus_hex    = parts[1]
        slot_func  = parts[2].split(".")
        slot_hex   = slot_func[0]
        func_val   = slot_func[1] if len(slot_func) > 1 else "0"

        hd = _sub(devices, "hostdev")
        hd.set("mode", "subsystem")
        hd.set("type", "pci")
        hd.set("managed", "yes")
        src = _sub(hd, "source")
        addr = _sub(src, "address")
        addr.set("domain", f"0x{domain_hex}")
        addr.set("bus",    f"0x{bus_hex}")
        addr.set("slot",   f"0x{slot_hex}")
        addr.set("function", f"0x{func_val}")

    @classmethod
    def _emit_guest_agent(cls, devices: ET.Element, cfg: VMConfig) -> None:
        sock = f"/tmp/qga-{cfg.vm_name}-{cfg.ssh_port}.sock"
        ch = _sub(devices, "channel")
        ch.set("type", "unix")
        src = _sub(ch, "source")
        src.set("mode", "bind")
        src.set("path", sock)
        tgt = _sub(ch, "target")
        tgt.set("type", "virtio")
        tgt.set("name", "org.qemu.guest_agent.0")

    @classmethod
    def _emit_qemu_commandline(
        cls, root: ET.Element, cfg: VMConfig, caps: QemuCaps
    ) -> None:
        args: list[str] = []

        # backing_shared root disk (omitted from native <disk>)
        if cfg.backing_shared:
            img = Path(cfg.images) / f"{cfg.vm_name}.qcow2"
            args += [
                "-drive",
                f"if=virtio,format=qcow2,file={img}"
                ",file.locking=off,backing.file.locking=off",
            ]

        # NVMe
        if cfg.nvme is not None:
            import re as _re
            if _re.match(r"^[0-9]+$", cfg.nvme):
                count = int(cfg.nvme)
                for i in range(1, count + 1):
                    args += cls._nvme_qemu_args(
                        f"{cfg.vm_name}-nvme{i}", i, cfg, caps
                    )
            elif _re.match(r"^-[0-9]+$", cfg.nvme):
                count = abs(int(cfg.nvme))
                for i in range(count):
                    name = f"{cfg.vm_name}-nvme{i}"
                    args += [
                        "-drive",
                        f"file=/dev/nullb{i},format=raw,if=none,id=nvme-{i}",
                        "-device", f"nvme,serial={name},drive=nvme-{i}",
                    ]
            else:
                import shlex as _shlex
                args += _shlex.split(cfg.nvme)

        # NVMe trace
        if cfg.nvme_trace is not None:
            if cfg.nvme_trace == "doorbell":
                args += ["-trace", "enable=pci_nvme_mmio_doorbell_sq"]
                args += ["-trace", "enable=pci_nvme_mmio_doorbell_cq"]
            elif cfg.nvme_trace == "all":
                args += ["-trace", "enable=pci_nvme*"]
            else:
                args += ["-trace", f"enable={cfg.nvme_trace}"]
            if cfg.nvme_trace_file:
                args += ["-trace", f"file={cfg.nvme_trace_file}"]

        # pci-mmio-bridge
        if cfg.pci_mmio_bridge:
            args += [
                "-device",
                "pci-mmio-bridge,id=mmio-bridge,shadow-gpa=0x80000000,"
                "shadow-size=8192,poll-interval-ns=1000000,addr=8.0",
                "-trace", "enable=pci_mmio_*",
            ]

        # pci-testdev
        if cfg.pci_testdev:
            args += ["-device", "pci-testdev,membar=16G,membar-backed=true"]

        # libvfio-user devices
        if cfg.vfio_userdev:
            chassis_base = len(cfg.pci_hostdev)
            args += [
                "-object",
                f"memory-backend-memfd,id=mem-vfio-user,size={cfg.vmem}M,share=on",
                "-numa", "node,memdev=mem-vfio-user",
            ]
            for j, sock in enumerate(cfg.vfio_userdev, start=1):
                chassis = chassis_base + j
                args += [
                    "-device",
                    f"pcie-root-port,id=pcie-vfu.{j},chassis={chassis}",
                ]
                dev_json = (
                    f'{{"driver":"vfio-user-pci","bus":"pcie-vfu.{j}",'
                    f'"socket":{{"path":"{sock}","type":"unix"}}}}'
                )
                args += ["-device", dev_json]

        # mcast NIC
        if cfg.mcast_group:
            hi = cfg.ssh_port // 256
            lo = cfg.ssh_port % 256
            mac = f"52:54:00:00:{hi:02x}:{lo:02x}"
            args += [
                "-netdev", f"socket,id=net1,mcast={cfg.mcast_group}",
                "-device", f"virtio-net-pci,netdev=net1,mac={mac}",
            ]

        # QMP
        if cfg.qmp_socket is not None:
            if cfg.qmp_socket == "true":
                path = f"/tmp/qmp-{cfg.vm_name}-{cfg.ssh_port}.sock"
            else:
                path = cfg.qmp_socket
            args += ["-qmp", f"unix:{path},server,nowait"]

        if not args:
            return

        # Emit hint comments + <qemu:commandline>
        hints = cls._build_hints(cfg)
        cmdline = ET.SubElement(root, f"{_Q}commandline")
        # Inject hint comments as processing instructions — ET doesn't support
        # XML comments, so we post-process the serialised output. Instead, we
        # encode hints as a single <qemu:env> element with a special name.
        # Actually: store hints in a custom attribute of a wrapper element
        # then strip in post-processing. Simplest approach: emit a
        # qemu:arg with a sentinel value that the parser recognises.
        # Best approach compatible with ET: use a sub-element to carry hints.
        if hints:
            hint_el = ET.SubElement(cmdline, f"{_Q}env")
            hint_el.set("name", "__qemu_tool_hints__")
            hint_el.set("value", ";".join(f"{k}={v}" for k, v in hints.items()))

        for i in range(0, len(args), 2):
            arg_el = ET.SubElement(cmdline, f"{_Q}arg")
            arg_el.set("value", args[i])
            if i + 1 < len(args):
                arg_el2 = ET.SubElement(cmdline, f"{_Q}arg")
                arg_el2.set("value", args[i + 1])

    @classmethod
    def _nvme_qemu_args(
        cls, name: str, idx: int, cfg: VMConfig, caps: QemuCaps
    ) -> list[str]:
        from .run_vm import NVME_SIZE
        img = Path(cfg.images) / f"{name}.qcow2"
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
            dev += f",vram-dev=vfio-host{cfg.vram_dev_index},vram-bar={cfg.vram_bar}"
        ns = f"nvme-ns,drive=nvme-{idx},bus=nvme-{idx}-dev,nsid=1"
        if cfg.nvme_lbaf_mask and caps.has_lbaf_mask:
            ns += f",lbaf-mask={cfg.nvme_lbaf_mask.lower()}"
        return ["-drive", drv, "-device", dev, "-device", ns]

    @classmethod
    def _build_hints(cls, cfg: VMConfig) -> dict[str, str]:
        hints: dict[str, str] = {}
        if cfg.nvme is not None:
            import re as _re
            if _re.match(r"^[0-9]+$", cfg.nvme):
                hints["nvme_count"] = cfg.nvme
            elif _re.match(r"^-[0-9]+$", cfg.nvme):
                hints["nvme_nullblk"] = cfg.nvme
            else:
                hints["nvme_literal"] = cfg.nvme
        if cfg.nvme_trace is not None:
            hints["nvme_trace"] = cfg.nvme_trace
        if cfg.nvme_trace_file is not None:
            hints["nvme_trace_file"] = str(cfg.nvme_trace_file)
        if cfg.nvme_lbaf_mask is not None:
            hints["nvme_lbaf_mask"] = cfg.nvme_lbaf_mask
        if cfg.nvme_recreate:
            hints["nvme_recreate"] = "true"
        if cfg.pci_mmio_bridge:
            hints["pci_mmio_bridge"] = "true"
        if cfg.vram_dev_index is not None:
            hints["vram_dev_index"] = str(cfg.vram_dev_index)
            hints["vram_bar"] = str(cfg.vram_bar)
        if cfg.mcast_group:
            hints["mcast_group"] = cfg.mcast_group
        if cfg.backing_shared:
            hints["backing_shared"] = "true"
        if cfg.vfio_userdev:
            hints["vfio_userdev"] = ",".join(cfg.vfio_userdev)
        if cfg.pci_testdev:
            hints["pci_testdev"] = "true"
        if cfg.qmp_socket is not None:
            hints["qmp_socket"] = cfg.qmp_socket
        if not cfg.qemu_guest_agent:
            hints["qemu_guest_agent"] = "false"
        if cfg.extra_hostfwd:
            hints["extra_hostfwd"] = ",".join(cfg.extra_hostfwd)
        return hints


# -------------------------------------------------------------------------
# Parse the hints-in-env-element format back out
# -------------------------------------------------------------------------

def _parse_env_hints(root: ET.Element) -> dict[str, str]:
    """Read hints stored in <qemu:env name='__qemu_tool_hints__' value='...'/>."""
    hints: dict[str, str] = {}
    cmdline = root.find(f"{_Q}commandline")
    if cmdline is None:
        return hints
    for env in cmdline.findall(f"{_Q}env"):
        if env.get("name") == "__qemu_tool_hints__":
            for pair in env.get("value", "").split(";"):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    hints[k.strip()] = v.strip()
    return hints


# Override _parse_hints to also check the env element approach
_orig_parse_hints = LibvirtXml._parse_hints.__func__


@classmethod  # type: ignore[misc]
def _parse_hints_combined(cls, root: ET.Element) -> dict[str, str]:
    hints = _orig_parse_hints(cls, root)
    hints.update(_parse_env_hints(root))
    return hints


LibvirtXml._parse_hints = _parse_hints_combined  # type: ignore[method-assign]


# -------------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------------

def _sub(parent: ET.Element, tag: str) -> ET.Element:
    return ET.SubElement(parent, tag)


def _to_mib(val: int, unit: str) -> int:
    unit = unit.lower()
    if unit in ("mib", "m"):
        return val
    if unit in ("kib", "k", "kb"):
        return val // 1024
    if unit in ("gib", "g", "gb"):
        return val * 1024
    return val  # assume MiB


def indent(elem: ET.Element, level: int = 0) -> None:
    """Add pretty-print indentation in place (Python 3.9+ has ET.indent)."""
    try:
        ET.indent(elem, space="  ")
    except AttributeError:
        _indent_fallback(elem, level)


def _indent_fallback(elem: ET.Element, level: int) -> None:
    i = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
        for child in elem:
            _indent_fallback(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i
