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

The trend flow mirrors the pattern used on ROCm/rocm-ernic:

- The rocjitsu and hipFile fio report artifacts already expose metric badge JSON.
- The publisher reads those badge files from the fetched artifact tree.
- A generated history file on `gh-pages` is used as the durable record.
- The publisher appends a new record when the latest metrics change.
- The publisher regenerates the performance page and the latest badge JSON files.

That keeps `main` free of CI-written history while still letting the site show
retained trends over time.

## Adding another report lane

1. Upload the lane output under its own stable artifact name.
2. Teach `qemu-minimal-publish-pages.yml` to fetch that artifact into
   `docs/<dest>/` before `mkdocs build` runs.
3. Add the lane's workflow `name:` — not its filename — to the
   `workflow_run` trigger list.
4. Add the lane to the report index and, if it exposes numeric badges, to the
   performance renderer.
5. Add a status badge to the README's report row.

Miss step 3 and the lane publishes nothing until some other lane finishes.
Miss step 5 and the lane is invisible on the report page.

## Failure model

- Missing artifacts leave the last published lane output in place.
- Retried Pages publishes rebuild from the latest `gh-pages` tip before pushing.
- Performance history is regenerated from the seeded `gh-pages` record, so a
  re-run does not truncate the trend page to one point.
