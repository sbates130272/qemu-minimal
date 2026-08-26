from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import VMConfig

_ARCH_MAP = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "riscv64": "riscv64",
}


def qemu_binary(cfg: VMConfig) -> str:
    qarch = _ARCH_MAP[cfg.arch]
    prefix = cfg.qemu_path.rstrip("/")
    if prefix:
        return f"{prefix}/qemu-system-{qarch}"
    return f"qemu-system-{qarch}"


@dataclass
class QemuCaps:
    has_ioeventfd: bool = False
    has_dbcs: bool = False
    has_vram_dev: bool = False
    has_lbaf_mask: bool = False
    has_pci_mmio_bridge: bool = False
    aio_mode: str = "threads"   # io_uring | native | threads
    probed: bool = False


def probe_caps(cfg: VMConfig) -> QemuCaps:
    caps = QemuCaps()
    binary = qemu_binary(cfg)

    if not shutil.which(binary) and not Path(binary).is_file():
        if cfg.dry_run:
            return caps
        sys.exit(
            f"Error: QEMU binary '{binary}' not found.\n"
            f"Set --qemu-path to the directory containing it."
        )

    caps.probed = True

    out = _run(binary, "-device", "help")
    caps.has_pci_mmio_bridge = 'name "pci-mmio-bridge"' in out

    nvme_help = _run(binary, "-device", "nvme,help")
    caps.has_ioeventfd = "ioeventfd=" in nvme_help
    caps.has_dbcs = "dbcs=" in nvme_help
    caps.has_vram_dev = "vram-dev=" in nvme_help

    ns_help = _run(binary, "-device", "nvme-ns,help")
    caps.has_lbaf_mask = "lbaf-mask=" in ns_help

    caps.aio_mode = _probe_aio(binary)
    return caps


def _probe_aio(binary: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".qcow2", delete=False) as f:
        probe_img = Path(f.name)
    try:
        result = subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(probe_img), "1M"],
            capture_output=True,
        )
        if result.returncode != 0:
            return "threads"
        for aio in ("io_uring", "native", "threads"):
            try:
                r = subprocess.run(
                    [
                        binary, "-machine", "none", "-monitor", "stdio",
                        "-drive",
                        f"file={probe_img},format=qcow2,if=none,id=t,aio={aio}",
                    ],
                    input=b"quit\n",
                    capture_output=True,
                    timeout=2,
                )
                if r.returncode == 0:
                    return aio
            except subprocess.TimeoutExpired:
                continue
    except FileNotFoundError:
        pass
    finally:
        probe_img.unlink(missing_ok=True)
    return "threads"


def _run(*args: str) -> str:
    result = subprocess.run(list(args), capture_output=True, text=True)
    return result.stdout + result.stderr
