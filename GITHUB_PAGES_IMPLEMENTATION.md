# GitHub Pages Implementation Plan

This document describes the implementation of GitHub Pages for the  repository.

## Summary

The GitHub Pages site is built from artifacts produced by CI report workflows and published automatically via the  workflow. The site resides on the  branch and is served at .

## Architecture

### CI Workflows (on snoc-beelink)
-  → 
-  →  + 
-  → 

### Publish Pages Workflow
- Fetches artifacts by name (not triggering run)
- Assembles site under staging/
- Renders metrics with render-site-perf.py
- Publishes to gh-pages branch with 3-retry logic

### render-site-perf.py
- Reads badge JSON from site/1vm/, site/rocjitsu/, site/ernic/, site/hipfile-fio/
- Maintains history of distinct readings (collapsed consecutive repeats)
- Computes rolling baseline (mean of last 5 distinct readings)
- Outputs: perf/index.md, perf/chart-*.svg, perf/badge-*.json

## Site Structure
site/
├── _config.yml
├── index.md
├── reports.md
├── publishing.md
├── perf/ (metrics, charts, badges, history.jsonl)
├── 1vm/
├── rocjitsu/
├── ernic/
└── hipfile-fio/

## Key Design Points

1. Artifact fetch by name enables stale reports to contribute their last results
2. 95%/80% regression band accounts for GPU benchmark jitter
3. All-green badge separate from history (dedupe hazard)
4. Never cancels concurrent publishes to avoid half-written commits
5. 3 retries on push conflict with 5s, 10s, 15s sleep intervals

## Open Issues

- GEMM benchmark hangs every run (2026-09-26), uploading clean hipfile-fio but failing overall
- Artifact rotation/deletion needed for old reports to save storage

