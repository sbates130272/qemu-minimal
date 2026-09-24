---
layout: page
title: Reports
permalink: /reports/
---

<div class="report-grid">
  <div class="report-card">
    <span class="eyebrow">Single guest</span>
    <h2><a href="{{ '/1vm/' | relative_url }}">Single VM report</a></h2>
    <p>One <code>gen-vm</code> guest with NVMe attached, built and inspected on a GitHub-hosted runner.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">GPU guest</span>
    <h2><a href="{{ '/rocjitsu/' | relative_url }}">rocjitsu report</a></h2>
    <p>rocjitsu guest inspection plus the current GEMM and hipFile benchmark outputs.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">Two guests</span>
    <h2><a href="{{ '/ernic/' | relative_url }}">ernic report</a></h2>
    <p>The ernic stack, with VM 1 at the top-level page and VM 2 nested underneath.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">fio lane</span>
    <h2><a href="{{ '/hipfile-fio/' | relative_url }}">hipFile fio report</a></h2>
    <p>The fio <code>libhipfile</code> benchmark lane, published independently from the rocjitsu report.</p>
  </div>
</div>

## Lane summary

| Lane | Schedule / trigger | Output |
| --- | --- | --- |
| `report-for-vm-basic` | Push to `main`, daily 01:00 MST, manual dispatch | `/1vm/` |
| `report-for-vm-rocjitsu` | Path-filtered push to `main`, daily 01:00 MST, manual dispatch | `/rocjitsu/` |
| `report-for-vm-ernic` | Path-filtered push to `main`, daily 01:00 MST, manual dispatch | `/ernic/` and `/ernic/vm2/` (ernic VM 1 / VM 2) |
| `report-for-hipfile-fio` | Path-filtered push to `main`, daily 01:00 MST, manual dispatch | `/hipfile-fio/` |

## Why the reports stay separate

Each lane uploads a named artifact and stops. The Pages publisher assembles the
latest artifact from each lane so one run cannot erase another lane's content.
That keeps the site complete even when a lane has not run for days. The four
crons now fire together, so the publisher sees four completions in quick
succession; it never cancels a run in progress, so they queue and the site
settles once the last one drains.
