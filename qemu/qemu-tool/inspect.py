from __future__ import annotations

import json
import socket as _socket
import subprocess
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
    """Read VM metadata from a sidecar JSON file, if present.

    Checks two locations in order:
    1. <images-dir>/vm-info.json  — the batesste-ci-images schema (schema_version key)
    2. <vm-name>.json             — the sidecar written by gen-vm

    Returns a normalised dict with keys: username, ssh_key_file (public key path).
    """
    if not image:
        return {}
    image_path = Path(image)
    images_dir = image_path.parent

    # batesste-ci-images schema: shared vm-info.json in the images directory.
    ci_info_path = images_dir / "vm-info.json"
    if ci_info_path.exists():
        try:
            raw = json.loads(ci_info_path.read_text())
            if "schema_version" in raw:
                result: dict[str, Any] = {}
                if "username" in raw:
                    result["username"] = raw["username"]
                ssh = raw.get("ssh_keys", {})
                pub = ssh.get("public_key_path", "")
                if pub:
                    # Path is relative to the images dir when it starts with /output/
                    pub_path = images_dir / Path(pub).name
                    if pub_path.exists():
                        result["ssh_key_file"] = str(pub_path)
                return result
        except (OSError, json.JSONDecodeError):
            pass

    # gen-vm sidecar: <vm-name>.json next to the qcow2.
    sidecar = image_path.with_suffix(".json")
    try:
        return json.loads(sidecar.read_text())
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


_GA_SCRIPT = r"""
import socket, json, sys, random

def _ga_run(path, timeout=3.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError:
        return {}
    # recv-only timeout: connect must be blocking to avoid EAGAIN on Unix sockets
    s.settimeout(timeout)

    def send_recv(cmd, args=None):
        req = {"execute": cmd}
        if args is not None:
            req["arguments"] = args
        s.sendall(json.dumps(req).encode() + b"\n")
        raw = b""
        while b"\n" not in raw:
            chunk = s.recv(65536)
            if not chunk:
                break
            raw += chunk
        return json.loads(raw.split(b"\n")[0])

    # Flush stale responses from any previous connection on this socket.
    sync_id = random.randint(1, 2**31)
    s.sendall(json.dumps({"execute": "guest-sync",
                           "arguments": {"id": sync_id}}).encode() + b"\n")
    for _ in range(20):
        try:
            raw = b""
            while b"\n" not in raw:
                chunk = s.recv(65536)
                if not chunk:
                    break
                raw += chunk
            if json.loads(raw.split(b"\n")[0]).get("return") == sync_id:
                break
        except socket.timeout:
            return {}

    result = {}
    for cmd, key in [("guest-get-hostname", "hostname"),
                      ("guest-network-get-interfaces", "interfaces")]:
        try:
            r = send_recv(cmd)
            if "return" in r:
                result[key] = r["return"]
            # CommandNotFound: older agent or guest agent compiled without this command.
        except socket.timeout:
            pass
    return result

print(json.dumps(_ga_run(__SOCKET__)))
"""


def _ga_query(ga_socket: str | None, container: str | None) -> dict[str, Any]:
    """Query the QEMU guest agent for live guest information.

    For containerised VMs, proxies the query through 'docker exec' since the
    socket lives inside the container's filesystem namespace. For bare VMs,
    opens the socket directly. Returns an empty dict on any error.
    """
    if not ga_socket:
        return {}
    try:
        if container:
            script = _GA_SCRIPT.replace("__SOCKET__", repr(ga_socket))
            r = subprocess.run(
                ["docker", "exec", container, "python3", "-c", script],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return {}
            return json.loads(r.stdout)
        else:
            # Direct path for non-containerised VMs — same logic inline.
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.connect(ga_socket)
            s.settimeout(3.0)
            import random as _random
            sync_id = _random.randint(1, 2**31)
            s.sendall(json.dumps({"execute": "guest-sync",
                                   "arguments": {"id": sync_id}}).encode() + b"\n")
            for _ in range(20):
                raw = b""
                while b"\n" not in raw:
                    raw += s.recv(65536)
                if json.loads(raw.split(b"\n")[0]).get("return") == sync_id:
                    break
            result: dict[str, Any] = {}
            for cmd, key in [("guest-get-hostname", "hostname"),
                              ("guest-network-get-interfaces", "interfaces")]:
                s.sendall(json.dumps({"execute": cmd}).encode() + b"\n")
                raw = b""
                while b"\n" not in raw:
                    raw += s.recv(65536)
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
        "qmp_available": vm.get("qmp_available", False),
        "ga_socket": vm.get("ga_socket"),
        "ga_available": vm.get("ga_available", False),
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
            avail = " (available)" if r.get("qmp_available") else " (unavailable)"
            print(f"  QMP:       {r['qmp_socket']}{avail}")
        if r.get("ga_socket"):
            avail = " (available)" if r.get("ga_available") else " (unavailable)"
            print(f"  GA:        {r['ga_socket']}{avail}")
        guest = r.get("guest", {})
        if guest.get("hostname"):
            print(f"  Hostname:  {guest['hostname'].get('host-name', '-')}")
        if guest.get("interfaces"):
            for iface in guest["interfaces"]:
                for addr in iface.get("ip-addresses", []):
                    if addr.get("ip-address-type") == "ipv4":
                        print(f"  IP:        {addr['ip-address']} ({iface['name']})")
        print()
