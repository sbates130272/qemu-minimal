"""Serve the LMCache MP coordinator's directory as Prometheus metrics.

The coordinator already exposes `/metrics`, so the obvious question is why
this exists. Because that endpoint does not carry the directory: it is an
OpenTelemetry pipeline whose subscribers all live on the SERVER side (L1, L2,
lookup, transfer), and the coordinator itself registers none of them. Measured
against a coordinator holding 2920 keys and 3120 placements, its `/metrics`
served exactly `lmcache_mp_event_bus_queue_depth`,
`lmcache_mp_event_bus_drain_lag_seconds` and the stock python/process
collectors -- nothing about the directory at all.

The directory is served by the HTTP API instead:

    /directory/stats   num_keys, num_placements, l1_keys_by_instance, blend
    /instances         registered members, their addresses and P2P endpoints
    /instances/usage   per-instance per-tier used/capacity bytes

which is the one view that answers "which node holds what", and the one thing
a fleet dashboard most wants. This polls those three and republishes them as
Prometheus text.

Shaped like ernic_stats.py deliberately -- same stdlib-only HTTP server, same
"a scrape reads live state, nothing is cached" contract -- so the two
exporters behave the same way in the same stack.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Prefix chosen to not collide with the server-side lmcache_mp_* families.
# These are the COORDINATOR's view, which is a different thing measured at a
# different place, and conflating them in one name would make a sum() across
# the fleet double-count.
_NS = "lmcache_coord"


def _escape(value: str) -> str:
    """Escape a Prometheus label value."""
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _labels(**kw: str) -> str:
    return ",".join(f'{k}="{_escape(str(v))}"' for k, v in kw.items() if v != "")


def _get(base: str, path: str, timeout: float) -> Any:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _emit(out: list[str], name: str, kind: str, help_text: str,
          samples: list[tuple[str, float]]) -> None:
    """Emit one metric family, or nothing at all if it has no samples.

    Nothing rather than a zero: a family with no series is how Prometheus
    says "this did not apply", while a fabricated 0 is indistinguishable
    from a real measurement of zero.
    """
    if not samples:
        return
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        suffix = f"{{{labels}}}" if labels else ""
        out.append(f"{name}{suffix} {value}")


def collect(base_url: str, timeout: float = 5.0) -> str:
    """Scrape the coordinator and render Prometheus text."""
    out: list[str] = []

    # Reachability first, and emitted whatever happens -- a dashboard needs to
    # tell "the coordinator says zero" from "the coordinator did not answer",
    # and without this they look identical.
    try:
        directory = _get(base_url, "/directory/stats", timeout)
        up = 1
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        print(f"lmcache-stats: {base_url} unreachable: {exc}", file=sys.stderr)
        out.append(f"# HELP {_NS}_up Coordinator HTTP API reachable")
        out.append(f"# TYPE {_NS}_up gauge")
        out.append(f"{_NS}_up 0")
        return "\n".join(out) + "\n"

    out.append(f"# HELP {_NS}_up Coordinator HTTP API reachable")
    out.append(f"# TYPE {_NS}_up gauge")
    out.append(f"{_NS}_up {up}")

    # ---- key directory --------------------------------------------------
    _emit(out, f"{_NS}_directory_keys", "gauge",
          "Distinct keys in the coordinator's key directory",
          [("", float(directory.get("num_keys", 0)))])
    _emit(out, f"{_NS}_directory_placements", "gauge",
          "Key placements tracked across all instances; exceeds the key "
          "count when a key is resident on more than one node",
          [("", float(directory.get("num_placements", 0)))])

    # The per-node breakdown, and the reason this exporter exists.
    _emit(out, f"{_NS}_l1_keys", "gauge",
          "Keys the coordinator believes are resident in this instance's L1",
          [(_labels(instance_id=inst), float(n))
           for inst, n in sorted(
               (directory.get("l1_keys_by_instance") or {}).items())])

    blend = directory.get("blend") or {}
    _emit(out, f"{_NS}_blend_contents", "gauge",
          "Distinct contents in the CacheBlend index",
          [("", float(blend["num_contents"]))] if "num_contents" in blend else [])
    _emit(out, f"{_NS}_blend_chunks", "gauge",
          "Chunks in the CacheBlend index",
          [("", float(blend["num_chunks"]))] if "num_chunks" in blend else [])

    # ---- membership -----------------------------------------------------
    try:
        instances = (_get(base_url, "/instances", timeout) or {}).get(
            "instances", [])
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        instances = []

    _emit(out, f"{_NS}_instances", "gauge",
          "MP servers currently registered with the coordinator",
          [("", float(len(instances)))])

    # registration_time is an absolute epoch, which is what a dashboard wants:
    # a restart shows as a step, and age is `time() - this`.
    _emit(out, f"{_NS}_instance_registration_timestamp_seconds", "gauge",
          "When this instance last registered, as a unix timestamp",
          [(_labels(instance_id=i.get("instance_id", ""),
                    ip=i.get("ip", ""),
                    p2p_url=i.get("p2p_advertised_url") or ""),
            float(i.get("registration_time", 0)))
           for i in instances if i.get("instance_id")])

    # ---- per-instance, per-tier usage ------------------------------------
    try:
        usage = (_get(base_url, "/instances/usage", timeout) or {}).get(
            "instances", [])
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        usage = []

    used: list[tuple[str, float]] = []
    capacity: list[tuple[str, float]] = []
    ratio: list[tuple[str, float]] = []
    registered: list[tuple[str, float]] = []

    for inst in usage:
        ident = inst.get("instance_id", "")
        if not ident:
            continue
        registered.append((_labels(instance_id=ident),
                           1.0 if inst.get("registered") else 0.0))
        for mod in inst.get("modules") or []:
            lbl = _labels(instance_id=ident,
                          tier=mod.get("tier", ""),
                          backend=mod.get("backend", ""))
            used.append((lbl, float(mod.get("used_bytes") or 0)))
            # capacity_bytes is 0 for a tier that declares none (the fs L2 is
            # bounded by the filesystem, not by LMCache), and usage_ratio is
            # then null. Emit neither rather than a 0 or a NaN: a ratio panel
            # should show no series for a tier that has no ceiling, not a
            # flat zero that reads as "empty".
            cap = mod.get("capacity_bytes") or 0
            if cap:
                capacity.append((lbl, float(cap)))
            if mod.get("usage_ratio") is not None:
                ratio.append((lbl, float(mod["usage_ratio"])))

    _emit(out, f"{_NS}_instance_registered", "gauge",
          "1 when the coordinator considers this instance registered",
          registered)
    _emit(out, f"{_NS}_tier_used_bytes", "gauge",
          "Bytes in use on this instance's tier, as the coordinator sees it",
          used)
    _emit(out, f"{_NS}_tier_capacity_bytes", "gauge",
          "Declared capacity of this tier; absent when the tier declares none",
          capacity)
    _emit(out, f"{_NS}_tier_usage_ratio", "gauge",
          "Used over declared capacity; absent when the tier declares none",
          ratio)

    return "\n".join(out) + "\n"


def run(base_url: str, port: int, addr: str, timeout: float) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.rstrip("/") not in ("", "/metrics"):
                self.send_error(404)
                return
            body = collect(base_url, timeout).encode()
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass  # one line per scrape is pure noise

    server = HTTPServer((addr, port), Handler)
    print(f"Serving LMCache coordinator metrics from {base_url} "
          f"on {addr}:{port}/metrics", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="qemu-tool lmcache-stats")
    p.add_argument("--coordinator-url", default="http://127.0.0.1:9300",
                   help="Base URL of the MP coordinator's HTTP API")
    p.add_argument("--port", type=int, default=9841,
                   help="Port to serve /metrics on (default: 9841, one past "
                        "the ernic exporter's 9840)")
    p.add_argument("--addr", default="0.0.0.0")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="Per-request timeout when polling the coordinator")
    p.add_argument("--once", action="store_true",
                   help="Print one scrape to stdout and exit, for debugging")
    args = p.parse_args(argv)
    if args.once:
        sys.stdout.write(collect(args.coordinator_url, args.timeout))
        return
    run(args.coordinator_url, args.port, args.addr, args.timeout)
