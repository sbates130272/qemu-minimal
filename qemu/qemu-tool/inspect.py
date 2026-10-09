from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .config import VMConfig
from .envfile import find as _find_env, overrides as _env_overrides, parse as _parse_env
from .list_vms import collect as _collect_vms, _fmt_uptime


def run(vm_name: str | None, env_file: Path | None, as_json: bool = False) -> None:
    try:
        vms = _collect_vms()
    except OSError as exc:
        sys.exit(f"Error: cannot read /proc: {exc}")

    if vm_name is not None:
        vms = [v for v in vms if v["name"] == vm_name]
        if not vms:
            sys.exit(f"Error: no running VM named {vm_name!r}")

    cfg = _load_config(env_file)
    ssh_public_keys = _read_public_keys(cfg.ssh_key_file)

    records = [_record(v, cfg, ssh_public_keys) for v in vms]

    if as_json:
        if vm_name is not None and len(records) == 1:
            print(json.dumps(records[0], indent=2))
        else:
            print(json.dumps(records, indent=2))
    else:
        _print_human(records)


def _load_config(env_file: Path | None) -> VMConfig:
    path = _find_env(env_file)
    env_overrides = _env_overrides(_parse_env(path)) if path else {}
    import dataclasses
    d = dataclasses.asdict(VMConfig())
    d.update(env_overrides)
    return VMConfig(**d)


def _read_public_keys(key_file: Path) -> list[str]:
    try:
        text = key_file.expanduser().read_text()
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _record(
    vm: dict[str, Any],
    cfg: VMConfig,
    ssh_public_keys: list[str],
) -> dict[str, Any]:
    port = vm["ssh_port_host"]
    ssh_cmd = f"ssh -p {port} {cfg.username}@localhost" if port is not None else None
    return {
        "name": vm["name"],
        "pid": vm["pid"],
        "arch": vm["arch"],
        "vcpus": vm["vcpus"],
        "memory_mib": vm["memory_mib"],
        "ssh_host": "localhost",
        "ssh_port": port,
        "username": cfg.username,
        "ssh_public_keys": ssh_public_keys,
        "ssh_cmd": ssh_cmd,
        "image": vm["image"],
        "container": vm["container"],
        "uptime_seconds": vm["uptime_seconds"],
        "vfio_user_sockets": vm["vfio_user_sockets"],
    }


def _print_human(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No QEMU VMs running.")
        return
    for r in records:
        name = r["name"] or "-"
        print(f"VM: {name}  (pid {r['pid']})")
        ssh = r["ssh_cmd"] or f"port {r['ssh_port'] or '-'}"
        print(f"  SSH:       {ssh}")
        print(f"  User:      {r['username']}")
        for key in r["ssh_public_keys"]:
            print(f"  Key:       {key}")
        vcpus = r["vcpus"] or "-"
        mem = f"{r['memory_mib']} MiB" if r["memory_mib"] else "-"
        print(f"  Arch:      {r['arch']}  vCPUs: {vcpus}  Memory: {mem}")
        print(f"  Image:     {r['image'] or '-'}")
        if r["container"]:
            print(f"  Container: {r['container']}")
        print(f"  Uptime:    {_fmt_uptime(r['uptime_seconds'])}")
        if r["vfio_user_sockets"]:
            socks = ", ".join(Path(s).name for s in r["vfio_user_sockets"])
            print(f"  VFIO:      {socks}")
        print()
