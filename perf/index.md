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

Generated from `65dc3a5c` at **2026-10-28 15:12 UTC**.

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
| rocjitsu GEMM | 837 kFLOP/s | 568 kFLOP/s | within tolerance (+47%) |
| rocjitsu hipFile | 114 MB/s | 156 MB/s | regressed (-27%) |
| hipFile fio | 81.3 MB/s | 132 MB/s | regressed (-39%) |

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
| 8ee55313 | 2026-10-26 15:11 UTC | 495 kFLOP/s | 161 MB/s | 132 MB/s |
| 8986530f | 2026-10-27 00:07 UTC | 495 kFLOP/s | 161 MB/s | 132 MB/s |
| 8986530f | 2026-10-27 00:36 UTC | 494 kFLOP/s | 155 MB/s | 128 MB/s |
| c81997e8 | 2026-10-27 01:01 UTC | 494 kFLOP/s | 155 MB/s | 128 MB/s |
| c81997e8 | 2026-10-27 01:36 UTC | 464 kFLOP/s | 159 MB/s | 144 MB/s |
| c81997e8 | 2026-10-27 14:10 UTC | 894 kFLOP/s | 148 MB/s | 115 MB/s |
| 0a353f0d | 2026-10-27 16:43 UTC | 894 kFLOP/s | 148 MB/s | 115 MB/s |
| 65dc3a5c | 2026-10-27 21:08 UTC | 894 kFLOP/s | 148 MB/s | 115 MB/s |
| 65dc3a5c | 2026-10-27 21:38 UTC | 491 kFLOP/s | 159 MB/s | 142 MB/s |
| 65dc3a5c | 2026-10-28 15:12 UTC | 837 kFLOP/s | 114 MB/s | 81.3 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 6 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-23** &middot; checked
2026-10-28 15:12 UTC &middot; currently
not all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-28 15:09 UTC | pass | `65dc3a5` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-28 15:09 UTC | pass | `65dc3a5` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-28 15:07 UTC | pass | `65dc3a5` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-28 15:07 UTC | pass | `65dc3a5` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-28 15:10 UTC | pass | `65dc3a5` | [/hipfile-fio/](/hipfile-fio/) |
| MI355X node | 2026-10-24 17:02 UTC | pass | ``unknown`` | [/mi355x/](/mi355x/) |
