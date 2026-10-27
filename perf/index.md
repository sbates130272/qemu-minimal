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

Generated from `8986530f` at **2026-10-27 00:36 UTC**.

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
| rocjitsu GEMM | 494 kFLOP/s | 539 kFLOP/s | watch (-8%) |
| rocjitsu hipFile | 155 MB/s | 174 MB/s | watch (-11%) |
| hipFile fio | 128 MB/s | 152 MB/s | watch (-16%) |

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
| c151650f | 2026-10-23 05:56 UTC | 502 kFLOP/s | 169 MB/s | 149 MB/s |
| c151650f | 2026-10-23 13:26 UTC | 497 kFLOP/s | 162 MB/s | 142 MB/s |
| 9ec66ea4 | 2026-10-23 22:01 UTC | 497 kFLOP/s | 162 MB/s | 142 MB/s |
| 9ec66ea4 | 2026-10-23 22:28 UTC | 496 kFLOP/s | 160 MB/s | 139 MB/s |
| 8ee55313 | 2026-10-24 20:38 UTC | 496 kFLOP/s | 160 MB/s | 139 MB/s |
| 8ee55313 | 2026-10-24 21:08 UTC | 496 kFLOP/s | 157 MB/s | 127 MB/s |
| 8ee55313 | 2026-10-25 13:12 UTC | 703 kFLOP/s | 228 MB/s | 219 MB/s |
| 8ee55313 | 2026-10-26 15:11 UTC | 495 kFLOP/s | 161 MB/s | 132 MB/s |
| 8986530f | 2026-10-27 00:07 UTC | 495 kFLOP/s | 161 MB/s | 132 MB/s |
| 8986530f | 2026-10-27 00:36 UTC | 494 kFLOP/s | 155 MB/s | 128 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 6 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-23** &middot; checked
2026-10-27 00:36 UTC &middot; currently
not all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-27 00:06 UTC | pass | `8986530` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-27 00:33 UTC | pass | `8986530` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-27 00:08 UTC | pass | `8986530` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-27 00:08 UTC | pass | `8986530` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-27 00:35 UTC | pass | `8986530` | [/hipfile-fio/](/hipfile-fio/) |
| MI355X node | 2026-10-24 17:02 UTC | pass | ``unknown`` | [/mi355x/](/mi355x/) |
