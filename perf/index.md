---
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

Generated from `d32c43c7` at **2026-10-02 20:29 UTC**.

![rocjitsu GEMM](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json)
![rocjitsu hipFile](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json)
![hipFile fio](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json)

Badge colour is health, not branding. Each latest reading is compared against
the mean of the previous 5 *distinct* readings — consecutive
republishes of an unchanged number are collapsed first, because a publish is
triggered by any report lane finishing and re-records the other lanes' last
artifacts untouched. Green is at or above 95% of that
baseline, amber down to 80%, red below it, and blue
means there is not yet a second distinct reading to compare against. The band
is wide on purpose: these benchmarks run against an emulated GPU over
vfio-user and swing hard between runs.

| Metric | Latest value | Baseline (last 5 distinct) | Verdict |
| --- | --- | --- | --- |
| rocjitsu GEMM | 665 kFLOP/s | 559 kFLOP/s | within tolerance (+19%) |
| rocjitsu hipFile | 246 MB/s | 149 MB/s | within tolerance (+65%) |
| hipFile fio | 161 MB/s | 117 MB/s | within tolerance (+38%) |

## Trend charts

### rocjitsu GEMM

![rocjitsu GEMM trend](chart-gemm.svg)
### rocjitsu hipFile

![rocjitsu hipFile trend](chart-hipfile.svg)
### hipFile fio

![hipFile fio trend](chart-hipfile-fio.svg)

## Recent runs

| Commit | Generated | GEMM | hipFile | hipFile fio |
| --- | --- | --- | --- | --- |
| b7b730b7 | 2026-10-01 01:55 UTC | 822 kFLOP/s | 144 MB/s | 114 MB/s |
| b7b730b7 | 2026-10-01 20:26 UTC | 734 kFLOP/s | 134 MB/s | 53.8 MB/s |
| 29d91692 | 2026-10-02 15:03 UTC | 734 kFLOP/s | 134 MB/s | 53.8 MB/s |
| 254e1a6b | 2026-10-02 15:46 UTC | 734 kFLOP/s | 134 MB/s | 53.8 MB/s |
| 1d9990ea | 2026-10-02 15:49 UTC | 734 kFLOP/s | 134 MB/s | 53.8 MB/s |
| fe1fb969 | 2026-10-02 19:19 UTC | 734 kFLOP/s | 134 MB/s | 53.8 MB/s |
| fe1fb969 | 2026-10-02 19:48 UTC | 383 kFLOP/s | 162 MB/s | 133 MB/s |
| 03d3b2f3 | 2026-10-02 20:02 UTC | 383 kFLOP/s | 162 MB/s | 133 MB/s |
| d32c43c7 | 2026-10-02 20:12 UTC | 383 kFLOP/s | 162 MB/s | 133 MB/s |
| d32c43c7 | 2026-10-02 20:29 UTC | 665 kFLOP/s | 246 MB/s | 161 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-02** &middot; checked
2026-10-02 20:29 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-02 20:01 UTC | pass | `03d3b2f` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-02 20:26 UTC | pass | `d32c43c` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-02 20:11 UTC | pass | `d32c43c` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-02 20:11 UTC | pass | `d32c43c` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-02 20:28 UTC | pass | `d32c43c` | [/hipfile-fio/](/hipfile-fio/) |
