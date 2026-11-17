#!/usr/bin/env python3
"""Is every guest exporter a fleet forwards a port for up in Prometheus?

    docker exec <stack>-prometheus-1 \\
        wget -q -O- 'http://127.0.0.1:9090/api/v1/query?query=up' |
      scripts/check-guest-exporters.py --xml qemu/stacks/rocm-golden.xml \\
        --guest 172.31.3.1 --guest 172.31.3.2

A fleet file forwards ports out of each guest with <vm-extra-hostfwd> so
that Prometheus can scrape the exporters behind them, at the address of the
guest's qemu container. Nothing checked the two ends against each other, and
rocm-golden.xml forwarded four ports of which two had nothing listening: the
amd-gpu and hsa-snoop targets were down in every guest while every step of
the daily lane passed, because that lane asked Prometheus only whether it
answered a query.

This reads the "up" query's answer on stdin and, for every guest address
given and every TCP port the fleet forwards, requires a target at
<address>:<port> whose value is 1. A forwarded port with no scrape job fails
as surely as a target that is down. It exits 0 when all are up, 1 when any
is not, naming each on stderr, and 2 when it could not tell.

Standard library only, and no import of qemu_tool: the lane that runs this
installs qemu-tool with pipx, where this interpreter cannot see it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET

FLEET_NS = "http://qemu-tool/fleet/1.0"
# QEMU's hostfwd: [tcp|udp]:[hostaddr]:hostport-[guestaddr]:guestport, and
# run-vm also takes the short "hostport-:guestport". The host side is the
# port open on the qemu container, which is what Prometheus scrapes.
_RULE = re.compile(r"^(?:(tcp|udp):[^:]*:)?(\d+)-[^:]*:\d+$")


class Unknown(Exception):
    """The question could not be answered."""


def forwarded_ports(xml_paths: list[str]) -> list[int]:
    """The TCP ports the last layer naming <vm-extra-hostfwd> forwards.

    Later files win element by element, as they do for qemu-tool.
    """
    value = None
    for path in xml_paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as error:
            raise Unknown(f"{path}: {error}") from error
        element = root.find(f"{{{FLEET_NS}}}vm-extra-hostfwd")
        if element is not None:
            value = element.text or ""
    if value is None:
        raise Unknown("no layer has a <vm-extra-hostfwd>, so the fleet "
                      "forwards no guest exporter")
    ports = []
    for rule in (part.strip() for part in value.split(",")):
        if not rule:
            continue
        match = _RULE.match(rule)
        if match is None:
            raise Unknown(f"<vm-extra-hostfwd> rule {rule!r} is not "
                          "[tcp|udp]:[addr]:port-[addr]:port")
        if match.group(1) != "udp":
            ports.append(int(match.group(2)))
    if not ports:
        raise Unknown("<vm-extra-hostfwd> forwards no TCP port")
    return ports


def targets(answer: str) -> dict[str, dict]:
    """instance -> {"up": bool, "job": str, "vm_name": str}, from "up"."""
    try:
        body = json.loads(answer)
        if body["status"] != "success":
            raise Unknown(f"Prometheus answered {body['status']!r}")
        found = {}
        for sample in body["data"]["result"]:
            metric = sample["metric"]
            found[metric["instance"]] = {
                "up": sample["value"][1] == "1",
                "job": metric.get("job", ""),
                "vm_name": metric.get("vm_name", ""),
            }
        return found
    except (ValueError, KeyError, TypeError, IndexError) as error:
        raise Unknown(f"not the answer to an instant query: {error!r}") from error


def check(ports: list[int], guests: list[str], seen: dict[str, dict]):
    """(lines describing every expected target, the ones that are not up)."""
    lines, bad = [], []
    for guest in guests:
        for port in ports:
            instance = f"{guest}:{port}"
            target = seen.get(instance)
            if target is None:
                state, label = "no scrape job", instance
            else:
                state = "up" if target["up"] else "down"
                label = " ".join(
                    part for part in (target["job"], instance,
                                      f"({target['vm_name']})"
                                      if target["vm_name"] else "") if part)
            lines.append(f"{state:>13}  {label}")
            if state != "up":
                bad.append(f"{label}: {state}")
    return lines, bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every guest exporter a fleet forwards a "
                    "port for is up in Prometheus. Reads the answer to the "
                    "instant query 'up' on stdin.")
    parser.add_argument("--xml", action="append", required=True,
                        metavar="FILE", help="a fleet XML layer; repeatable, "
                        "later files win")
    parser.add_argument("--guest", action="append", required=True,
                        metavar="ADDRESS", help="the address Prometheus "
                        "scrapes a guest at: its qemu container's; repeatable")
    args = parser.parse_args(argv)
    try:
        ports = forwarded_ports(args.xml)
        seen = targets(sys.stdin.read())
    except Unknown as error:
        print(f"check-guest-exporters: {error}", file=sys.stderr)
        return 2
    lines, bad = check(ports, args.guest, seen)
    # Flushed, so the table is above the summary when a caller merges the
    # two streams and reads the last line.
    print("\n".join(lines), flush=True)
    if bad:
        print("guest exporter targets not up: " + "; ".join(bad),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
