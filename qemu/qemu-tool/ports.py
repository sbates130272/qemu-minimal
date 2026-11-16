"""Host TCP ports: the bind probe, and the picker behind `--ssh-port auto`."""

from __future__ import annotations

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


def port_bindable(port: int) -> bool:
    """Whether a listener can be put on `port`, on every address and on loopback.

    This is what a SLIRP hostfwd and a docker port publish both do: bind the
    wildcard address with SO_REUSEADDR and listen. The option is set here too
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
