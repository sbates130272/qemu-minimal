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

Generated from `84fa06d1` at **2026-09-26 20:21 UTC**.

![rocjitsu GEMM](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json)
![rocjitsu hipFile](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json)
![hipFile fio](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json)

| Metric | Latest value | Interpretation |
| --- | --- | --- |
| rocjitsu GEMM | 823 kFLOP/s | Higher is better |
| rocjitsu hipFile | 123 MB/s | Higher is better |
| hipFile fio | 10 MB/s | Higher is better |

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
| a933df21 | 2026-09-23 04:05 UTC | 484 kFLOP/s | 159 MB/s | 147 MB/s |
| d939b601 | 2026-09-23 20:13 UTC | 484 kFLOP/s | 159 MB/s | 147 MB/s |
| e73a1e63 | 2026-09-24 23:18 UTC | 484 kFLOP/s | 159 MB/s | 147 MB/s |
| e73a1e63 | 2026-09-25 13:26 UTC | 823 kFLOP/s | 123 MB/s | 81.2 MB/s |
| 6153a06e | 2026-09-25 17:51 UTC | 823 kFLOP/s | 123 MB/s | 81.2 MB/s |
| 603b8c81 | 2026-09-25 20:33 UTC | 823 kFLOP/s | 123 MB/s | 81.2 MB/s |
| fa6d8556 | 2026-09-25 23:57 UTC | 823 kFLOP/s | 123 MB/s | 11.2 MB/s |
| fa6d8556 | 2026-09-26 12:49 UTC | 823 kFLOP/s | 123 MB/s | 10.6 MB/s |
| 9e3ccdf2 | 2026-09-26 19:22 UTC | 823 kFLOP/s | 123 MB/s | 10 MB/s |
| 84fa06d1 | 2026-09-26 20:21 UTC | 823 kFLOP/s | 123 MB/s | 10 MB/s |

## Current report freshness

| Report | Generated | Commit | Page |
| --- | --- | --- | --- |
| Single VM | 2026-09-26 20:21 UTC | `84fa06d` | [/1vm/](/1vm/) |
| rocjitsu | 2026-09-24 23:44 UTC | `e73a1e6` | [/rocjitsu/](/rocjitsu/) |
| ernic VM 1 | 2026-09-26 19:25 UTC | `9e3ccdf` | [/ernic/](/ernic/) |
| ernic VM 2 | 2026-09-26 19:25 UTC | `9e3ccdf` | [/ernic/vm2/](/ernic/vm2/) |
| hipFile fio | 2026-09-26 13:14 UTC | `fa6d855` | [/hipfile-fio/](/hipfile-fio/) |
