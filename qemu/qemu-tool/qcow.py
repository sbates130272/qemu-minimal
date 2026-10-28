"""qemu-tool qcow — inspect and manage VM overlay qcow2 state.

Subcommand flags:
  --list          list all overlays under VM_IMAGES_DIR
  --state <vm>    show overlay info for one VM
  --clean <vm>    delete overlay and NVMe scratch, recreate from backing
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .list_vms import collect as _running_vms

# A freshly created qcow2 overlay has ~200 KiB of header/metadata but no
# data blocks. Anything at or below this threshold is treated as "0 B" delta.
_FRESH_QCOW2_BYTES = 256 * 1024


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

def _default_image_dirs() -> list[Path]:
    """Return all image directories to scan when --images is not given.

    Checks (in order, deduplicating):
      1. System-wide /var/lib/qemu-tool/images/
      2. User XDG ~/.local/share/qemu-tool/images/
      3. VM_IMAGES_DIR from any local env files found by the env-file search
    """
    from .config import _INSTALLED_IMAGES, _xdg_data_home  # type: ignore[attr-defined]
    from .envfile import load as _load_env  # type: ignore[attr-defined]
    seen: set[Path] = set()
    dirs: list[Path] = []

    def _add(p: Path) -> None:
        p = p.resolve()
        if p not in seen and p.is_dir():
            seen.add(p)
            dirs.append(p)

    _add(_INSTALLED_IMAGES)
    _add(_xdg_data_home() / "qemu-tool" / "images")

    # Shell environment variable takes priority for interactive use.
    env_var = os.environ.get("VM_IMAGES_DIR")
    if env_var:
        _add(Path(env_var))

    # Also pick up VM_IMAGES_DIR from any local env file the tool would load.
    try:
        env = _load_env([])
        images_from_env = env.get("VM_IMAGES_DIR")
        if images_from_env:
            _add(Path(images_from_env))
    except Exception:
        pass

    if not dirs:
        from .config import default_images  # type: ignore[attr-defined]
        _add(default_images())
    return dirs


def _age_days(path: Path) -> str:
    """Return how many days since path was last modified, e.g. '3d' or 'today'."""
    try:
        days = int((time.time() - path.stat().st_mtime) / 86400)
        return "today" if days == 0 else f"{days}d"
    except OSError:
        return "-"


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else "…" + s[-(n - 1):]


def list_overlays(images: Path | None, show_nvme: bool = False) -> None:
    if images is not None:
        dirs = [images]
    else:
        dirs = _default_image_dirs()

    all_overlays: list[tuple[Path, Path]] = []  # (images_dir, overlay_path)
    for d in dirs:
        for p in _all_overlays(d):
            all_overlays.append((d, p))

    if not all_overlays:
        print(f"No VM overlays found in: {', '.join(str(d) for d in dirs)}")
        return

    running = {v.get("name") for v in (_running_vms() or [])}

    hdr = (f"{'Name':<26} {'Path':<38} {'Virtual':>10} {'On Disk':>10} {'Delta':>10} "
           f"{'Age':>5} {'Backing Age':>11} {'Backing':<28} {'State'}")
    print(hdr)
    print("-" * len(hdr))

    prev_dir: Path | None = None
    for images_dir, p in all_overlays:
        if images_dir != prev_dir and len(dirs) > 1:
            print(f"\n  {images_dir}/")
            prev_dir = images_dir

        vm_name = p.stem
        info = _qemu_img_info(p)
        vsize = _fmt_bytes(info.get("virtual-size"))
        overlay_bytes = p.stat().st_blocks * 512 if p.exists() else 0
        dsize = _fmt_bytes(overlay_bytes)

        # Delta: the overlay's own on-disk footprint is the COW delta — it
        # only stores blocks that differ from the backing. A fresh overlay
        # has just qcow2 header/refcount metadata (~200 KiB); treat that as 0.
        # N/A when there is no backing file (standalone image, not an overlay).
        has_backing = bool(info.get("backing-filename"))
        if not has_backing:
            delta = "N/A"
        elif overlay_bytes <= _FRESH_QCOW2_BYTES:
            delta = "0 B"
        else:
            delta = _fmt_bytes(overlay_bytes)

        age_overlay = _age_days(p)
        backing_path = _backing_path(images_dir, vm_name)
        age_backing = _age_days(backing_path) if backing_path.exists() else "-"

        backing_file = info.get("backing-filename", "-")
        backing_file = _trunc(backing_file, 28) if backing_file != "-" else "-"

        dirty = " dirty" if info.get("dirty-flag") else ""
        status = "running" if vm_name in running else "stopped"
        path_str = _trunc(str(p), 38)
        print(f"{vm_name:<26} {path_str:<38} {vsize:>10} {dsize:>10} {delta:>10} "
              f"{age_overlay:>5} {age_backing:>11} {backing_file:<28} {status}{dirty}")

    if show_nvme:
        nvme_rows: list[tuple[Path, Path]] = []
        for images_dir, p in all_overlays:
            for np in _nvme_paths(images_dir, p.stem):
                if np.exists():
                    nvme_rows.append((images_dir, np))
        if nvme_rows:
            print(f"\n{'NVMe Images':<40} {'On Disk':>10} {'Age':>5}")
            print("-" * 60)
            for images_dir, np in nvme_rows:
                sz = _fmt_bytes(np.stat().st_blocks * 512)
                age = _age_days(np)
                print(f"{_trunc(np.name, 40):<40} {sz:>10} {age:>5}")
        else:
            print("\nNVMe Images: none")
    else:
        total = sum(
            np.stat().st_blocks * 512
            for images_dir, p in all_overlays
            for np in _nvme_paths(images_dir, p.stem)
            if np.exists()
        )
        if total:
            print(f"\nNVMe Images: {_fmt_bytes(total)} total  (use --nvme for details)")


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
