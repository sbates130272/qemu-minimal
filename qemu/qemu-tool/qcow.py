"""qemu-tool qcow — inspect and manage VM overlay qcow2 state.

Subcommand flags:
  --list          list all overlays under VM_IMAGES_DIR
  --state <vm>    show overlay info for one VM
  --clean <vm>    delete overlay and NVMe scratch, recreate from backing
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .list_vms import collect as _running_vms


# ---------------------------------------------------------------------------
# Internal helpers

def _qemu_img_info(path: Path) -> dict:
    """Return parsed qemu-img info JSON for path, or {} on error."""
    try:
        r = subprocess.run(
            ["qemu-img", "info", "--output=json", "-U", str(path)],
            capture_output=True, text=True, check=True,
        )
        return json.loads(r.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return {}


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    for unit, divisor in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= divisor:
            return f"{n / divisor:.1f} {unit}"
    return f"{n} B"


def _is_running(vm_name: str) -> bool:
    try:
        vms = _running_vms()
        return any(v.get("name") == vm_name for v in vms)
    except Exception:
        return False


def _overlay_path(images: Path, vm_name: str) -> Path:
    return images / f"{vm_name}.qcow2"


def _backing_path(images: Path, vm_name: str) -> Path:
    return images / f"{vm_name}-backing.qcow2"


def _nvme_paths(images: Path, vm_name: str) -> list[Path]:
    return sorted(images.glob(f"{vm_name}-nvme*.qcow2"))


def _all_overlays(images: Path) -> list[Path]:
    """Return overlay qcow2s: exclude *-backing.qcow2 and *-nvme*.qcow2."""
    return sorted(
        p for p in images.glob("*.qcow2")
        if not p.name.endswith("-backing.qcow2")
        and "-nvme" not in p.name
    )


# ---------------------------------------------------------------------------
# Public commands

def list_overlays(images: Path) -> None:
    overlays = _all_overlays(images)
    if not overlays:
        print(f"No VM overlays found in {images}")
        return

    running = {v.get("name") for v in (_running_vms() or [])}

    header = f"{'Name':<28} {'Path':<45} {'Virtual':>10} {'On Disk':>10} {'Delta':>10} {'State'}"
    print(header)
    print("-" * len(header))

    for p in overlays:
        vm_name = p.stem
        info = _qemu_img_info(p)
        vsize = _fmt_bytes(info.get("virtual-size"))
        overlay_bytes = p.stat().st_blocks * 512 if p.exists() else 0
        dsize = _fmt_bytes(overlay_bytes)

        # Delta: how much the overlay has grown beyond a fresh state.
        # Positive = written N bytes on top of the backing; "fresh" = barely touched.
        backing_path = _backing_path(images, vm_name)
        if backing_path.exists() and overlay_bytes:
            backing_bytes = backing_path.stat().st_blocks * 512
            diff = overlay_bytes - backing_bytes
            delta = _fmt_bytes(diff) if diff > 0 else "fresh"
        else:
            delta = "-"

        dirty = " dirty" if info.get("dirty-flag") else ""
        status = "running" if vm_name in running else "stopped"
        path_str = str(p)
        if len(path_str) > 44:
            path_str = "…" + path_str[-43:]
        print(f"{vm_name:<28} {path_str:<45} {vsize:>10} {dsize:>10} {delta:>10} {status}{dirty}")

    nvme_total = sum(
        p.stat().st_blocks * 512
        for vm in [o.stem for o in overlays]
        for p in _nvme_paths(images, vm)
        if p.exists()
    )
    if nvme_total:
        print(f"\nNVMe scratch: {_fmt_bytes(nvme_total)} across all VMs")


def state(images: Path, vm_name: str) -> None:
    overlay = _overlay_path(images, vm_name)
    if not overlay.exists():
        sys.exit(f"Error: overlay not found: {overlay}")

    info = _qemu_img_info(overlay)
    backing = info.get("backing-filename", "-")
    vsize = _fmt_bytes(info.get("virtual-size"))
    dsize = _fmt_bytes(overlay.stat().st_blocks * 512)
    dirty = info.get("dirty-flag", False)
    running = _is_running(vm_name)

    print(f"VM:       {vm_name}")
    print(f"Overlay:  {overlay}  ({vsize} virtual, {dsize} on disk)")
    print(f"Backing:  {backing}")
    print(f"Dirty:    {'yes' if dirty else 'no'}")
    print(f"Status:   {'running' if running else 'stopped'}")

    nvmes = _nvme_paths(images, vm_name)
    if nvmes:
        print("NVMe:")
        for p in nvmes:
            print(f"  {p.name}  ({_fmt_bytes(p.stat().st_blocks * 512)} on disk)")
    else:
        print("NVMe:     none")


def clean(images: Path, vm_name: str, yes: bool = False) -> None:
    overlay = _overlay_path(images, vm_name)
    backing = _backing_path(images, vm_name)
    nvmes = _nvme_paths(images, vm_name)

    if _is_running(vm_name):
        sys.exit(f"Error: {vm_name} is running; stop it before cleaning.")

    targets = []
    if overlay.exists():
        info = _qemu_img_info(overlay)
        dsize = _fmt_bytes(overlay.stat().st_blocks * 512)
        targets.append(f"  {overlay.name}  ({dsize} on disk)")
    for p in nvmes:
        targets.append(f"  {p.name}  ({_fmt_bytes(p.stat().st_blocks * 512)} on disk)")

    if not targets:
        print(f"Nothing to clean for {vm_name}.")
        return

    print(f"Will delete:")
    for t in targets:
        print(t)
    if backing.exists():
        print(f"Will recreate overlay from: {backing.name}")
    else:
        print("(No backing image found — overlay will not be recreated)")

    if not yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            print("Aborted.")
            return

    if overlay.exists():
        overlay.unlink()
        print(f"Deleted {overlay.name}")

    for p in nvmes:
        p.unlink()
        print(f"Deleted {p.name}")

    if backing.exists():
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2",
             "-b", str(backing), "-F", "qcow2", str(overlay)],
            check=True,
        )
        print(f"Created fresh overlay {overlay.name} from {backing.name}")
    else:
        print(f"Warning: backing image not found — overlay not recreated.")
