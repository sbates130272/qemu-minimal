"""The home dashboard's layout, and what the optional tests leave for it.

Two things the owner saw in Grafana on 2026-11-18, after a passing
"quick-start test --sdxi-test --p4-test": no panel showed either result, and
"some of the panels seem not lined up nicely".

The layout. quickstart.json placed panels by hand and three bands were
wrong: two status tiles started at x=1 and ran into the next row's heading,
the two tiles under that heading filled half the width, and two graphs ran
into the row after. Grafana resolves an overlap by pushing panels down, so
what was drawn was not what the file said. layout_faults() below is the
rule every dashboard under grafana/dashboards/fleet/ now has to pass, in
every stack: rows of bands, each band one height and 24 columns wide, no
gap and no overlap from the first row to the last panel.

The results. Neither optional test leaves anything Prometheus can see once
it has ended. The NIC's server completes an fp8 reduction in place of the
transfer and counts no bytes for it, and one 4 MiB SDXI copy is a blip in a
counter. So a play of vm-quickstart.yml hands each result line to
quickstart-test-metrics.py, which leaves it in the guest for node_exporter's
textfile collector, and the dashboard's P4 row and one SDXI tile read "the
last pass" from there. What is held:

  the script     run for real: the line p4_fp8_bench.c prints, the line the
                 SDXI play echoes, lines that are neither, a guest with no
                 collector directory, a file it cannot replace; and what it
                 writes is parsed as exposition text
  the play       where it sits, who it runs as, and the play itself run by
                 ansible-playbook against local stand-ins for five guests
  the dashboard  the P4 row is in every copy and every quickstart_* series
                 a panel reads is one the script writes

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PLAYBOOKS = REPO / "ansible" / "playbooks"
QUICKSTART = PLAYBOOKS / "vm-quickstart.yml"
SCRIPT_PATH = Path("roles") / "rocjitsu_setup" / "files" / "quickstart-test-metrics.py"
SCRIPT = PLAYBOOKS / SCRIPT_PATH
BENCH_SOURCE = PLAYBOOKS / "roles" / "rocm_ernic_setup" / "files" / "p4_fp8_bench.c"
ROCJITSU_METRICS = PLAYBOOKS / "roles" / "rocjitsu_setup" / "tasks" / "metrics.yml"
COMPOSE = REPO / "qemu" / "compose"
FLEET_DIR = Path("grafana") / "dashboards" / "fleet"
HOME = FLEET_DIR / "quickstart.json"
NAMES = ("rocm-golden", "helios-tray", "mi355x-server")
DOCS = (REPO / "README.md", REPO / "AGENTS.md",
        COMPOSE / "rocm-golden" / "README.md")

SDXI_PLAY = "Copy data through the SDXI device on each guest"
P4_PLAY = "Run the P4 fp8 reduction in the first guest's NIC"
FACTS_PLAY = "Collect the facts the report quotes"
RECORD_PLAY = "Record what the optional tests measured for the dashboard"

P4_ROW = "P4 — FP8 Reduction In The NIC"
P4_SERIES = {"quickstart_p4_fp8_bytes_per_second",
             "quickstart_p4_fp8_operation_seconds",
             "quickstart_p4_fp8_elements",
             "quickstart_p4_fp8_iterations",
             "quickstart_p4_fp8_timestamp_seconds"}
SDXI_SERIES = {"quickstart_sdxi_copy_bytes",
               "quickstart_sdxi_copy_timestamp_seconds"}

COLUMNS = 24

# Every query the two dashboards had at a7b33fe, by panel id: the text and
# not a count. A count passes a metric renamed in all three copies, and five
# such renames (node_network_info, rdma_port_info, ernic_rdma_bytes_total,
# node_load1, ernic_instances) each went through the whole suite. Each
# expression is one line so a metric name can be found with grep. A query
# changed on purpose is changed here in the same commit. fleet.json's
# panels had no id at a7b33fe; the ids are the ones they were given, in the
# order the panels were in.
HOME_QUERIES = {
    2: [
        ('A', 'count(up{job="fleet-node"} == 1) or vector(0)'),
        ('B', 'count(up{job="fleet-node"}) or vector(0)'),
    ],
    3: [
        ('A', 'ernic_instances'),
    ],
    4: [
        ('A', 'sum(rate(ernic_rdma_bytes_total[$__rate_interval]))'),
    ],
    5: [
        ('A', 'sum(rate(ernic_ip_bytes_total[$__rate_interval]))'),
    ],
    6: [
        ('A', 'count(amdgpu_devices_total{job="fleet-node"} >= 1) or vector(0)'),
    ],
    7: [
        ('A', 'sum(rate(sdxi_bytes_moved_total{job="fleet-node",context="1"}[2m]))'),
    ],
    11: [
        ('A', '100 - (avg by (vm_name) (rate(node_cpu_seconds_total{job="fleet-node",mode="idle",vm_name=~"$vm"}[$__rate_interval])) * 100)'),
    ],
    12: [
        ('A', '100 * (1 - (node_memory_MemAvailable_bytes{job="fleet-node",vm_name=~"$vm"} / node_memory_MemTotal_bytes{job="fleet-node",vm_name=~"$vm"}))'),
    ],
    13: [
        ('A', 'node_load1{job="fleet-node",vm_name=~"$vm"}'),
    ],
    14: [
        ('A', 'node_memory_MemAvailable_bytes{job="fleet-node",vm_name=~"$vm"}'),
    ],
    25: [
        ('A', 'rate(node_disk_read_bytes_total{job="fleet-node",device="nvme0n1",vm_name=~"$vm"}[$__rate_interval])'),
        ('B', 'rate(node_disk_written_bytes_total{job="fleet-node",device="nvme0n1",vm_name=~"$vm"}[$__rate_interval])'),
    ],
    26: [
        ('A', 'rate(node_disk_reads_completed_total{job="fleet-node",device="nvme0n1",vm_name=~"$vm"}[$__rate_interval])'),
        ('B', 'rate(node_disk_writes_completed_total{job="fleet-node",device="nvme0n1",vm_name=~"$vm"}[$__rate_interval])'),
    ],
    31: [
        ('A', 'sum(rate(ernic_rdma_bytes_sent_total[$__rate_interval]))'),
        ('B', 'sum(rate(ernic_rdma_bytes_received_total[$__rate_interval]))'),
        ('C', 'sum(rate(ernic_rdma_bytes_read_total[$__rate_interval]))'),
        ('D', 'sum(rate(ernic_rdma_bytes_write_total[$__rate_interval]))'),
    ],
    32: [
        ('A', 'sum by (ernic_id, role) (rate(ernic_rdma_bytes_total[$__rate_interval]))'),
    ],
    33: [
        ('A', 'sum(rate(ernic_ip_bytes_tx_total[$__rate_interval]))'),
        ('B', 'sum(rate(ernic_ip_bytes_rx_total[$__rate_interval]))'),
    ],
    34: [
        ('A', 'sum(rate(ernic_qp_doorbell_send_total[$__rate_interval]))'),
        ('B', 'sum(rate(ernic_qp_doorbell_recv_total[$__rate_interval]))'),
        ('C', 'sum(rate(ernic_qp_cqes_posted_total[$__rate_interval]))'),
        ('D', 'sum(rate(ernic_qp_wqes_processed_total[$__rate_interval]))'),
    ],
    35: [
        ('A', 'rate(ernic_rdma_bytes_sent_total[$__rate_interval])'),
        ('B', 'rate(ernic_rdma_bytes_received_total[$__rate_interval])'),
    ],
    36: [
        ('A', 'count(node_network_info{job="fleet-node",device="rocm-ernic0"}) or vector(0)'),
    ],
    37: [
        ('A', 'count(up{job="rdma-exporter"} == 1) or vector(0)'),
    ],
    41: [
        ('A', 'count(rdma_port_info{job="rdma-exporter",state="ACTIVE"}) or vector(0)'),
    ],
    42: [
        ('A', 'rdma_port_info{job="rdma-exporter"}'),
    ],
    43: [
        ('A', 'rate(rdma_total_bytes_sent_total{job="rdma-exporter",vm_name=~"$vm"}[$__rate_interval])'),
        ('B', 'rate(rdma_total_bytes_received_total{job="rdma-exporter",vm_name=~"$vm"}[$__rate_interval])'),
        ('C', 'rate(rdma_total_bytes_rdma_read_total{job="rdma-exporter",vm_name=~"$vm"}[$__rate_interval])'),
        ('D', 'rate(rdma_total_bytes_rdma_write_total{job="rdma-exporter",vm_name=~"$vm"}[$__rate_interval])'),
    ],
    44: [
        ('A', 'rate(rdma_commands_total{job="rdma-exporter",vm_name=~"$vm"}[$__rate_interval])'),
    ],
    56: [
        ('A', '{job="docker"}'),
    ],
    61: [
        ('A', 'sdxi_up{job="fleet-node",vm_name=~"$vm"}'),
    ],
    62: [
        ('A', 'sum by (vm_name) (increase(sdxi_num_errors_total{job="fleet-node",vm_name=~"$vm"}[$__range]))'),
    ],
    63: [
        ('A', 'rate(sdxi_bytes_moved_total{job="fleet-node",context="1",vm_name=~"$vm"}[2m])'),
    ],
    64: [
        ('A', 'rate(sdxi_num_copy_total{job="fleet-node",context="1",vm_name=~"$vm"}[2m])'),
    ],
    65: [
        ('A', 'rate(sdxi_bytes_compressed_total{job="fleet-node",context="1",vm_name=~"$vm"}[2m])'),
        ('B', 'rate(sdxi_bytes_decompressed_total{job="fleet-node",context="1",vm_name=~"$vm"}[2m])'),
    ],
    66: [
        ('A', 'rate(sdxi_num_compress_total{job="fleet-node",context="1",vm_name=~"$vm"}[2m])'),
        ('B', 'rate(sdxi_num_decompress_total{job="fleet-node",context="1",vm_name=~"$vm"}[2m])'),
    ],
}

FLEET_QUERIES = {
    2: [
        ('A', 'count(up{job="fleet-node"} == 1) or vector(0)'),
        ('B', 'count(up{job="fleet-node"}) or vector(0)'),
    ],
    3: [
        ('A', 'ernic_instances'),
    ],
    5: [
        ('A', 'sum(rate(ernic_rdma_bytes_sent_total[$__rate_interval]))'),
        ('B', 'sum(rate(ernic_rdma_bytes_received_total[$__rate_interval]))'),
        ('C', 'sum(rate(ernic_rdma_bytes_read_total[$__rate_interval]))'),
        ('D', 'sum(rate(ernic_rdma_bytes_write_total[$__rate_interval]))'),
    ],
    6: [
        ('A', 'sum(rate(ernic_ip_bytes_tx_total[$__rate_interval]))'),
        ('B', 'sum(rate(ernic_ip_bytes_rx_total[$__rate_interval]))'),
    ],
    7: [
        ('A', 'sum by (ernic_id, role) (rate(ernic_rdma_bytes_total[$__rate_interval]))'),
    ],
    8: [
        ('A', 'sum(rate(ernic_qp_doorbell_send_total[$__rate_interval]))'),
        ('B', 'sum(rate(ernic_qp_doorbell_recv_total[$__rate_interval]))'),
        ('C', 'sum(rate(ernic_qp_cqes_posted_total[$__rate_interval]))'),
        ('D', 'sum(rate(ernic_qp_wqes_processed_total[$__rate_interval]))'),
    ],
    10: [
        ('A', '100 - (avg by (vm_name) (rate(node_cpu_seconds_total{job="fleet-node",mode="idle",vm_name=~"$vm"}[$__rate_interval])) * 100)'),
    ],
    11: [
        ('A', '100 * (1 - (node_memory_MemAvailable_bytes{job="fleet-node",vm_name=~"$vm"} / node_memory_MemTotal_bytes{job="fleet-node",vm_name=~"$vm"}))'),
    ],
    12: [
        ('A', 'up{job="fleet-node",vm_name=~"$vm"}'),
    ],
}

# GitHub sets CI=true on every runner, and Gen Compose Check, the one
# workflow that runs tests/, sets it on the step as well.
IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI, as the other suites do."""
    if available:
        return
    if IN_CI:
        raise AssertionError(
            f"{what} is missing in CI. These tests must not be skipped "
            "there: install it in Gen Compose Check, or remove the test "
            "deliberately.")
    print(f"SKIP: {what} is not installed; this check is NOT running",
          file=sys.stderr)
    raise unittest.SkipTest(f"{what} is not installed")


# ---------------------------------------------------------------------------
# The layout rule
# ---------------------------------------------------------------------------

def _grid(panel: dict) -> tuple[int, int, int, int]:
    grid = panel["gridPos"]
    return grid["x"], grid["y"], grid["w"], grid["h"]


def _queries(dashboard: dict, ids=None) -> dict:
    """panel id -> [(refId, expr)] for every panel with a query, or for the
    panels in IDS."""
    return {p["id"]: [(t["refId"], t["expr"]) for t in p["targets"]]
            for p in dashboard["panels"]
            if p.get("targets") and (ids is None or p["id"] in ids)}


def _overlap(a: dict, b: dict) -> bool:
    ax, ay, aw, ah = _grid(a)
    bx, by, bw, bh = _grid(b)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def layout_faults(dashboard: dict) -> list[str]:
    """Everything wrong with where a dashboard's panels are; [] when sound.

    A dashboard is rows. Under each row are bands: panels that share a y
    and a height and, side by side from column 0, fill all 24 columns. The
    first band starts on the line under its row, each band starts where the
    one above ends, and the next row starts where the last band ends. So
    nothing overlaps, nothing is pushed, and no line of the page is part
    empty. A collapsed row hides its panels and is a fault too: the page is
    meant to be read by scrolling.
    """
    faults = []
    panels = dashboard["panels"]

    def name(panel):
        return f"{panel.get('title')!r} (id {panel.get('id')})"

    ids = [panel.get("id") for panel in panels]
    for panel in panels:
        if not isinstance(panel.get("id"), int):
            faults.append(f"no id: {name(panel)}")
        if panel["type"] == "row" and (panel.get("collapsed")
                                       or panel.get("panels")):
            faults.append(f"collapsed row: {name(panel)}")
        x, y, w, h = _grid(panel)
        if x < 0 or y < 0 or w < 1 or h < 1 or x + w > COLUMNS:
            faults.append(f"off the grid: {name(panel)} at x={x} w={w}")
    for seen in sorted({i for i in ids if ids.count(i) > 1 and i is not None}):
        faults.append(f"duplicate id: {seen}")

    for at, panel in enumerate(panels):
        for other in panels[at + 1:]:
            if _overlap(panel, other):
                faults.append(f"overlap: {name(panel)} and {name(other)}")

    if not panels or panels[0]["type"] != "row":
        faults.append("the first panel is not a row")
        return faults

    sections: list[tuple[dict, list[dict]]] = []
    for panel in panels:
        if panel["type"] == "row":
            sections.append((panel, []))
        else:
            sections[-1][1].append(panel)

    line = 0
    for row, members in sections:
        x, y, w, h = _grid(row)
        if (x, w, h) != (0, COLUMNS, 1):
            faults.append(f"row is not one full line: {name(row)}")
        if y != line:
            faults.append(f"gap: row {name(row)} is at y={y}, and what is "
                          f"above it ends at y={line}")
        line = y + 1
        if not members:
            faults.append(f"empty row: {name(row)}")
        bands: dict[int, list[dict]] = {}
        for panel in members:
            if _grid(panel)[1] <= y:
                faults.append(f"above its row: {name(panel)} at "
                              f"y={_grid(panel)[1]} under {name(row)} at y={y}")
            bands.setdefault(_grid(panel)[1], []).append(panel)
        for top in sorted(bands):
            band = sorted(bands[top], key=lambda panel: _grid(panel)[0])
            titles = ", ".join(name(panel) for panel in band)
            if top != line:
                faults.append(f"gap: the band at y={top} ({titles}) does not "
                              f"start at y={line}, where the one above ends")
            heights = {_grid(panel)[3] for panel in band}
            if len(heights) != 1:
                faults.append(f"heights differ in the band at y={top}: "
                              f"{titles}")
            edge = 0
            for panel in band:
                if _grid(panel)[0] != edge:
                    break
                edge += _grid(panel)[2]
            else:
                if edge == COLUMNS:
                    edge = None
            if edge is not None:
                total = sum(_grid(panel)[2] for panel in band)
                faults.append(f"band at y={top} does not fill {COLUMNS} "
                              f"columns from 0 (widths total {total}): "
                              f"{titles}")
            line = top + max(heights)
    return faults


def _fleet_dashboards(stack: str) -> list[Path]:
    return sorted((COMPOSE / stack / FLEET_DIR).glob("*.json"))


def _sound() -> dict:
    """Two rows, three bands: the smallest dashboard with every shape."""
    def panel(pid, kind, x, y, w, h):
        return {"id": pid, "type": kind, "title": f"panel {pid}",
                "gridPos": {"x": x, "y": y, "w": w, "h": h}}
    return {"panels": [
        panel(1, "row", 0, 0, 24, 1),
        panel(2, "stat", 0, 1, 12, 3), panel(3, "stat", 12, 1, 12, 3),
        panel(4, "timeseries", 0, 4, 24, 6),
        panel(5, "row", 0, 10, 24, 1),
        panel(6, "timeseries", 0, 11, 8, 6), panel(7, "timeseries", 8, 11, 16, 6),
    ]}


class Layout(unittest.TestCase):

    def test_every_fleet_dashboard_in_every_stack_is_sound(self):
        checked = 0
        for stack in NAMES:
            found = _fleet_dashboards(stack)
            self.assertEqual([path.name for path in found],
                             ["fleet.json", "quickstart.json"], stack)
            for path in found:
                with self.subTest(dashboard=str(path.relative_to(REPO))):
                    self.assertEqual(
                        layout_faults(json.loads(path.read_text())), [])
                    checked += 1
        self.assertEqual(checked, 2 * len(NAMES))

    def test_the_three_stacks_carry_the_same_two_dashboards(self):
        """No generator: an edit goes to all three, and this is what says
        so for fleet.json, which nothing held equal before."""
        for name in ("fleet.json", "quickstart.json"):
            texts = {(COMPOSE / stack / FLEET_DIR / name).read_text()
                     for stack in NAMES}
            self.assertEqual(len(texts), 1, name)

    def test_the_files_are_what_json_dumps_writes(self):
        """So a scripted edit is a small diff: two-space indent, non-ASCII
        kept as it is, one trailing newline."""
        for path in _fleet_dashboards("rocm-golden"):
            text = path.read_text()
            self.assertEqual(
                json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                + "\n", text, path.name)

    def test_the_stand_in_is_sound(self):
        self.assertEqual(layout_faults(_sound()), [])

    def _one(self, change) -> list[str]:
        dashboard = _sound()
        change({p["id"]: p for p in dashboard["panels"]}, dashboard["panels"])
        return layout_faults(dashboard)

    def test_an_overlap_is_found(self):
        faults = self._one(lambda by, _: by[4]["gridPos"].update(y=3))
        self.assertTrue(any(f.startswith("overlap: 'panel 2'") for f in faults),
                        faults)

    def test_a_band_that_does_not_fill_the_width_is_found(self):
        faults = self._one(lambda by, _: by[3]["gridPos"].update(w=6))
        self.assertEqual(len(faults), 1, faults)
        self.assertIn("does not fill 24 columns", faults[0])
        self.assertIn("widths total 18", faults[0])

    def test_a_band_that_starts_off_the_left_edge_is_found(self):
        """Two tiles of 6 at x=1 and x=7: the fault the file had."""
        def change(by, _):
            by[2]["gridPos"].update(x=1, w=6)
            by[3]["gridPos"].update(x=7, w=6)
        faults = self._one(change)
        self.assertTrue(any("does not fill 24 columns" in f for f in faults),
                        faults)

    def test_a_band_of_24_columns_that_overlaps_itself_is_not_passed(self):
        """Widths that total 24 are not enough: they have to tile."""
        def change(by, _):
            by[2]["gridPos"].update(x=0, w=16)
            by[3]["gridPos"].update(x=16, w=8)
            by[6]["gridPos"].update(x=4, w=8)
        faults = self._one(change)
        self.assertTrue(any("band at y=11 does not fill" in f for f in faults),
                        faults)

    def test_a_panel_that_starts_above_its_row_is_found(self):
        faults = self._one(lambda by, _: by[6]["gridPos"].update(y=9))
        self.assertTrue(any(f.startswith("above its row: 'panel 6'")
                            for f in faults), faults)

    def test_a_duplicate_id_and_a_missing_one_are_found(self):
        faults = self._one(lambda by, _: by[7].update(id=2))
        self.assertIn("duplicate id: 2", faults)
        faults = self._one(lambda by, _: by[7].pop("id"))
        self.assertTrue(any(f.startswith("no id: ") for f in faults), faults)

    def test_heights_that_differ_in_a_band_are_found(self):
        faults = self._one(lambda by, _: by[2]["gridPos"].update(h=2))
        self.assertTrue(any(f.startswith("heights differ") for f in faults),
                        faults)

    def test_a_gap_is_found(self):
        def change(by, _):
            for pid in (5, 6, 7):
                by[pid]["gridPos"]["y"] += 2
        faults = self._one(change)
        self.assertEqual(len(faults), 1, faults)
        self.assertTrue(faults[0].startswith("gap: row 'panel 5'"), faults)

    def test_an_empty_row_and_a_collapsed_one_are_found(self):
        def empty(by, panels):
            del panels[5:]
        self.assertTrue(any(f.startswith("empty row: 'panel 5'")
                            for f in self._one(empty)))
        faults = self._one(lambda by, _: by[5].update(collapsed=True))
        self.assertTrue(any(f.startswith("collapsed row") for f in faults),
                        faults)

    def test_a_panel_past_the_right_edge_is_found(self):
        faults = self._one(lambda by, _: by[7]["gridPos"].update(w=17))
        self.assertTrue(any(f.startswith("off the grid: 'panel 7'")
                            for f in faults), faults)

    def test_what_the_home_dashboard_had_is_what_the_check_finds(self):
        """Three rows as committed at a7b33fe, moved up to start at y=0."""
        def panel(pid, title, x, y, w, h, kind="stat"):
            return {"id": pid, "type": kind, "title": title,
                    "gridPos": {"x": x, "y": y - 36, "w": w, "h": h}}
        was = {"panels": [
            panel(38, "Network & Driver Status", 0, 36, 24, 1, "row"),
            panel(36, "rocm-ernic0 (ionic)", 1, 37, 6, 3),
            panel(37, "ionic_rdma (RDMA exporter up)", 7, 37, 6, 3),
            panel(40, "RDMA Device Counters (rdma_exporter)", 0, 39, 24, 1,
                  "row"),
            panel(41, "IB Port Active", 0, 40, 6, 3),
            panel(42, "Link Speed", 6, 40, 6, 3),
            panel(43, "RDMA Bytes/s Per Device", 0, 43, 12, 6),
            panel(44, "RDMA Commands/s Per Device", 12, 43, 12, 6),
            panel(30, "RDMA \u2014 Ernic Mesh", 0, 47, 24, 1, "row"),
            panel(31, "RDMA Throughput \u2014 Fleet Total", 0, 48, 12, 8),
            panel(32, "RDMA By Node", 12, 48, 6, 8),
            panel(33, "TCP Mesh Fleet Traffic", 18, 48, 6, 8),
        ]}
        self.assertEqual(layout_faults(was), [
            "overlap: 'rocm-ernic0 (ionic)' (id 36) and 'RDMA Device "
            "Counters (rdma_exporter)' (id 40)",
            "overlap: 'ionic_rdma (RDMA exporter up)' (id 37) and 'RDMA "
            "Device Counters (rdma_exporter)' (id 40)",
            "overlap: 'RDMA Bytes/s Per Device' (id 43) and "
            "'RDMA \u2014 Ernic Mesh' (id 30)",
            "overlap: 'RDMA Bytes/s Per Device' (id 43) and 'RDMA "
            "Throughput \u2014 Fleet Total' (id 31)",
            "overlap: 'RDMA Commands/s Per Device' (id 44) and "
            "'RDMA \u2014 Ernic Mesh' (id 30)",
            "overlap: 'RDMA Commands/s Per Device' (id 44) and 'RDMA By "
            "Node' (id 32)",
            "overlap: 'RDMA Commands/s Per Device' (id 44) and 'TCP Mesh "
            "Fleet Traffic' (id 33)",
            "band at y=1 does not fill 24 columns from 0 (widths total "
            "12): 'rocm-ernic0 (ionic)' (id 36), 'ionic_rdma (RDMA exporter "
            "up)' (id 37)",
            "gap: row 'RDMA Device Counters (rdma_exporter)' (id 40) is at "
            "y=3, and what is above it ends at y=4",
            "band at y=4 does not fill 24 columns from 0 (widths total "
            "12): 'IB Port Active' (id 41), 'Link Speed' (id 42)",
            "gap: row 'RDMA \u2014 Ernic Mesh' (id 30) is at y=11, and what "
            "is above it ends at y=13",
        ])

    def test_the_fleet_tiles_are_one_width(self):
        """They were 3, 3, 6, 4, 4, 4."""
        home = json.loads((COMPOSE / "rocm-golden" / HOME).read_text())
        tiles = [p for p in home["panels"] if _grid(p)[1] == 1]
        self.assertEqual(len(tiles), 6)
        self.assertEqual({_grid(p)[2] for p in tiles}, {4})

    def test_the_status_tiles_are_one_band(self):
        """The NIC, its RDMA device, the port and its speed side by side,
        under one heading; the counters' row keeps the two graphs."""
        home = json.loads((COMPOSE / "rocm-golden" / HOME).read_text())
        panels = home["panels"]
        titles = [p["title"] for p in panels]
        at = titles.index("Network & Driver Status")
        self.assertEqual(titles[at + 1:at + 6], [
            "rocm-ernic0 (ionic)", "ionic_rdma (RDMA exporter up)",
            "IB Port Active", "Link Speed",
            "RDMA Device Counters (rdma_exporter)"])
        self.assertEqual(
            {_grid(p)[1:] for p in panels[at + 1:at + 5]},
            {(panels[at]["gridPos"]["y"] + 1, 6, 3)})

    def test_no_id_title_or_query_was_lost(self):
        """Every panel the dashboard had at a7b33fe, by id and title, with
        the queries it had."""
        had = {
            1: "Fleet", 2: "Guests Up", 3: "ROCm Ernic Instances",
            4: "RDMA Fleet Throughput", 5: "TCP Mesh Fleet Throughput",
            6: "GPU Up", 7: "SDXI Copy Throughput",
            10: "Guests — CPU & Memory", 11: "CPU Busy % Per VM",
            12: "Memory Used % Per VM", 13: "System Load 1m Per VM",
            14: "Memory Available Per VM", 27: "NVMe Storage",
            25: "NVMe I/O Per VM", 26: "NVMe IOPS Per VM", 60: "SDXI",
            61: "SDXI Device Up", 62: "SDXI Errors (dashboard range)",
            63: "SDXI Copy Throughput Per VM", 64: "SDXI Copies/s Per VM",
            65: "SDXI Compress / Decompress Bytes/s Per VM",
            66: "SDXI Compress / Decompress Ops/s Per VM",
            38: "Network & Driver Status", 36: "rocm-ernic0 (ionic)",
            37: "ionic_rdma (RDMA exporter up)",
            40: "RDMA Device Counters (rdma_exporter)", 41: "IB Port Active",
            42: "Link Speed", 43: "RDMA Bytes/s Per Device",
            44: "RDMA Commands/s Per Device", 30: "RDMA — Ernic Mesh",
            31: "RDMA Throughput — Fleet Total", 32: "RDMA By Node",
            33: "TCP Mesh Fleet Traffic",
            34: "Doorbells & Completions — Fleet Total",
            35: "RDMA Traffic By Node", 50: "Stack Logs",
            56: "Stack Logs (Alloy → Loki)"}
        home = json.loads((COMPOSE / "rocm-golden" / HOME).read_text())
        now = {p["id"]: p["title"] for p in home["panels"]}
        self.assertEqual({k: now.get(k) for k in had}, had)
        self.assertEqual(set(now) - set(had), {67, 70, 71, 72, 73, 74})
        self.assertEqual(_queries(home, had), HOME_QUERIES)
        self.assertEqual(sum(map(len, HOME_QUERIES.values())), 46)

    def test_no_query_of_the_fleet_dashboard_was_lost(self):
        """fleet.json was given ids and nothing else."""
        fleet = json.loads(
            (COMPOSE / "rocm-golden" / FLEET_DIR / "fleet.json").read_text())
        self.assertEqual(_queries(fleet), FLEET_QUERIES)
        self.assertEqual(sum(map(len, FLEET_QUERIES.values())), 17)

    def test_a_renamed_metric_is_found(self):
        """The check that was missing: the same text swap in the file, as
        the five that passed were made."""
        path = COMPOSE / "rocm-golden" / HOME
        for metric in ("node_network_info", "rdma_port_info",
                       "ernic_rdma_bytes_total", "node_load1"):
            text = path.read_text()
            self.assertIn(metric, text)
            swapped = json.loads(text.replace(metric, metric[:-1]))
            self.assertNotEqual(_queries(swapped, HOME_QUERIES),
                                HOME_QUERIES, metric)
        fleet = (COMPOSE / "rocm-golden" / FLEET_DIR / "fleet.json")
        swapped = json.loads(fleet.read_text().replace(
            "ernic_instances", "ernic_instance"))
        self.assertNotEqual(_queries(swapped), FLEET_QUERIES)


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------

def parse_exposition(text: str) -> dict:
    """family -> {"help", "type", "samples": [(labels, value)]}.

    Strict, because node_exporter drops a file it cannot parse, and every
    other collector's file with it.
    """
    if not text.endswith("\n"):
        raise ValueError("no trailing newline")
    families: dict[str, dict] = {}
    current = None
    for line in text[:-1].split("\n"):
        if not line:
            raise ValueError("blank line")
        comment = re.fullmatch(
            r"# (HELP|TYPE) ([a-zA-Z_:][a-zA-Z0-9_:]*) (.+)", line)
        if comment:
            kind, name, rest = comment.groups()
            family = families.setdefault(name, {"samples": []})
            if kind.lower() in family:
                raise ValueError(f"second {kind} for {name}")
            if family["samples"]:
                raise ValueError(f"{kind} for {name} after its samples")
            if kind == "TYPE" and rest != "gauge":
                raise ValueError(f"type {rest!r} for {name}")
            if kind == "TYPE" and "help" not in family:
                raise ValueError(f"TYPE before HELP for {name}")
            family[kind.lower()] = rest
            current = name
            continue
        sample = re.fullmatch(
            r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([a-z_]+)="([^"\\\n]*)"\})? '
            r'(\d+(?:\.\d+)?(?:e-?\d+)?)', line)
        if not sample:
            raise ValueError(f"not a sample: {line!r}")
        name, label, value, number = sample.groups()
        if name != current:
            raise ValueError(f"{name} outside its family")
        family = families[name]
        if "help" not in family or "type" not in family:
            raise ValueError(f"{name} has no HELP and TYPE")
        if family["samples"]:
            raise ValueError(f"{name} twice")
        family["samples"].append(({label: value} if label else {},
                                  float(number)))
    for name, family in families.items():
        if not family["samples"]:
            raise ValueError(f"{name} has no sample")
        if name.endswith("_total"):
            raise ValueError(f"{name} is named as a counter")
    return families


def bench_line(elements=262144, iters=2000, ms=1.355, rate=193.5) -> str:
    """The line p4_fp8_bench prints, from its own format string."""
    (fmt,) = re.findall(r'printf\("(p4 fp8 bench: [^"]*)\\n"',
                        BENCH_SOURCE.read_text())
    return fmt.replace("%lu", "%d") % (elements, iters, ms, rate)


def sdxi_line(count=4194304, device="/dev/sdxi0") -> str:
    """The line the SDXI play echoes, by running its echo."""
    plays = yaml.safe_load(QUICKSTART.read_text())
    play = next(p for p in plays if p["name"] == SDXI_PLAY)
    task = next(t for t in play["tasks"] if t["name"]
                == "Copy random data through the device and compare it")
    (echo,) = re.findall(r'echo "sdxi copy: .*?identical"',
                         task["ansible.builtin.shell"]["cmd"], re.S)
    assert "{{" not in echo, echo
    with tempfile.TemporaryDirectory() as work:
        (Path(work) / "dst").write_bytes(b"\0" * count)
        done = subprocess.run(
            ["bash", "-c", f'work="$1"; dev="$2"; {echo}', "bash", work,
             device], capture_output=True, text=True, check=True)
    return done.stdout.rstrip("\n")


class _Scratch(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quickstart-metrics-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = self.tmp / "collector"
        self.dir.mkdir()
        self.p4 = self.dir / "quickstart_p4_fp8.prom"
        self.sdxi = self.dir / "quickstart_sdxi_copy.prom"

    def run_script(self, *args, directory=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--dir",
             str(directory or self.dir), *args],
            capture_output=True, text=True, timeout=60)

    def record_p4(self, line=None, device="rocm-rdma-ernic0", **kwargs):
        return self.run_script("p4-fp8", "--device", device, "--line",
                               bench_line() if line is None else line,
                               **kwargs)

    def record_sdxi(self, line=None, **kwargs):
        return self.run_script("sdxi-copy", "--line",
                               sdxi_line() if line is None else line, **kwargs)

    def written(self) -> set[str]:
        """Every series name the script writes, for both tests."""
        self.assertEqual(self.record_p4().returncode, 0)
        self.assertEqual(self.record_sdxi().returncode, 0)
        return (set(parse_exposition(self.p4.read_text()))
                | set(parse_exposition(self.sdxi.read_text())))


class Script(_Scratch):

    def test_the_two_lines_are_the_ones_the_tests_print(self):
        self.assertEqual(
            bench_line(),
            "p4 fp8 bench: 262144 elements x 2000 iters, avg 1.355 ms, "
            "193.5 MB/s")
        self.assertEqual(
            sdxi_line(),
            "sdxi copy: 4194304 bytes through /dev/sdxi0, source and "
            "destination identical")

    def test_a_bench_line_becomes_five_gauges(self):
        before = int(time.time())
        done = self.record_p4()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout.strip(),
                         f"quickstart-test-metrics: recorded {self.p4}")
        families = parse_exposition(self.p4.read_text())
        self.assertEqual(set(families), P4_SERIES)
        values = {}
        for name, family in families.items():
            ((labels, value),) = family["samples"]
            self.assertEqual(labels, {"device": "rocm-rdma-ernic0"}, name)
            values[name.removeprefix("quickstart_p4_fp8_")] = value
        stamp = values.pop("timestamp_seconds")
        self.assertEqual(values, {"bytes_per_second": 193500000.0,
                                  "operation_seconds": 0.001355,
                                  "elements": 262144.0,
                                  "iterations": 2000.0})
        self.assertGreaterEqual(stamp, before)
        self.assertLessEqual(stamp, time.time() + 1)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["quickstart_p4_fp8.prom"])

    def test_the_rate_is_in_bytes_and_the_benchs_mb_is_a_million(self):
        """p4_fp8_bench divides by 1e6, and the panel's unit is SI bytes a
        second, so 194.1 MB/s is drawn as 194 MB/s."""
        self.assertIn("/ elapsed / 1e6", BENCH_SOURCE.read_text())
        self.assertEqual(self.record_p4(bench_line(rate=194.1)).returncode, 0)
        ((_, value),) = parse_exposition(self.p4.read_text())[
            "quickstart_p4_fp8_bytes_per_second"]["samples"]
        self.assertEqual(value, 194100000.0)

    def test_a_verified_copy_becomes_two_gauges(self):
        before = int(time.time())
        done = self.record_sdxi()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        families = parse_exposition(self.sdxi.read_text())
        self.assertEqual(set(families), SDXI_SERIES)
        self.assertEqual(families["quickstart_sdxi_copy_bytes"]["samples"],
                         [({}, 4194304.0)])
        ((labels, stamp),) = families[
            "quickstart_sdxi_copy_timestamp_seconds"]["samples"]
        self.assertEqual(labels, {})
        self.assertGreaterEqual(stamp, before)

    def test_the_two_records_are_two_files(self):
        """One test run again does not take the other's result with it."""
        self.written()
        first = self.sdxi.read_text()
        self.assertEqual(self.record_p4(bench_line(iters=200)).returncode, 0)
        self.assertEqual(self.sdxi.read_text(), first)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["quickstart_p4_fp8.prom", "quickstart_sdxi_copy.prom"])

    def test_node_exporter_reads_both(self):
        """It reads *.prom in its textfile directory, as its own user."""
        self.written()
        for path in (self.p4, self.sdxi):
            self.assertEqual(path.suffix, ".prom")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_the_file_is_readable_whatever_the_umask(self):
        done = subprocess.run(
            ["bash", "-c", 'umask 077; exec "$@"', "bash", sys.executable,
             str(SCRIPT), "--dir", str(self.dir), "sdxi-copy", "--line",
             sdxi_line()], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stat.S_IMODE(self.sdxi.stat().st_mode), 0o644)

    def test_the_file_is_replaced_not_rewritten(self):
        """node_exporter may read at any moment: a reader holding the old
        file keeps all of it, and no half-written one is ever in place."""
        self.assertEqual(self.record_p4().returncode, 0)
        with open(self.p4, encoding="ascii") as held:
            inode = os.fstat(held.fileno()).st_ino
            self.assertEqual(
                self.record_p4(bench_line(rate=12.5)).returncode, 0)
            self.assertNotEqual(self.p4.stat().st_ino, inode)
            self.assertIn(" 193500000\n", held.read())
        self.assertIn(" 12500000\n", self.p4.read_text())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["quickstart_p4_fp8.prom"])
        source = SCRIPT.read_text()
        self.assertIn("os.replace(tmp, path)", source)
        # The scratch name is not one node_exporter reads.
        self.assertIn('suffix=".tmp")', source)

    def test_a_symlink_at_the_old_scratch_name_is_not_written_through(self):
        """The scratch file was the fixed name <file>.tmp, opened with a
        plain open(): a symlink left there was followed, the exposition
        went to its target and the .prom became the link."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_text("untouched\n")
        planted = self.dir / "quickstart_p4_fp8.prom.tmp"
        planted.symlink_to(elsewhere)
        self.assertEqual(self.record_p4().returncode, 0)
        self.assertEqual(elsewhere.read_text(), "untouched\n")
        self.assertFalse(self.p4.is_symlink())
        self.assertTrue(planted.is_symlink())
        self.assertEqual(stat.S_IMODE(self.p4.stat().st_mode), 0o644)
        self.assertIn(" 193500000\n", self.p4.read_text())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["quickstart_p4_fp8.prom",
                          "quickstart_p4_fp8.prom.tmp"])

    def test_a_line_that_is_not_a_result_writes_nothing(self):
        self.assertEqual(self.record_p4().returncode, 0)
        kept = self.p4.read_text()
        for line in ("", "device=rocm-rdma-ernic0",
                     "p4 fp8 bench: 262144 elements x 2000 iters",
                     bench_line(rate=0.0), bench_line(iters=0),
                     bench_line(elements=0),
                     bench_line() + " and more",
                     bench_line().replace("MB/s", "MiB/s"),
                     sdxi_line()):
            with self.subTest(line=line):
                done = self.record_p4(line)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("not a p4_fp8_bench result", done.stderr)
                self.assertEqual(self.p4.read_text(), kept)
        for line in ("", "sdxi-cli copy exited 124",
                     "the copy differs from its source: /dev/sdxi0 returned "
                     "the wrong data", sdxi_line(count=0), bench_line()):
            with self.subTest(line=line):
                done = self.record_sdxi(line)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("not a verified SDXI copy", done.stderr)
                self.assertFalse(self.sdxi.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["quickstart_p4_fp8.prom"])

    def test_a_device_name_that_would_break_the_label_is_refused(self):
        for device in ('a"b', "a b", "a\\b", "", "a\nb", "a{b}"):
            with self.subTest(device=device):
                done = self.record_p4(device=device)
                self.assertEqual(done.returncode, 2)
                self.assertFalse(self.p4.exists())
        for device in ("rocm-rdma-ernic0", "rocep1s0", "mlx5_0"):
            with self.subTest(device=device):
                self.assertEqual(self.record_p4(device=device).returncode, 0)
                parse_exposition(self.p4.read_text())

    def test_a_guest_with_no_collector_directory_is_not_a_failure(self):
        """No node_exporter, so nothing would serve the file: one line,
        exit 0, and the directory is not made for it."""
        absent = self.tmp / "absent"
        for done in (self.record_p4(directory=absent),
                     self.record_sdxi(directory=absent)):
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual(
                done.stdout.strip(),
                f"quickstart-test-metrics: not recorded: {absent} is not "
                "there, so this guest has no node_exporter textfile "
                "collector to serve it")
        self.assertFalse(absent.exists())

    def test_a_bad_line_is_refused_before_the_directory_is_looked_at(self):
        done = self.record_p4("nonsense", directory=self.tmp / "absent")
        self.assertEqual(done.returncode, 2)

    def test_a_file_it_cannot_replace_is_exit_1(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        done = self.record_sdxi()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr.strip(),
                         f"quickstart-test-metrics: {self.sdxi}: "
                         "Permission denied")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_the_default_directory_is_the_one_node_exporter_is_given(self):
        source = SCRIPT.read_text()
        self.assertIn('DIRECTORY = "/var/lib/prometheus/node-exporter"', source)
        metrics = ROCJITSU_METRICS.read_text()
        self.assertIn("path: /var/lib/prometheus/node-exporter", metrics)
        self.assertIn("--collector.textfile.directory="
                      "/var/lib/prometheus/node-exporter", metrics)

    def test_it_imports_the_standard_library_only(self):
        """It is copied to a guest and run there, with nothing installed
        for it."""
        import ast
        tree = ast.parse(SCRIPT.read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertEqual(names, {"argparse", "os", "re", "sys", "tempfile", "time"})
        self.assertTrue(SCRIPT.read_text().startswith("#!/usr/bin/python3\n"))


# ---------------------------------------------------------------------------
# The play
# ---------------------------------------------------------------------------

def _plays() -> list[dict]:
    return yaml.safe_load(QUICKSTART.read_text())


def _play(name: str) -> dict:
    return next(p for p in _plays() if p.get("name") == name)


class Play(unittest.TestCase):

    def setUp(self):
        self.play = _play(RECORD_PLAY)
        self.tasks = {t["name"]: t for t in self.play["tasks"]}

    def test_it_sits_after_the_facts_and_before_the_last_play(self):
        names = [p["name"] for p in _plays()]
        at = names.index(RECORD_PLAY)
        self.assertEqual(names[at - 1], FACTS_PLAY)
        # One play after it: the optional RDMA one, which stays last.
        self.assertEqual(len(names[at + 1:]), 1)
        self.assertEqual(names[at - 3:at - 1], [SDXI_PLAY, P4_PLAY])

    def test_it_is_every_guest_with_no_facts_and_no_tags(self):
        self.assertEqual(self.play["hosts"], "fleet")
        self.assertIs(self.play["gather_facts"], False)
        self.assertNotIn("tags", self.play)

    def test_root_is_taken_by_these_two_tasks_and_nowhere_in_the_tests(self):
        """The collector directory is root's, 0755, as rocjitsu_setup
        makes it. The two test plays stay the login user's."""
        self.assertIs(self.play["become"], False)
        self.assertEqual(list(self.tasks), ["Record the verified SDXI copy",
                                            "Record the P4 fp8 rate"])
        for task in self.play["tasks"]:
            self.assertIs(task["become"], True, task["name"])
        for name in (SDXI_PLAY, P4_PLAY):
            play = _play(name)
            self.assertIs(play["become"], False)
            for task in play["tasks"]:
                self.assertNotIn("become", task, task["name"])
        made = next(t for t in yaml.safe_load(ROCJITSU_METRICS.read_text())
                    if t["name"] == "Create the textfile collector directory")
        self.assertEqual(made["ansible.builtin.file"]["mode"], "0755")
        self.assertNotIn("owner", made["ansible.builtin.file"])
        self.assertIs(made["become"], True)

    def test_each_task_runs_the_script_on_the_tests_own_line(self):
        self.assertEqual(self.play["vars"]["quickstart_record_script"],
                         str(SCRIPT_PATH))
        self.assertTrue(SCRIPT.is_file())
        self.assertEqual(self.play["vars"]["quickstart_record_dir"],
                         "/var/lib/prometheus/node-exporter")
        sdxi = self.tasks["Record the verified SDXI copy"]
        p4 = self.tasks["Record the P4 fp8 rate"]
        for task in (sdxi, p4):
            script = task["ansible.builtin.script"]
            self.assertEqual(script["executable"], "/usr/bin/python3")
            self.assertTrue(script["cmd"].startswith(
                "{{ quickstart_record_script }} "
                "--dir {{ quickstart_record_dir | quote }} "))
        self.assertIn("sdxi-copy --line {{ quickstart_sdxi_copy.stdout_lines",
                      sdxi["ansible.builtin.script"]["cmd"])
        self.assertIn("select('match', 'sdxi copy: ') | list | last | quote }}",
                      sdxi["ansible.builtin.script"]["cmd"])
        self.assertTrue(p4["ansible.builtin.script"]["cmd"].endswith(
            "p4-fp8 --device {{ quickstart_p4_ib_device | quote }} "
            "--line {{ quickstart_p4_bw | quote }}"))

    def test_the_names_it_reads_are_the_ones_the_test_plays_set(self):
        sdxi = {t["name"]: t for t in _play(SDXI_PLAY)["tasks"]}
        self.assertEqual(
            sdxi["Copy random data through the device and compare it"][
                "register"], "quickstart_sdxi_copy")
        p4 = {t["name"]: t for t in _play(P4_PLAY)["tasks"]}
        self.assertIn("quickstart_p4_bw",
                      p4["Record the rate"]["ansible.builtin.set_fact"])
        self.assertIn("quickstart_p4_ib_device",
                      p4["Record the IB device name"][
                          "ansible.builtin.set_fact"])
        # set_fact and register outlive their play; a play var does not,
        # so nothing here reads one of the test plays' own.
        text = yaml.safe_dump(self.play, width=10_000)
        for name in list(_play(SDXI_PLAY)["vars"]) + list(_play(P4_PLAY)["vars"]):
            self.assertNotIn(name, text)

    def test_a_run_with_neither_switch_reaches_no_guest(self):
        """Every task is conditional on a result an optional play left."""
        sdxi = self.tasks["Record the verified SDXI copy"]["when"]
        self.assertEqual(sdxi[0], "quickstart_sdxi_copy is defined")
        self.assertEqual(sdxi[1], "quickstart_sdxi_copy.rc | default(1) == 0")
        self.assertIn("select('match', 'sdxi copy: ') | list | length > 0",
                      sdxi[2])
        self.assertEqual(self.tasks["Record the P4 fp8 rate"]["when"],
                         ["quickstart_p4_bw is defined",
                          "quickstart_p4_ib_device is defined"])

    def test_a_record_that_cannot_be_written_does_not_fail_the_run(self):
        for task in self.play["tasks"]:
            self.assertIs(task["ignore_errors"], True, task["name"])
            self.assertIn("'recorded ' in", task["changed_when"])
            self.assertTrue(task["register"].startswith("quickstart_"))


class EndToEnd(unittest.TestCase):
    """The play itself, run by Ansible against local stand-ins for guests."""

    @classmethod
    def setUpClass(cls):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        _require(os.path.exists("/usr/bin/python3"), "/usr/bin/python3")
        cls.tmp = Path(tempfile.mkdtemp(prefix="quickstart-record-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        (cls.tmp / "play.yml").write_text(yaml.safe_dump([_play(RECORD_PLAY)]))
        # The script is found relative to the playbook, as in the checkout.
        (cls.tmp / SCRIPT_PATH).parent.mkdir(parents=True)
        shutil.copy(SCRIPT, cls.tmp / SCRIPT_PATH)
        copied = {"rc": 0, "stdout_lines": [sdxi_line()]}
        hosts = {
            # The first guest: both tests passed.
            "guest-1": {"quickstart_sdxi_copy": copied,
                        "quickstart_p4_bw": bench_line(),
                        "quickstart_p4_ib_device": "rocm-rdma-ernic0"},
            # Any other guest: the SDXI test alone.
            "guest-2": {"quickstart_sdxi_copy": copied},
            # No node_exporter.
            "guest-3": {"quickstart_sdxi_copy": copied},
            # A line the script does not take.
            "guest-4": {"quickstart_p4_bw": "p4 fp8 bench: nothing",
                        "quickstart_p4_ib_device": "rocep1s0"},
            # A run with neither switch.
            "guest-5": {},
        }
        for name in hosts:
            if name != "guest-3":
                (cls.tmp / name).mkdir()
        (cls.tmp / "inventory.yml").write_text(yaml.safe_dump({"fleet": {
            "hosts": hosts,
            "vars": {"ansible_connection": "local",
                     "ansible_python_interpreter": sys.executable}}}))
        env = {**os.environ, "ANSIBLE_NOCOLOR": "1",
               "ANSIBLE_STDOUT_CALLBACK": "default",
               "ANSIBLE_LOCAL_TEMP": str(cls.tmp / "local-tmp"),
               "ANSIBLE_REMOTE_TEMP": str(cls.tmp / "remote-tmp")}
        # ansible_become, because the stand-ins are this user's own
        # directories and a test must not ask for sudo: the task's
        # "become: true" is held by Play above. The directory is an extra
        # var because the play sets it, and a play var outranks anything
        # an inventory says: as a host var it was ignored, and the run
        # went for the real /var/lib/prometheus/node-exporter.
        (cls.tmp / "extra.json").write_text(json.dumps({
            "ansible_become": False,
            "quickstart_record_dir":
                str(cls.tmp) + "/{{ inventory_hostname }}"}))
        cls.done = subprocess.run(
            ["ansible-playbook", "-i", "inventory.yml",
             "-e", "@extra.json", "play.yml"],
            cwd=cls.tmp, env=env, capture_output=True, text=True, timeout=300)

    def recap(self, host: str) -> dict[str, int]:
        (line,) = re.findall(rf"(?m)^{host}\s+: (.*)$", self.done.stdout)
        return {key: int(value) for key, value in
                re.findall(r"(\w+)=(\d+)", line)}

    def files(self, host: str) -> list[str]:
        return sorted(p.name for p in (self.tmp / host).iterdir())

    def test_the_run_passes(self):
        self.assertEqual(self.done.returncode, 0,
                         self.done.stdout + self.done.stderr)

    def test_the_first_guest_gets_both_records(self):
        self.assertEqual(self.recap("guest-1"),
                         {"ok": 2, "changed": 2, "unreachable": 0, "failed": 0,
                          "skipped": 0, "rescued": 0, "ignored": 0})
        self.assertEqual(self.files("guest-1"), ["quickstart_p4_fp8.prom",
                                                 "quickstart_sdxi_copy.prom"])
        p4 = parse_exposition(
            (self.tmp / "guest-1" / "quickstart_p4_fp8.prom").read_text())
        self.assertEqual(set(p4), P4_SERIES)
        self.assertEqual(p4["quickstart_p4_fp8_bytes_per_second"]["samples"],
                         [({"device": "rocm-rdma-ernic0"}, 193500000.0)])
        sdxi = parse_exposition(
            (self.tmp / "guest-1" / "quickstart_sdxi_copy.prom").read_text())
        self.assertEqual(sdxi["quickstart_sdxi_copy_bytes"]["samples"],
                         [({}, 4194304.0)])

    def test_a_guest_that_ran_the_sdxi_test_alone_gets_that_record(self):
        recap = self.recap("guest-2")
        self.assertEqual((recap["ok"], recap["changed"], recap["skipped"]),
                         (1, 1, 1))
        self.assertEqual(self.files("guest-2"), ["quickstart_sdxi_copy.prom"])

    def test_a_guest_with_no_collector_directory_is_ok_and_unchanged(self):
        recap = self.recap("guest-3")
        self.assertEqual((recap["ok"], recap["changed"], recap["failed"],
                          recap["ignored"]), (1, 0, 0, 0))
        self.assertFalse((self.tmp / "guest-3").exists())

    def test_a_line_the_script_refuses_is_shown_and_ignored(self):
        recap = self.recap("guest-4")
        self.assertEqual((recap["failed"], recap["ignored"]), (0, 1))
        self.assertEqual(self.files("guest-4"), [])
        self.assertIn("not a p4_fp8_bench result", self.done.stdout)
        self.assertIn("...ignoring", self.done.stdout)

    def test_a_run_with_neither_switch_skips_both_tasks(self):
        self.assertEqual(self.recap("guest-5"),
                         {"ok": 0, "changed": 0, "unreachable": 0, "failed": 0,
                          "skipped": 2, "rescued": 0, "ignored": 0})
        self.assertEqual(self.files("guest-5"), [])


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------

def _panels(path: Path) -> list[dict]:
    return json.loads(path.read_text())["panels"]


def _exprs(panel: dict) -> list[str]:
    return [target["expr"] for target in panel.get("targets", [])]


class Dashboard(_Scratch):
    """The P4 row and the SDXI tile, in every stack's copy."""

    ROW = ["P4 FP8 Rate (last pass)",
           "P4 FP8 Time Per Reduction (last pass)",
           "P4 FP8 Test Size (last pass)",
           "P4 FP8 Measured (time since last pass)"]
    SDXI_TILE = "SDXI Copy Verified (time since last pass)"
    SELECT = '{job="fleet-node",vm_name=~"$vm"}'

    def row(self, stack: str = "rocm-golden") -> list[dict]:
        panels = _panels(COMPOSE / stack / HOME)
        at = [p["title"] for p in panels].index(P4_ROW)
        self.assertEqual(panels[at]["type"], "row")
        rest = panels[at + 1:]
        return rest[:next(i for i, p in enumerate(rest) if p["type"] == "row")]

    def tile(self, stack: str = "rocm-golden") -> dict:
        (panel,) = [p for p in _panels(COMPOSE / stack / HOME)
                    if p["title"] == self.SDXI_TILE]
        return panel

    def recorded(self, stack: str = "rocm-golden") -> list[dict]:
        return self.row(stack) + [self.tile(stack)]

    def test_every_copy_has_the_row_and_the_tile(self):
        for stack in NAMES:
            with self.subTest(stack=stack):
                self.assertEqual([p["title"] for p in self.row(stack)],
                                 self.ROW)
                self.assertEqual(self.tile(stack)["id"], 67)
                self.assertEqual([p["id"] for p in self.row(stack)],
                                 [71, 72, 73, 74])

    def test_the_row_sits_with_the_other_devices(self):
        rows = [p["title"] for p in _panels(COMPOSE / "rocm-golden" / HOME)
                if p["type"] == "row"]
        self.assertEqual(rows, [
            "Fleet", "Guests — CPU & Memory", "NVMe Storage", "SDXI",
            P4_ROW, "Network & Driver Status",
            "RDMA Device Counters (rdma_exporter)", "RDMA — Ernic Mesh",
            "Stack Logs"])

    def test_every_recorded_series_a_panel_reads_is_one_the_play_writes(self):
        written = self.written()
        self.assertEqual(written, P4_SERIES | SDXI_SERIES)
        read = set()
        for stack in NAMES:
            for path in _fleet_dashboards(stack):
                for panel in _panels(path):
                    found = {name for expr in _exprs(panel) for name in
                             re.findall(r"\bquickstart_[a-z0-9_]+", expr)}
                    self.assertLessEqual(
                        found, written,
                        f"{path.relative_to(REPO)}: {panel.get('title')!r}")
                    read |= found
        self.assertEqual(read, P4_SERIES | {
            "quickstart_sdxi_copy_timestamp_seconds"})

    def test_the_four_p4_tiles_read_the_five_series(self):
        by_title = {p["title"]: p for p in self.row()}
        expected = {
            "P4 FP8 Rate (last pass)": (
                [f"quickstart_p4_fp8_bytes_per_second{self.SELECT}"], "Bps"),
            "P4 FP8 Time Per Reduction (last pass)": (
                [f"quickstart_p4_fp8_operation_seconds{self.SELECT}"], "s"),
            "P4 FP8 Test Size (last pass)": (
                [f"quickstart_p4_fp8_elements{self.SELECT}",
                 f"quickstart_p4_fp8_iterations{self.SELECT}"], "none"),
            "P4 FP8 Measured (time since last pass)": (
                ["clamp_min(time() - quickstart_p4_fp8_timestamp_seconds"
                 f"{self.SELECT}, 0)"], "s"),
        }
        for title, (exprs, unit) in expected.items():
            with self.subTest(panel=title):
                self.assertEqual(_exprs(by_title[title]), exprs)
                self.assertEqual(
                    by_title[title]["fieldConfig"]["defaults"]["unit"], unit)
        self.assertEqual(
            _exprs(self.tile()),
            ["clamp_min(time() - quickstart_sdxi_copy_timestamp_seconds"
             f"{self.SELECT}, 0)"])
        self.assertEqual(self.tile()["fieldConfig"]["defaults"]["unit"], "s")

    def test_a_result_is_a_value_and_never_a_rate(self):
        """Each series is one figure a test left, re-served on every
        scrape: a rate() of it is zero and an increase() is nonsense."""
        for panel in self.recorded():
            for expr in _exprs(panel):
                with self.subTest(panel=panel["title"]):
                    self.assertNotRegex(expr, r"\b(rate|irate|increase|sum)\(")

    def test_every_tile_is_per_vm_and_says_no_test_run_when_there_is_none(self):
        config = yaml.safe_load(
            (COMPOSE / "rocm-golden" / "prometheus.yml").read_text())
        (job,) = [j for j in config["scrape_configs"]
                  if j["job_name"] == "fleet-node"]
        self.assertIn("vm_name", job["static_configs"][0]["labels"])
        for panel in self.recorded():
            with self.subTest(panel=panel["title"]):
                self.assertEqual(panel["type"], "stat")
                self.assertEqual(panel["datasource"],
                                 {"type": "prometheus",
                                  "uid": "fleet-prometheus"})
                for target in panel["targets"]:
                    self.assertEqual(target["datasource"], panel["datasource"])
                    self.assertIn(self.SELECT, target["expr"])
                    self.assertIn("{{vm_name}}", target["legendFormat"])
                self.assertEqual([t["refId"] for t in panel["targets"]],
                                 list("ABCD"[:len(panel["targets"])]))
                defaults = panel["fieldConfig"]["defaults"]
                self.assertEqual(defaults["noValue"], "no test run")
                # A test nobody ran is not a failure, and nor is a slow one.
                self.assertEqual(defaults["thresholds"]["steps"],
                                 [{"color": "blue", "value": None}])
                self.assertNotIn('"red"', json.dumps(panel))
                self.assertEqual(panel["options"]["textMode"],
                                 "value_and_name")
                self.assertEqual(panel["options"]["graphMode"], "none")

    def test_the_descriptions_say_which_test_and_how_old_it_can_be(self):
        for panel in self.row():
            with self.subTest(panel=panel["title"]):
                text = panel["description"]
                self.assertIn("--p4-test", text)
                self.assertIn("older than this boot", text)
        rate = self.row()[0]["description"]
        self.assertIn("not a live rate", rate)
        self.assertIn("counts no bytes for a reduction", rate)
        self.assertIn("only the first guest runs it", rate)
        self.assertEqual(_play(P4_PLAY)["hosts"], "fleet[0]")
        tile = self.tile()["description"]
        self.assertIn("--sdxi-test", tile)
        self.assertIn("older than this boot", tile)
        self.assertIn("not a benchmark", tile)

    def test_a_tile_says_it_is_the_last_pass_and_not_the_last_run(self):
        """A run that fails stops at the test's assert, before the record
        play, so the file is the last pass's. Run against stand-ins: a pass
        at 201.0 MB/s, then four kinds of failing run, left the 201.0 and
        its timestamp in place. "(last test)" was the title."""
        for panel in self.recorded():
            with self.subTest(panel=panel["title"]):
                self.assertRegex(panel["title"], r"last pass\)$")
                self.assertNotIn("last test", panel["title"])
                self.assertIn("A later run that fails changes nothing here",
                              panel["description"])
        plays = yaml.safe_load(QUICKSTART.read_text())
        names = [play["name"] for play in plays]
        for play in (SDXI_PLAY, P4_PLAY):
            self.assertLess(names.index(play), names.index(RECORD_PLAY))
            self.assertNotIn("ignore_errors", json.dumps(_play(play)))

    def test_an_age_is_never_negative(self):
        """The timestamp is the guest's clock and time() is Prometheus's,
        so a guest that runs ahead would read as a negative age."""
        ages = [p for p in self.recorded() if "time since" in p["title"]]
        self.assertEqual([p["id"] for p in ages], [74, 67])
        for panel in ages:
            with self.subTest(panel=panel["title"]):
                (expr,) = _exprs(panel)
                self.assertRegex(
                    expr, r"^clamp_min\(time\(\) - quickstart_\w+"
                    r"_timestamp_seconds\{[^}]+\}, 0\)$")
                self.assertIn("reads as 0 s", panel["description"])

    def test_the_sdxi_throughput_panels_say_why_they_read_zero(self):
        """One copy of the test's size, over the window the rate is taken
        on, is the figure the descriptions quote."""
        size = _play(SDXI_PLAY)["vars"]["quickstart_sdxi_bytes"]
        panels = {p["title"]: p for p in _panels(COMPOSE / "rocm-golden" / HOME)}
        for title in ("SDXI Copy Throughput", "SDXI Copy Throughput Per VM"):
            with self.subTest(panel=title):
                panel = panels[title]
                (window,) = set(re.findall(r"\[(\d+)m\]", _exprs(panel)[0]))
                self.assertEqual(round(size / (int(window) * 60) / 1000), 35)
                self.assertIn("Zero while nothing is copying",
                              panel["description"])
                self.assertIn(f"copies {size // 2**20} MiB once",
                              panel["description"])
                self.assertIn("about 35 kB/s for two minutes",
                              panel["description"])
        self.assertIn("about 35 kB/s for two minutes",
                      self.tile()["description"])

    def test_the_dashboard_says_it_has_p4(self):
        home = json.loads((COMPOSE / "rocm-golden" / HOME).read_text())
        self.assertIn("P4", home["description"])


class Docs(_Scratch):

    def test_every_recorded_series_the_docs_name_is_one_the_play_writes(self):
        written = self.written()
        named = set()
        for path in DOCS + (QUICKSTART, REPO / "CHANGELOG.md"):
            found = {name for name in
                     re.findall(r"\bquickstart_(?:p4_fp8|sdxi_copy)_[a-z0-9_]*",
                                path.read_text())
                     # A prefix, as in quickstart_p4_fp8_*.
                     if not name.endswith("_")}
            self.assertLessEqual(found, written, path.name)
            named |= found
        self.assertEqual(named, written)

    def test_the_readme_says_where_the_p4_figure_is(self):
        text = " ".join((REPO / "README.md").read_text().split())
        self.assertIn("**P4** row", text)
        self.assertIn("quickstart_p4_fp8_bytes_per_second", text)
        self.assertIn("--p4-test", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
