#!/usr/bin/env python3
"""Turn what the storage flavor's tests said into a report and badges.

    storage-report.py ARTIFACTS_DIR OUT_DIR

ARTIFACTS_DIR is where `qemu-tool quick-start --flavor storage test` leaves
one storage-<client>.md for each client: a line for every test, `storage
<name>: ok, ...` or `storage <name>: FAIL: ...`, and a closing `storage: `
line. OUT_DIR gets:

    storage-report.md        every test of every client, as a table
    badge-storage-*.json     one shields endpoint file for each tracked
                             rate, in the form scripts/render-site-perf.py
                             reads ("142 MB/s")

A tracked rate comes from the first client. Its badge is written only when
that test passed and reported a rate: a missing file leaves the metric at
its last recorded value, which is what a test that did not run should do.

Exits 1 when there is no result to report, or when any test failed, after
writing what it has.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# test name -> (badge file, label). The names are the ones
# ansible/playbooks/roles/storage_setup/tasks/tests.yml gives its tests, and
# the files the `source` entries of METRICS in render-site-perf.py.
TRACKED = {
    "nic-nvmeof-lba": ("badge-storage-nic-nvmeof.json", "NVMe-oF in NIC"),
    "server-nvmeof-first": ("badge-storage-nvmeof.json", "NVMe-oF RDMA"),
    "nfs-rdma": ("badge-storage-nfs.json", "NFS RDMA"),
    "hipfile-server-nvmeof": (
        "badge-storage-hipfile-nvmeof.json", "hipFile NVMe-oF"),
    "hipfile-nfs-rdma": ("badge-storage-hipfile-nfs.json", "hipFile NFS RDMA"),
}

_LINE = re.compile(r"^storage ([a-z0-9-]+): (ok|FAIL)\b[,:]? ?(.*)$")
_RATE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?) (MiB|MB)/s\b")
_SCALE = {"MiB": 1024 * 1024, "MB": 1000 * 1000}


def parse(text: str) -> list[tuple[str, bool, str]]:
    """(name, passed, detail) for each test line, in order."""
    found = []
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match:
            found.append((match.group(1), match.group(2) == "ok",
                          match.group(3)))
    return found


def rate(detail: str) -> float | None:
    """Bytes a second, from the last rate a test's line gives."""
    matches = _RATE.findall(detail)
    if not matches:
        return None
    value, unit = matches[-1]
    return float(value) * _SCALE[unit]


def display(value: float) -> str:
    """A rate as render-site-perf.py's BADGE_RE reads one."""
    for prefix, scale in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if value >= scale:
            return f"{value / scale:.3g} {prefix}B/s"
    return f"{value:.3g} B/s"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    artifacts, out = Path(argv[1]), Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(artifacts.glob("storage-*.md"))
    clients = [(path.stem[len("storage-"):], parse(path.read_text()))
               for path in files]
    clients = [(name, tests) for name, tests in clients if tests]
    if not clients:
        print(f"no storage-*.md with a test line in {artifacts}",
              file=sys.stderr)
        return 1

    lines = ["# Storage tests", "",
             "| Client | Test | Result | Rate | Detail |",
             "|---|---|---|---|---|"]
    failed = 0
    for client, tests in clients:
        for name, passed, detail in tests:
            failed += not passed
            value = rate(detail) if passed else None
            lines.append(
                f"| `{client}` | `{name}` | {'pass' if passed else '**FAIL**'} "
                f"| {display(value) if value else ''} "
                f"| {detail.replace('|', '/')} |")
    total = sum(len(tests) for _, tests in clients)
    lines += ["", f"{total - failed} of {total} tests passed."]
    (out / "storage-report.md").write_text("\n".join(lines) + "\n")

    first = dict((name, (passed, detail))
                 for name, passed, detail in clients[0][1])
    for name, (badge, label) in TRACKED.items():
        passed, detail = first.get(name, (False, ""))
        value = rate(detail) if passed else None
        if value:
            (out / badge).write_text(json.dumps({
                "schemaVersion": 1, "label": label,
                "message": display(value), "color": "blue"}, indent=2) + "\n")

    print(f"{total - failed} of {total} storage tests passed; "
          f"report in {out / 'storage-report.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
