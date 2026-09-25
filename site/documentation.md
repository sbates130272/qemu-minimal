---
layout: page
title: Documentation
permalink: /documentation/
---

<div class="doc-grid">
  <div class="doc-section">
    <span class="eyebrow">Overview</span>
    <h2>Why this site exists</h2>
    <p>
      qemu-minimal publishes more than raw VM reports: it now carries the structure
      around them, including an explanation of the lanes, how the site is assembled,
      and where the retained benchmark history comes from.
    </p>
  </div>
  <div class="doc-section">
    <span class="eyebrow">Reports</span>
    <h2>What is live</h2>
    <p>
      The report lanes publish single-VM, rocjitsu, rocm-ernic, and hipFile fio guest
      inspection pages. Each page is regenerated from CI artifacts and linked from
      the report index.
    </p>
  </div>
  <div class="doc-section">
    <span class="eyebrow">Performance</span>
    <h2>What is retained</h2>
    <p>
      The benchmark-facing lanes also expose numeric badge JSON. Those snapshots are
      folded into a history file on <code>gh-pages</code> so the performance page can
      show trends instead of only the latest report.
    </p>
  </div>
</div>

## Site sections

| Section | Purpose |
| --- | --- |
| [Home]({{ '/' | relative_url }}) | Landing page with badges, site summary, and entry points. |
| [Documentation]({{ '/documentation/' | relative_url }}) | High-level explanation of the published site and its moving parts. |
| [Reports]({{ '/reports/' | relative_url }}) | Directory of the live CI-generated report pages. |
| [Publishing flow]({{ '/publishing/' | relative_url }}) | How workflow artifacts become the published site and how trend history is retained. |
| [Performance trends]({{ '/perf/' | relative_url }}) | Generated benchmark history, latest badges, and recent-run tables. |

## Report-producing lanes

| Workflow | Published path | Notes |
| --- | --- | --- |
| `report-for-vm-basic` | `/1vm/` | General single-VM guest inspection. |
| `report-for-vm-rocjitsu` | `/rocjitsu/` | rocjitsu VM report with GEMM and hipFile benchmark output. |
| `report-for-vm-ernic` | `/ernic/` and `/ernic/vm2/` | ernic stack reports for VM 1 and VM 2. |
| `report-for-hipfile-fio` | `/hipfile-fio/` | fio `libhipfile` benchmark report. |

## Notes

- The site keeps using GitHub Pages branch builds, so Jekyll renders the Markdown.
- Live report artifacts remain the source of truth for guest state.
- Performance history is stored only on <code>gh-pages</code>; it is not committed on
  <code>main</code>.
