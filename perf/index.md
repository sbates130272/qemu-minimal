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

Generated from `c151650f` at **2026-10-23 05:56 UTC**.

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
| rocjitsu GEMM | 502 kFLOP/s | 584 kFLOP/s | watch (-14%) |
| rocjitsu hipFile | 169 MB/s | 178 MB/s | watch (-5%) |
| hipFile fio | 149 MB/s | 146 MB/s | within tolerance (+2%) |

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
| f7a0e002 | 2026-10-22 00:30 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| b0ea911a | 2026-10-22 00:45 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| c0791954 | 2026-10-22 02:16 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| c0791954 | 2026-10-22 13:44 UTC | 478 kFLOP/s | 194 MB/s | 158 MB/s |
| c7af4403 | 2026-10-22 19:55 UTC | 478 kFLOP/s | 194 MB/s | 158 MB/s |
| c7af4403 | 2026-10-22 20:28 UTC | 490 kFLOP/s | 160 MB/s | 141 MB/s |
| 3c522b9a | 2026-10-23 00:04 UTC | 490 kFLOP/s | 160 MB/s | 141 MB/s |
| 3c522b9a | 2026-10-23 00:33 UTC | 495 kFLOP/s | 162 MB/s | 140 MB/s |
| c151650f | 2026-10-23 05:24 UTC | 495 kFLOP/s | 162 MB/s | 140 MB/s |
| c151650f | 2026-10-23 05:56 UTC | 502 kFLOP/s | 169 MB/s | 149 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 6 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-23** &middot; checked
2026-10-23 05:56 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-23 05:29 UTC | pass | `c151650` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-23 05:53 UTC | pass | `c151650` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-23 05:31 UTC | pass | `c151650` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-23 05:31 UTC | pass | `c151650` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-23 05:56 UTC | pass | `c151650` | [/hipfile-fio/](/hipfile-fio/) |
| MI355X node | 2026-10-23 05:31 UTC | pass | ``unknown`` | [/mi355x/](/mi355x/) |
