"""Read VM settings from the env file shared with docker compose.

One file (qemu/env.example -> qemu/.env) configures gen-vm, run-vm and the
compose stacks. The format is the docker compose env-file format, so compose
consumes the same file verbatim via --env-file.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import types
import typing
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from .config import VMConfig

# Installed by the .deb; the admin edits this copy.
_INSTALLED_ENV = Path("/etc/qemu-tool/env")
# Source checkout: this file is qemu/qemu-tool/envfile.py, so qemu/ is two up.
_SOURCE_ENV = Path(__file__).parent.parent / ".env"


def _xdg_env() -> Path:
    """Per-user settings file, for installs that cannot write /etc.

    `pipx install qemu-tool` never creates /etc/qemu-tool, so without this a
    rootless install has nowhere to keep settings but the working directory.
    """
    raw = os.environ.get("XDG_CONFIG_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "qemu-tool" / "env"

# Field name -> env key, where "VM_" + field.upper() is not the name we want.
_KEY_OVERRIDES = {
    "vm_name": "VM_NAME",
    "images": "VM_IMAGES_DIR",
}

# Per-invocation actions, not configuration. Honouring these from a file means
# a stale .env silently turns every later run into a dry run or a rebuild.
_NOT_CONFIGURABLE = frozenset({
    "force", "dry_run", "restore_image", "ansible_only", "nvme_recreate",
})

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_key(field_name: str) -> str:
    return _KEY_OVERRIDES.get(field_name, "VM_" + field_name.upper())


def find(explicit: Path | None = None) -> Path | None:
    """Return the env file to use, or None if there is no file to read."""
    if explicit is not None:
        if not explicit.is_file():
            sys.exit(f"Error: env file not found: {explicit}")
        return explicit
    candidates = []
    from_env = os.environ.get("QEMU_TOOL_ENV")
    if from_env:
        candidates.append(Path(from_env))
    # A user's own file beats the system one; both lose to the checkout and
    # to $QEMU_TOOL_ENV, which are the deliberate per-invocation choices.
    candidates += [Path.cwd() / ".env", _SOURCE_ENV, _xdg_env(), _INSTALLED_ENV]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse(path: Path) -> dict[str, str]:
    """Parse an env file into {KEY: value}, ignoring blanks and comments."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            sys.exit(f"Error: {path}:{lineno}: expected KEY=value, got {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def overrides(values: dict[str, str]) -> dict[str, Any]:
    """Map env-file keys onto VMConfig fields, coercing to the field's type.

    Keys that name no field are left alone -- the same file carries the
    compose-only settings (image tags, MAC addresses, ROCJITSU_CONFIG).
    """
    hints = typing.get_type_hints(VMConfig)
    result: dict[str, Any] = {}
    for field in dataclasses.fields(VMConfig):
        if field.name in _NOT_CONFIGURABLE:
            continue
        key = env_key(field.name)
        raw = values.get(key)
        if raw is None or raw == "":
            continue
        result[field.name] = _coerce(hints[field.name], raw, key)
    return result


def load(explicit: Path | None = None) -> dict[str, Any]:
    """find() + parse() + overrides(), the combination every caller wants."""
    path = find(explicit)
    if path is None:
        return {}
    return overrides(parse(path))


def _coerce(hint: Any, raw: str, key: str) -> Any:
    if get_origin(hint) in (Union, types.UnionType):
        args = tuple(a for a in get_args(hint) if a is not type(None))
        if len(args) == 1:
            hint = args[0]
    if get_origin(hint) is list:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if hint is bool:
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        sys.exit(f"Error: {key} must be a boolean, got {raw!r}")
    if hint is int:
        try:
            return int(raw)
        except ValueError:
            sys.exit(f"Error: {key} must be an integer, got {raw!r}")
    if hint is Path:
        return Path(raw).expanduser()
    return raw
