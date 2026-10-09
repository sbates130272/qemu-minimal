from __future__ import annotations

import json
import socket as _socket
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

    records = [_record(v, cfg) for v in vms]

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


def _read_vm_info(image: str | None) -> dict[str, Any]:
    """Read the sidecar <vm-name>.json written by gen-vm, if present."""
    if not image:
        return {}
    info_path = Path(image).with_suffix(".json")
    try:
        return json.loads(info_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _ssh_key_for_image(
    image: str | None, info: dict[str, Any], cfg: VMConfig
) -> tuple[list[str], Path | None]:
    """Return (public_key_lines, private_key_path).

    Priority: sidecar ssh_key_file > image-adjacent .pub > config default.
    """
    candidates: list[Path] = []

    sidecar_key = info.get("ssh_key_file")
    if sidecar_key:
        candidates.append(Path(sidecar_key))

    if image:
        candidates.append(Path(image).with_suffix(".pub"))

    candidates.append(cfg.ssh_key_file.expanduser())

    for pub in candidates:
        try:
            text = pub.read_text()
        except OSError:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        private = pub.parent / pub.stem
        return lines, (private if private.exists() else None)

    return [], None


def _ga_query(ga_socket: str | None, container: str | None) -> dict[str, Any]:
    """Query the QEMU guest agent for live guest information.

    Returns an empty dict when the socket is absent, inside a container
    (not reachable from the host), or the guest agent is not responding.
    """
    if not ga_socket or container:
        return {}
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(ga_socket)
            s.sendall(b'{"execute":"guest-info"}\n')
            raw = b""
            while b"\n" not in raw:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
            resp = json.loads(raw.split(b"\n")[0])
            if "error" in resp:
                return {}
            # Guest agent is alive — query hostname and network interfaces.
            result: dict[str, Any] = {}
            for cmd, key in [
                ("guest-get-hostname", "hostname"),
                ("guest-network-get-interfaces", "interfaces"),
            ]:
                s.sendall(json.dumps({"execute": cmd}).encode() + b"\n")
                raw = b""
                while b"\n" not in raw:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                r = json.loads(raw.split(b"\n")[0])
                if "return" in r:
                    result[key] = r["return"]
            return result
    except Exception:
        return {}


def _record(vm: dict[str, Any], cfg: VMConfig) -> dict[str, Any]:
    info = _read_vm_info(vm["image"])
    pub_keys, private_key = _ssh_key_for_image(vm["image"], info, cfg)
    username = info.get("username") or cfg.username
    ga_info = _ga_query(vm.get("ga_socket"), vm["container"])

    port = vm["ssh_port_host"]
    identity = f" -i {private_key}" if private_key is not None else ""
    ssh_cmd = (
        f"ssh{identity} -p {port} {username}@localhost"
        if port is not None
        else None
    )

    record: dict[str, Any] = {
        "name": vm["name"],
        "pid": vm["pid"],
        "arch": vm["arch"],
        "vcpus": vm["vcpus"],
        "memory_mib": vm["memory_mib"],
        "ssh_host": "localhost",
        "ssh_port": port,
        "username": username,
        "ssh_public_keys": pub_keys,
        "ssh_cmd": ssh_cmd,
        "image": vm["image"],
        "container": vm["container"],
        "uptime_seconds": vm["uptime_seconds"],
        "vfio_user_sockets": vm["vfio_user_sockets"],
        "qmp_socket": vm.get("qmp_socket"),
        "ga_socket": vm.get("ga_socket"),
    }
    if ga_info:
        record["guest"] = ga_info
    return record


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
        if r.get("qmp_socket"):
            print(f"  QMP:       {r['qmp_socket']}")
        if r.get("ga_socket"):
            print(f"  GA:        {r['ga_socket']}")
        guest = r.get("guest", {})
        if guest.get("hostname"):
            print(f"  Hostname:  {guest['hostname'].get('host-name', '-')}")
        if guest.get("interfaces"):
            for iface in guest["interfaces"]:
                for addr in iface.get("ip-addresses", []):
                    if addr.get("ip-address-type") == "ipv4":
                        print(f"  IP:        {addr['ip-address']} ({iface['name']})")
        print()
