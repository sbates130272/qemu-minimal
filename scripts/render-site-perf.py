#!/usr/bin/env python3
"""Render qemu-minimal performance history and trend pages for GitHub Pages."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

METRICS = (
    {
        "key": "gemm",
        "label": "rocjitsu GEMM",
        "badge_file": "badge-gemm.json",
        "source": ("rocjitsu", "badge-gemm.json"),
        "base_unit": "FLOP/s",
        "higher_is_better": True,
    },
    {
        "key": "hipfile",
        "label": "rocjitsu hipFile",
        "badge_file": "badge-hipfile.json",
        "source": ("rocjitsu", "badge-hipfile.json"),
        "base_unit": "B/s",
        "higher_is_better": True,
    },
    {
        "key": "hipfile-fio",
        "label": "hipFile fio",
        "badge_file": "badge-hipfile-fio.json",
        "source": ("hipfile-fio", "badge-hipfile-fio.json"),
        "base_unit": "B/s",
        "higher_is_better": True,
    },
)

PREFIX_SCALE = {
    "": 1.0,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
}

BADGE_COLORS = {
    "gemm": "ED1C24",
    "hipfile": "76B900",
    "hipfile-fio": "0071C5",
}

CHART_COLORS = {
    "gemm": "#d94b52",
    "hipfile": "#198754",
    "hipfile-fio": "#0d6efd",
}

REPORTS = (
    {"key": "1vm", "label": "Single VM", "path": "1vm/index.md"},
    {"key": "rocjitsu", "label": "rocjitsu", "path": "rocjitsu/index.md"},
    {"key": "two-vm", "label": "Two-VM VM 1", "path": "two-vm/index.md"},
    {"key": "two-vm-vm2", "label": "Two-VM VM 2", "path": "two-vm/vm2/index.md"},
    {"key": "hipfile-fio", "label": "hipFile fio", "path": "hipfile-fio/index.md"},
)

BADGE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?)([A-Za-z/]+)$")
REPORT_META_RE = re.compile(r"Generated:\s*\*\*(.*?)\*\*\s*&middot;\s*Commit:\s*(.*)")
COMMIT_RE = re.compile(r"`([0-9a-f]{7,40})`")


@dataclass
class MetricValue:
    key: str
    label: str
    display: str
    value: float
    base_unit: str


@dataclass
class ReportStamp:
    key: str
    label: str
    generated: str
    commit: str
    path: str


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text())


def dump_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def parse_metric(meta: Dict, badge: Dict) -> Optional[MetricValue]:
    message = (badge.get("message") or "").strip()
    match = BADGE_RE.match(message)
    if not match:
        return None
    value = float(match.group(1)) * PREFIX_SCALE[match.group(2)]
    unit = match.group(3)
    if unit != meta["base_unit"]:
        return None
    return MetricValue(
        key=meta["key"],
        label=meta["label"],
        display=message,
        value=value,
        base_unit=unit,
    )


def parse_report_stamp(site_dir: Path, report: Dict) -> Optional[ReportStamp]:
    path = site_dir / report["path"]
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        match = REPORT_META_RE.search(line)
        if not match:
            continue
        commit_match = COMMIT_RE.search(match.group(2))
        commit = commit_match.group(1) if commit_match else match.group(2).strip()
        return ReportStamp(
            key=report["key"],
            label=report["label"],
            generated=match.group(1).strip(),
            commit=commit,
            path="/" + report["path"].replace("index.md", ""),
        )
    return None


def load_history(path: Optional[Path]) -> List[Dict]:
    if path is None or not path.exists():
        return []
    rows: List[Dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def build_record(site_dir: Path) -> Optional[Dict]:
    metrics: Dict[str, Dict] = {}
    for meta in METRICS:
        badge_path = site_dir / meta["source"][0] / meta["source"][1]
        if not badge_path.exists():
            continue
        metric = parse_metric(meta, load_json(badge_path))
        if metric is None:
            continue
        metrics[metric.key] = {
            "label": metric.label,
            "display": metric.display,
            "value": metric.value,
            "base_unit": metric.base_unit,
        }
    if not metrics:
        return None

    reports = {
        stamp.key: {
            "label": stamp.label,
            "generated": stamp.generated,
            "commit": stamp.commit,
            "path": stamp.path,
        }
        for stamp in (parse_report_stamp(site_dir, report) for report in REPORTS)
        if stamp is not None
    }
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sha = os.environ.get("GITHUB_SHA", "")
    return {
        "generated": now,
        "sha": sha[:8] if sha else "unknown",
        "metrics": metrics,
        "reports": reports,
    }


def same_record(a: Dict, b: Dict) -> bool:
    return a.get("sha") == b.get("sha") and a.get("metrics") == b.get("metrics")


def append_history(history: List[Dict], record: Optional[Dict]) -> List[Dict]:
    if record is None:
        return history
    if history and same_record(history[-1], record):
        return history
    return history + [record]


def write_badges(perf_dir: Path, history: List[Dict]) -> None:
    latest = history[-1] if history else {"metrics": {}}
    for meta in METRICS:
        metric = latest.get("metrics", {}).get(meta["key"])
        dump_json(
            perf_dir / meta["badge_file"],
            {
                "schemaVersion": 1,
                "label": meta["label"],
                "message": metric["display"] if metric else "n/a",
                "color": BADGE_COLORS[meta["key"]] if metric else "lightgrey",
            },
        )


def _svg_points(values: List[float], width: int, height: int, padding: int) -> str:
    if len(values) == 1:
        return f"{width / 2:.1f},{height / 2:.1f}"
    vmin = min(values)
    vmax = max(values)
    if math.isclose(vmin, vmax):
        vmin *= 0.95
        vmax *= 1.05 if vmax else 1.0
    usable_w = width - 2 * padding
    usable_h = height - 2 * padding
    points = []
    for idx, value in enumerate(values):
        x = padding + usable_w * idx / max(len(values) - 1, 1)
        y = padding + usable_h * (1 - ((value - vmin) / (vmax - vmin)))
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def write_chart(perf_dir: Path, meta: Dict, history: List[Dict]) -> bool:
    series = [
        (row.get("sha", "?"), row["metrics"][meta["key"]]["value"], row["metrics"][meta["key"]]["display"])
        for row in history
        if row.get("metrics", {}).get(meta["key"])
    ]
    if not series:
        return False
    width = 680
    height = 220
    padding = 28
    values = [value for _, value, _ in series]
    points = _svg_points(values, width, height, padding)
    label_latest = series[-1][2]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{meta['label']} trend">
  <rect width="100%" height="100%" rx="18" fill="#ffffff" stroke="#d9e2f0"/>
  <polyline fill="none" stroke="{CHART_COLORS[meta['key']]}" stroke-width="4" points="{points}"/>
  <text x="28" y="34" fill="#111827" font-family="system-ui,sans-serif" font-size="18" font-weight="700">{meta['label']}</text>
  <text x="28" y="56" fill="#4b5563" font-family="system-ui,sans-serif" font-size="12">oldest run at left, newest at right</text>
  <text x="28" y="190" fill="#6b7280" font-family="system-ui,sans-serif" font-size="12">{series[0][0]}</text>
  <text x="652" y="190" text-anchor="end" fill="#6b7280" font-family="system-ui,sans-serif" font-size="12">{series[-1][0]}</text>
  <text x="652" y="34" text-anchor="end" fill="#111827" font-family="system-ui,sans-serif" font-size="14" font-weight="700">latest: {label_latest}</text>
</svg>
'''
    (perf_dir / f"chart-{meta['key']}.svg").write_text(svg)
    return True


def write_history(history_path: Path, history: List[Dict]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text("".join(json.dumps(row) + "\n" for row in history))


def report_rows(history: List[Dict]) -> str:
    latest_reports = history[-1].get("reports", {}) if history else {}
    rows = []
    for report in REPORTS:
        item = latest_reports.get(report["key"])
        if item is None:
            continue
        rows.append(
            f"| {item['label']} | {item['generated']} | `{item['commit']}` | [{item['path']}]({item['path']}) |"
        )
    return "\n".join(rows) if rows else "| No report metadata available | - | - | - |"


def history_rows(history: List[Dict]) -> str:
    rows = []
    for row in history[-10:]:
        metrics = row.get("metrics", {})
        rows.append(
            "| {sha} | {generated} | {gemm} | {hipfile} | {hipfile_fio} |".format(
                sha=row.get("sha", "?"),
                generated=row.get("generated", "unknown"),
                gemm=metrics.get("gemm", {}).get("display", "n/a"),
                hipfile=metrics.get("hipfile", {}).get("display", "n/a"),
                hipfile_fio=metrics.get("hipfile-fio", {}).get("display", "n/a"),
            )
        )
    return "\n".join(rows) if rows else "| No retained runs yet | - | - | - | - |"


def latest_metric_rows(history: List[Dict]) -> str:
    metrics = history[-1].get("metrics", {}) if history else {}
    rows = []
    for meta in METRICS:
        metric = metrics.get(meta["key"])
        rows.append(
            f"| {meta['label']} | {metric['display'] if metric else 'n/a'} | {'Higher is better' if meta['higher_is_better'] else 'Lower is better'} |"
        )
    return "\n".join(rows)


def write_perf_page(site_dir: Path, history: List[Dict]) -> None:
    perf_dir = site_dir / "perf"
    perf_dir.mkdir(parents=True, exist_ok=True)
    charts = [meta for meta in METRICS if write_chart(perf_dir, meta, history)]
    chart_blocks = "\n".join(
        f"### {meta['label']}\n\n![{meta['label']} trend](chart-{meta['key']}.svg)"
        for meta in charts
    )
    latest = history[-1] if history else None
    latest_generated = latest.get("generated", "unknown") if latest else "unknown"
    latest_sha = latest.get("sha", "unknown") if latest else "unknown"
    body = f"""---
layout: page
title: Performance trends
permalink: /perf/
---

This page is generated during the Pages publish from retained benchmark history on
`gh-pages`, using the latest rocjitsu and hipFile fio report artifacts as inputs.
It mirrors the same basic model used by the ROCm/rocm-ernic site: badges for the
latest published numbers, a durable history file, and a trend page regenerated as
new CI data lands.

## Latest published snapshot

Generated from `{latest_sha}` at **{latest_generated}**.

![rocjitsu GEMM](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json)
![rocjitsu hipFile](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json)
![hipFile fio](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json)

| Metric | Latest value | Interpretation |
| --- | --- | --- |
{latest_metric_rows(history)}

## Trend charts

{chart_blocks if chart_blocks else 'No retained benchmark history yet.'}

## Recent runs

| Commit | Generated | GEMM | hipFile | hipFile fio |
| --- | --- | --- | --- | --- |
{history_rows(history)}

## Current report freshness

| Report | Generated | Commit | Page |
| --- | --- | --- | --- |
{report_rows(history)}
"""
    (perf_dir / "index.md").write_text(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-dir", required=True, type=Path)
    parser.add_argument("--history-in", type=Path)
    parser.add_argument("--history-out", required=True, type=Path)
    args = parser.parse_args()

    history = load_history(args.history_in)
    history = append_history(history, build_record(args.site_dir))
    write_history(args.history_out, history)
    write_badges(args.site_dir / "perf", history)
    write_perf_page(args.site_dir, history)


if __name__ == "__main__":
    main()
