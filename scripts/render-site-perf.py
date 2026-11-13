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
        "label": "Rocjitsu GEMM",
        "badge_file": "badge-gemm.json",
        "source": ("rocjitsu", "badge-gemm.json"),
        "base_unit": "FLOP/s",
        "higher_is_better": True,
    },
    {
        "key": "hipfile",
        "label": "Rocjitsu HipFile",
        "badge_file": "badge-hipfile.json",
        "source": ("rocjitsu", "badge-hipfile.json"),
        "base_unit": "B/s",
        "higher_is_better": True,
    },
    {
        "key": "hipfile-fio",
        "label": "HipFile Fio",
        "badge_file": "badge-hipfile-fio.json",
        "source": ("hipfile-fio", "badge-hipfile-fio.json"),
        "base_unit": "B/s",
        "higher_is_better": True,
    },
    {
        "key": "ernic-p4-bw",
        "label": "Ernic P4 FP8 BW",
        "badge_file": "badge-ernic-p4-bw.json",
        "source": ("ernic", "badge-ernic-p4-bw.json"),
        "base_unit": "B/s",
        "higher_is_better": True,
    },
    {
        "key": "sdxi-bw",
        "label": "SDXI copy BW",
        "badge_file": "badge-sdxi-bw.json",
        "source": ("helios", "badge-sdxi-bw.json"),
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

# Health, not branding. These used to be the AMD/NVIDIA/Intel brand colours,
# which meant GEMM rendered red on its best day and none of the three ever
# changed -- the colour carried no information at all. Now the right-hand half
# of each badge encodes the latest reading against a rolling baseline.
#
# The dead band is deliberately wide. These benchmarks run against an emulated
# GPU over vfio-user and swing hard between runs; a strict "below baseline is
# red" rule would spend most of its life red for reasons that have nothing to
# do with a regression. 95%/80% keeps ordinary jitter green while still
# catching the real collapses, e.g. hipFile fio falling from 81 MB/s to
# 11 MB/s over three publishes.
REGRESSION_WARN_RATIO = 0.95
REGRESSION_FAIL_RATIO = 0.80

# Number of prior readings averaged into the baseline.
BASELINE_WINDOW = 5

COLOR_OK = "brightgreen"
COLOR_WARN = "yellow"
COLOR_REGRESSED = "red"
# Distinguishable from both: a value with nothing to compare it against is not
# a pass, and colouring it green would claim a verdict that has not been made.
COLOR_NO_BASELINE = "blue"
COLOR_MISSING = "lightgrey"

CHART_COLORS = {
    "gemm": "#d94b52",
    "hipfile": "#198754",
    "hipfile-fio": "#0d6efd",
    "ernic-p4-bw": "#ed6b21",
    "sdxi-bw": "#0071C5",
}

BADGE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?)([A-Za-z/]+)$")
RECORD_TIME_FMT = "%Y-%m-%d %H:%M UTC"


@dataclass
class MetricValue:
    key: str
    label: str
    display: str
    value: float
    base_unit: str


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text())


def dump_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def scale_value(value: float, base_unit: str) -> str:
    """Format a base-unit float back into an SI-prefixed display string.

    The inverse of parse_metric, so a computed baseline renders in the same
    shape as the measured values it is derived from ("823 kFLOP/s"), rather
    than as a bare float nobody can compare by eye. Mirrors scale_metric() in
    scripts/generate-vm-report.sh, including its %.3g.
    """
    for prefix in ("T", "G", "M", "k"):
        scale = PREFIX_SCALE[prefix]
        if abs(value) >= scale:
            return f"{value / scale:.3g} {prefix}{base_unit}"
    return f"{value:.3g} {base_unit}"


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


def load_history(path: Optional[Path]) -> List[Dict]:
    if path is None or not path.exists():
        return []
    rows: List[Dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def build_record(input_dir: Path) -> Optional[Dict]:
    """One history row from the badge JSON the benchmark jobs uploaded.

    input_dir holds one subdirectory per artifact, named by METRICS' source.
    """
    metrics: Dict[str, Dict] = {}
    for meta in METRICS:
        badge_path = input_dir / meta["source"][0] / meta["source"][1]
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

    now = datetime.now(timezone.utc).strftime(RECORD_TIME_FMT)
    sha = os.environ.get("GITHUB_SHA", "")
    return {
        "generated": now,
        "sha": sha[:8] if sha else "unknown",
        "metrics": metrics,
    }


def same_record(a: Dict, b: Dict) -> bool:
    return a.get("sha") == b.get("sha") and a.get("metrics") == b.get("metrics")


def append_history(history: List[Dict], record: Optional[Dict]) -> List[Dict]:
    if record is None:
        return history
    if history and same_record(history[-1], record):
        return history
    return history + [record]


def distinct_values(history: List[Dict], key: str) -> List[float]:
    """Readings for one metric, with consecutive repeats collapsed.

    A row in the history is one *publish*, not one benchmark run: a publish
    re-records every artifact it finds, including ones no job refreshed. Five rows can therefore be one measurement republished
    five times, and averaging them verbatim would weight a wedged value by
    however many unrelated publishes happened to fire while it was stuck.
    Collapsing consecutive repeats makes the window mean "the last N readings
    that actually changed", which is what a baseline is supposed to be.

    Rows where the metric is absent are skipped rather than read as zero --
    a dropped metric is a gap in the series, not a reading of nothing.
    """
    values: List[float] = []
    for row in history:
        metric = row.get("metrics", {}).get(key)
        if not metric:
            continue
        value = metric.get("value")
        if value is None:
            continue
        if values and math.isclose(values[-1], value):
            continue
        values.append(float(value))
    return values


def baseline_for(history: List[Dict], key: str) -> Optional[float]:
    """Mean of the BASELINE_WINDOW distinct readings before the latest one."""
    values = distinct_values(history, key)
    if len(values) < 2:
        return None
    window = values[-(BASELINE_WINDOW + 1):-1]
    if not window:
        return None
    return sum(window) / len(window)


def health(value: float, baseline: Optional[float], higher_is_better: bool) -> Dict:
    """Colour and delta for one reading against its baseline."""
    if baseline is None or baseline == 0:
        return {"color": COLOR_NO_BASELINE, "delta": None, "ratio": None}
    # Normalise so that ratio >= 1 always means "at least as good", whichever
    # direction the metric improves in. Only higher_is_better metrics exist
    # today, but the flag is declared on every entry in METRICS and silently
    # ignoring it here is exactly how the next lower-is-better metric would get
    # its colour backwards.
    ratio = value / baseline if higher_is_better else baseline / value
    if ratio >= REGRESSION_WARN_RATIO:
        color = COLOR_OK
    elif ratio >= REGRESSION_FAIL_RATIO:
        color = COLOR_WARN
    else:
        color = COLOR_REGRESSED
    return {"color": color, "delta": (ratio - 1.0) * 100.0, "ratio": ratio}


def write_badges(perf_dir: Path, history: List[Dict]) -> None:
    latest = history[-1] if history else {"metrics": {}}
    for meta in METRICS:
        metric = latest.get("metrics", {}).get(meta["key"])
        if not metric:
            dump_json(
                perf_dir / meta["badge_file"],
                {
                    "schemaVersion": 1,
                    "label": meta["label"],
                    "message": "n/a",
                    "color": COLOR_MISSING,
                },
            )
            continue
        state = health(
            float(metric["value"]),
            baseline_for(history, meta["key"]),
            meta["higher_is_better"],
        )
        message = metric["display"]
        if state["delta"] is not None:
            message = f"{message} ({state['delta']:+.0f}%)"
        dump_json(
            perf_dir / meta["badge_file"],
            {
                "schemaVersion": 1,
                "label": meta["label"],
                "message": message,
                "color": state["color"],
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


def history_rows(history: List[Dict]) -> str:
    rows = []
    for row in history[-10:]:
        metrics = row.get("metrics", {})
        cells = [row.get("sha", "?"), row.get("generated", "unknown")] + [
            metrics.get(meta["key"], {}).get("display", "n/a") for meta in METRICS
        ]
        rows.append("| " + " | ".join(cells) + " |")
    if rows:
        return "\n".join(rows)
    return "| No retained runs yet | - |" + " - |" * len(METRICS)


def latest_metric_rows(history: List[Dict]) -> str:
    metrics = history[-1].get("metrics", {}) if history else {}
    rows = []
    for meta in METRICS:
        metric = metrics.get(meta["key"])
        if not metric:
            rows.append(f"| {meta['label']} | n/a | - | no data |")
            continue
        baseline = baseline_for(history, meta["key"])
        state = health(float(metric["value"]), baseline, meta["higher_is_better"])
        if baseline is None:
            verdict = "no baseline yet"
            baseline_cell = "-"
        else:
            verdict = {
                COLOR_OK: "within tolerance",
                COLOR_WARN: "watch",
                COLOR_REGRESSED: "regressed",
            }[state["color"]]
            verdict = f"{verdict} ({state['delta']:+.0f}%)"
            baseline_cell = scale_value(baseline, meta["base_unit"])
        rows.append(
            f"| {meta['label']} | {metric['display']} | {baseline_cell} | {verdict} |"
        )
    return "\n".join(rows)


BADGE_URL = (
    "https://img.shields.io/endpoint?url="
    "https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2F"
)


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
    badges = "\n".join(
        f"![{meta['label']}]({BADGE_URL}{meta['badge_file']})" for meta in METRICS
    )
    history_head = "| Commit | Generated | " + " | ".join(
        meta["label"] for meta in METRICS
    ) + " |\n|" + " --- |" * (len(METRICS) + 2)
    body = f"""---
title: Performance trends
---

This page is generated during the Pages publish from retained benchmark history on
`gh-pages`, using the badge JSON the benchmark jobs upload as inputs. It mirrors
the same basic model used by the ROCm/rocm-ernic site: badges for the latest
published numbers, a durable history file, and a trend page regenerated as new
CI data lands.

## Latest published snapshot

Generated from `{latest_sha}` at **{latest_generated}**.

{badges}

Badge colour is health, not branding. Each latest reading is compared against
the mean of the previous {BASELINE_WINDOW} *distinct* readings — consecutive
republishes of an unchanged number are collapsed first, because a publish
re-records every artifact it finds, including ones no job refreshed. Green is
at or above {REGRESSION_WARN_RATIO:.0%} of that
baseline, amber down to {REGRESSION_FAIL_RATIO:.0%}, red below it, and blue
means there is not yet a second distinct reading to compare against. The band
is wide on purpose: these benchmarks run against an emulated GPU over
vfio-user and swing hard between runs.

| Metric | Latest value | Baseline (last {BASELINE_WINDOW} distinct) | Verdict |
| --- | --- | --- | --- |
{latest_metric_rows(history)}

## Trend charts

{chart_blocks if chart_blocks else 'No retained benchmark history yet.'}

## Recent runs

{history_head}
{history_rows(history)}
"""
    (perf_dir / "index.md").write_text(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    # Where the benchmark artifacts were unpacked, one subdirectory each.
    # Separate from --site-dir so the raw inputs are never published.
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--site-dir", required=True, type=Path)
    parser.add_argument("--history-in", type=Path)
    parser.add_argument("--history-out", required=True, type=Path)
    args = parser.parse_args()

    history = load_history(args.history_in)
    history = append_history(history, build_record(args.input_dir))
    write_history(args.history_out, history)

    write_badges(args.site_dir / "perf", history)
    write_perf_page(args.site_dir, history)


if __name__ == "__main__":
    main()
