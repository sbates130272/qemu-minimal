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

Generated from `c7af4403` at **2026-10-22 19:55 UTC**.

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
| rocjitsu GEMM | 478 kFLOP/s | 598 kFLOP/s | regressed (-20%) |
| rocjitsu hipFile | 194 MB/s | 184 MB/s | within tolerance (+6%) |
| hipFile fio | 158 MB/s | 159 MB/s | within tolerance (-1%) |

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
| 603bb555 | 2026-10-21 20:43 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| 3ee8f661 | 2026-10-21 22:21 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| 713581aa | 2026-10-21 22:23 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| b3056c5f | 2026-10-21 23:53 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| abd79c8e | 2026-10-22 00:20 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| f7a0e002 | 2026-10-22 00:30 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| b0ea911a | 2026-10-22 00:45 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| c0791954 | 2026-10-22 02:16 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |
| c0791954 | 2026-10-22 13:44 UTC | 478 kFLOP/s | 194 MB/s | 158 MB/s |
| c7af4403 | 2026-10-22 19:55 UTC | 478 kFLOP/s | 194 MB/s | 158 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 6 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-22** &middot; checked
2026-10-22 20:02 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-22 19:59 UTC | pass | `c7af440` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-22 13:41 UTC | pass | `c079195` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-22 13:47 UTC | pass | `c079195` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-22 13:47 UTC | pass | `c079195` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-22 13:43 UTC | pass | `c079195` | [/hipfile-fio/](/hipfile-fio/) |
| MI355X node | 2026-10-22 20:02 UTC | pass | ``unknown`` | [/mi355x/](/mi355x/) |
