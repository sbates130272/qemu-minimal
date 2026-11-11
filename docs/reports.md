---
title: Reports
---

<div class="report-grid">
  <div class="report-card">
    <span class="eyebrow">Single guest</span>
    <h2><a href="../1vm/">Single VM report</a></h2>
    <p>One <code>gen-vm</code> guest with NVMe attached, built and inspected on a GitHub-hosted runner.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">GPU guest</span>
    <h2><a href="../rocjitsu/">rocjitsu report</a></h2>
    <p>rocjitsu guest inspection plus the current GEMM and hipFile benchmark outputs.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">Two guests</span>
    <h2><a href="../ernic/">ernic report</a></h2>
    <p>The ernic stack, with VM 1 at the top-level page and VM 2 nested underneath.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">Two guests</span>
    <h2><a href="../lmcache/">lmcache report</a></h2>
    <p>LMCache L1 P2P between two guests: whether the KV bytes crossed the ernic as RDMA rather than falling back to TCP.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">Two guests</span>
    <h2><a href="../quickstart/">quickstart report</a></h2>
    <p>The README's Quick Start, run daily: <code>qemu-tool quick-start</code> brings up two guests from the published image, each with a GPU, an NVMe and an RDMA NIC, plus Prometheus and Grafana, with a GPU test and an inter-guest RDMA test.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">Node shape</span>
    <h2><a href="../mi355x/">MI355X node report</a></h2>
    <p>An MI355X-shaped PCIe tree: 8 GPUs, 8 RDMA NICs and 8 switches in one guest, one NIC and one GPU below each switch, with the tree the guest actually enumerates.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">fio lane</span>
    <h2><a href="../hipfile-fio/">hipFile fio report</a></h2>
    <p>The fio <code>libhipfile</code> benchmark lane, published independently from the rocjitsu report.</p>
  </div>
  <div class="report-card">
    <span class="eyebrow">DKMS bake</span>
    <h2><a href="../ernic-p4/">Ernic SRQ+P4 bake report</a></h2>
    <p>Boots the published ernic-rocjitsu qcow2 and bakes the SRQ+P4 ionic DKMS stack in; verifies <code>ernic_p4.ko</code> is present and loadable.</p>
  </div>
</div>

## Lane summary

| Lane | Schedule / trigger | Output |
| --- | --- | --- |
| `report-for-vm-basic` | Push to `main`, daily 01:00 MST, manual dispatch | `/1vm/` |
| `report-for-vm-rocjitsu` | Path-filtered push to `main`, daily 01:00 MST, manual dispatch | `/rocjitsu/` |
| `report-for-vm-ernic` | Path-filtered push to `main`, daily 01:00 MST, manual dispatch | `/ernic/` and `/ernic/vm2/` |
| `report-for-lmcache` | Path-filtered push to `main`, weekly Sunday, manual dispatch | `/lmcache/` |
| `report-for-quickstart` | Path-filtered push to `main`, daily 04:17 MST, manual dispatch | `/quickstart/` |
| `report-for-mi355x` | Path-filtered push to `main`, daily 06:41 MST, manual dispatch | `/mi355x/` |
| `report-for-hipfile-fio` | Path-filtered push to `main`, daily 01:00 MST, manual dispatch | `/hipfile-fio/` |
| `vm-ernic-srq-p4` | Weekly Monday 11:23 UTC, path-filtered push to `main`, manual dispatch | `/ernic-p4/` |

## Publishing model

Each lane uploads a named artifact; the Pages publisher assembles the latest
artifact from every lane so one run cannot overwrite another's content. The
site stays complete even when a lane has not run for days.
