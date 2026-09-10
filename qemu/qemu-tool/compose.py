from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_STACKS = (
    "vfio-user-ernic-vm",
    "vfio-user-rocjitsu-vm",
    "vfio-user-ernic-rocjitsu-vm",
    "vfio-user-ernic-2vm",
)
_DEFAULT_STACK = "vfio-user-ernic-rocjitsu-vm"

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
    vm_name: str | None,
    images_dir: Path | None,
    compose_args: list[str],
    stack: str = _DEFAULT_STACK,
    vm2_name: str | None = None,
) -> None:
    cdir = _compose_dir(stack)
    env = os.environ.copy()
    if vm_name is not None:
        env["VM_NAME"] = vm_name
        env["VM1_NAME"] = vm_name
    if vm2_name is not None:
        env["VM2_NAME"] = vm2_name
    if images_dir is not None:
        env["VM_IMAGES_DIR"] = str(images_dir.resolve())
    cmd = ["docker", "compose"]
    caller_env = Path.cwd() / ".env"
    if caller_env.exists():
        cmd += ["--env-file", str(caller_env)]
    cmd += compose_args
    result = subprocess.run(cmd, cwd=cdir, env=env)
    sys.exit(result.returncode)
