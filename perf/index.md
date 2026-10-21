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

Generated from `a8b3eb4b` at **2026-10-21 13:37 UTC**.

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
| rocjitsu GEMM | 717 kFLOP/s | 552 kFLOP/s | within tolerance (+30%) |
| rocjitsu hipFile | 251 MB/s | 173 MB/s | within tolerance (+45%) |
| hipFile fio | 231 MB/s | 130 MB/s | within tolerance (+77%) |

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
| 23dc08d2 | 2026-10-21 02:19 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| fa219cd3 | 2026-10-21 03:41 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| 261bde7e | 2026-10-21 04:03 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| d03edd73 | 2026-10-21 04:20 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| 9455bf1e | 2026-10-21 04:51 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| c2c97882 | 2026-10-21 06:26 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| cc93a5bc | 2026-10-21 06:36 UTC | 662 kFLOP/s | 220 MB/s | 219 MB/s |
| cc93a5bc | 2026-10-21 07:07 UTC | 742 kFLOP/s | 124 MB/s | 59 MB/s |
| a8b3eb4b | 2026-10-21 07:23 UTC | 742 kFLOP/s | 124 MB/s | 59 MB/s |
| a8b3eb4b | 2026-10-21 13:37 UTC | 717 kFLOP/s | 251 MB/s | 231 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-21** &middot; checked
2026-10-21 13:37 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-21 07:27 UTC | pass | `a8b3eb4` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-21 13:34 UTC | pass | `a8b3eb4` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-21 06:40 UTC | pass | `cc93a5b` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-21 06:40 UTC | pass | `cc93a5b` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-21 13:36 UTC | pass | `a8b3eb4` | [/hipfile-fio/](/hipfile-fio/) |
