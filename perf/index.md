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

Generated from `71c00464` at **2026-10-21 00:26 UTC**.

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
| rocjitsu GEMM | 496 kFLOP/s | 500 kFLOP/s | within tolerance (-1%) |
| rocjitsu hipFile | 159 MB/s | 164 MB/s | within tolerance (-3%) |
| hipFile fio | 138 MB/s | 131 MB/s | within tolerance (+5%) |

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
| b4eb5528 | 2026-10-20 13:34 UTC | 504 kFLOP/s | 199 MB/s | 176 MB/s |
| 48f9eef9 | 2026-10-20 15:59 UTC | 504 kFLOP/s | 199 MB/s | 176 MB/s |
| 48f9eef9 | 2026-10-20 16:34 UTC | 750 kFLOP/s | 109 MB/s | 88.5 MB/s |
| 2f489fa1 | 2026-10-20 18:53 UTC | 750 kFLOP/s | 109 MB/s | 88.5 MB/s |
| ef2d5c17 | 2026-10-20 18:58 UTC | 750 kFLOP/s | 109 MB/s | 88.5 MB/s |
| ef2d5c17 | 2026-10-20 19:18 UTC | 488 kFLOP/s | 197 MB/s | 148 MB/s |
| 1da22630 | 2026-10-20 22:33 UTC | 488 kFLOP/s | 197 MB/s | 148 MB/s |
| 1da22630 | 2026-10-20 23:00 UTC | 372 kFLOP/s | 164 MB/s | 148 MB/s |
| 71c00464 | 2026-10-20 23:58 UTC | 372 kFLOP/s | 164 MB/s | 148 MB/s |
| 71c00464 | 2026-10-21 00:26 UTC | 496 kFLOP/s | 159 MB/s | 138 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 5 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-21** &middot; checked
2026-10-21 00:26 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-20 23:58 UTC | pass | `71c0046` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-21 00:23 UTC | pass | `71c0046` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-21 00:01 UTC | pass | `71c0046` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-21 00:01 UTC | pass | `71c0046` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-21 00:26 UTC | pass | `71c0046` | [/hipfile-fio/](/hipfile-fio/) |
