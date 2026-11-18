"""Host TCP ports: the bind probe, and the picker behind `--ssh-port auto`."""

from __future__ import annotations

import ipaddress
import socket

DEFAULT_SSH_PORT = 2222
# How far above the default the picker walks, and how often a caller that can
# see QEMU lose the race for a probed port starts it again on the next one.
AUTO_SSH_PORT_SPAN = 100
AUTO_SSH_PORT_ATTEMPTS = 5


def ssh_hostfwd_refused(port: int) -> bytes:
    """What QEMU prints when it cannot bind the SSH forward's host port.

    The rule is part of it: QEMU says the same of any hostfwd, and a held
    extra rule is not cured by moving the SSH port.
    """
    return b"Could not set up host forwarding rule 'tcp::%d-:22'" % port


class NoFreePort(Exception):
    pass


def publish_address(text: str) -> str:
    """The canonical form of a host address a port is published on.

    An IPv4 or IPv6 literal, with or without the brackets docker wants round
    an IPv6 one. A host name is refused: docker takes none in a port mapping,
    and what it would resolve to is not something to leave to the moment of
    `up`. So is an IPv6 zone (`fe80::1%eth0`): Python parses one and docker
    compose answers `invalid IP address`. ValueError says what was wrong.
    """
    bare = text.strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    if "%" in bare:
        raise ValueError(
            f"{text!r} carries an IPv6 zone, which docker does not take in a "
            f"port mapping")
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        raise ValueError(
            f"{text!r} is not an IPv4 or IPv6 address") from None


def publish_mapping(addr: str, host_port: int | str,
                    container_port: int | str) -> str:
    """A compose `ports:` entry bound to `addr`; IPv6 goes in brackets."""
    addr = publish_address(addr)
    if ":" in addr:
        addr = f"[{addr}]"
    return f"{addr}:{host_port}:{container_port}"


def mapping_address(mapping: str) -> str | None:
    """The host address of a compose `ports:` entry; None when it names none.

    None is the case that matters: with no address docker publishes the port
    on every interface the host has.
    """
    if mapping.startswith("["):
        return mapping[1:].partition("]")[0]
    parts = mapping.split(":")
    return parts[0] if len(parts) == 3 else None


def reach_address(addr: str) -> str:
    """The address to connect to, from this host, for a port bound to `addr`.

    A wildcard bind is reached on loopback of the same family; any other
    address is the only one that answers, loopback included.
    """
    ip = ipaddress.ip_address(publish_address(addr))
    if ip.is_unspecified:
        return "127.0.0.1" if ip.version == 4 else "::1"
    return str(ip)


def reach_name(addr: str) -> str:
    """reach_address(), as a person types it: localhost for IPv4 loopback."""
    reached = reach_address(addr)
    return "localhost" if reached == "127.0.0.1" else reached


def is_loopback(addr: str) -> bool:
    """Whether a port bound to `addr` can be reached from this host only."""
    return ipaddress.ip_address(publish_address(addr)).is_loopback


def port_bindable(port: int) -> bool:
    """Whether a listener can be put on `port`, on every address and on loopback.

    This is what a SLIRP hostfwd does, and a docker port publish on every
    interface: bind the wildcard address with SO_REUSEADDR and listen. A
    publish on one address, which is what a stack's <publish-bind-addr>
    gives, needs less, and a port this calls free is free for it too: a
    listener on any address of the port fails the wildcard bind. The option is set here too
    because without it a port merely in TIME_WAIT from the last guest reads
    as taken when QEMU would bind it; the listen is here because with it two
    sockets can share a bind and only the second listen() fails. Loopback is
    asked separately since it is the address the guest is then reached on.
    """
    for addr in ("0.0.0.0", "127.0.0.1"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((addr, port))
                sock.listen(1)
            except OSError:
                return False
    return True


def pick_free_port(
    start: int = DEFAULT_SSH_PORT, last: int | None = None
) -> int:
    """The first bindable port in start..last; NoFreePort when there is none."""
    if last is None:
        last = DEFAULT_SSH_PORT + AUTO_SSH_PORT_SPAN - 1
    last = min(last, 65535)
    for port in range(start, last + 1):
        if port_bindable(port):
            return port
    # A retry starts one past a refused port, which can be past the end.
    where = f"in {start}-{last}" if start <= last else f"up to {last}"
    raise NoFreePort(
        f"no free host port for the guest's SSH forward {where}; "
        "free one, or name a port with --ssh-port <number>."
    )


def parse_ssh_port(text: str) -> int | str:
    """argparse type for --ssh-port: a port number, or the word auto."""
    if text.strip().lower() == "auto":
        return "auto"
    # int() alone also takes '+22', '2_222' and non-ASCII digits.
    digits = text.strip()
    port = int(digits) if digits.isascii() and digits.isdigit() else 0
    if not 1 <= port <= 65535:
        raise ValueError(f"{text!r} is not a port number or 'auto'")
    return port
