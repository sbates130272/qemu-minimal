---
title: Documentation
---

<div class="doc-grid">
  <div class="doc-section">
    <span class="eyebrow">Reports</span>
    <h2>What is live</h2>
    <p>
      Seven CI lanes publish guest inspection pages: single-VM, rocjitsu, ernic,
      lmcache, quickstart, MI355X, and hipFile fio. Each page is regenerated from the
      latest artifact and linked from the report index.
    </p>
  </div>
  <div class="doc-section">
    <span class="eyebrow">Performance</span>
    <h2>What is retained</h2>
    <p>
      Benchmark lanes expose numeric badge JSON. Those snapshots accumulate in a
      history file on <code>gh-pages</code> so the performance page shows trends
      rather than only the latest reading.
    </p>
  </div>
  <div class="doc-section">
    <span class="eyebrow">Publishing</span>
    <h2>How it fits together</h2>
    <p>
      The Pages publisher fetches each lane's latest artifact by name and assembles
      them into one site. No lane can overwrite another's content.
    </p>
  </div>
</div>

## Site sections

| Section | Purpose |
| --- | --- |
| [Home](index.md) | Landing page with badges, site summary, and entry points. |
| [Documentation](documentation.md) | High-level explanation of the published site and its moving parts. |
| [Reports](reports.md) | Directory of the live CI-generated report pages. |
| [Publishing flow](publishing.md) | How workflow artifacts become the published site and how trend history is retained. |
| [Performance trends](perf/index.md) | Generated benchmark history, latest badges, and recent-run tables. |

## Report-producing lanes

| Workflow | Published path | Notes |
| --- | --- | --- |
| `report-for-vm-basic` | `/1vm/` | General single-VM guest inspection. |
| `report-for-vm-rocjitsu` | `/rocjitsu/` | rocjitsu VM report with GEMM and hipFile benchmark output. |
| `report-for-vm-ernic` | `/ernic/` and `/ernic/vm2/` | ernic stack reports for VM 1 and VM 2. |
| `report-for-hipfile-fio` | `/hipfile-fio/` | fio `libhipfile` benchmark report. |
| `report-for-lmcache` | `/lmcache/` | LMCache L1 P2P between two guests, with the ernic RDMA byte counters. |
| `report-for-quickstart` | `/quickstart/` | The README's Quick Start, run end to end. |
| `report-for-mi355x` | `/mi355x/` | An MI355X-shaped PCIe tree, as the guest enumerates it. |

## Notes

- The site is built from `docs/` using MkDocs Material and pushed as pre-built
  HTML to the `gh-pages` branch.
- Live report artifacts remain the source of truth for guest state.
- Performance history is stored only on `gh-pages`; it is not committed on
  `main`.
