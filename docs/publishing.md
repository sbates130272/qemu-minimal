---
title: Publishing flow
---

## Site assembly

The published site is built from two sources:

1. Repository-owned Pages content under `docs/`.
2. The latest unexpired report artifacts uploaded by the report workflows.

The `qemu-minimal-publish-pages.yml` workflow fetches each report artifact by
name into the `docs/` tree, runs `mkdocs build` to produce `site/`, and pushes
the built HTML to the `gh-pages` branch.

## Performance tracking flow

The rocjitsu and hipFile fio artifacts expose metric badge JSON. The publisher
reads those badges, appends a new record when metrics change, and regenerates
the performance page and badge JSON from a history file on `gh-pages`. That
keeps `main` free of CI-written history while retaining trends over time.

## Adding another report lane

1. Upload lane output under its own stable artifact name.
2. Fetch that artifact into `docs/<dest>/` in `qemu-minimal-publish-pages.yml`.
3. Add the lane's workflow `name:` to the `workflow_run` trigger list — omit
   this and the lane publishes nothing until another lane finishes.
4. Add the lane to the report index and, if it exposes numeric badges, to the
   performance renderer.
5. Add a status badge to the README — omit this and the lane is invisible on the
   report page.

## Failure model

- Missing artifacts leave the last published lane output in place.
- Retried Pages publishes rebuild from the latest `gh-pages` tip before pushing.
- Performance history is regenerated from the seeded `gh-pages` record, so a
  re-run does not truncate the trend page to one point.
