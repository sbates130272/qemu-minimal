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


def _stack_dir(stack_dir: Path) -> Path:
    cdir = stack_dir.expanduser()
    compose_file = cdir / "docker-compose.yml"
    if not compose_file.is_file():
        sys.exit(
            f"Error: no docker-compose.yml in --stack-dir {cdir}.\n"
            f"       Looked for: {compose_file}\n"
            "       Generate one with: qemu-tool gen-compose --output-dir "
            f"{cdir}"
        )
    return cdir


def _warn_if_stale(
    cdir: Path, settings: list[Path], stack: str | None, env_file: list[Path] | None
) -> None:
    """Warn, never block, when the generated YAML predates the env file.

    Blocking would be wrong: `down` against a stale tree is exactly how you
    recover from a bad edit, and `logs`/`ps` have to keep working too.
    """
    from . import gen_compose

    recorded = gen_compose.recorded_digest(cdir / "docker-compose.yml")
    if recorded is None:
        return
    # Against the digest of all the layers, not the first one: an overlay is
    # as much an input to the render as the golden file it sits on, so an
    # edit to either has to read as stale.
    if recorded == gen_compose.env_digest(settings):
        return
    arg = "".join(f" --env-file {p}" for p in env_file) if env_file else ""
    names = ", ".join(str(p) for p in settings)
    target = f" --stack {stack}" if stack is not None else f" --output-dir {cdir}"
    print(
        f"Warning: {cdir / 'docker-compose.yml'} was generated from a different "
        f"version of {names}.\n"
        f"         Regenerate with: qemu-tool gen-compose{target}{arg}",
        file=sys.stderr,
    )


def run(
    vm_name: str | None,
    images_dir: Path | None,
    compose_args: list[str],
    stack: str | None = None,
    vm2_name: str | None = None,
    env_file: list[Path] | None = None,
    stack_dir: Path | None = None,
) -> None:
    if stack_dir is not None and stack is not None:
        sys.exit(
            "Error: --stack-dir and --stack select the same thing two ways.\n"
            f"       Got --stack-dir {stack_dir} and --stack {stack}; pass one."
        )
    # A rendered stack is the only way to run an overlay fleet: VM_COUNT and
    # the VM names are baked into the generated YAML, not interpolated from
    # the env file, so without this the committed stack ran no matter which
    # layers were passed -- an 8-VM golden stack silently standing in for the
    # 32-VM one just generated.
    if stack_dir is not None:
        cdir = _stack_dir(stack_dir)
    else:
        cdir = _compose_dir(stack or _DEFAULT_STACK)
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
    if settings:
        # One --env-file per layer, in our order. This reads like it would be a
        # conflict, but docker compose takes the flag repeatedly and applies
        # last-wins across the files -- the same rule envfile.merge() uses, so
        # compose and qemu-tool see identical values. Verified on compose
        # v5.3.1 here; do not "simplify" this to a single file.
        for layer in settings:
            cmd += ["--env-file", str(layer.resolve())]
        # A --stack-dir is always a rendered tree, and one rendered from a
        # different version of the layers is exactly what that flag makes easy
        # to hit, so it is checked whatever stack it was rendered from.
        if stack_dir is not None or (stack or _DEFAULT_STACK) in _GENERATED_STACKS:
            named = None if stack_dir is not None else (stack or _DEFAULT_STACK)
            _warn_if_stale(cdir, settings, named, env_file)
    # Belt and braces: cli._split_compose_argv already drops a user-typed "--",
    # but docker compose treats everything after a "--" as positional, so a
    # leaked separator made `-- --profile lmcache up -d` bring up the default
    # services and report success -- the quiet failure, not a visible one.
    if compose_args and compose_args[0] == "--":
        compose_args = compose_args[1:]
    cmd += compose_args
    result = subprocess.run(cmd, cwd=cdir, env=env)
    sys.exit(result.returncode)
