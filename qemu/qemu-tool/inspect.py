from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import VMConfig
from .envfile import (
    find as _find_env,
    overrides as _env_overrides,
    parse_layers as _parse_env_layers,
)
from .list_vms import (
    collect as _collect_vms,
    _device_opts,
    _device_servers,
    _fmt_uptime,
    _gpus as _rocjitsu_gpus,
    _normalise_bdf,
    _pci_class,
    _short_bdf,
)

# A passed-through accelerator is "VGA compatible" (0x0300xx) on a consumer
# part and "Processing accelerator" (0x1200xx) on an Instinct one -- the
# emulated rocjitsu device itself reports class 120000 in the guest, so a
# 0x0300-only filter would miss every MI-class GPU this repo cares about.
_GPU_CLASS_PREFIXES = ("0x0300", "0x1200")

# Only buses QEMU emulates in this repo's launchers (-drive if=virtio, the
# default SATA CD-ROM). A real device is always handed over as vfio-pci and
# reaches the guest as nvme, so nothing real can arrive on these.
_EMULATED_BUS_TYPES = ("virtio", "ide", "sata")

_QEMU_NVME_NQN_PREFIX = "nqn.2019-08.org.qemu:"


def records(
    vm_name: str | None,
    env_file: list[Path] | None,
) -> list[dict[str, Any]]:
    """Every running VM's record, or just one named VM's.

    Factored out of run() so `qemu-tool ssh` reaches a guest through exactly
    the same resolution inspect prints -- the username from the image's own
    sidecar, and the key the GUEST says it accepts rather than the configured
    default. A second implementation would be free to disagree with the
    output the user is reading, which is the whole failure this record exists
    to prevent.
    """
    try:
        vms = _collect_vms()
    except OSError as exc:
        sys.exit(f"Error: cannot read /proc: {exc}")

    if vm_name is not None:
        vms = [v for v in vms if v["name"] == vm_name]
        if not vms:
            sys.exit(f"Error: no running VM named {vm_name!r}")

    cfg = _load_config(env_file)

    # One docker exec per VM at ~300 ms, serial, was 10.9 s over the 32-VM
    # fleet before this query grew any commands. executor.map keeps collect()
    # order so --json stays diffable between runs.
    if len(vms) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(vms))) as pool:
            return list(pool.map(lambda v: _record(v, cfg), vms))
    return [_record(v, cfg) for v in vms]


def run(
    vm_name: str | None,
    env_file: list[Path] | None,
    as_json: bool = False,
) -> None:
    found = records(vm_name, env_file)

    if as_json:
        if vm_name is not None and len(found) == 1:
            print(json.dumps(found[0], indent=2))
        else:
            print(json.dumps(found, indent=2))
    else:
        _print_human(found)


# Guests built from one backing image all carry the SAME host key, and the
# fleet hands them consecutive ports on localhost -- so port 2222 is a
# different machine on every fleet, and re-creating an overlay changes nothing
# about that. Strict checking therefore fails with REMOTE HOST IDENTIFICATION
# HAS CHANGED as a matter of course, and writing these to known_hosts would
# poison the entry for whatever uses that port next. The generated ansible
# inventory passes exactly this pair for the same reason.
_SSH_LOOSE_HOST_KEY = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    # Without this the two options above still print a warning per connection.
    "-o", "LogLevel=ERROR",
]


def ssh_argv(
    record: dict[str, Any],
    strict_host_key: bool = False,
    extra: Sequence[str] | None = None,
) -> list[str]:
    """The ssh command for a VM record, as argv.

    Built from the record rather than from the config, so it agrees with what
    `inspect` printed: the username the image actually has, and an -i only
    when the guest named a key whose private half is on this host. When it did
    not, no -i is passed and ssh falls through to its other methods -- offering
    an identity the guest does not accept just buys a refusal.
    """
    port = record.get("ssh_port")
    if port is None:
        sys.exit(
            f"Error: {record['name']} has no host SSH port, so there is "
            "nothing to connect to.\n"
            "       Its qemu container publishes none -- check the stack's "
            "ports: mapping."
        )

    argv = ["ssh"]
    if not strict_host_key:
        argv += _SSH_LOOSE_HOST_KEY
    argv += ["-p", str(port)]
    if record.get("ssh_key_file"):
        argv += ["-i", record["ssh_key_file"]]
    argv.append(f"{record['username']}@{record.get('ssh_host') or 'localhost'}")
    argv += list(extra or [])
    return argv


def ssh_run(
    vm_name: str,
    env_file: list[Path] | None,
    strict_host_key: bool = False,
    extra: Sequence[str] | None = None,
    dry_run: bool = False,
) -> None:
    found = records(vm_name, env_file)
    record = found[0]
    argv = ssh_argv(record, strict_host_key=strict_host_key, extra=extra)

    # Say why a password prompt is about to appear, BEFORE it appears. The
    # guest publishing only a key we do not hold is the common case on a fleet
    # booted from a published image, and an unexplained prompt reads as a
    # broken VM rather than a missing key.
    if not record.get("ssh_key_file"):
        note = record.get("ssh_key_note") or "no local key matched"
        print(f"Note: connecting without a key ({note}).", file=sys.stderr)
        for key in record.get("ssh_keys") or []:
            if key.get("accepted"):
                comment = f" {key['comment']}" if key.get("comment") else ""
                print(f"      guest accepts {key['fingerprint']}{comment}",
                      file=sys.stderr)

    if dry_run:
        print(shlex.join(argv))
        return

    try:
        # exec rather than subprocess: ssh wants the real tty for its password
        # prompt and for an interactive session, and the caller should see
        # ssh's own exit status, not a wrapper's.
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.exit(f"Error: cannot run ssh: {exc}")


def _load_config(env_file: list[Path] | None) -> VMConfig:
    paths = _find_env(env_file)
    env_overrides = _env_overrides(_parse_env_layers(paths)) if paths else {}
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
            if not pub.is_file():
                continue
            text = pub.read_text()
        except OSError:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        private = pub.parent / pub.stem
        return lines, (private if private.exists() else None)

    return [], None


# A guest's authorized_keys is as long as its owner made it, and a public key
# is not interesting enough to print a dozen of. One candidate directory of
# .pub files is likewise bounded so a stray directory cannot turn inspect into
# a filesystem walk.
_GUEST_KEY_LIMIT = 4
_GLOB_KEY_LIMIT = 64
_PUB_FILE_MAX_BYTES = 65536

# Everything a guest hands us -- an authorized_keys comment, a logind USER or
# REMOTE_HOST -- is written by whoever is inside that guest and printed to an
# operator's terminal. str.split() does not split on ESC or BEL, so a comment
# carrying a control sequence reached the tty intact and could redraw lines
# inspect had already printed for a different VM.
_GUEST_TEXT_MAX = 128


def _sanitise(text: str, limit: int = _GUEST_TEXT_MAX) -> str:
    """Guest-controlled text, safe to print: no control characters, bounded."""
    out = []
    for ch in text[:limit]:
        if ch.isprintable():
            out.append(ch)
        elif ord(ch) < 0x100:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append("\\u%04x" % ord(ch))
    if len(text) > limit:
        out.append("...")
    return "".join(out)


def _resolve(found: list[Any], complete: Any) -> list[Any] | None:
    """The one place an empty answer is allowed to become "none".

    Every guest fact inspect reports has the same two ways of being empty --
    we looked and there was nothing, or we never got to look -- and the two
    must not print alike: "none" is a claim about the guest, "unknown" is a
    claim about this query. Six rounds of point fixes each taught one caller
    the difference and left the next one claiming a confident "none" for a
    read that failed, a budget that expired, a filter that excluded somebody
    or an answer in a shape it did not recognise.

    So the rule is enforced once, here: an empty result is "none" only against
    `complete is True`, positive evidence carried from wherever the looking
    happened. Anything else -- False, missing, an absent key read off a dict
    -- is None, which every renderer turns into "unknown". A caller that
    cannot name its evidence does not get to say "none".
    """
    if found:
        return list(found)
    return [] if complete is True else None


def _read_failed(ga_info: dict[str, Any], path: str | None) -> bool:
    """Whether the in-guest script's read of `path` came back short.

    A file that opened but failed or truncated its read is in partial_files.
    Its absence from that list is the positive evidence that the bytes the
    host is parsing are the whole file.
    """
    return path is None or path in (ga_info.get("partial_files") or [])


def _fingerprint(blob: bytes) -> str:
    """The SHA256 fingerprint `ssh-keygen -lf` prints, base64 without padding."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


# Options that mean the key does not simply log you in. `restrict` turns
# everything off, `from` and `expiry-time` can make the key useless from this
# host or at this moment, and `command` replaces the shell -- all four make a
# bare `ssh -i <key>` a claim inspect cannot stand behind.
_RESTRICTION_OPTIONS = ("from", "command", "expiry-time", "restrict")


def _split_options(text: str) -> list[str]:
    """The option list of an authorized_keys line, split the way sshd splits it.

    A comma inside a quoted value does not separate options, so a naive split
    reads command="rm,x" as two options -- and, worse, a quoted value could
    hide the comma that separates a real restriction from the next option.
    """
    out: list[str] = []
    cur: list[str] = []
    quoted = False
    escaped = False
    for ch in text:
        if escaped:
            cur.append(ch)
            escaped = False
        elif quoted and ch == "\\":
            escaped = True
        elif ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [o for o in out if o]


def _classify_options(text: str) -> tuple[bool, list[str]]:
    """(is a cert-authority line, restriction option names) for an options field."""
    cert_authority = False
    restrictions: list[str] = []
    for opt in _split_options(text):
        name = opt.split("=", 1)[0].strip().lower()
        if name == "cert-authority":
            cert_authority = True
        elif name in _RESTRICTION_OPTIONS and name not in restrictions:
            restrictions.append(name)
    return cert_authority, restrictions


def _parse_pubkey_line(line: str) -> dict[str, Any] | None:
    """One public key line, or None if it is not one.

    Comparison has to be by fingerprint rather than by string: an
    authorized_keys line carries options ahead of the key and a comment after
    it, neither of which appears in the .pub file holding the same key, so
    equality never matches even when the key does.

    The key field is found by decoding rather than by a type whitelist -- the
    blob's own leading length-prefixed string names the algorithm, so a line is
    a key exactly when some field decodes to a blob naming the field before it.
    That also makes a malformed or truncated line simply not a key, instead of
    an exception in the middle of inspect.

    What is in front of the key is parsed, not skipped. Skipping it printed
    `ssh -i <the CA's private key>` in the maximum-confidence "accepted by
    guest" form for a `cert-authority` line -- a key that signs certificates
    and authenticates nothing, which sshd will refuse.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    fields = line.split()
    for i in range(1, len(fields)):
        tok = fields[i]
        try:
            blob = base64.b64decode(tok, validate=True)
        except Exception:
            continue
        if len(blob) < 4:
            continue
        n = int.from_bytes(blob[:4], "big")
        if n <= 0 or n > 64 or 4 + n > len(blob):
            continue
        if blob[4:4 + n].decode("ascii", "replace") != fields[i - 1]:
            continue
        cert_authority, restrictions = _classify_options(" ".join(fields[:i - 1]))
        return {
            "fingerprint": _fingerprint(blob),
            "comment": _sanitise(" ".join(fields[i + 1:])),
            "cert_authority": cert_authority,
            "restrictions": restrictions,
        }
    return None


def _guest_accepted_keys(ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Keys the guest accepts, or None when the guest could not be asked.

    None and [] are different answers, as everywhere else in inspect: an
    unreachable agent is not a guest that accepts nothing.

    Only the standard authorized_keys of the reported user is read. A guest
    with a non-default sshd AuthorizedKeysFile can therefore accept a key that
    does not appear here, but nothing printed from this is a guess -- the keys
    it does return are keys the guest really accepts.

    An empty result goes through _resolve like every other one: "the guest
    accepts no keys at all" is a statement about the guest, and a read that
    failed or stopped short of the end of the file is not evidence for it.
    """
    path = ga_info.get("authorized_keys")
    text = (ga_info.get("files") or {}).get(path) if path else None
    if text is None:
        return None
    out: list[dict[str, Any]] = []
    by_fp: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        key = _parse_pubkey_line(line)
        if not key:
            continue
        prev = by_fp.get(key["fingerprint"])
        if prev is None:
            by_fp[key["fingerprint"]] = key
            out.append(key)
        elif prev["cert_authority"] and not key["cert_authority"]:
            # The same key listed both ways is a login key; the CA listing is
            # the one that does not authenticate, so it must not win the dedupe.
            out[out.index(prev)] = key
            by_fp[key["fingerprint"]] = key
    return _resolve(out, not _read_failed(ga_info, path))


def _local_key_candidates(image: str | None, info: dict[str, Any],
                          cfg: VMConfig) -> list[Path]:
    """Local files that might hold a public key, nearest the VM first.

    The injected key of a stack lives beside the images it was baked into
    (<images-dir>/keys/) rather than in ~/.ssh, so an index built only from
    the user's own directory cannot name the one key a fleet guest accepts.

    Named candidates are collected before any glob, and each globbed directory
    is capped on its own rather than against a budget the earlier ones have
    already spent. A flat cap over the concatenated list -- which is what the
    paragraph above used to describe and the code did not do -- let one noisy
    directory consume the whole allowance and push every directory behind it
    off the end: 64 .pub files in a directory of baked images is nothing, and
    it was enough to stop ~/.ssh being read at all.
    """
    named: list[Path] = []
    groups: list[list[Path]] = []
    seen: set[Path] = set()

    def add(p: Path, bucket: list[Path]) -> None:
        if p not in seen:
            seen.add(p)
            bucket.append(p)

    def add_glob(d: Path) -> None:
        try:
            found = sorted(d.glob("*.pub"))
        except OSError:
            return
        group: list[Path] = []
        for p in found:
            add(p, group)
            if len(group) >= _GLOB_KEY_LIMIT:
                break
        groups.append(group)

    sidecar = info.get("ssh_key_file")
    if sidecar:
        add(Path(sidecar), named)
        add(Path(str(sidecar) + ".pub"), named)

    if image:
        add(Path(image).with_suffix(".pub"), named)

    configured = Path(str(cfg.ssh_key_file)).expanduser()
    add(configured, named)
    add(Path(str(configured) + ".pub"), named)

    if image:
        img = Path(image)
        add_glob(img.parent / "keys")
        add_glob(img.parent)

    try:
        add_glob(Path.home() / ".ssh")
    except RuntimeError:
        pass

    return named + [p for group in groups for p in group]


def _local_key_index(image: str | None, info: dict[str, Any],
                     cfg: VMConfig) -> dict[str, dict[str, Any]]:
    """fingerprint -> {pub, private} for every local public key worth offering."""
    index: dict[str, dict[str, Any]] = {}
    for pub in _local_key_candidates(image, info, cfg):
        try:
            # is_file() before open(), because the globs take whatever is in
            # the directory: one `mkfifo stale.pub` in the images dir made
            # read_text() block until a writer appeared, and nothing above
            # here has a wall-clock bound. It is False for a FIFO, a socket
            # and a device node, and it does not block on any of them. The
            # size guard cannot stand in for it -- a FIFO stats as 0 bytes.
            if not pub.is_file() or pub.stat().st_size > _PUB_FILE_MAX_BYTES:
                continue
            text = pub.read_text(errors="replace")
        except (OSError, ValueError):
            continue
        private = pub.with_name(pub.name[:-4]) if pub.name.endswith(".pub") else None
        for line in text.splitlines():
            key = _parse_pubkey_line(line)
            if not key or key["fingerprint"] in index:
                continue
            index[key["fingerprint"]] = {
                "pub": pub,
                "private": private if private and private.exists() else None,
                "comment": key["comment"],
            }
    return index


def _ssh_identity(image: str | None, info: dict[str, Any], cfg: VMConfig,
                  ga_info: dict[str, Any]) -> dict[str, Any]:
    """What to print on the SSH and Key lines, and how sure we are of it.

    Three outcomes, which must not look alike. The guest named a key we hold
    locally: the ssh command carries that key and is pasteable. The guest named
    keys we do not hold: the fingerprints, so the user knows what to go find,
    and no -i that would fail. The guest could not be asked: the configured
    default, labelled unverified, because printing a guess in the shape of a
    fact is what sent a user into three password prompts.
    """
    accepted = _guest_accepted_keys(ga_info)

    if accepted is None:
        pub_lines, private = _ssh_key_for_image(image, info, cfg)
        keys = [k for k in (_parse_pubkey_line(l) for l in pub_lines) if k]
        for k in keys:
            k["path"] = None
            k["accepted"] = None
        return {
            "source": "configured" if keys or private else None,
            "key_file": str(private) if private is not None else None,
            "keys": keys[:_GUEST_KEY_LIMIT],
            "note": ("key not verified against the guest" if keys or private
                     else None),
        }

    # A cert-authority line is not a fourth outcome, it is not a login key at
    # all: it tells sshd which CA may sign certificates, and offering its
    # private half as an identity gets a refusal.
    usable = [k for k in accepted if not k["cert_authority"]]

    index = _local_key_index(image, info, cfg) if usable else {}

    # An unrestricted key first: a restricted one still authenticates, so it
    # is worth offering, but only if nothing better is here.
    match = next((k for k in usable
                  if k["fingerprint"] in index and not k["restrictions"]), None)
    if match is None:
        match = next((k for k in usable if k["fingerprint"] in index), None)

    keys: list[dict[str, Any]] = []
    for k in accepted[:_GUEST_KEY_LIMIT]:
        local = index.get(k["fingerprint"]) if not k["cert_authority"] else None
        keys.append({
            "fingerprint": k["fingerprint"],
            "comment": k["comment"],
            "path": str(local["pub"]) if local else None,
            "accepted": not k["cert_authority"],
            "cert_authority": k["cert_authority"],
            "restrictions": list(k["restrictions"]),
        })

    if match is None:
        if not accepted:
            note = "guest accepts no keys at all"
        elif not usable:
            note = "guest lists only cert-authority keys, which do not log in"
        else:
            note = "guest accepts no key found locally"
        return {"source": "guest", "key_file": None, "keys": keys, "note": note}

    private = index[match["fingerprint"]]["private"]
    notes = []
    if private is None:
        notes.append("the accepted key has no private half here")
    if match["restrictions"]:
        # from= may exclude this host, expiry-time= may have passed and
        # command= yields no shell, so the command below is not a promise.
        notes.append("the accepted key is restricted ("
                     + ", ".join(match["restrictions"]) + ")")
    return {
        "source": "guest",
        "key_file": str(private) if private is not None else None,
        "keys": keys,
        "note": "; ".join(notes) or None,
    }


# Runs under `docker exec python3 -c` for a containerised VM and under this
# interpreter as a subprocess otherwise, so subprocess.run's timeout is a hard
# bound in both cases -- an in-process connect() to a wedged guest's socket
# would not be, and inspect must never hang on one VM of a 32-VM fleet.
_GA_SCRIPT = r"""
import base64, socket, json, sys, random, time

def _ga_run(path, timeout=2.0, budget=8.0):
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

    deadline = time.monotonic() + budget

    # Everything below draws on that one budget, and the two kinds of work it
    # pays for fail in opposite directions. The device probes -- partitions,
    # KFD nodes, NVMe subsystem NQNs -- are a bounded handful of reads that
    # every inspect wants, and a starved one used to render as a confident
    # EMPTY LIST: "this VM has no GPU". The session walk is the open-ended
    # one, as long as the guest has accounts and sessions, and a starved one
    # renders as "unknown". So the bounded always-wanted work goes first and
    # the open-ended work takes what is left.
    #
    # This floor is the other half: it is not a target for the probes, which
    # normally finish in a fraction of it, but an agent slow enough to spend
    # the whole budget above would otherwise leave Login permanently unknown
    # on that guest. At the floor the probes stop and say so, and the walk
    # gets the remaining quarter.
    device_deadline = deadline - budget * 0.25

    result = {}
    for cmd, key in [("guest-get-host-name", "hostname"),
                      ("guest-network-get-interfaces", "interfaces"),
                      ("guest-get-disks", "disks"),
                      ("guest-get-fsinfo", "fsinfo"),
                      ("guest-get-users", "users")]:
        if time.monotonic() > deadline:
            break
        try:
            r = send_recv(cmd)
            if "return" in r:
                result[key] = r["return"]
            # CommandNotFound: older agent or guest agent compiled without this command.
        except (socket.timeout, ValueError):
            pass

    # Paths that opened but whose contents did not come back whole: the read
    # errored, or it stopped before end of file. The host needs these by name,
    # because a short read of authorized_keys is not a guest with no keys and
    # a short read of a logind user file is not a user with no sessions.
    partial = []

    def open_file(path):
        # True/False that the path opened, or None when the agent could not be
        # asked at all -- which is not evidence that the path is missing.
        try:
            r = send_recv("guest-file-open", {"path": path, "mode": "r"})
        except (socket.timeout, ValueError):
            return None
        handle = r.get("return")
        if not isinstance(handle, int):
            return False
        try:
            send_recv("guest-file-close", {"handle": handle})
        except Exception:
            pass
        return True

    def read_file(path, count=65536):
        # The agent has no readdir and no command for disk capacity or for
        # GPUs at all, so sysfs/procfs reads are the only read-only source.
        try:
            r = send_recv("guest-file-open", {"path": path, "mode": "r"})
        except (socket.timeout, ValueError):
            # An agent that did not answer is not a file that is absent, and
            # this branch used to be the one place that confused them: both
            # returned None with nothing in `partial`, so the logind walk
            # skipped the file, still claimed it had looked everywhere, and a
            # guest with a live ssh session printed "Login: none". open_file
            # next door separates the three states; so does this now.
            partial.append(path)
            return None
        handle = r.get("return")
        if not isinstance(handle, int):
            return None  # an error response to the OPEN: the file is not there
        try:
            rr = send_recv("guest-file-read", {"handle": handle, "count": count})
            ret = rr.get("return")
            # An error response to the READ used to be indistinguishable from
            # an empty file -- both became "" -- and only a failed OPEN
            # returned None, which is what every caller tested. So a file that
            # could not be read kept the enumeration above looking complete,
            # and an unreadable authorized_keys rendered as "the guest accepts
            # no keys at all". A read that did not happen is an absent answer,
            # exactly like an open that did not happen.
            if not isinstance(ret, dict) or not isinstance(ret.get("buf-b64"), str):
                partial.append(path)
                return None
            data = base64.b64decode(ret["buf-b64"])
            eof = ret.get("eof")
            if eof is None:
                # eof is mandatory in the QGA schema, so an agent omitting it
                # is already broken -- but treating absence as a short read
                # would mark every file partial and turn a whole fleet
                # "unknown", which is the over-correction. A read that came
                # back shorter than the count asked for is the end of a
                # regular file, which is the same evidence eof carries.
                eof = len(data) < count
            if not eof:
                partial.append(path)
            return data.decode("utf-8", "replace")
        except Exception:
            partial.append(path)
            return None
        finally:
            # An abandoned handle leaks inside someone's live guest.
            try:
                send_recv("guest-file-close", {"handle": handle})
            except Exception:
                pass

    files = {}

    def grab(path):
        if time.monotonic() > deadline:
            return None
        text = read_file(path)
        if text is not None:
            files[path] = text
        return text

    # One read gives every block device's size; cheaper than N sysfs reads.
    grab("/proc/partitions")

    # Which keys the guest accepts is a guest fact, and inspect used to print
    # the configured ssh_key_file instead: a fleet guest whose authorized_keys
    # holds only the injected fleet key was advertised as reachable with
    # ~/.ssh/id_rsa, and following that line got a password prompt and a
    # permission denial. The home directory comes from the guest's own passwd
    # entry because /home/<user> is wrong for root and for a relocated home.
    passwd = grab("/etc/passwd")
    user = __USERNAME__
    if user:
        home = ""
        for line in (passwd or "").splitlines():
            f = line.split(":")
            if len(f) >= 6 and f[0] == user:
                home = f[5]
                break
        if not home:
            home = "/root" if user == "root" else "/home/" + user
        akeys = home.rstrip("/") + "/.ssh/authorized_keys"
        if grab(akeys) is not None:
            result["authorized_keys"] = akeys

    # Node 0 is the CPU node. Probing stops at the first ENOENT because the
    # agent cannot list a directory -- and that ENOENT is the only thing that
    # makes the node count a fact. A read that failed, an agent that went
    # quiet, the 16-node cap or the floor above all leave gpu_complete False,
    # so an empty GPU list reads as unknown rather than as a VM with no GPU.
    gpu_complete = False
    for n in range(1, 17):
        if time.monotonic() > device_deadline:
            break
        node = "/sys/class/kfd/kfd/topology/nodes/%d/properties" % n
        before = len(partial)
        text = read_file(node)
        if text is None:
            gpu_complete = len(partial) == before
            break
        files[node] = text
    result["gpu_complete"] = gpu_complete

    # The NQN only ever refines a disk's origin -- without it _block_origin
    # falls back to the command line and finally to "unknown" -- so a miss
    # here costs a label, never a false claim, and it needs no flag of its own.
    for d in result.get("disks") or []:
        if time.monotonic() > device_deadline:
            break
        name = d.get("name") or ""
        if d.get("partition") or not name.startswith("/dev/nvme"):
            continue
        grab("/sys/block/%s/device/subsysnqn" % name.rsplit("/", 1)[-1])

    if result.get("users") == [] and time.monotonic() <= deadline:
        # guest-get-users reads utmp and nothing else, and Ubuntu 26.04 has
        # dropped utmp -- it returns [] there even with someone logged in over
        # ssh. Whether the file exists is what separates "nobody is logged in"
        # from "this agent cannot tell", and an empty answer must not be
        # reported as the first when it is the second.
        #
        # The key is set only when the probe gave a definite answer. Leaving
        # it absent is the whole point: the host grants "none" to utmp only on
        # `is True`, so a budget spent before this line, or an agent that went
        # quiet during it, cannot pass itself off as an empty utmp.
        probed = open_file("/var/run/utmp")
        if probed is not None:
            result["utmp"] = probed

    if not result.get("users") and result.get("utmp") is not True:
        # No usable utmp means guest-get-users can never answer on this guest,
        # so fall through to systemd-logind, whose state is plain files under
        # /run -- reads, so no command runs inside the guest.
        #
        # Opening the sessions directory is the readability sentinel: logind
        # creates it at startup, so it exists whether or not anyone is on the
        # box. Without it, finding no sessions would be indistinguishable
        # from logind not running, and inspect would report a confident
        # "none" it has no evidence for. It is an OPEN and not a read because
        # a directory has no bytes to give: read_file returns None for one,
        # the way it does for any read that did not happen.
        opened = (open_file("/run/systemd/sessions")
                  if time.monotonic() <= deadline else None)
        if opened is not None:
            result["logind"] = opened
        if opened:
            # Whether the enumeration FINISHED is a separate fact from whether
            # it started, and the host must have it: every way this falls
            # short -- deadline, cap, unreadable passwd, a file that would not
            # read -- used to produce the same empty list as a guest nobody is
            # on, so a guest with a live ssh session printed "Login: none".
            # "none" has to mean we looked everywhere.
            #
            # The claim is positive about the WHOLE file, not merely about
            # having got something: passwd is read with one 65536-byte read,
            # and a larger file comes back truncated. The path is in `partial`
            # when that happens, but `passwd is not None` did not test for it
            # and partial_before is sampled below -- after the grab -- so the
            # growth check could not see it either. Every account past the cut
            # was dropped and the guest still reported "none". Raising the
            # read size is not a fix; the next larger passwd brings it back.
            complete = passwd is not None and "/etc/passwd" not in partial

            # And /etc/passwd has to be the whole account list, which is a
            # claim about nsswitch.conf, not about passwd. A session owned by
            # an sssd, LDAP or NIS account is not in there at all, so the uid
            # walk below never looks for it -- `files` and `compat` are the
            # file itself, and `systemd` adds only DynamicUser accounts, which
            # cannot hold an interactive login. Anything else and the walk
            # cannot claim to have looked everywhere.
            #
            # An unreadable or absent nsswitch.conf leaves the claim alone:
            # glibc's built-in default is files, and marking every guest
            # without the file unknown would be a worse answer than the bug.
            # The hole this leaves is systemd-homed, which `systemd` also
            # covers and which can hold a real login.
            nsswitch = grab("/etc/nsswitch.conf")
            for line in (nsswitch or "").splitlines():
                key, _, sources = line.partition(":")
                if key.strip() != "passwd":
                    continue
                if any(s not in ("files", "compat", "systemd")
                       for s in sources.split("#", 1)[0].split()):
                    complete = False
                break

            partial_before = len(partial)

            # Reading a user file for each of this guest's 37 passwd entries
            # would be 37 round trips whose answer is almost always "no such
            # file", so there is a filter, and the filter has to be one the
            # completeness claim above can survive. The login shell is: an
            # account whose shell is one of these stubs cannot hold an
            # interactive session, so skipping it excludes nobody who could be
            # logged in. A uid range cannot say that -- it skipped a service
            # account at uid 500 with a real shell and still claimed the look
            # was complete, which is a confident "none" over somebody's live
            # session. A count cap is no better: Ubuntu appends regular users
            # at the end of passwd, so it drops exactly the accounts in use.
            uids = []
            for line in (passwd or "").splitlines():
                f = line.split(":")
                if len(f) < 7:
                    continue
                # Digits only: this value names a path below.
                if not f[2].isdigit():
                    continue
                if f[6].rsplit("/", 1)[-1] in ("nologin", "false", "sync",
                                                "true", "shutdown", "halt"):
                    continue
                uids.append(f[2])
                if len(uids) >= 256:
                    complete = False
                    break

            # Session ids are sparse -- 1, 2, 9 on a guest measured here --
            # so probing 1..N and stopping at the first miss, the way the KFD
            # loop below does, would stop at the gap and miss every session
            # past it. SESSIONS= in the per-user file is the enumeration.
            #
            # The user file is guest-writable, so the token loop is bounded
            # and membership is a set: a crafted SESSIONS= line of thousands
            # of ids against a list scan cost 35 s of HOST cpu per VM, despite
            # this budget, because nothing checked the deadline inside it.
            sids = []
            seen_sids = set()
            for uid in uids:
                if time.monotonic() > deadline:
                    complete = False
                    break
                for line in (grab("/run/systemd/users/" + uid) or "").splitlines():
                    if not line.startswith("SESSIONS="):
                        continue
                    toks = line[len("SESSIONS="):].split()
                    if len(toks) > 64:
                        complete = False
                        toks = toks[:64]
                    for sid in toks:
                        if time.monotonic() > deadline:
                            complete = False
                            break
                        # Digits only: these ids name a path, and the file
                        # they come from is writable by the guest.
                        if sid.isdigit() and sid not in seen_sids:
                            seen_sids.add(sid)
                            sids.append(sid)
            if len(sids) > 32:
                complete = False
                sids = sids[:32]
            for sid in sids:
                if time.monotonic() > deadline:
                    complete = False
                    break
                # A listed session can already be gone -- uid 1001 listed
                # session 12 with no file for it -- so a miss is ordinary and
                # is not a reason to stop enumerating.
                grab("/run/systemd/sessions/" + sid)
            if len(partial) > partial_before:
                # A user file or a session file that opened and then would not
                # read is a session this enumeration cannot rule out, so it
                # did not look everywhere. Only reads made inside this block
                # count: an unreadable authorized_keys says nothing about who
                # is logged in.
                complete = False
            result["logind_complete"] = complete

    if files:
        result["files"] = files
    if partial:
        # Bounded for the same reason everything else a guest can grow is:
        # the host only looks up a handful of named paths in it.
        result["partial_files"] = sorted(set(partial))[:64]
    return result

print(json.dumps(_ga_run(__SOCKET__)))
"""


def _ga_query(ga_socket: str | None, container: str | None,
              username: str | None = None) -> dict[str, Any]:
    """Query the QEMU guest agent for live guest information.

    For containerised VMs, proxies the query through 'docker exec' since the
    socket lives inside the container's filesystem namespace. For bare VMs,
    runs the same script under this interpreter. Returns an empty dict on any
    error.

    `username` is whose authorized_keys to read; the agent has no way to ask
    "the user inspect is reporting", so the name has to travel into the guest.
    """
    if not ga_socket:
        return {}
    script = (_GA_SCRIPT.replace("__SOCKET__", repr(ga_socket))
                        .replace("__USERNAME__", repr(username or "")))
    if container:
        cmd = ["docker", "exec", container, "python3", "-c", script]
    else:
        # The same script as a subprocess rather than a second inline copy:
        # the old inline version had an unbounded recv loop that spun on EOF
        # and no wall-clock bound at all, and two copies of the command list
        # drifted apart (the hostname bug below lived in both).
        cmd = [sys.executable, "-I", "-c", script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


def _proc_partitions(text: str | None) -> dict[str, int]:
    """Block device base name -> capacity in bytes, from /proc/partitions.

    No guest-get-* command returns a device's capacity: guest-get-disks has no
    size field at all and guest-get-fsinfo reports the size of a mounted
    *filesystem*, which says nothing about an unmounted or raw passed-through
    device. One read of /proc/partitions covers every device in 1 KiB blocks.
    """
    out: dict[str, int] = {}
    for line in (text or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            out[fields[3]] = int(fields[2]) * 1024
        except ValueError:
            continue
    return out


def _gfx_from_kfd(text: str) -> str | None:
    """gfx target of one KFD topology node, or None if it is not a GPU.

    gfx_target_version packs major/minor/step as decimal MMmmss and the minor
    and step digits are read as hex: 120500 -> gfx1250, 90402 -> gfx942. That
    is the spelling the rocjitsu --config filename uses, so the guest's answer
    and the host's can be compared directly.
    """
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "gfx_target_version":
            try:
                v = int(fields[1])
            except ValueError:
                return None
            if v <= 0:
                return None  # node 0 is the CPU node
            return f"gfx{v // 10000}{(v // 100) % 100:x}{v % 100:x}"
    return None


def _guest_gfx(ga_info: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for path, text in (ga_info.get("files") or {}).items():
        if "/kfd/" not in path:
            continue
        gfx = _gfx_from_kfd(text)
        if gfx:
            out.append(gfx)
    return sorted(out)


def _passthrough_gpus(argv: list[str]) -> list[dict[str, Any]]:
    """Real GPUs handed to a VM, as host BDFs off '-device vfio-pci,host=...'.

    A GPU, a NIC and an NVMe are all passed through by that identical
    argument, so the host PCI class decides which is which -- list_vms._gpus
    covers only the emulated rocjitsu sockets and _nvme_devices deliberately
    drops everything that is not NVMe class, so neither reports this.
    """
    out: list[dict[str, Any]] = []
    for i, a in enumerate(argv):
        if a != "-device" or i + 1 >= len(argv):
            continue
        driver, opts = _device_opts(argv[i + 1])
        if driver != "vfio-pci":
            continue
        bdf = _normalise_bdf(opts.get("host", ""))
        if not bdf:
            continue
        cls = _pci_class(bdf)
        if cls is None or not cls.startswith(_GPU_CLASS_PREFIXES):
            continue
        out.append({"gfx": None, "origin": "passthrough", "source": _short_bdf(bdf)})
    return out


def _gpu_devices(vm: dict[str, Any], ga_info: dict[str, Any],
                 argv: list[str]) -> list[dict[str, Any]] | None:
    """GPUs attached to a VM, each with an origin the host can stand behind.

    Origin is never a guest fact: the guest sees a real-looking KFD node for
    an emulated rocjitsu just as it would for a passed-through card. Only the
    command line distinguishes them -- a vfio-user socket served by rocjitsu
    is emulated by construction, a vfio-pci host BDF of GPU class is real.
    The guest supplies the gfx target for the real case, where no --config
    filename exists to read it off.

    None when nothing was found and the guest probe could not finish: the KFD
    walk shares the in-guest budget with the session enumeration, and a walk
    that never ran is not a VM with no GPU.
    """
    out: list[dict[str, Any]] = []
    for sock in vm.get("vfio_user_sockets") or []:
        for gfx in _rocjitsu_gpus([sock]):
            out.append({"gfx": gfx, "origin": "emulated", "source": Path(sock).name})

    passthrough = _passthrough_gpus(argv)
    out.extend(passthrough)

    unclaimed = list(_guest_gfx(ga_info))
    for entry in out:
        if entry["gfx"] in unclaimed:
            unclaimed.remove(entry["gfx"])

    # A single real GPU and a single gfx the emulated ones did not account for
    # is the only pairing that is not a guess. Anything else stays unknown.
    if len(passthrough) == 1 and len(unclaimed) == 1 and passthrough[0]["gfx"] is None:
        passthrough[0]["gfx"] = unclaimed.pop()

    for gfx in unclaimed:
        out.append({"gfx": gfx, "origin": "unknown", "source": "guest"})

    if not ga_info:
        # No agent to ask, and none needed to say no: the host wrote the
        # command line, so a guest cannot hold a GPU that is not on it. The
        # KFD probe only ever ADDS a device the host-side parse did not
        # recognise, which is why its silence matters only when the host
        # found nothing either.
        return out
    return _resolve(out, ga_info.get("gpu_complete"))


def _block_origin(disk: dict[str, Any], subsysnqn: str | None,
                  nvme: list[dict[str, Any]]) -> tuple[str, str | None]:
    """(origin, host_bdf) for one whole disk the guest reported."""
    bus = (disk.get("address") or {}).get("bus-type")
    if bus in _EMULATED_BUS_TYPES:
        return "emulated", None
    if bus != "nvme":
        return "unknown", None

    nqn = (subsysnqn or "").strip()
    serial = (disk.get("address") or {}).get("serial") or ""
    for d in nvme:
        argv_serial = d.get("serial")
        if d.get("kind") != "emulated" or not argv_serial:
            continue
        # QEMU space-pads the serial into the 20-byte identify field and the
        # guest's udev folds it into ID_SERIAL as "QEMU_NVMe_Ctrl_<serial>..._1",
        # so equality can never match -- but the subsystem NQN carries it
        # unpadded.
        if nqn.endswith(":" + argv_serial) or argv_serial in serial:
            return "emulated", None
    if nqn.startswith(_QEMU_NVME_NQN_PREFIX):
        return "emulated", None

    hostdevs = [d for d in nvme if d.get("kind") == "passthrough"]
    if len(hostdevs) == 1:
        return "passthrough", hostdevs[0].get("bdf")
    if hostdevs:
        return "passthrough", None
    return "unknown", None


def _block_devices(vm: dict[str, Any],
                   ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Whole block devices in the guest, or None if the agent could not say.

    None and [] are different answers: no agent to ask is not the same fact as
    a guest with no disks, and only the guest knows the /dev names at all.
    """
    disks = ga_info.get("disks")
    if disks is None:
        return None
    files = ga_info.get("files") or {}
    sizes = _proc_partitions(files.get("/proc/partitions"))
    mounts = _mountpoints(ga_info.get("fsinfo"))
    nvme = vm.get("nvme") or []

    out: list[dict[str, Any]] = []
    for disk in disks:
        # Partitions carry no address at all; they are folded into the parent
        # device they depend on.
        if disk.get("partition"):
            continue
        name = disk.get("name") or ""
        base = name.rsplit("/", 1)[-1]
        if not base:
            continue
        size = sizes.get(base)
        if base.startswith("loop") and not size:
            continue  # an unbacked loop device is not a disk anyone asked about
        subsysnqn = files.get(f"/sys/block/{base}/device/subsysnqn")
        origin, bdf = _block_origin(disk, subsysnqn, nvme)
        out.append({
            "dev": name,
            "bytes": size,
            "size": _fmt_bytes(size),
            "bus": (disk.get("address") or {}).get("bus-type"),
            "origin": origin,
            "host_bdf": bdf,
            "mountpoints": mounts.get(base, []),
        })
    return out


# A whole device whose own name ends in a digit, so trailing digits are part
# of its identity rather than a partition number.
_WHOLE_DEV_RE = re.compile(r"^(?:nvme\d+n\d+|mmcblk\d+|loop\d+|md\d+)$")

# A partition on such a device: the "p" separator is what marks it.
_P_PART_RE = re.compile(r"^(.*\d)p\d+$")


def _parent_device(dev: str) -> str:
    """The whole-disk name a partition belongs to.

    Stripping trailing digits is right for vda1 -> vda and sdb2 -> sdb, and
    WRONG for every NVMe: nvme0n1 is a whole disk, not partition 1 of
    "nvme0n", and nvme0n1p3 is its third partition. Stripping gave "nvme0n"
    and "nvme0n1p", neither of which names a device, so a mounted NVMe
    matched nothing and inspect showed it with no mountpoint at all. On a
    fleet guest with VM_NVME=8 that is every data disk on the box.
    """
    part = _P_PART_RE.match(dev)
    if part:
        return part.group(1)
    if _WHOLE_DEV_RE.match(dev):
        return dev
    return dev.rstrip("0123456789") or dev


def _mountpoints(fsinfo: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Parent device base name -> mountpoints of its filesystems."""
    out: dict[str, list[str]] = {}
    for fs in fsinfo or []:
        mount = fs.get("mountpoint")
        if not mount:
            continue
        for disk in fs.get("disk") or []:
            dev = (disk.get("dev") or "").rsplit("/", 1)[-1]
            if not dev:
                continue
            # vda1 belongs to vda; a 9p mount has an empty disk list and so
            # never reaches here, which is right -- it is not a block device.
            out.setdefault(_parent_device(dev), []).append(mount)
    return out


_LOGIND_SESSION_DIR = "/run/systemd/sessions"

# systemd >= 256 labels a user manager CLASS=manager, and uses background,
# greeter and lock-screen for the other things that are not someone logging
# in. Anything in the user-* family is a login; an older systemd that writes
# no CLASS at all falls back to the SERVICE check below.
_LOGIN_CLASSES = ("user", "user-early", "user-incomplete")

# A session logind is still opening or already tearing down is not somebody
# logged in. The states that are: active (foreground) and online (logged in,
# not on the foreground seat). Everything else -- closing, opening, offline,
# lingering -- is not a login, and an older logind writing no STATE at all
# falls back to the CLASS and SERVICE checks.
# A set, not a tuple, and deliberately: it is a membership test with no
# meaningful order, and GitGuardian's Authentication Tuple detector reads any
# two-string tuple as a (username, password) pair -- ("active", "online")
# failed the PR's secret scan as a credential.
_LOGIN_STATES = frozenset({"active", "online"})

# A REALTIME outside this window is not a login time, whatever it is. The
# field is guest-written, and both ends have bitten: a 300-digit value raised
# OverflowError out of the division and aborted the inspect of all 32 VMs,
# and a merely large one dated a login to 58000 AD, which nothing downstream
# would have rejected.
_EPOCH_MIN = 946684800.0    # 2000-01-01
_EPOCH_MAX = 4102444800.0   # 2100-01-01


def _epoch(value: Any) -> float | None:
    """A guest-supplied login time, or None when it cannot be one.

    Everything that reaches here was written inside a guest, so the whole
    range of arithmetic failures is in scope, not just a bad string.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (ValueError, ArithmeticError):
        return None
    if out != out or not (_EPOCH_MIN <= out <= _EPOCH_MAX):
        return None
    return out


def _logind_fields(text: str | None) -> dict[str, str]:
    """KEY=VALUE pairs from one /run/systemd/{sessions,users}/* file.

    The file opens with "# This is private data. Do not parse." and this
    parses it anyway. The alternative is guest-exec of loginctl, which means
    running code inside someone's live guest for a session list; a read-only
    file read is the smaller sin, and a format change degrades this to
    "unknown" rather than to a wrong answer.
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _logind_session(text: str | None) -> dict[str, Any] | None:
    """One login session from its logind state file, or None if it is not one."""
    fields = _logind_fields(text)
    user = fields.get("USER")
    if not user:
        return None

    # A lingering user manager is not a login. ernic_fleet_guest enables
    # linger deliberately (AGENTS.md issue 8), so every fleet guest carries
    # one of these per user whether or not anybody is on it, and counting
    # them would leave the line reading "logged in" forever.
    if fields.get("SERVICE") == "systemd-user":
        return None
    cls = fields.get("CLASS")
    if cls and cls not in _LOGIN_CLASSES:
        return None
    state = fields.get("STATE")
    if state and state.lower() not in _LOGIN_STATES:
        return None

    # REALTIME is epoch MICROseconds. ArithmeticError covers the OverflowError
    # an enormous value raises out of the division -- it is not a ValueError,
    # so the guard missed it and one crafted file took down the whole run.
    try:
        login: float | None = int(fields["REALTIME"]) / 1_000_000
    except (KeyError, ValueError, ArithmeticError):
        login = None
    if login is not None and not (_EPOCH_MIN <= login <= _EPOCH_MAX):
        login = None

    host = fields.get("REMOTE_HOST") if fields.get("REMOTE") == "1" else None
    return {
        "user": _sanitise(user),
        "login_time": login,
        "remote_host": _sanitise(host) if host else None,
        "service": _sanitise(fields.get("SERVICE") or "") or None,
    }


def _logind_sessions(ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Login sessions from logind state, or None when that state was unreadable.

    Opening the sessions directory is the evidence that logind answered, but
    only that the enumeration STARTED. Finishing it is a separate fact, which
    the in-guest script reports as logind_complete: without it an enumeration
    cut short by the deadline, by a cap or by a file that would not read
    looked exactly like a guest nobody is on, and a guest with a live ssh
    session on it printed "Login: none".

    The sentinel is the script's own `logind` flag rather than the presence of
    the directory among the files it read back: a directory opens and then
    fails the read, so it never had any bytes to carry, and the day read_file
    stopped inventing "" for a failed read that sentinel would have silently
    gone away.
    """
    if ga_info.get("logind") is not True:
        return None
    files = ga_info.get("files") or {}
    out: list[dict[str, Any]] = []
    for path, text in files.items():
        head, _, sid = path.rpartition("/")
        if head != _LOGIND_SESSION_DIR or not sid:
            continue
        session = _logind_session(text)
        if session is None:
            continue
        session["session"] = _sanitise(sid, 32)
        out.append(session)
    out.sort(key=lambda s: (s["login_time"] or 0.0, s["session"]))
    return _resolve(out, ga_info.get("logind_complete"))


def _utmp_sessions(ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Logged-in users from guest-get-users, or None when it did not answer.

    Two pieces of evidence license an empty answer here, and both are
    positive. The file has to exist: the script reports `utmp` only once it
    has probed for it, so an absent key is a probe that never ran -- the 8 s
    in-guest budget spent during the guest-get-* commands is enough, and on a
    loaded fleet guest answering in ~1.75 s a command it happens every time.
    Reading that absence as "utmp exists and is empty" returned a confident
    "none" and skipped logind entirely.

    And every entry the agent returned has to be one we could read a name out
    of. An agent spelling that field differently yields a non-empty answer
    that parses to nothing, which is this code not understanding the shape --
    unknown, and worth falling through to logind for, not a claim that the
    guest is empty.
    """
    users = ga_info.get("users")
    if not isinstance(users, list):
        return None
    out: list[dict[str, Any]] = []
    for u in users:
        name = u.get("user") if isinstance(u, dict) else None
        if not name:
            continue
        out.append({
            "user": _sanitise(str(name)),
            "login_time": _epoch(u.get("login-time")),
            "remote_host": None,
            "session": None,
            "service": None,
        })
    return _resolve(out, ga_info.get("utmp") is True and len(out) == len(users))


def _ssh_sessions(ga_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Logged-in users, or None when neither source could answer.

    guest-get-users comes first: where utmp exists it is one command instead
    of a directory's worth of file reads, and it is what every guest older
    than this fleet has. It dedupes by username and reads utmp only, so it
    cannot tell an ssh login from a console one and reports users, not
    sessions.

    It cannot answer at all on this repo's Ubuntu 26.04 guests, which ship no
    /var/run/utmp: it returns [] there with an ssh session open. That empty
    answer used to be the end of it, and the Login line read "unknown" for a
    guest somebody was sitting on. logind knows, and its state is files, so
    anything utmp cannot vouch for falls through to those.
    """
    sessions = _utmp_sessions(ga_info)
    if sessions:
        return sessions
    if sessions == []:
        # The only "none" utmp is entitled to: the file was there and held
        # nobody. Every other way of being empty is None above, and logind is
        # the second opinion that may still name somebody.
        return []
    return _logind_sessions(ga_info)


def _vm_argv(pid: int) -> list[str]:
    """The VM's command line. list_vms.collect() parses and discards it."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _record(vm: dict[str, Any], cfg: VMConfig) -> dict[str, Any]:
    info = _read_vm_info(vm["image"])
    username = info.get("username") or cfg.username
    ga_info = _ga_query(vm.get("ga_socket"), vm["container"], username)
    ssh = _ssh_identity(vm["image"], info, cfg, ga_info)

    port = vm["ssh_port_host"]
    identity = f" -i {ssh['key_file']}" if ssh["key_file"] else ""
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
        "ssh_keys": ssh["keys"],
        "ssh_key_file": ssh["key_file"],
        "ssh_key_source": ssh["source"],
        "ssh_key_note": ssh["note"],
        "ssh_cmd": ssh_cmd,
        "image": vm["image"],
        "container": vm["container"],
        "uptime_seconds": vm["uptime_seconds"],
        "vfio_user_sockets": vm["vfio_user_sockets"],
        "qmp_socket": vm.get("qmp_socket"),
        "qmp_available": vm.get("qmp_available", False),
        "ga_socket": vm.get("ga_socket"),
        "ga_available": vm.get("ga_available", False),
        "gpus": _gpu_devices(vm, ga_info, _vm_argv(vm["pid"])),
        "block_devices": _block_devices(vm, ga_info),
        "ssh_sessions": _ssh_sessions(ga_info),
    }
    if ga_info:
        # The raw sysfs and procfs text is an input to the three collectors
        # above, not an answer: several KiB per VM of it would bury --json,
        # which already takes 60 lines to get through one VM's interfaces.
        ga_info.pop("files", None)
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
        note = f"   ({r['ssh_key_note']})" if r.get("ssh_key_note") else ""
        print(f"  SSH:       {ssh}{note}")
        print(f"  User:      {r['username']}")
        for key in r.get("ssh_keys") or []:
            print(f"  Key:       {_key_line(key)}")
        vcpus = r["vcpus"] or "-"
        mem = f"{r['memory_mib']} MiB" if r["memory_mib"] else "-"
        print(f"  Arch:      {r['arch']}  vCPUs: {vcpus}  Memory: {mem}")
        gpus = r.get("gpus")
        if gpus is None:
            # Printing nothing here would read as "no GPU" on a line-oriented
            # listing, which is the claim this has no evidence for.
            print("  GPU:       unknown")
        for gpu in gpus or []:
            gfx = gpu["gfx"] or "unknown gfx"
            source = f", {gpu['source']}" if gpu.get("source") else ""
            print(f"  GPU:       {gfx} ({gpu['origin']}{source})")
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
        devices = r.get("block_devices")
        if devices is None and r.get("ga_socket"):
            # Same rule as the Login line directly below, for the same reason:
            # an agent that is there and did not answer leaves the disks
            # unasked, and a blank where the Disk lines go reads as a VM with
            # no disks. A VM with no agent socket at all prints neither line.
            print("  Disk:      unknown")
        for label, line in _disk_lines(devices):
            # Labels are padded to the same 11 columns every other repeated
            # label uses, so "Boot Disk:" lines up with "Disk:" rather than
            # pushing its value out by five.
            print(f"  {label + ':':<11}{line}")
        login = _login_line(r)
        if login is not None:
            print(f"  Login:     {login}")
        print()


def _key_line(key: dict[str, Any]) -> str:
    """A fingerprint and a comment, never a whole authorized_keys line.

    The fingerprint is what `ssh-keygen -lf` prints, so it can be compared
    against a file by hand; the full key would be four lines of base64 that
    nobody reads, and the point of the line is which key, not what it is.
    """
    parts = [key["fingerprint"]]
    if key.get("comment"):
        parts.append(_sanitise(key["comment"]))
    if key.get("cert_authority"):
        parts.append("(cert-authority, not a login key)")
        return " ".join(parts)
    if key.get("accepted") and key.get("path"):
        parts.append(f"({key['path']}, accepted by guest)")
    elif key.get("accepted"):
        parts.append("(accepted by guest, not found locally)")
    else:
        parts.append("(configured default, unverified)")
    if key.get("restrictions"):
        parts.append("(restricted: %s)" % ", ".join(key["restrictions"]))
    return " ".join(parts)


def _disk_sort_key(dev: str) -> list[Any]:
    """Natural order, so nvme10n1 sorts after nvme9n1 rather than after nvme1n1.

    Plain lexicographic order is alphabetical only while every number is the
    same width. A guest with 8 NVMe is fine either way; one with 10+ is not,
    and VM_NVME has no ceiling.
    """
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", dev)
    ]


def _is_boot_disk(d: dict[str, Any]) -> bool:
    """The disk carrying /, which is not reliably the first one listed.

    The guest agent returns disks in its own order -- on a fleet guest with 8
    NVMe the root /dev/vda came back second -- so the boot disk has to be
    identified by where it is mounted, not by position.
    """
    return "/" in (d.get("mountpoints") or [])


def _disk_lines(
    devices: list[dict[str, Any]] | None,
) -> list[tuple[str, str]]:
    """(label, text) per disk, in the repeated-label style of Key: and IP:.

    Every disk, with the boot disk FIRST and labelled "Boot Disk" rather than
    sorted in among the data disks under a marker. The root device is the one
    you look for first, so it should not be something you find by scanning a
    column -- on a guest with 8 NVMe it sorted to the bottom, after everything
    it is least like.

    The rest follow in natural order. It used to print the first six in
    agent order and then "+N more", which hid a third of the devices behind a
    count and could hide the root disk itself. Truncating a list whose length
    is a setting (VM_NVME) was the wrong call: it is as long as the operator
    asked for.

    Nothing at all when the agent could not answer, matching how a failed
    hostname or interfaces query leaves no trace in the record.
    """
    if not devices:
        return []
    ordered = sorted(devices, key=lambda x: _disk_sort_key(x["dev"]))
    # Stable within each group, so several boot-ish disks keep natural order.
    ordered.sort(key=lambda d: not _is_boot_disk(d))

    lines = []
    for d in ordered:
        origin = d["origin"]
        if origin == "passthrough" and d.get("host_bdf"):
            # A host BDF is a different address space from the guest's, so it
            # is labelled rather than printed bare.
            origin = f"passthrough host:{_short_bdf(d['host_bdf'])}"
        bus = f" ({d['bus']})" if d.get("bus") else ""
        mounts = "  " + ",".join(d["mountpoints"]) if d.get("mountpoints") else ""
        label = "Boot Disk" if _is_boot_disk(d) else "Disk"
        lines.append(
            (label, f"{d['dev']:<14} {d['size']:>10}  {origin}{bus}{mounts}"))
    return lines


def _login_line(record: dict[str, Any]) -> str | None:
    """"none", "unknown", or the logged-in users.

    Nobody logged in and nobody to ask are different facts, so an agent that
    is there but did not answer says unknown rather than claiming the guest
    is empty. A VM with no agent socket at all prints no line, like the rest
    of the guest-derived output.
    """
    sessions = record.get("ssh_sessions")
    if sessions is None:
        return "unknown" if record.get("ga_socket") else None
    if not sessions:
        return "none"
    out = []
    now = time.time()
    for s in sessions:
        since = ""
        login = _epoch(s.get("login_time"))
        if login:
            # Clock time and elapsed, because they answer different questions:
            # "20:52" is what you match against a log line, "7m" is how long
            # someone has been on a guest you are about to restart. _fmt_uptime
            # is the same formatter the Uptime field uses, so a session and the
            # VM holding it read in one unit. Both can still fail on a value
            # that survived the range check in a --json record, and a timestamp
            # is not worth aborting the run for.
            try:
                stamp = time.strftime("%H:%M", time.localtime(login))
                elapsed = _fmt_uptime(max(0, int(now - login)))
                since = f" (since {stamp}, {elapsed})"
            except (ValueError, OSError, ArithmeticError):
                since = ""
        host = f" from {_sanitise(str(s['remote_host']))}" if s.get("remote_host") else ""
        out.append(f"{_sanitise(str(s['user']))}{host}{since}")
    return ", ".join(out)
