from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_STACKS = ("vfio-user-vm", "vfio-user-2vm")
_DEFAULT_STACK = "vfio-user-vm"

# Installed location (Debian package).
_INSTALLED_COMPOSE_ROOT = Path("/usr/share/qemu-tool/compose")

# Source-tree fallback: three levels up from this file is qemu/, one more is
# the repo root, then qemu/compose/<stack>/
_SOURCE_COMPOSE_ROOT = Path(__file__).parent.parent.parent / "qemu" / "compose"


def _compose_dir(stack: str) -> Path:
    installed = _INSTALLED_COMPOSE_ROOT / stack
    if installed.is_dir():
        return installed
    source = _SOURCE_COMPOSE_ROOT / stack
    if source.is_dir():
        return source
    raise FileNotFoundError(
        f"Compose stack '{stack}' not found at {installed} or {source}. "
        f"Available stacks: {', '.join(_STACKS)}"
    )


def run(
    vm_name: str,
    images_dir: Path,
    compose_args: list[str],
    stack: str = _DEFAULT_STACK,
) -> None:
    cdir = _compose_dir(stack)
    env = os.environ.copy()
    env["VM_NAME"] = vm_name
    env["VM1_NAME"] = vm_name
    env["VM_IMAGES_DIR"] = str(images_dir.resolve())
    cmd = ["docker", "compose", *compose_args]
    result = subprocess.run(cmd, cwd=cdir, env=env)
    sys.exit(result.returncode)
