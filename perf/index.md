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

Generated from `e7c0f839` at **2026-10-30 00:30 UTC**.

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
| Rocjitsu GEMM | 484 kFLOP/s | 566 kFLOP/s | watch (-14%) |
| Rocjitsu HipFile | 195 MB/s | 216 MB/s | watch (-10%) |
| HipFile Fio | 137 MB/s | 177 MB/s | regressed (-23%) |
| Ernic P4 FP8 BW | n/a | - | no data |

## Trend charts

### Rocjitsu GEMM

![Rocjitsu GEMM trend](chart-gemm.svg)
### Rocjitsu HipFile

![Rocjitsu HipFile trend](chart-hipfile.svg)
### HipFile Fio

![HipFile Fio trend](chart-hipfile-fio.svg)

## Recent runs

| Commit | Generated | GEMM | hipFile | hipFile fio |
| --- | --- | --- | --- | --- |
| 9ed9e68d | 2026-10-29 15:10 UTC | 596 kFLOP/s | 246 MB/s | 188 MB/s |
| 9e984d20 | 2026-10-29 16:44 UTC | 596 kFLOP/s | 246 MB/s | 188 MB/s |
| 9e984d20 | 2026-10-29 17:20 UTC | 470 kFLOP/s | 165 MB/s | 137 MB/s |
| 656952bd | 2026-10-29 17:34 UTC | 470 kFLOP/s | 165 MB/s | 137 MB/s |
| 333246f4 | 2026-10-29 18:27 UTC | 470 kFLOP/s | 165 MB/s | 137 MB/s |
| 15a9b834 | 2026-10-29 19:49 UTC | 470 kFLOP/s | 165 MB/s | 137 MB/s |
| ae551b3b | 2026-10-29 20:53 UTC | 470 kFLOP/s | 165 MB/s | 137 MB/s |
| ae551b3b | 2026-10-29 21:18 UTC | 634 kFLOP/s | 250 MB/s | 205 MB/s |
| e7c0f839 | 2026-10-30 00:02 UTC | 634 kFLOP/s | 250 MB/s | 205 MB/s |
| e7c0f839 | 2026-10-30 00:30 UTC | 484 kFLOP/s | 195 MB/s | 137 MB/s |

## Current report freshness

![all reports green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json)

A report counts as green when it carries a `pass` status stamp *and* was
generated within 36 hours of the publish. Both are needed: a
lane that fails uploads no artifact at all, so its previous report stays on the
branch and would otherwise keep reading `pass` indefinitely. The badge above
shows the last date on which all 6 reports were green at once —
amber once that date is no longer today.

Last all-green: **2026-10-30** &middot; checked
2026-10-30 00:30 UTC &middot; currently
all green.

| Report | Generated | Status | Commit | Page |
| --- | --- | --- | --- | --- |
| Single VM | 2026-10-30 00:07 UTC | pass | `e7c0f83` | [/1vm/](/1vm/) |
| rocjitsu | 2026-10-30 00:27 UTC | pass | `e7c0f83` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-10-29 21:00 UTC | pass | `ae551b3` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-10-29 21:01 UTC | pass | `ae551b3` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-10-30 00:30 UTC | pass | `e7c0f83` | [/hipfile-fio/](/hipfile-fio/) |
| MI355X node | 2026-10-29 21:01 UTC | pass | ``unknown`` | [/mi355x/](/mi355x/) |
