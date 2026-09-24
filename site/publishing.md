---
layout: page
title: Publishing flow
permalink: /publishing/
---

## Site assembly

The published site is built from two sources:

1. Repository-owned Pages content under <code>site/</code>.
2. The latest unexpired report artifacts uploaded by the report workflows.

The <code>qemu-minimal-publish-pages.yml</code> workflow copies the repository-owned site into a
staging tree, fetches each report artifact by name, and then syncs the staged
content onto the <code>gh-pages</code> branch.

## Performance tracking flow

The trend flow mirrors the pattern used on ROCm/rocm-ernic:

- The rocjitsu and hipFile fio report artifacts already expose metric badge JSON.
- The publisher reads those badge files from staging.
- A generated history file on <code>gh-pages</code> is used as the durable record.
- The publisher appends a new record when the latest metrics change.
- The publisher regenerates the performance page and the latest badge JSON files.

That keeps <code>main</code> free of CI-written history while still letting the site show
retained trends over time.

## Adding another report lane

1. Upload the lane output under its own stable artifact name.
2. Teach <code>qemu-minimal-publish-pages.yml</code> to fetch that artifact into a dedicated path.
3. Add the lane's workflow <code>name:</code> &mdash; not its filename &mdash; to the
   <code>workflow_run</code> trigger list.
4. Add the lane to the report index and, if it exposes numeric badges, to the
   performance renderer.

## Failure model

- Missing artifacts leave the last published lane output in place.
- Retried Pages publishes rebuild from the latest <code>gh-pages</code> tip before pushing.
- Performance history is regenerated from the seeded <code>gh-pages</code> record, so a
  re-run does not truncate the trend page to one point.
