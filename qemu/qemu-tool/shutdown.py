"""Shut guests down cleanly before their containers are stopped.

`docker compose down` sends SIGTERM to PID 1, and run_vm execs QEMU, so PID 1
IS qemu-system-x86_64. QEMU exits on SIGTERM; it does not ACPI-shutdown the
guest. Every teardown is therefore the equivalent of pulling the power cord,
and the guest's filesystem is left needing journal replay -- or, observed on a
32-guest fleet, an aborted journal and a root remounted read-only.

Nothing in the stack had a place to hook this: there is no shim to trap the
signal, and raising stop_grace_period alone only makes Docker wait longer
before killing a guest that was never asked to stop. So the request has to
come from outside, over the guest agent, before the containers are stopped.

QGA rather than QMP because the fleet already has it: run_vm wires a
virtserialport on every guest unconditionally, while QMP is opt-in and off by
default (`qmp_available: false` on a fleet guest).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .list_vms import collect as _collect_vms

# Same shape as inspect's _GA_SCRIPT: a self-contained program run under the
# container's own interpreter, because the socket lives in that container's
# filesystem namespace.
#
# guest-sync first, for the same reason inspect does it -- a previous
# connection may have left an unread response on the socket, and reading it as
# the answer to our shutdown would report success for someone else's query.
#
# guest-shutdown returns NOTHING on success: the agent powers the guest off
# mid-request, so the connection drops before a reply is written. An empty
# read is therefore the expected case and a reply is the surprising one.
_SHUTDOWN_SCRIPT = r"""
import json, random, socket, sys

path = __SOCKET__
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(path)
except OSError as exc:
    print(json.dumps({"ok": False, "error": "connect: %s" % exc}))
    sys.exit(0)
s.settimeout(5.0)

def line():
    raw = b""
    while b"\n" not in raw:
        chunk = s.recv(65536)
        if not chunk:
            return None
        raw += chunk
    return json.loads(raw.split(b"\n")[0])

sync_id = random.randint(1, 2**31)
s.sendall(json.dumps({"execute": "guest-sync",
                      "arguments": {"id": sync_id}}).encode() + b"\n")
synced = False
for _ in range(20):
    try:
        got = line()
    except socket.timeout:
        break
    if got is None:
        break
    if got.get("return") == sync_id:
        synced = True
        break
if not synced:
    print(json.dumps({"ok": False, "error": "agent did not sync"}))
    sys.exit(0)

s.sendall(json.dumps({"execute": "guest-shutdown",
                      "arguments": {"mode": "powerdown"}}).encode() + b"\n")
try:
    got = line()
except (socket.timeout, OSError):
    got = None
# A reply at all means the agent refused; success looks like a dropped
# connection, because the guest is already going down.
if isinstance(got, dict) and "error" in got:
    print(json.dumps({"ok": False, "error": str(got["error"])}))
else:
    print(json.dumps({"ok": True}))
"""


def _request_shutdown(vm: dict[str, Any]) -> tuple[str, bool, str]:
    sock = vm.get("ga_socket")
    if not sock:
        return vm["name"], False, "no guest-agent socket"
    script = _SHUTDOWN_SCRIPT.replace("__SOCKET__", repr(sock))
    container = vm.get("container")
    if container:
        cmd = ["docker", "exec", container, "python3", "-c", script]
    else:
        cmd = [sys.executable, "-I", "-c", script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = json.loads(r.stdout.strip() or "{}")
    except Exception as exc:
        return vm["name"], False, f"{type(exc).__name__}: {exc}"
    return vm["name"], bool(out.get("ok")), out.get("error", "")


def _still_running(names: set[str]) -> set[str]:
    try:
        return {v["name"] for v in _collect_vms() if v["name"] in names}
    except OSError:
        return set()


def run(vm_name: str | None, timeout: int = 120, dry_run: bool = False) -> None:
    try:
        vms = _collect_vms()
    except OSError as exc:
        sys.exit(f"Error: cannot read /proc: {exc}")

    if vm_name is not None:
        vms = [v for v in vms if v["name"] == vm_name]
        if not vms:
            sys.exit(f"Error: no running VM named {vm_name!r}")
    if not vms:
        print("No running VMs.")
        return

    if dry_run:
        for vm in vms:
            print(f"would shut down {vm['name']} via {vm.get('ga_socket')}")
        return

    print(f"Requesting shutdown of {len(vms)} VM(s)...")
    # Concurrent, because a fleet of 32 at ~1 s each is otherwise half a
    # minute of waiting before the first guest has even begun to stop.
    workers = min(8, len(vms))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_request_shutdown, vms))

    asked = set()
    for name, ok, err in results:
        if ok:
            asked.add(name)
        else:
            print(f"  {name}: shutdown not requested ({err})", file=sys.stderr)

    if not asked:
        sys.exit(
            "Error: no guest accepted a shutdown request.\n"
            "       Stopping the containers now would hard-kill them; check "
            "the agent is running."
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = _still_running(asked)
        if not left:
            print(f"All {len(asked)} guest(s) powered off.")
            return
        time.sleep(2)

    left = _still_running(asked)
    # Not an error: the caller's next step is to stop the containers anyway,
    # and saying which guests did not finish is more useful than refusing.
    print(
        f"Warning: {len(left)} guest(s) still running after {timeout}s: "
        + ", ".join(sorted(left))
        + "\n         Stopping them now will not be clean.",
        file=sys.stderr,
    )
