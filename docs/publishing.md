---
title: Publishing flow
---

## Site assembly

The published site is built from two sources:

1. Repository-owned Pages content under `docs/`.
2. The latest unexpired `perf-*` artifacts uploaded by the benchmark jobs.

The `qemu-minimal-publish-pages.yml` workflow fetches each artifact by name
into a scratch directory, regenerates `docs/perf/` from it, runs `mkdocs build`
to produce `site/`, and pushes the built HTML to the `gh-pages` branch. The
artifacts themselves are inputs only and are never published.

## Performance tracking flow

Each artifact holds the badge JSON for one benchmark. The publisher reads those
badges, appends a new record when metrics change, and regenerates the
performance page and badge JSON from a history file on `gh-pages`. That keeps
`main` free of CI-written history while retaining trends over time.

## Adding another benchmark

1. Have the job upload its `badge-*.json` under its own stable `perf-<name>`
   artifact name.
2. Fetch that artifact in `qemu-minimal-publish-pages.yml`.
3. If the job lives in a new workflow, add its `name:` to the `workflow_run`
   trigger list — omit this and the number publishes only when another
   workflow finishes.
4. Add the metric to `METRICS` in `scripts/render-site-perf.py`.
5. Add the endpoint badge to the README.

## Failure model

- A missing or expired artifact leaves that metric's last recorded value in
  the history; its badge reads `n/a` until a new number arrives.
- Retried Pages publishes rebuild from the latest `gh-pages` tip before pushing.
- Performance history is regenerated from the seeded `gh-pages` record, so a
  re-run does not truncate the trend page to one point.
