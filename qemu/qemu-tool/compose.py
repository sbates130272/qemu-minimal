from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .envfile import find as find_env_file
from .resources import COMPOSE as _COMPOSE_PACKAGE, packaged_path

_STACKS = (
    "vfio-user-ernic-vm",
    "vfio-user-rocjitsu-vm",
    "vfio-user-ernic-rocjitsu-vm",
    "vfio-user-ernic-2vm",
    "vfio-user-ernic-rocjitsu-scale-out",
)
_DEFAULT_STACK = "vfio-user-ernic-rocjitsu-vm"

# Stacks whose docker-compose.yml is produced by `qemu-tool gen-compose` rather
# than hand-written, and so can fall out of step with the env file.
_GENERATED_STACKS = frozenset({"vfio-user-ernic-rocjitsu-scale-out"})

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
    # Last resort: the copy inside the wheel, which is all a pipx install has.
    packaged = packaged_path(_COMPOSE_PACKAGE, stack)
    if packaged is not None and packaged.is_dir():
        return packaged
    raise FileNotFoundError(
        f"Compose stack '{stack}' not found at {installed} or {source}, "
        f"and not bundled with this install. "
        f"Available stacks: {', '.join(_STACKS)}"
    )


def _warn_if_stale(
    cdir: Path, settings: Path, stack: str, env_file: Path | None
) -> None:
    """Warn, never block, when the generated YAML predates the env file.

    Blocking would be wrong: `down` against a stale tree is exactly how you
    recover from a bad edit, and `logs`/`ps` have to keep working too.
    """
    from . import gen_compose

    recorded = gen_compose.recorded_digest(cdir / "docker-compose.yml")
    if recorded is None:
        return
    if recorded == gen_compose.env_digest(settings):
        return
    arg = f" --env-file {env_file}" if env_file is not None else ""
    print(
        f"Warning: {cdir / 'docker-compose.yml'} was generated from a different "
        f"version of {settings}.\n"
        f"         Regenerate with: qemu-tool gen-compose --stack {stack}{arg}",
        file=sys.stderr,
    )


def run(
    vm_name: str | None,
    images_dir: Path | None,
    compose_args: list[str],
    stack: str = _DEFAULT_STACK,
    vm2_name: str | None = None,
    env_file: Path | None = None,
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
    # Same search path gen-vm and run-vm use, so one file drives all three.
    # Without --env-file docker compose would read <stack dir>/.env instead,
    # which is not where the settings live any more.
    settings = find_env_file(env_file)
    if settings is not None:
        cmd += ["--env-file", str(settings.resolve())]
        if stack in _GENERATED_STACKS:
            _warn_if_stale(cdir, settings, stack, env_file)
    # argparse.REMAINDER KEEPS the "--" separator in the list, and docker
    # compose treats everything after a "--" as positional arguments. So
    # `qemu-tool compose -- --profile lmcache up -d` forwarded a literal "--"
    # and docker silently ignored the profile, bringing up the default
    # services and reporting success. Without the "--", argparse rejects the
    # call outright -- so before this, there was no way to pass a profile
    # through at all, and the failure that mattered was the quiet one.
    if compose_args and compose_args[0] == "--":
        compose_args = compose_args[1:]
    cmd += compose_args
    result = subprocess.run(cmd, cwd=cdir, env=env)
    sys.exit(result.returncode)
