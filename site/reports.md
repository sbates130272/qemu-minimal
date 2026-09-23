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
    <h2><a href="{{ '/two-vm/' | relative_url }}">ernic report</a></h2>
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
| `vm-report` | Push to `main`, weekly cron, manual dispatch | `/1vm/` |
| `vm-report-rocjitsu` | Path-filtered push to `main`, weekly cron, manual dispatch | `/rocjitsu/` |
| `vm-report-two-vms` | Weekly cron, manual dispatch | `/two-vm/` and `/two-vm/vm2/` (ernic VM 1 / VM 2) |
| `vm-report-hipfile-fio` | Path-filtered push to `main`, weekly cron, manual dispatch | `/hipfile-fio/` |

## Why the reports stay separate

Each lane uploads a named artifact and stops. The Pages publisher assembles the
latest artifact from each lane so one run cannot erase another lane's content.
That keeps the site complete even when lanes run on different schedules.
