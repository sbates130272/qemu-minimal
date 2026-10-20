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

# Names a rendered stack directory for every compose command in this shell,
# so a fleet is selected once instead of on every invocation. Mirrors
# $QEMU_TOOL_ENV in envfile.find(); never consulted by gen-compose, where the
# output directory has to stay a typed decision.
_STACK_DIR_ENV = "QEMU_TOOL_STACK"

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


def _stack_dir(stack_dir: Path, source: str = "--stack-dir") -> Path:
    cdir = stack_dir.expanduser()
    compose_file = cdir / "docker-compose.yml"
    if not compose_file.is_file():
        sys.exit(
            f"Error: no docker-compose.yml in {source} {cdir}.\n"
            f"       Looked for: {compose_file}\n"
            "       Generate one with: qemu-tool gen-compose --output-dir "
            f"{cdir}"
        )
    return cdir


def _is_rendered_stack(cdir: Path) -> bool:
    """Is this directory one gen-compose rendered?

    The provenance header is the identity test, not the mere presence of a
    docker-compose.yml: without it, running qemu-tool from any unrelated
    docker project directory would adopt that project as "the stack".
    """
    from . import gen_compose

    compose_file = cdir / "docker-compose.yml"
    return compose_file.is_file() and gen_compose.recorded_digest(compose_file) is not None


def _discover_stack_dir(stack: str | None) -> Path | None:
    """The rendered stack to drive when no --stack-dir was passed, or None.

    $QEMU_TOOL_STACK first -- the same shape as $QEMU_TOOL_ENV in
    envfile.find(), and what lets a fleet be selected once per shell instead
    of on every command. Then the cwd, if it is itself a rendered stack, the
    way docker compose picks up the compose.yaml beside you.

    An identity test on one directory, never a walk-up. docker compose can
    walk up safely because what it finds is what it runs; here a walk-up from
    anywhere inside this checkout would reach qemu/compose/<default>/ and
    drive the committed 8-VM stack under the name of a 32-VM fleet, which is
    the silent substitution --stack-dir was added to prevent.
    """
    if stack is not None:
        # A named stack is a choice the user typed; nothing ambient outranks it.
        return None
    raw = os.environ.get(_STACK_DIR_ENV)
    if raw:
        return _stack_dir(Path(raw), source=f"${_STACK_DIR_ENV}")
    cwd = Path.cwd()
    if _is_rendered_stack(cwd):
        return cwd
    return None


def _resolve_recorded(name: str, cdir: Path) -> Path | None:
    """Locate one env layer named in a stack's provenance header.

    The header records the layers as typed, so a relative name's base is the
    directory gen-compose ran in -- which it does not record. Two bases are
    therefore tried, both of them things the reader can actually know: the
    stack directory, and the cwd (the committed stacks record
    "qemu/env/scale-out", which resolves from the repo root and nowhere else).
    Returns None rather than searching further: falling back to
    envfile.find() here is how a 32-VM YAML ends up driven by an ambient
    qemu/.env.
    """
    raw = Path(name).expanduser()
    candidates = [raw] if raw.is_absolute() else [cdir / raw, Path.cwd() / raw]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _remembered_layers(cdir: Path) -> list[Path]:
    """The env layers a rendered stack was built from, resolved to real files.

    This is the memory the fleet wrapper script existed to supply: the
    generator has always written the layer names into the header, and until
    now nothing but a test read them back.
    """
    from . import gen_compose

    compose_file = cdir / "docker-compose.yml"
    names = gen_compose.recorded_env_files(compose_file)
    if not names:
        sys.exit(
            f"Error: {compose_file} records no env files, so there is nothing "
            "to remember.\n"
            "       Pass --env-file, or regenerate the stack with: "
            f"qemu-tool gen-compose --output-dir {cdir} --env-file <file>"
        )
    resolved: list[Path] = []
    for name in names:
        found = _resolve_recorded(name, cdir)
        if found is None:
            looked = (
                str(Path(name).expanduser())
                if Path(name).expanduser().is_absolute()
                else f"{cdir / name} or {Path.cwd() / name}"
            )
            sys.exit(
                f"Error: {compose_file} was generated from {name}, which is "
                "not there now.\n"
                f"       Looked for: {looked}\n"
                "       Pass --env-file to name the layers explicitly."
            )
        resolved.append(found)
    return resolved


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
    # From the RESOLVED layers, not the raw command line. env_file is None
    # exactly when the layers were remembered from the header -- which is the
    # case this warning now fires in most -- so building the hint from the CLI
    # printed "gen-compose --output-dir <dir>" with no --env-file at all, and
    # running it verbatim fails for want of a settings file. A regenerate hint
    # that does not regenerate is worth less than no hint.
    arg = "".join(f" --env-file {p}" for p in settings) if settings else ""
    names = ", ".join(str(p) for p in settings)
    target = f" --stack {stack}" if stack is not None else f" --output-dir {cdir}"
    # Two different faults, worded differently. Content drift ("same files,
    # edited since") is routine and regenerating fixes it. Being handed
    # layers that are not the ones the YAML was rendered from is not drift at
    # all -- it is a fleet driven by someone else's settings -- and now that
    # the header's layer names are read back, it can be said outright.
    was = [str(_resolve_recorded(name, cdir) or name)
           for name in gen_compose.recorded_env_files(cdir / "docker-compose.yml")]
    if was and was != [str(p.resolve()) for p in settings]:
        print(
            f"Warning: {cdir / 'docker-compose.yml'} was rendered from "
            f"{', '.join(was)}; you passed {names}.\n"
            f"         Regenerate with: qemu-tool gen-compose{target}{arg}",
            file=sys.stderr,
        )
        return
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
        rendered = True
    else:
        discovered = _discover_stack_dir(stack)
        rendered = discovered is not None
        cdir = discovered if rendered else _compose_dir(stack or _DEFAULT_STACK)
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
    # Explicit flags win; otherwise a rendered stack names its own layers in
    # its provenance header, which is what makes `qemu-tool compose ps` work
    # with no arguments at all from inside a fleet directory. Only a rendered
    # stack remembers: for a named stack the search path is still the answer,
    # and resolving a committed stack's relative header from an arbitrary cwd
    # would be a guess. A --stack-dir with no provenance header at all was
    # not generated by us and claims no memory, so it keeps the old search
    # path; a header that records a digest but no layers does claim one, and
    # _remembered_layers refuses rather than silently reaching for qemu/.env.
    if env_file is None and rendered and _is_rendered_stack(cdir):
        settings = _remembered_layers(cdir)
    else:
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
        # A rendered tree is always checked, and one rendered from a different
        # version of the layers is exactly what driving a stack directory
        # makes easy to hit. Remembering the layers makes it easier still --
        # same names, edited content, is the common case -- so the digest is
        # re-hashed from the files' current bytes even when those files came
        # from the header that carries the digest.
        if rendered or (stack or _DEFAULT_STACK) in _GENERATED_STACKS:
            named = None if rendered else (stack or _DEFAULT_STACK)
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
