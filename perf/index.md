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

Generated from `e56dcda3` at **2026-10-28 23:19 UTC**.

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
| rocjitsu GEMM | 464 kFLOP/s | 670 kFLOP/s | regressed (-31%) |
| rocjitsu hipFile | 170 MB/s | 165 MB/s | within tolerance (+3%) |
| hipFile fio | 149 MB/s | 137 MB/s | within tolerance (+8%) |

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
| c81997e8 | 2026-10-27 14:10 UTC | 894 kFLOP/s | 148 MB/s | 115 MB/s |
| 0a353f0d | 2026-10-27 16:43 UTC | 894 kFLOP/s | 148 MB/s | 115 MB/s |
| 65dc3a5c | 2026-10-27 21:08 UTC | 894 kFLOP/s | 148 MB/s | 115 MB/s |
| 65dc3a5c | 2026-10-27 21:38 UTC | 491 kFLOP/s | 159 MB/s | 142 MB/s |
| 65dc3a5c | 2026-10-28 15:12 UTC | 837 kFLOP/s | 114 MB/s | 81.3 MB/s |
| 2115cee9 | 2026-10-28 19:50 UTC | 837 kFLOP/s | 114 MB/s | 81.3 MB/s |
| 17f60519 | 2026-10-28 20:23 UTC | 837 kFLOP/s | 114 MB/s | 81.3 MB/s |
| 17f60519 | 2026-10-28 20:50 UTC | 664 kFLOP/s | 247 MB/s | 205 MB/s |
| e56dcda3 | 2026-10-28 22:49 UTC | 664 kFLOP/s | 247 MB/s | 205 MB/s |
| e56dcda3 | 2026-10-28 23:19 UTC | 464 kFLOP/s | 170 MB/s | 149 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 6 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-23** &middot; checked
2026-10-28 23:19 UTC &middot; currently
not all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-28 22:49 UTC | pass | `e56dcda` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-28 23:16 UTC | pass | `e56dcda` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-28 22:52 UTC | pass | `e56dcda` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-28 22:52 UTC | pass | `e56dcda` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-28 23:18 UTC | pass | `e56dcda` | [/hipfile-fio/](/hipfile-fio/) |
| MI355X node | 2026-10-24 17:02 UTC | pass | ``unknown`` | [/mi355x/](/mi355x/) |
