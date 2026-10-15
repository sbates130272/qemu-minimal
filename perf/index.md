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

Generated from `4b223d97` at **2026-10-15 20:26 UTC**.

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
| rocjitsu GEMM | 479 kFLOP/s | 508 kFLOP/s | watch (-6%) |
| rocjitsu hipFile | 152 MB/s | 177 MB/s | watch (-14%) |
| hipFile fio | 137 MB/s | 146 MB/s | watch (-6%) |

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
| a76e8818 | 2026-10-10 14:33 UTC | 479 kFLOP/s | 160 MB/s | 141 MB/s |
| a76e8818 | 2026-10-11 14:59 UTC | 482 kFLOP/s | 159 MB/s | 140 MB/s |
| a76e8818 | 2026-10-12 17:36 UTC | 470 kFLOP/s | 163 MB/s | 145 MB/s |
| b6e1231f | 2026-10-13 15:38 UTC | 470 kFLOP/s | 163 MB/s | 145 MB/s |
| b6e1231f | 2026-10-13 16:03 UTC | 625 kFLOP/s | 247 MB/s | 193 MB/s |
| c5d51ce8 | 2026-10-13 22:17 UTC | 625 kFLOP/s | 247 MB/s | 193 MB/s |
| c5d51ce8 | 2026-10-13 22:45 UTC | 480 kFLOP/s | 159 MB/s | 110 MB/s |
| c5d51ce8 | 2026-10-14 13:39 UTC | 481 kFLOP/s | 158 MB/s | 140 MB/s |
| c5d51ce8 | 2026-10-15 13:27 UTC | 479 kFLOP/s | 152 MB/s | 137 MB/s |
| 4b223d97 | 2026-10-15 20:26 UTC | 479 kFLOP/s | 152 MB/s | 137 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-15** &middot; checked
2026-10-15 20:27 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-15 13:15 UTC | pass | `c5d51ce` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-15 13:23 UTC | pass | `c5d51ce` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-15 13:20 UTC | pass | `c5d51ce` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-15 13:20 UTC | pass | `c5d51ce` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-15 13:26 UTC | pass | `c5d51ce` | [/hipfile-fio/](/hipfile-fio/) |
