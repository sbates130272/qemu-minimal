---
title: qemu-minimal
---

<div class="hero">
  <span class="kicker">Documentation and performance tracking</span>
  <h1>qemu-minimal Pages</h1>
  <p>
    Documentation for the qemu-minimal compose stacks, and the benchmark
    numbers CI measures against them every day, retained as a trend.
  </p>
  <div class="cta-row">
    <a class="button-link primary" href="perf/">Performance trends</a>
    <a class="button-link secondary" href="documentation/">Documentation</a>
  </div>
  <div class="badge-strip">
    <div class="metric-card">
      <div class="eyebrow">Tracked performance</div>
      <p><img alt="rocjitsu GEMM" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json" /></p>
      <p><img alt="rocjitsu hipFile" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json" /></p>
      <p><img alt="hipFile fio" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json" /></p>
      <p><img alt="Ernic P4 FP8 BW" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-ernic-p4-bw.json" /></p>
      <p><img alt="SDXI copy BW" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-sdxi-bw.json" /></p>
      <p>Green is within 5% of the mean of the last five distinct readings, amber within 20%, red below that.</p>
    </div>
    <div class="metric-card">
      <div class="eyebrow">Publishing model</div>
      <p>The site is assembled on <code>gh-pages</code> from repository-owned documentation plus the badge JSON the benchmark jobs upload.</p>
    </div>
  </div>
</div>

<div class="card-grid">
  <div class="panel">
    <span class="eyebrow">Performance</span>
    <h2>Trend the benchmarks</h2>
    <p>
      Every benchmark job uploads its latest numbers; the publisher appends
      them to a retained history on <code>gh-pages</code> and regenerates the
      badges and the trend page.
    </p>
    <p><a href="perf/">Performance trends →</a></p>
  </div>
  <div class="panel">
    <span class="eyebrow">Docs</span>
    <h2>How the site works</h2>
    <p>
      What is measured, where the numbers come from, and how they reach the
      published site.
    </p>
    <p><a href="documentation/">Documentation overview →</a></p>
  </div>
</div>

<div class="callout">
  <strong>Source repository:</strong>
  <a href="https://github.com/sbates130272/qemu-minimal">sbates130272/qemu-minimal</a>
</div>
