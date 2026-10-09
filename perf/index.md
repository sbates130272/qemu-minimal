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

Generated from `a76e8818` at **2026-10-09 15:09 UTC**.

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
| rocjitsu GEMM | 662 kFLOP/s | 590 kFLOP/s | within tolerance (+12%) |
| rocjitsu hipFile | 246 MB/s | 144 MB/s | within tolerance (+71%) |
| hipFile fio | 199 MB/s | 121 MB/s | within tolerance (+64%) |

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
| 4bb8f75f | 2026-10-03 13:23 UTC | 819 kFLOP/s | 110 MB/s | 98.5 MB/s |
| 4bb8f75f | 2026-10-04 14:02 UTC | 484 kFLOP/s | 157 MB/s | 140 MB/s |
| 4bb8f75f | 2026-10-05 16:59 UTC | 476 kFLOP/s | 151 MB/s | 132 MB/s |
| bfa9d453 | 2026-10-06 02:02 UTC | 476 kFLOP/s | 151 MB/s | 132 MB/s |
| bfa9d453 | 2026-10-06 02:32 UTC | 726 kFLOP/s | 124 MB/s | 102 MB/s |
| bfa9d453 | 2026-10-06 15:04 UTC | 469 kFLOP/s | 156 MB/s | 133 MB/s |
| bfa9d453 | 2026-10-07 15:20 UTC | 475 kFLOP/s | 159 MB/s | 136 MB/s |
| a76e8818 | 2026-10-07 17:59 UTC | 475 kFLOP/s | 159 MB/s | 136 MB/s |
| a76e8818 | 2026-10-08 15:34 UTC | 804 kFLOP/s | 128 MB/s | 102 MB/s |
| a76e8818 | 2026-10-09 15:09 UTC | 662 kFLOP/s | 246 MB/s | 199 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-09** &middot; checked
2026-10-09 15:09 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-09 15:09 UTC | pass | `a76e881` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-09 15:07 UTC | pass | `a76e881` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-08 15:32 UTC | pass | `a76e881` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-08 15:32 UTC | pass | `a76e881` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-09 15:09 UTC | pass | `a76e881` | [/hipfile-fio/](/hipfile-fio/) |
