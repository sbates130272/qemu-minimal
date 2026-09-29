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

Generated from `86cb5326` at **2026-09-29 23:41 UTC**.

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
| rocjitsu GEMM | 381 kFLOP/s | 550 kFLOP/s | regressed (-31%) |
| rocjitsu hipFile | 158 MB/s | 152 MB/s | within tolerance (+4%) |
| hipFile fio | 147 MB/s | 174 MB/s | watch (-16%) |

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
| 54cc234d | 2026-09-28 05:28 UTC | 476 kFLOP/s | 157 MB/s | 147 MB/s |
| f3f5fc5d | 2026-09-29 01:03 UTC | 476 kFLOP/s | 157 MB/s | 147 MB/s |
| f3f5fc5d | 2026-09-29 01:18 UTC | 476 kFLOP/s | 157 MB/s | 226 MB/s |
| 6d2d4f93 | 2026-09-29 02:58 UTC | 476 kFLOP/s | 157 MB/s | 226 MB/s |
| 6d2d4f93 | 2026-09-29 03:23 UTC | 476 kFLOP/s | 157 MB/s | 146 MB/s |
| 6d2d4f93 | 2026-09-29 14:56 UTC | 476 kFLOP/s | 157 MB/s | 145 MB/s |
| 37d6f802 | 2026-09-29 18:18 UTC | 476 kFLOP/s | 157 MB/s | 145 MB/s |
| 37d6f802 | 2026-09-29 18:37 UTC | 476 kFLOP/s | 157 MB/s | 208 MB/s |
| 86cb5326 | 2026-09-29 23:11 UTC | 476 kFLOP/s | 157 MB/s | 208 MB/s |
| 86cb5326 | 2026-09-29 23:41 UTC | 381 kFLOP/s | 158 MB/s | 147 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-09-29** &middot; checked
2026-09-29 23:41 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-09-29 23:11 UTC | pass | `86cb532` | [/1vm/](/1vm/) |
| rocjitsu | 2026-09-29 23:37 UTC | pass | `86cb532` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-09-29 23:12 UTC | pass | `86cb532` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-09-29 23:12 UTC | pass | `86cb532` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-09-29 23:40 UTC | pass | `86cb532` | [/hipfile-fio/](/hipfile-fio/) |
