"""Serve rocm-ernic stats files as Prometheus metrics.

`rocm-ernic -S PATH` rewrites a human-readable stats dump every ~1s. This
turns a directory of them into a Prometheus scrape endpoint, one series set
per instance.

Why this exists when rocm-ernic ships prometheus/ernic-exporter: that exporter
is built for the systemd/ernicctl deployment. It requires an instances.json
manifest that ernicctl writes, it only looks at files whose stem parses as an
integer, and it reports liveness with pid_alive() against PIDs from its own
namespace. None of those survive the compose stack, where each server is PID 1
in its own container and nothing writes a manifest. The CI container image also
carries only the rocm-ernic binary -- no exporter, no ernicctl, no
prometheus_client.

So the code is separate but the *schema* is not: metric names, label names and
the " : " parsing contract are taken from that exporter verbatim, so
prometheus/grafana/ernic-dashboard.json works against this endpoint unmodified.
Keep them in step.
"""

from __future__ import annotations

import argparse
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# (metric, stats-file key, help). Names match rocm-ernic's
# prometheus/ernic-exporter INSTANCE_COUNTERS.
_INSTANCE_COUNTERS = [
    ("ernic_ip_bytes_tx_total", "total_ip_bytes_tx", "Total IP/Ethernet bytes transmitted"),
    ("ernic_ip_bytes_rx_total", "total_ip_bytes_rx", "Total IP/Ethernet bytes received"),
    ("ernic_rdma_bytes_sent_total", "total_bytes_sent", "Total bytes sent via RDMA SEND operations"),
    ("ernic_rdma_bytes_received_total", "total_bytes_received", "Total bytes received via RDMA RECV operations"),
    ("ernic_rdma_bytes_read_total", "total_bytes_rdma_read", "Total bytes transferred via RDMA Read"),
    ("ernic_rdma_bytes_write_total", "total_bytes_rdma_write", "Total bytes transferred via RDMA Write"),
    ("ernic_flr_reset_total", "reset_count", "Total FLR/device reset count"),
    ("ernic_commands_total", "commands", "Total admin queue commands processed"),
    ("ernic_interrupts_total", "interrupts", "Total interrupts delivered"),
    ("ernic_mmio_reads_total", "mmio_reads_total", "Total MMIO read operations"),
    ("ernic_mmio_writes_total", "mmio_writes_total", "Total MMIO write operations"),
    ("ernic_stats_writes_total", "Write count", "Number of times the server has written stats"),
]

_INSTANCE_AGGREGATES = [
    ("ernic_rdma_bytes_total",
     ["total_bytes_sent", "total_bytes_received",
      "total_bytes_rdma_read", "total_bytes_rdma_write"],
     "Total RDMA bytes (send+recv+read+write)"),
    ("ernic_ip_bytes_total", ["total_ip_bytes_tx", "total_ip_bytes_rx"],
     "Total IP bytes (tx+rx)"),
]

_QP_COUNTERS = [
    ("ernic_qp_bytes_sent_total", "bytes_sent", "Bytes sent via SEND on this QP"),
    ("ernic_qp_bytes_received_total", "bytes_received", "Bytes received via RECV on this QP"),
    ("ernic_qp_bytes_rdma_read_total", "bytes_rdma_read", "Bytes transferred via RDMA Read on this QP"),
    ("ernic_qp_bytes_rdma_write_total", "bytes_rdma_write", "Bytes transferred via RDMA Write on this QP"),
    ("ernic_qp_wqes_processed_total", "wqes_processed", "Total WQEs processed on this QP"),
    ("ernic_qp_cqes_posted_total", "cqes_posted", "Total CQEs posted on this QP"),
    ("ernic_qp_doorbell_send_total", "doorbell_send", "Send doorbell rings on this QP"),
    ("ernic_qp_doorbell_recv_total", "doorbell_recv", "Receive doorbell rings on this QP"),
]

_WQE_METRIC = "ernic_qp_wqes_by_opcode_total"

# "ernic-3.stats" -> id "3". Falls back to the whole stem, so a file named
# for its service still gets a stable label rather than being dropped -- the
# upstream exporter's int()-only rule silently ignores anything else.
_ID_RE = re.compile(r"(\d+)")


def parse(text: str) -> tuple[dict[str, int | str], dict[str, dict]]:
    """Split a stats dump into (device counters, {qp handle: counters}).

    The " : " separator is a documented contract of pvrdma_stats.c, which
    pads every label specifically so the value never touches the colon.
    """
    device: dict[str, int | str] = {}
    qps: dict[str, dict] = {}
    current_qp: str | None = None
    in_opcodes = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            in_opcodes = False
            continue
        if line.startswith("QP ") and line.endswith(":"):
            current_qp = line[3:-1].strip()
            qps.setdefault(current_qp, {"_opcodes": {}})
            in_opcodes = False
            continue
        if line == "WQEs by opcode:":
            in_opcodes = True
            continue
        if " : " not in line:
            # Any other bare "Section:" header ends the current QP. Without
            # this a device-level key printed after the per-QP block would be
            # attributed to the last QP seen.
            if line.endswith(":"):
                current_qp = None
                in_opcodes = False
            continue

        key, _, val = line.partition(" : ")
        key, val = key.strip(), val.strip()
        try:
            parsed: int | str = int(val)
        except ValueError:
            parsed = val

        if current_qp is not None:
            if in_opcodes:
                qps[current_qp]["_opcodes"][key] = parsed
            else:
                qps[current_qp][key] = parsed
        else:
            device[key] = parsed

    return device, qps


def _as_int(values: dict[str, int | str], key: str) -> int:
    val = values.get(key, 0)
    return val if isinstance(val, int) else 0


def _escape(label: str) -> str:
    return label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def collect(stats_dir: Path) -> str:
    """Render every *.stats file under stats_dir as Prometheus text."""
    instances = []
    for path in sorted(stats_dir.glob("*.stats")):
        match = _ID_RE.search(path.stem)
        ident = match.group(1) if match else path.stem
        try:
            device, qps = parse(path.read_text())
        except OSError:
            # The server rewrites the file in place with no atomic rename, so
            # a scrape can land mid-write. Skipping beats emitting zeros,
            # which would look like a counter reset to Prometheus.
            continue
        instances.append((ident, device, qps))

    out: list[str] = []
    out.append("# HELP ernic_instances Number of rocm-ernic instances found")
    out.append("# TYPE ernic_instances gauge")
    out.append(f"ernic_instances {len(instances)}")

    def emit(name: str, help_text: str, kind: str, samples: list[tuple[str, int]]) -> None:
        if not samples:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            out.append(f"{name}{{{labels}}} {value}")

    for metric, key, help_text in _INSTANCE_COUNTERS:
        emit(metric, help_text, "counter", [
            (f'ernic_id="{_escape(i)}",role="{_escape(_role(i))}"', _as_int(d, key))
            for i, d, _ in instances
        ])

    for metric, keys, help_text in _INSTANCE_AGGREGATES:
        emit(metric, help_text, "counter", [
            (f'ernic_id="{_escape(i)}",role="{_escape(_role(i))}"',
             sum(_as_int(d, k) for k in keys))
            for i, d, _ in instances
        ])

    for metric, key, help_text in _QP_COUNTERS:
        emit(metric, help_text, "counter", [
            (f'ernic_id="{_escape(i)}",role="{_escape(_role(i))}",qp="{_escape(qp)}"',
             _as_int(stats, key))
            for i, _, qps in instances
            for qp, stats in sorted(qps.items())
        ])

    emit(_WQE_METRIC, "WQEs processed on this QP by opcode", "counter", [
        (f'ernic_id="{_escape(i)}",role="{_escape(_role(i))}",'
         f'qp="{_escape(qp)}",opcode="{_escape(opcode)}"', count)
        for i, _, qps in instances
        for qp, stats in sorted(qps.items())
        for opcode, count in sorted(stats.get("_opcodes", {}).items())
        if isinstance(count, int)
    ])

    return "\n".join(out) + "\n"


def _role(ident: str) -> str:
    """Instance 1 is the mesh manager; 2..N are workers.

    See rocm-ernic/docs/architecture.rst -- the topology is manager/worker,
    not peer-to-peer, and the generated stack wires it that way.
    """
    return "manager" if ident == "1" else "worker"


def run(stats_dir: Path, port: int, addr: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.rstrip("/") not in ("", "/metrics"):
                self.send_error(404)
                return
            body = collect(stats_dir).encode()
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass  # one line per scrape per VM is pure noise at fleet scale

    if not stats_dir.is_dir():
        print(f"Warning: {stats_dir} does not exist yet; serving an empty set",
              file=sys.stderr)
    server = HTTPServer((addr, port), Handler)
    print(f"Serving rocm-ernic metrics from {stats_dir} on {addr}:{port}/metrics",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="qemu-tool ernic-stats")
    p.add_argument("--stats-dir", type=Path, default=Path("/run/ernic-stats"))
    p.add_argument("--port", type=int, default=9840)
    p.add_argument("--addr", default="0.0.0.0")
    args = p.parse_args(argv)
    run(args.stats_dir, args.port, args.addr)
