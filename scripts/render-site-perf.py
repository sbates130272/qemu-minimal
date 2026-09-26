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
}

REPORTS = (
    {"key": "1vm", "label": "Single VM", "path": "1vm/index.md"},
    {"key": "rocjitsu", "label": "rocjitsu", "path": "rocjitsu/index.md"},
    {"key": "ernic", "label": "ernic VM 1", "path": "ernic/index.md"},
    {"key": "ernic-vm2", "label": "ernic VM 2", "path": "ernic/vm2/index.md"},
    {"key": "hipfile-fio", "label": "hipFile fio", "path": "hipfile-fio/index.md"},
)

BADGE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kMGT]?)([A-Za-z/]+)$")
REPORT_META_RE = re.compile(r"Generated:\s*\*\*(.*?)\*\*\s*&middot;\s*Commit:\s*(.*)")
COMMIT_RE = re.compile(r"`([0-9a-f]{7,40})`")
# Optional on purpose: reports already on gh-pages predate the stamp, and a
# report with no Status is treated as unknown rather than as a pass.
REPORT_STATUS_RE = re.compile(r"Status:\s*\*\*(.*?)\*\*")
REPORT_TIME_FMT = "%Y-%m-%d %H:%M UTC"

# How recently a report must have been generated to count towards "all reports
# green". Every report lane runs on a daily 08:00 cron, so anything inside a
# day and a half is this cycle's report; anything older means that lane last
# failed to produce one. Freshness and the pass stamp are both required --
# the stamp alone would call a month-old success green forever, and freshness
# alone would call a fresh failure green.
DEFAULT_GREEN_MAX_AGE_HOURS = 36


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
    status: str


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


def parse_report_stamp(site_dir: Path, report: Dict) -> Optional[ReportStamp]:
    path = site_dir / report["path"]
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        match = REPORT_META_RE.search(line)
        if not match:
            continue
        # The status stamp lives on the same line, after another &middot;, so
        # trim the tail before falling back to the raw text as a commit.
        tail = match.group(2).split("&middot;")[0]
        commit_match = COMMIT_RE.search(tail)
        commit = commit_match.group(1) if commit_match else tail.strip()
        status_match = REPORT_STATUS_RE.search(line)
        return ReportStamp(
            key=report["key"],
            label=report["label"],
            generated=match.group(1).strip(),
            commit=commit,
            path="/" + report["path"].replace("index.md", ""),
            status=status_match.group(1).strip() if status_match else "unknown",
        )
    return None


def parse_report_time(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, REPORT_TIME_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_green(path: Optional[Path]) -> Dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # A truncated state file must not fail the publish. Losing the stamp
        # costs one date; failing here costs the whole site update.
        return {}


def load_history(path: Optional[Path]) -> List[Dict]:
    if path is None or not path.exists():
        return []
    rows: List[Dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def build_reports(site_dir: Path) -> Dict[str, Dict]:
    return {
        stamp.key: {
            "label": stamp.label,
            "generated": stamp.generated,
            "commit": stamp.commit,
            "path": stamp.path,
            "status": stamp.status,
        }
        for stamp in (parse_report_stamp(site_dir, report) for report in REPORTS)
        if stamp is not None
    }


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

    reports = build_reports(site_dir)
    now = datetime.now(timezone.utc).strftime(REPORT_TIME_FMT)
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


def distinct_values(history: List[Dict], key: str) -> List[float]:
    """Readings for one metric, with consecutive repeats collapsed.

    A row in the history is one *publish*, not one benchmark run: any report
    lane finishing triggers a publish, which re-records the other lanes' last
    artifacts unchanged. Five rows can therefore be one measurement republished
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


def report_green(entry: Dict, now: datetime, max_age_hours: float) -> bool:
    """Whether one report counts as green at publish time.

    Both halves are required. The pass stamp on its own would call a report
    that succeeded last month green forever; freshness on its own would call a
    report that ran today and failed green, because a failing lane uploads
    nothing and simply leaves its previous report in place. Together they mean
    "this lane produced a passing report this cycle".
    """
    if entry.get("status") != "pass":
        return False
    generated = parse_report_time(entry.get("generated", ""))
    if generated is None:
        return False
    return (now - generated).total_seconds() <= max_age_hours * 3600


def evaluate_green(record: Optional[Dict], now: datetime, max_age_hours: float) -> bool:
    """True when every report in REPORTS is present, stamped pass and fresh."""
    if record is None:
        return False
    reports = record.get("reports", {})
    if len(reports) < len(REPORTS):
        return False
    return all(
        report_green(reports[meta["key"]], now, max_age_hours)
        for meta in REPORTS
        if meta["key"] in reports
    )


def update_green(state: Dict, record: Optional[Dict], now: datetime,
                 max_age_hours: float) -> Dict:
    """Advance the durable all-green stamp.

    This is kept in its own small file rather than in the history rows because
    append_history dedupes on (sha, metrics): a publish where the benchmarks
    produced identical numbers appends no row at all, and a green date carried
    in the rows could never advance on such a publish. Report freshness moves
    independently of benchmark values, so it needs a store that moves with it.
    """
    green = evaluate_green(record, now, max_age_hours)
    updated = dict(state)
    updated["checked"] = now.strftime(REPORT_TIME_FMT)
    updated["green"] = green
    if green:
        updated["last_all_green"] = now.strftime("%Y-%m-%d")
    return updated


def write_green_badge(perf_dir: Path, state: Dict) -> None:
    last = state.get("last_all_green")
    if state.get("green"):
        message, color = last, COLOR_OK
    elif last:
        # Amber rather than red: the date is still true, it has just stopped
        # being today. Red is reserved for never having been green at all.
        message, color = last, COLOR_WARN
    else:
        message, color = "never", COLOR_REGRESSED
    dump_json(
        perf_dir / "badge-all-green.json",
        {
            "schemaVersion": 1,
            "label": "all reports green",
            "message": message,
            "color": color,
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
            f"| {item['label']} | {item['generated']} | {item.get('status', 'unknown')} "
            f"| `{item['commit']}` | [{item['path']}]({item['path']}) |"
        )
    return "\n".join(rows) if rows else "| No report metadata available | - | - | - | - |"


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


def write_perf_page(site_dir: Path, history: List[Dict], green: Dict,
                    green_max_age: float) -> None:
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

Badge colour is health, not branding. Each latest reading is compared against
the mean of the previous {BASELINE_WINDOW} *distinct* readings — consecutive
republishes of an unchanged number are collapsed first, because a publish is
triggered by any report lane finishing and re-records the other lanes' last
artifacts untouched. Green is at or above {REGRESSION_WARN_RATIO:.0%} of that
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

| Commit | Generated | GEMM | hipFile | hipFile fio |
| --- | --- | --- | --- | --- |
{history_rows(history)}

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within {green_max_age:g} hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all {len(REPORTS)} reports were green at once —
amber once that date is no longer today.

Last all-green: **{green.get('last_all_green') or 'never'}** &middot; checked
{green.get('checked', 'unknown')} &middot; currently
{'all green' if green.get('green') else 'not all green'}.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
{report_rows(history)}
"""
    (perf_dir / "index.md").write_text(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-dir", required=True, type=Path)
    parser.add_argument("--history-in", type=Path)
    parser.add_argument("--history-out", required=True, type=Path)
    # Separate from the history file on purpose; see update_green().
    parser.add_argument("--green-in", type=Path)
    parser.add_argument("--green-out", type=Path)
    parser.add_argument(
        "--green-max-age-hours",
        type=float,
        default=DEFAULT_GREEN_MAX_AGE_HOURS,
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    history = load_history(args.history_in)
    record = build_record(args.site_dir)
    history = append_history(history, record)
    write_history(args.history_out, history)

    # Evaluate greenness from the reports on disk rather than from `record`,
    # which is None when no metric badge is present at all. Report freshness
    # has nothing to do with whether a benchmark produced a number.
    green = update_green(
        load_green(args.green_in),
        {"reports": build_reports(args.site_dir)},
        now,
        args.green_max_age_hours,
    )
    if args.green_out:
        dump_json(args.green_out, green)

    write_badges(args.site_dir / "perf", history)
    write_green_badge(args.site_dir / "perf", green)
    write_perf_page(args.site_dir, history, green, args.green_max_age_hours)


if __name__ == "__main__":
    main()
