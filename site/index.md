---
layout: page
title: qemu-minimal
permalink: /
---

<div class="hero">
  <span class="kicker">Documentation and live CI output</span>
  <h1>qemu-minimal Pages</h1>
  <p>
    A documentation-first landing page for the published VM reports, compose
    stacks, and continuously refreshed benchmark snapshots that qemu-minimal
    exposes through GitHub Pages.
  </p>
  <div class="cta-row">
    <a class="button-link primary" href="{{ '/documentation/' | relative_url }}">Read the docs</a>
    <a class="button-link secondary" href="{{ '/reports/' | relative_url }}">Browse report lanes</a>
    <a class="button-link secondary" href="{{ '/perf/' | relative_url }}">Open performance trends</a>
  </div>
  <div class="badge-strip">
    <div class="metric-card">
      <div class="eyebrow">Tracked performance</div>
      <p><img alt="rocjitsu GEMM" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json" /></p>
      <p><img alt="rocjitsu hipFile" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json" /></p>
      <p><img alt="hipFile fio" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json" /></p>
    </div>
    <div class="metric-card">
      <div class="eyebrow">Published reports</div>
      <p>Single VM, rocjitsu, rocm-ernic, and hipFile fio outputs stay live on the site and are refreshed by CI.</p>
    </div>
    <div class="metric-card">
      <div class="eyebrow">Publishing model</div>
      <p>The site is assembled on <code>gh-pages</code> from named workflow artifacts plus repository-owned documentation content.</p>
    </div>
  </div>
</div>

<div class="card-grid">
  <div class="panel">
    <span class="eyebrow">Docs</span>
    <h2>What this site covers</h2>
    <p>
      The Pages site now mirrors the role of the ROCm/rocm-ernic site more closely:
      docs at the front, live measurements beside them, and report artifacts linked
      from one place.
    </p>
    <p><a href="{{ '/documentation/' | relative_url }}">Documentation overview →</a></p>
  </div>
  <div class="panel">
    <span class="eyebrow">Performance</span>
    <h2>Trend the benchmark lanes</h2>
    <p>
      The rocjitsu and hipFile report artifacts now feed a retained history on
      <code>gh-pages</code>, which produces the latest badges and a trend page for
      the site.
    </p>
    <p><a href="{{ '/perf/' | relative_url }}">Performance trends →</a></p>
  </div>
  <div class="panel">
    <span class="eyebrow">Reports</span>
    <h2>Inspect the current guests</h2>
    <p>
      Every report still comes from a real CI boot, so the published output reflects
      the guest that CI actually produced rather than static documentation.
    </p>
    <p><a href="{{ '/reports/' | relative_url }}">Report index →</a></p>
  </div>
</div>

<div class="callout">
  <strong>Source repository:</strong>
  <a href="https://github.com/sbates130272/qemu-minimal">sbates130272/qemu-minimal</a>
</div>
