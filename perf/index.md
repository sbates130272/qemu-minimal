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

Generated from `261bde7e` at **2026-10-21 04:03 UTC**.

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
| rocjitsu GEMM | 662 kFLOP/s | 522 kFLOP/s | within tolerance (+27%) |
| rocjitsu hipFile | 220 MB/s | 166 MB/s | within tolerance (+33%) |
| hipFile fio | 219 MB/s | 139 MB/s | within tolerance (+57%) |

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
| ef2d5c17 | 2026-10-20 18:58 UTC | 750 kFLOP/s | 109 MB/s | 88.5 MB/s |
| ef2d5c17 | 2026-10-20 19:18 UTC | 488 kFLOP/s | 197 MB/s | 148 MB/s |
| 1da22630 | 2026-10-20 22:33 UTC | 488 kFLOP/s | 197 MB/s | 148 MB/s |
| 1da22630 | 2026-10-20 23:00 UTC | 372 kFLOP/s | 164 MB/s | 148 MB/s |
| 71c00464 | 2026-10-20 23:58 UTC | 372 kFLOP/s | 164 MB/s | 148 MB/s |
| 71c00464 | 2026-10-21 00:26 UTC | 496 kFLOP/s | 159 MB/s | 138 MB/s |
| 23dc08d2 | 2026-10-21 01:58 UTC | 496 kFLOP/s | 159 MB/s | 138 MB/s |
| 23dc08d2 | 2026-10-21 02:19 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| fa219cd3 | 2026-10-21 03:41 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| 261bde7e | 2026-10-21 04:03 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-21** &middot; checked
2026-10-21 04:03 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-21 04:02 UTC | pass | `261bde7` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-21 02:16 UTC | pass | `23dc08d` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-21 02:00 UTC | pass | `23dc08d` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-21 02:00 UTC | pass | `23dc08d` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-21 02:18 UTC | pass | `23dc08d` | [/hipfile-fio/](/hipfile-fio/) |
