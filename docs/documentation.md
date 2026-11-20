---
title: Documentation
---

<div class="doc-grid">
  <div class="doc-section">
    <span class="eyebrow">Performance</span>
    <h2>What is retained</h2>
    <p>
      Each benchmark job uploads its latest number as badge JSON. Those
      snapshots accumulate in a history file on <code>gh-pages</code> so the
      performance page shows trends rather than only the latest reading.
    </p>
  </div>
  <div class="doc-section">
    <span class="eyebrow">Publishing</span>
    <h2>How it fits together</h2>
    <p>
      The Pages publisher fetches each benchmark's latest artifact by name, so
      a benchmark that failed today keeps its last good number on the site.
    </p>
  </div>
</div>

## Site sections

| Section | Purpose |
| --- | --- |
| [Home](index.md) | Landing page with badges, site summary, and entry points. |
| [Documentation](documentation.md) | High-level explanation of the published site and its moving parts. |
| [Fleet XML reference](fleet-xml.md) | Every element of the settings format, generated from the parser's tables, and the two schemas that go with it. |
| [Publishing flow](publishing.md) | How workflow artifacts become the published site and how trend history is retained. |
| [Performance trends](perf/index.md) | Generated benchmark history, latest badges, and recent-run tables. |

## Tracked benchmarks

All five are measured by `qemu-minimal-report-for-vm-rocm-golden.yml` against
one stack, described by `qemu/stacks/rocm-golden.xml`: two guests from the
published rocm-golden image, each with a GPU, an NVMe, an RDMA NIC and an SDXI
device. The lane runs that file unmodified, so the numbers come from the QEMU
it pins: the `sbates-fork` build since 2026-11-18, and the stock build of the
same QEMU release before that, which is a step to expect in the trend lines.
The two hipFile metrics step much further on the same date, by about seventy
times: that file now points the guest's NVMe at its GPU's memory
(`<vm-nvme-vram>`), so hipFile's direct read maps that memory where, most
likely, it went through QEMU a page at a time before (inferred, not
profiled). That is the setting and not faster code. The fio metric is fio
3.43, the first release with the `libhipfile` engine, from the first daily
run after that change reached main. It is still built in the guest before
the run, because no published golden image carries it yet; an image baked
after the change has it under `/opt/fio-hipfile` and the build is skipped.
The performance page
lists such changes, with their dates, under "Changes to what is measured".

| Metric | Measured on | Artifact |
| --- | --- | --- |
| rocjitsu GEMM | guest 1 | `perf-rocjitsu` |
| rocjitsu hipFile | guest 1 | `perf-rocjitsu` |
| hipFile fio | guest 2 | `perf-hipfile-fio` |
| Ernic P4 FP8 bandwidth | guest 1 | `perf-ernic` |
| SDXI copy bandwidth | guest 1 | `perf-helios` |

## Notes

- The site is built from `docs/` using MkDocs Material and pushed as pre-built
  HTML to the `gh-pages` branch.
- No guest inspection report is published. The benchmark jobs upload badge
  JSON only.
- Performance history is stored only on `gh-pages`; it is not committed on
  `main`.
