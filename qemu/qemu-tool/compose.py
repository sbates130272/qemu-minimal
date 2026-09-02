from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_COMPOSE_STACK = "vfio-user-vm"

# Installed location (Debian package).
_INSTALLED_COMPOSE_DIR = Path("/usr/share/qemu-tool/compose") / _COMPOSE_STACK

# Source-tree fallback: three levels up from this file is the repo root,
# then qemu/compose/<stack>/
_SOURCE_COMPOSE_DIR = (
    Path(__file__).parent.parent.parent / "qemu" / "compose" / _COMPOSE_STACK
)


def _compose_dir() -> Path:
    if _INSTALLED_COMPOSE_DIR.is_dir():
        return _INSTALLED_COMPOSE_DIR
    if _SOURCE_COMPOSE_DIR.is_dir():
        return _SOURCE_COMPOSE_DIR
    raise FileNotFoundError(
        f"Compose directory not found at {_INSTALLED_COMPOSE_DIR} "
        f"or {_SOURCE_COMPOSE_DIR}"
    )


def run(vm_name: str, images_dir: Path, compose_args: list[str]) -> None:
    cdir = _compose_dir()
    env = os.environ.copy()
    env["VM_NAME"] = vm_name
    env["VM_IMAGES_DIR"] = str(images_dir.resolve())
    cmd = ["docker", "compose", *compose_args]
    result = subprocess.run(cmd, cwd=cdir, env=env)
    sys.exit(result.returncode)
