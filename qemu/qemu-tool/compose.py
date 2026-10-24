from __future__ import annotations

import grp
import os
import re
import subprocess
import sys
from pathlib import Path

from .envfile import find as find_env_file, parse_layers as parse_env_layers
from .resources import COMPOSE as _COMPOSE_PACKAGE, packaged_path

_STACKS = (
    "vfio-user-ernic-vm",
    "vfio-user-rocjitsu-vm",
    "vfio-user-ernic-rocjitsu-vm",
    "vfio-user-ernic-2vm",
    "vfio-user-ernic-rocjitsu-scale-out",
    "vfio-user-mi355x-node",
)
_DEFAULT_STACK = "vfio-user-ernic-rocjitsu-vm"

# Stacks whose docker-compose.yml is produced by `qemu-tool gen-compose` rather
# than hand-written, and so can fall out of step with the env file.
_GENERATED_STACKS = frozenset({
    "vfio-user-ernic-rocjitsu-scale-out",
    "vfio-user-mi355x-node",
})

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


# Directories a stack bind-mounts and writes into. Each one belongs to
# whoever ran the fleet, and each is created by the Docker daemon -- as root,
# 0755 -- if it does not exist when the service starts. Creating them here
# first is what makes that moot. Keyed by the settings key that names the path.
_DATA_DIR_KEYS = ("PROM_DATA_DIR", "LOKI_DATA_DIR", "GRAFANA_DATA_DIR")

# Subcommands that actually start containers. `ps`, `logs` and `down` must
# keep working against a stack whose artifacts are in any state at all --
# `down` especially, since recovering from a bad run is exactly when the
# checks below would otherwise be in the way.
_STARTING = frozenset({"up", "start", "run", "create"})


def _caller_identity() -> dict[str, str]:
    """CONTAINER_USER and CONTAINER_KVM_GID for whoever ran this command.

    The whole point of the setting is that the containers run as the person
    who started them, and the overwhelmingly common answer to "which uid" is
    "mine". Requiring that to be typed into an env file made root the
    practical default for years, because the value cannot be committed (it
    differs per host) and so there was nowhere obvious to put it.

    getuid()/getgid(), not getlogin() or $USER: those answer "who owns the
    terminal", which is a different question the moment this runs under sudo,
    a systemd unit or CI.
    """
    ident = {"CONTAINER_USER": f"{os.getuid()}:{os.getgid()}"}
    # The gid that owns /dev/kvm, asked of the device itself first. The group
    # is conventionally called "kvm" and conventionally is the owner, but the
    # question the qemu container actually needs answered is "which gid opens
    # this node" -- and a host can have the group without the device, or the
    # device without that name. The name is the fallback, not the source.
    try:
        ident["CONTAINER_KVM_GID"] = str(os.stat("/dev/kvm").st_gid)
    except OSError:
        try:
            ident["CONTAINER_KVM_GID"] = str(grp.getgrnam("kvm").gr_gid)
        except KeyError:
            # Neither. Left unset, so compose refuses rather than starting a
            # fleet that silently falls back to TCG and runs ~20x slower --
            # which presents as "the guests are hanging", not as a missing
            # group. A host with no /dev/kvm cannot run this stack anyway.
            pass
    return ident


# Same shape gen_compose enforces on an env file, applied here to whatever
# value is actually in force -- which may have come from the environment and
# so have been seen by neither the generator nor the settings file.
_UID_GID_RE = re.compile(r"[0-9]+:[0-9]+")


def _reject_bad_user(value: str, source: str) -> None:
    """Refuse a CONTAINER_USER that cannot work, naming where it came from.

    Every way of getting this wrong fails a long way from the thing that set
    it, and one of them does not fail at all:

    * A name ("stebates"). Docker resolves it against the IMAGE's /etc/passwd,
      which carries no entry for your uid, and dies at container create with
      "unable to find user".
    * A bare uid ("1000"). Docker then wants the gid from that uid's passwd
      entry, which does not exist either.
    * "${UID}:${GID}", the spelling people try first and the worst of the
      three. Bash exports neither, so it survives as literal text, compose
      substitutes empty for both, and `user: ":"` runs as root without
      erroring.

    gen_compose checks the env file at render time. This checks the value the
    containers will actually get, which is not the same thing: a stack is
    rendered once and run many times, and the environment outranks the file.
    """
    if _UID_GID_RE.fullmatch(value.strip()):
        return
    sys.exit(
        f"Error: CONTAINER_USER must be numeric <uid>:<gid>, got {value!r}\n"
        f"       (from {source}).\n"
        "       A user name does not resolve: the images carry no passwd "
        "entry for your uid.\n"
        "       ${UID}:${GID} does not work either -- bash exports neither, "
        "so it silently becomes root.\n"
        "       Use the literal ids, e.g. CONTAINER_USER=$(id -u):$(id -g) "
        "evaluated by your shell,\n"
        "       or unset it and qemu-tool will fill in your own."
    )


def _declared_in_layers(settings: list[Path]) -> dict[str, str]:
    if not settings:
        return {}
    try:
        return {k: v for k, v in parse_env_layers(settings).items() if v.strip()}
    except SystemExit:
        raise
    except Exception:
        # A layer this tool cannot parse is not a reason to refuse to run
        # docker compose, which may well parse it fine.
        return {}


def resolve_identity(settings: list[Path]) -> tuple[dict[str, str], str]:
    """The uid:gid the containers will really run as, and where it came from.

    Precedence matches what compose itself does with the same keys, or the
    tool and the containers disagree about the value -- the failure mode this
    stack's repeated --env-file handling exists to avoid. Compose resolves a
    variable from the process environment first and the --env-file layers
    second, so anything set in either is already a decision.

    Returns the full pair plus a human-readable source for CONTAINER_USER, so
    a caller can say which of the three answers it got rather than just
    printing a number.
    """
    declared = _declared_in_layers(settings)
    ident = dict(_caller_identity())
    source = "this user"
    for key in ("CONTAINER_USER", "CONTAINER_KVM_GID"):
        if os.environ.get(key, "").strip():
            ident[key] = os.environ[key]
            if key == "CONTAINER_USER":
                source = "the environment"
        elif declared.get(key):
            ident[key] = declared[key]
            if key == "CONTAINER_USER":
                names = ", ".join(str(p) for p in settings)
                source = names
    _reject_bad_user(ident["CONTAINER_USER"], source)
    return ident, source


def _apply_identity(env: dict[str, str], settings: list[Path]) -> None:
    """Fill in CONTAINER_USER/CONTAINER_KVM_GID, without overriding a choice.

    Only a key nobody has expressed an opinion about is filled in; the rest is
    left for compose to resolve exactly as it would have. resolve_identity
    decides what the answer is and refuses an unusable one -- this only
    decides whether to write it down.

    Writing a layer's own value back into the environment would reach the same
    containers, since compose resolves to the same string either way. It is
    still wrong: the environment outranks the layers, so doing it converts a
    value the operator can override by editing a file into one they cannot.
    """
    ident, _ = resolve_identity(settings)
    declared = _declared_in_layers(settings)
    for key, value in ident.items():
        if os.environ.get(key, "").strip() or declared.get(key):
            continue
        env[key] = value


def _warn_about_root_artifacts(env: dict[str, str]) -> None:
    """Name the files an earlier root-era run left behind, before they bite.

    This is the migration hazard, and it does not present as a permissions
    problem. A <vm>-nvmeN.qcow2 created by a root container is 0644
    root-owned; QEMU opens a drive read-write, so the guest does not boot at
    all. A PROM_DATA_DIR created by the daemon is 0755 root-owned and
    Prometheus crash-loops on "mkdir data/: permission denied". Both read as
    "the fleet is broken" a long way from the cause.

    A warning, never a refusal. Someone may be deliberately running a mixed
    fleet, and `up` on a stack with one stale file should still bring up the
    other thirty-one guests.
    """
    uid = os.getuid()
    offenders: list[str] = []
    images = env.get("VM_IMAGES_DIR")
    if images:
        try:
            # Only the files a container creates. The backing and overlay
            # qcow2s are made by gen-vm on the host, as the caller, and a
            # fleet legitimately shares a backing image owned by someone else.
            for path in sorted(Path(images).glob("*-nvme*.qcow2")):
                if path.stat().st_uid != uid:
                    offenders.append(f"{path}  (uid {path.stat().st_uid})")
        except OSError:
            pass
    for key in _DATA_DIR_KEYS:
        raw = env.get(key)
        if not raw:
            continue
        path = Path(raw)
        try:
            if path.is_dir() and path.stat().st_uid != uid:
                offenders.append(f"{path}  ({key}, uid {path.stat().st_uid})")
        except OSError:
            continue
    if not offenders:
        return
    print(
        f"Warning: these are not owned by uid {uid}, which the containers now "
        "run as:",
        file=sys.stderr,
    )
    for line in offenders:
        print(f"           {line}", file=sys.stderr)
    print(
        "         They were probably created by a root-era run of this stack.\n"
        "         An NVMe qcow2 the guest cannot write stops it booting; a data\n"
        "         directory it cannot write crash-loops the service. Delete the\n"
        "         qcow2s (they are scratch) and chown the directories.",
        file=sys.stderr,
    )


def _precreate_data_dirs(env: dict[str, str]) -> None:
    """Create the bind-mount targets as the caller, not as the daemon.

    Docker creates a missing bind-mount source itself, root-owned, and the
    service that then cannot write it is the one that reports the problem --
    several minutes and one confusing stack trace later. Making them here
    costs nothing and removes the "chown these three directories before your
    first run" step from the settings file entirely.
    """
    for key in _DATA_DIR_KEYS:
        raw = env.get(key)
        if not raw:
            continue
        try:
            Path(raw).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Not fatal: the path may be on a filesystem only root can write,
            # in which case the operator has already chowned it or is about
            # to find out. Say so rather than failing the whole command.
            print(
                f"Warning: could not create {key} at {raw}: {exc}",
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
    # Every service carries `user: ${CONTAINER_USER:?...}` now, so this is
    # what makes the supported path need no configuration: unset means "run
    # the fleet as me", not "run it as root". Plain `docker compose` against
    # the same file gets the :? error instead, which says what to set.
    _apply_identity(env, settings)
    if any(arg in _STARTING for arg in compose_args):
        # The settings under the environment, which is the order compose
        # itself resolves them in. Only for the checks below -- nothing here
        # is passed on; compose reads the layers itself.
        paths = dict(parse_env_layers(settings)) if settings else {}
        for key in ("VM_IMAGES_DIR", *_DATA_DIR_KEYS):
            if env.get(key):
                paths[key] = env[key]
        _precreate_data_dirs(paths)
        _warn_about_root_artifacts(paths)
    # Belt and braces: cli._split_compose_argv already drops a user-typed "--",
    # but docker compose treats everything after a "--" as positional, so a
    # leaked separator made `-- --profile lmcache up -d` bring up the default
    # services and report success -- the quiet failure, not a visible one.
    if compose_args and compose_args[0] == "--":
        compose_args = compose_args[1:]
    cmd += compose_args
    result = subprocess.run(cmd, cwd=cdir, env=env)
    sys.exit(result.returncode)
