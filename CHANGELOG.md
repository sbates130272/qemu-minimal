# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed

- Workflow triggers now follow from the fact that `main` is protected and every
  commit arrives as a PR merge. No workflow runs on both `pull_request` and
  `push: main` any more — `shell-check`, `spell-check`,
  `rocm-gemm-benchmark-compile`, `build-packages` and `ansible-setup-test` each used to run twice per change with
  byte-identical path filters. Those five lost their `push` trigger *and* their
  `paths` filter, because they are intended to become required checks and a
  path-filtered required check never reports on a PR outside its filter,
  leaving that PR blocked on a status that never arrives. The trade is that a
  docs-only PR now also pays `build-packages` (~7m) and `ansible-setup-test`
  (~7m). The four report lanes converged on one shape — `push: main` plus a
  daily cron plus manual — which gives `vm-report-ernic` a push trigger it
  never had, and their crons moved from weekly to daily.
  `smoke-test-rocm-ernic` moved from Monday 04:00 to Monday 07:07.

- The four report lanes are all scheduled at **01:00 MST (`0 8 * * *`)**
  rather than staggered across 02:47–06:29 UTC. GitHub cron is UTC-only and
  MST is a fixed `-07:00` with no DST, so one expression holds year round;
  under Mountain *Daylight* Time it lands at 02:00 local. Each lane gets its
  own hosted runner, so the nightly window contracts from ~3h45m of wall clock
  to roughly the ~57m of the slowest lane. `publish-pages` consequently sees
  four `workflow_run` completions inside an hour rather than spread across the
  night; it sets `cancel-in-progress: false`, so they queue and none is
  dropped, but the Pages branch is rewritten four times in succession and the
  site only settles after the last drains. Scheduled runs at `:00` are also the
  most likely to start late, `:00` being the most contended minute on the
  platform.

- `ansible-playbook-test-rocjitsu` and `ansible-playbook-test-ernic-rocjitsu`
  gate pull requests again. The rocjitsu lane had become dispatch-only while
  still being documented as the PR gate for the emulated GPU, which is how the
  RVS regression in #154 reached `main` and was found an hour later by
  `vm-report-rocjitsu` instead of before merge. Neither is a required check.

- Every file in `.github/workflows/` is now named for the workflow it
  contains, slugified: `qemu-minimal - Shell Check` lives in
  `qemu-minimal-shell-check.yml`. Most were a missing `qemu-minimal-` prefix;
  eight more were renamed a second time on 2026-09-23 once the display names
  settled, so that the rule now has no exceptions: lowercase the workflow name,
  turn each ` - ` and each space into `-`, append `.yml`. The four playbook
  tests gained the `vm-` their names carry (`...-ansible-playbook-test-rocm.yml`
  → `...-ansible-playbook-test-vm-rocm.yml`), and the four report lanes moved
  from `vm-report*` to `report-for-*`. Five had drifted further still and were
  renamed harder — `shell-check.yml` →
  `qemu-minimal-shell-check.yml`, `bench-compile.yml` →
  `qemu-minimal-rocm-gemm-benchmark-compile.yml`, `qemu-tool-dry-run.yml` →
  `qemu-minimal-dry-run-qemu-tool.yml`, `qemu-tool-smoke-test.yml` →
  `qemu-minimal-smoke-test-qemu-tool.yml`, and `smoke-test-rocm-two-vms.yml` →
  `qemu-minimal-smoke-test-rocm-ernic.yml`. Nothing about what any workflow
  *does* changed. The self-referential entries in each `paths:` filter, the
  `workflow_run` list in `publish-pages`, and the README badges were all
  updated to match. Concurrency groups and uploaded
  artifact names were deliberately *not* renamed: `publish-pages` fetches each
  report by artifact name, so those are load-bearing across workflows.
  See **Upgrading**.

- Workflow `name:` fields are now title-cased prose rather than a second copy
  of the filename: `qemu-minimal - Shell Check`, `qemu-minimal - Spell Check`,
  `qemu-minimal - ROCm GEMM Benchmark Compile`, `qemu-minimal - Ansible
  Playbook Test - vm-rocjitsu`, `qemu-minimal - Smoke Test - rocm-ernic`. The four report lanes
  are named for the playbook they exercise — `qemu-minimal - Report for
  vm-basic`, `- Report for vm-rocjitsu`, `- Report for vm-ernic` — except
  `Report for hipfile-fio`, which runs `vm-rocjitsu.yml` but reports on the
  hipFile/fio storage benchmark. `publish-pages` selects its upstream lanes by
  workflow *name* in its `workflow_run` trigger, so all four were updated there
  in the same commit; a mismatch there fails silently and freezes the site.
  `qemu-minimal - Package` became `qemu-minimal - Build Packages` (and
  `qemu-minimal-build-packages.yml`), since "Package" named a noun where every
  other lane names an action.

- Job `name:` fields follow the workflow names. They were kebab-case slugs that
  had drifted from their lanes — `shell-check.yml` declared `shellcheck`,
  `rocm-gemm-benchmark-compile.yml` declared `bench-compile`. Two rules now
  cover all 28 jobs: a single-job workflow names its job after the workflow
  minus the `qemu-minimal - ` prefix (`Shell Check`, `Report for hipfile-fio`,
  `Ansible Playbook Test - vm-rocm`), and a multi-job workflow title-cases each
  job's own identity (`Build Packages` holds `Build Deb`, `Install Smoke Test
  (<kvm-group>)`, `Build Wheel`, `Wheel Smoke Test`). Job **ids** are
  unchanged, so every `needs:` edge still resolves. Branch protection matches
  job names unqualified by workflow, which is why the single-job rule keeps the
  profile suffix — four lanes all naming a job `Report` would collapse into one
  required context. See **Upgrading**.

- `vm-report-two-vms` is now `vm-report-ernic`, and its report moves from
  `/two-vm/` to `/ernic/` and `/two-vm/vm2/` to `/ernic/vm2/`. The old name
  described the *shape* of the lane — two guests — while every other report
  lane is named for the thing it exercises, and what this one actually proves
  is the ernic RDMA NIC across a pair of guests. The workflow file, workflow
  name, job id, concurrency group, guest names (`ernic-report-1` and
  `-2`), cloud-image cache key and uploaded artifact (`vm-report-ernic`) all
  follow. The compose stack keeps its `vfio-user-ernic-2vm` name, since
  `smoke-test-rocm-ernic` shares it. See **Upgrading**.

### Removed

- The `slash-command-dispatch` workflow, and with it the `/run-ci-full` comment
  command. It existed so a reviewer could ask for the four
  `ansible-playbook-test-*` lanes on a pull request back when those lanes had
  no `pull_request` trigger at all. All four are now PR-triggered within their
  path filters and all four carry `workflow_dispatch`, so the remaining case —
  forcing a run on a PR whose changes fell outside the filter — is served by
  the Actions "Run workflow" button. Two things do change for reviewers: that
  button is per-workflow, so it is four selections rather than one comment, and
  it needs write access to the repository, where the comment path accepted any
  commenter who passed the workflow's own permission check. See **Upgrading**.

### Upgrading

- `/run-ci-full` comments are no longer answered by anything. A comment
  containing it is now inert rather than an error, so the only signal that it
  stopped working is the absence of the acknowledgement reply. Use the Actions
  "Run workflow" button on each `ansible-playbook-test-vm-*` workflow instead.

- `main` has no required status checks configured: the `contexts` list under
  `required_status_checks` is empty, which is why the post-merge `push`
  triggers removed here were the only guaranteed validation. Nothing in this release changes branch
  protection. Once the renamed workflows have landed and each gate has reported
  once, add `Shell Check`, `Spell Check`, `ROCm GEMM Benchmark Compile`,
  `Ansible Syntax Check`, `Generate VM Ansible Setup`, `Build Deb`,
  `Build Wheel` and `Wheel Smoke Test` as required contexts. Adding them
  *before* the first run leaves PRs waiting on contexts GitHub has never seen —
  and because the job renames above mean GitHub has seen *none* of these eight
  strings, that sequencing is not optional here.

- Renaming a workflow file orphans its run history: GitHub keys runs on the
  file path, so each renamed workflow starts empty and its README badge reads
  "no status" until it next runs. The old runs are still reachable in the
  Actions tab under the retired filename. Any bookmark of the form
  `/actions/workflows/<old>.yml` needs updating. Branch protection is not
  disturbed by the *file* renames — required checks key on job name — but the
  job renames in this release do change every context string, so any protection
  rule, external status consumer or merge-queue config pinned to the old
  kebab-case names must be updated in step.

- The two-guest ernic report moved from `/two-vm/` to `/ernic/`. Links to
  `https://sbates130272.github.io/qemu-minimal/two-vm/` will keep serving the
  last report published under the old name until that directory is removed
  from `gh-pages` by hand — `publish-pages.yml` scopes its `--delete` to the
  lane directories it knows about, so a retired one is left untouched rather
  than silently erased. `/ernic/` itself stays empty until the renamed lane
  next runs; it is cron- and dispatch-only, so trigger it if you want the new
  path populated sooner.

- A guest built by an older `gen-vm` may come up unreachable after a bump.
  `run-vm` now always presents a MAC derived from `--ssh-port`, and a guest
  whose netplan pins the MAC it saw at *its* first boot will not match it. SSH
  reports `Connection timed out during banner exchange` rather than `refused`,
  because slirp's `hostfwd` still completes the TCP handshake with nothing
  behind it.

  Check what the guest actually pinned, rather than reasoning from versions:
  `grep -A4 ethernets /etc/netplan/50-cloud-init.yaml` in the guest, or on the
  mounted qcow2. If there is a `macaddress:` line, pass that value as `--mac`
  (or `VM_MAC`) and the guest runs as-is; otherwise the guest matches on
  interface name and needs nothing. Rebuilding the guest also fixes it
  permanently. Which MAC got baked in depends on both the commit and whether
  the guest was built with `--mgmt-tap`, so the file in the guest is the only
  reliable answer.

### Added

- `pciutils` in `packages.d/packages-default`, which backs the new PCI
  devices section below. See **Fixed** for why the report lanes now use
  that manifest at all.
- A **PCI devices** section in every VM report, from `lspci -nn`. This
  repository exists to run emulated PCI devices — ernic NICs, rocjitsu GPUs —
  and the reports described the guests without ever listing them.
- A repository-owned Pages site under `site/`, so the published `gh-pages`
  branch is no longer just a generated landing page plus raw report subtrees.
  The new layout is documentation-first, closer to the ROCm/rocm-ernic site:
  it adds a styled home page, report index, publishing-flow documentation, and
  a generated performance-trends page backed by retained badge history on
  `gh-pages`. `publish-pages.yml` now checks out the repository, stages the
  site content, seeds the trend renderer from the existing `perf/history.jsonl`
  record on `gh-pages`, regenerates the badges at `/perf/`, and then syncs the
  static site content and report artifacts together.
- `bench-compile.yml`, which compiles both `scripts/vm-report/*.hip` files on
  every pull request that touches them. Until now the only thing that ever
  compiled them was the rocjitsu report lane, which runs on push to `main` and
  needs a GPU, a VM and two and a half hours — so a benchmark that was not
  valid C++ could sit on `main` indefinitely, and did. This lane installs
  hipcc and the hipFile SDK from the same TheRock `stable` channel the guests
  use — pinned to the `ubuntu2404` path, because the runner is `ubuntu-24.04`
  while the rocjitsu guest is built `--release resolute` and takes
  `ubuntu2604` — and runs the compile only: no GPU, no VM, and it is
  path-filtered to `scripts/vm-report/**` and the workflow itself, so it costs
  nothing on the PRs that do not touch the benchmarks. It compiles with
  `-Wall -Wextra`, which is stricter than the report script's own line, and
  without `-Werror`, so only hard errors fail it.

- `--mac ADDR` (`VM_MAC`) on `run-vm` and `gen-vm`, which sets the management
  NIC MAC instead of deriving it from the SSH port. Images this repo's
  `gen-vm` builds today do not need it — their netplan matches on interface
  name — but images it built before `3969f07` pin `match: macaddress:` to
  whatever MAC they saw at their own first boot, which is not necessarily one
  qemu-tool will pick again; an image built elsewhere may do the same. The NIC
  then comes up unconfigured and only SLIRP's fallback eventually gets an
  address on it, slowly. `--mac` makes qemu-tool present the MAC such an image
  expects, so it can be run as-is rather than rebuilt. See **Upgrading** above
  for how to find out what a given guest pinned. The value is validated as a
  unicast MAC up front, because qemu rejects a malformed one at
  device-creation time, long after `gen-vm` has fetched an image and built a
  seed.

- `publish-pages.yml`, now the only workflow that writes the site. The report
  lanes upload a named artifact and stop; this assembles them onto a
  `gh-pages` branch behind a landing page at `/`, with the single-VM report
  at `/1vm/` and the two-VM pair at `/two-vm/` and `/two-vm/vm2/`. It fetches
  the latest artifact *by name* rather than from the run that triggered it,
  so a lane that has not run for a week still contributes its last report and
  the site is never partial. Each lane's subtree is synced with its own
  scoped `--delete` instead of one delete over the whole site, so a lane
  whose artifact has expired keeps the report it last published, and a
  `perf/` record written by a future perf lane survives without needing to be
  named. A push rejected by a concurrent writer is retried from the new tip.
- PyPI publishing, so `pipx install qemu-tool` needs no checkout and no
  downloaded `.deb`. `release.yml` uploads via Trusted Publishing against a
  `pypi` environment rather than a stored API token, and runs last: a PyPI
  version can never be reused even after a delete, so publishing it before
  the deb has built would burn the version for a release that then has to be
  cut again as the next one. `qemu/pyproject.toml` gains the metadata a
  project page needs — readme, classifiers, keywords and URLs. The readme is
  written inline because the sdist root is `qemu/`, so `../README.md` is
  outside the project and cannot be packaged, and because the repo README
  documents compose stacks, Ansible and libvirt that a `pip install` does not
  install. `package.yml` and `release.yml` both run `twine check --strict`,
  so metadata that PyPI would reject fails on a PR rather than at tag time.
- `scripts/release.sh <version>`, which cuts a release from a clean `main`:
  it stamps `qemu/pyproject.toml`, generates the `qemu/debian/changelog`
  stanza from the `[Unreleased]` section of this file — preserving the
  Added/Changed/Fixed/Removed grouping as dpkg `[ Section ]` markers, since
  flattened into one list a removal reads exactly like an addition —
  retitles that section as the new version, commits signed-off and makes a
  signed tag. `--dry-run` shows the generated stanza and touches nothing. It
  never pushes; it prints the command that does. A failed commit restores
  the tree, which is safe because it refuses to start on a dirty one.
- A `verify-version` job gating `release.yml`. Nothing is built or published
  unless the tag, `qemu/pyproject.toml` and `qemu/debian/changelog` agree on
  the version and `CHANGELOG.md` has a heading for the tag. v1.3.0 shipped
  with no tag at all and every check stayed green, because the tag was the
  only thing that would have disagreed and nothing compared it to anything.
- A self-contained wheel. The compose stacks, both package manifests,
  `env.example` and the man page now ship inside the Python distribution, so
  `pipx install qemu-tool` is a working tool rather than a degraded one —
  previously a non-editable install could not locate a compose stack or the
  default package manifest at all. Bringing a stack *up* still needs
  `QEMU_TOOL_SRC` pointing at a checkout, because every stack builds
  qemu-tool from source inside its container and neither a wheel nor the
  `.deb` is a source tree; the README says so now. `pyproject.toml` maps
  them in from where they already live via
  `[tool.setuptools.package-dir]`, so `qemu/compose/`
  and `qemu/packages.d/` stay put and there is no second copy to keep in
  sync. They are namespaced under `qemu_tool.share` rather than directly
  under `qemu_tool`, because a data package named `qemu_tool.compose` would
  collide with the `compose` module and `importlib.resources` would silently
  resolve to the wrong directory.
- Per-user fallbacks for the two paths a rootless install cannot have:
  `$XDG_CONFIG_HOME/qemu-tool/env` for settings (searched between `qemu/.env`
  and `/etc/qemu-tool/env`) and `$XDG_DATA_HOME/qemu-tool/images` for images.
  The images default falls back only when `/var/lib/qemu-tool/images` is not
  *writable*, so a user outside the `kvm` group gets a usable directory
  instead of a permission failure on the default.
- `package.yml`, replacing `deb-package.yml`, now covering the wheel as well
  as the deb: it builds both, rebuilds the wheel from the sdist to prove the
  sdist carries the same data, and installs the wheel into a bare
  `python:3.12-slim` with no `/usr/share/qemu-tool` and no checkout to prove
  a pipx install resolves every stack and manifest. See `ci.md`.

- `rocjitsu_gpu_test`, a repo-local role that runs HIP workloads against a live
  rocjitsu vfio-user server: a scratch-free `vector_add`, a four-kernel
  private-segment repro, and an `hsa-snoop` trace of a dispatch on the emulated
  device. It runs from `vm-rocjitsu-test.yml` against a booted guest rather
  than at image build, because the server has to be serving. The repro carries
  a per-kernel watchdog and is run with `async`/`poll`: a scratch-starved
  dispatch hangs rather than failing, and the wedged ROCr thread leaves an
  unreapable zombie whose stdout never closes, which blocks a plain `command:`
  forever regardless of `timeout(1)`.
- hsa-snoop 1.1.1 in the rocjitsu guest image, with `bpftrace`. Pinned to a
  release tag rather than the rolling `latest` pre-release, so two builds of
  the same commit install the same bits. The packaged system-wide collector is
  disabled: it polls queues for its `:9488` exporter, which is free on real
  hardware but competes with the workload on a device that serves
  single-threaded. The test role invokes hsa-snoop per-workload instead.
- `ionic_image_prep`, a repo-local Ansible role that prepares a guest image for
  rocm-ernic's ionic mode: a pinned mainline kernel from `kernel.ubuntu.com`,
  the DKMS and rdma-core toolchain, the `1dd8:100a` `pci.ids` entry, the
  `modules-load.d` drop-in and the rdma-core provider stamp. Collection 0.2.0
  removed `ernic_image_prep` and now assumes an already-prepared guest; this
  repo builds its own with `gen-vm`, so it owns that preparation.
- `smoke-test-rocm-two-vms` and `vm-report-two-vms` workflows, replacing
  `2vm-smoke-test` and `2vm-vm-report`. Guest vCPU and memory are derived from
  the runner rather than fixed, with a floor of 4 vCPUs per guest.
- `ansible/playbooks/vars/ernic-pins.yml`: the mainline kernel ref and the
  rocm-ernic source commit, each written once and read by both plays.
- `qemu/env.example`, one settings template for the whole tool, replacing the
  four per-stack `env.example` files. Copy it to `qemu/.env`; `gen-vm`,
  `run-vm` and `compose` all read that one file. Every `VM_*` key maps to the
  flag of the same name, so `VM_VCPUS` is `--vcpus` and `VM_IMAGES_DIR` is
  `--images`, and the file is searched for at `--env-file`, `$QEMU_TOOL_ENV`,
  `./.env`, `qemu/.env`, then `/etc/qemu-tool/env`. Values apply lowest to
  highest from the defaults, a `--domain` XML, the file, then explicit flags,
  so a flag always beats the file. One-shot actions (`--dry-run`, `--force`,
  `--restore-image`, `--ansible-only`, `--nvme-recreate`) are deliberately not
  readable from it.
- `--env-file` on `gen-vm`, `run-vm` and `compose`.

### Changed

- The rocjitsu benchmark numbers pick their own SI prefix instead of always
  being reported in G-units. The GPU is attached over vfio-user, so the same
  benchmark spans five orders of magnitude between bare metal and the guest —
  231 GFLOP/s against 1.88 MFLOP/s — and a fixed unit renders one of the two
  unreadably, as `0.00187734275 GFLOP/s` on a shields.io badge. The value now
  selects between T, G, M, k and none, rounded to three significant figures,
  for the report table and both badges. The benchmarks themselves still emit
  `gflops=` and `read_gbs=`, so the parse and the raw output block are
  unchanged. The table carries a note saying the low absolute numbers are
  expected of a vfio-user guest and that the trend is what to watch.

- `vm-report` and `vm-report-two-vms` no longer publish the site themselves.
  Both called `actions/deploy-pages` with their own full `_site/`, and a Pages
  deploy replaces the whole site, so whichever ran last won and the other
  lane's report vanished — with both workflows reporting success. They now
  upload an artifact and `publish-pages.yml` assembles the site. The second
  guest's report moves from `site/2vm` to `site/vm2`, since nested under
  `/two-vm/` the old name read as a duplicate of its parent.

- The `.deb` no longer ships the bundled copy of the data the wheel carries.
  It installs it at the FHS locations as before, and `debian/rules` strips the
  duplicate from `dist-packages` at `dh_installdeb` — not at
  `dh_auto_install`, where `dh_python3` re-stages the package afterwards and
  silently undoes the removal. The `/usr/share/qemu-tool` copies keep
  precedence at runtime, so editing the installed files still takes effect.
- **rocm-ernic is now ionic-based.** Upstream deleted the out-of-tree
  `rocm_ernic_eth`/`rocm_ernic_rdma` drivers and the patched verbs provider on
  2026-09-16. The guest now builds upstream `ionic` + `ionic_rdma` as the
  `ionic-ernic` DKMS package and uses stock rdma-core's `providers/ionic`; the
  emulated device moved from `1022:8000` to Pensando `1dd8:100a`.
- `ansible-playbook-test` split into per-profile workflows (`-rocm`,
  `-rocjitsu`, `-ernic`, `-ernic-rocjitsu`) sharing a `setup-vm-job` action,
  and every workflow renamed to a consistent `qemu-minimal - <name>` form.
- `requirements.yml` pins the rocm-ernic collection to an upstream git SHA.
  Galaxy publishes only 0.1.0, which is the pre-ionic collection, so the
  previous `>=0.1.0` could never have resolved to 0.2.0.
- Container images move off the previous pins: rocm-ernic to
  `20260919.g959f0cf` (`ernic.6ca9a46` → `ernic.0b48aa1`), qemu to
  `20260919.g359579e`, and rocjitsu to `20260921.gb3399b3-rocjitsu.8e01a5a`
  (`rocjitsu.20d4ce1` → `rocjitsu.2d8a73f` → `rocjitsu.8e01a5a`).

  rocjitsu is built from rocm-systems `develop` at
  `8e01a5a3fbee92f2b570dde97f314000d5226327`, which is where the vfio-pci work
  landed — 14 commits ahead of the previous pin under `emulation/rocjitsu`,
  including the `vram_store.cpp` 16-bit `atomic_load` and `compare_exchange`
  fixes. Note that rocjitsu and qemu no longer share a CI build sha. That is
  fine and deliberate: they only have to agree on libvfio-user, which is
  unchanged at `vfu.8039244`. Do not "fix" the mismatch by rebuilding qemu
  unless libvfio-user itself moves.
- `spell-check` runs `codespell` instead of `pyspelling`/aspell. codespell
  matches a fixed list of known misspellings rather than validating every word
  against a dictionary, so the 441-entry `.wordlist.txt` is gone: hostnames,
  flags, image tags and hex fragments are no longer words anyone has to
  allow-list. Configuration is `.codespellrc`, and the workflow runs a bare
  `codespell` so a local run is the same command. It also covers the whole
  tree, not just `**/*.md`.
- ernic guests now get 4 vCPUs. `ionic_lif_size()` derives its EQ count from
  `num_online_cpus()` and `ionic_create_rdma_admin()` rejects fewer than
  `IONIC_EQ_COUNT_MIN`, so at 2 vCPUs `ionic_rdma` could never probe.

### Fixed

- Both rocjitsu-family report lanes failed on every push to `main` since RVS
  was added, at `Install ROCm Validation Suite`: the task asked apt for
  `amd-smi-lib` and `rocm-validation-suite`, which are `repo.radeon.com`
  names, while the guest resolves against TheRock. TheRock carries amd-smi as
  `amdrocm-amdsmi` and does not package RVS at all, under any name. The
  amd-smi request is renamed, and RVS now comes from AMD's standalone tarball
  at `repo.amd.com/rocm/rvs/tarball/`, unpacked into its own `/opt/rvs`
  prefix — not over the versioned ROCm tree, which apt owns and whose `bin`,
  `lib`, `include` and `share` the tarball would collide with. The task also
  had no `become: true`, so it would have failed on permissions next; the
  availability error simply fired first. `rocjitsu_gpu_test` follows the new
  prefix. Only `amdrocm7` tarballs are published, but every soname RVS needs
  is one TheRock 10.0 ships, and TheRock's own libraries pull `rocm_sysdeps`
  and `llvm` in through their `$ORIGIN` rpaths — verified by running the
  1.5.125 tarball against a bare 10.0.0-4 tree with nothing else on the
  library path. See #153.
- The published `/1vm/` report carried a nested copy of the whole site —
  `1vm/index.md` alongside `1vm/reports.md`, `1vm/documentation.md`,
  `1vm/publishing.md`, `1vm/assets/` and `1vm/perf/`. `vm-report.yml` checks
  out the repository, which populates `site/` with the repository-owned pages,
  then generated its report into that same `site/` and uploaded the whole tree
  as the `vm-report-1vm` artifact. The duplicated pages declare absolute
  permalinks (`/reports/` and friends), so they silently contended with the
  real root pages for those URLs. Both `vm-report` and `vm-report-two-vms` now
  generate into a lane subdirectory and upload only that — `site/1vm/`, and
  `site/two-vm/` plus `site/two-vm/vm2/` — the shape the rocjitsu and
  hipfile-fio lanes already used. `publish-pages.yml` syncs with `--delete`,
  so the stale files clear on the next publish without touching `gh-pages`
  by hand.
- Every published report said `nvme-cli unavailable` under **NVMe**, three of
  them next to a real device. `gen-vm` defaults `--packages` to
  `packages-default`, which carries `nvme-cli`, but all eleven `gen-vm` call
  sites in CI override that to `none` — a choice that arrived unexplained with
  the first `vm-report` workflow and was copied ever since. `vm-report` passes
  `--nvme 1`; `vm-report-rocjitsu` and `vm-report-hipfile-fio` reach the same
  place through `VM_NVME: "1"` in their compose stacks. All four report lanes
  now pass `--packages qemu/packages.d/packages-default`, so a report describes
  the guest a plain `gen-vm` gives you rather than a stripped-down one. That
  manifest is longer than `none`, so the lanes' SSH waits are worth watching if
  cloud-init ever slows. `packages-minimal` is left alone.
- Every published VM report linked its commit to `/commit/unknown`. All four
  pages — `1vm`, `two-vm`, `two-vm/vm2` and `rocjitsu` — carried the literal
  text `Commit: unknown` wrapped in a dead link, because
  `generate-vm-report.sh` resolved the SHA with `git rev-parse --short HEAD`
  and every report lane runs the script inside a container, where that call
  fails. `2>/dev/null` hid the reason, so it silently fell through to the
  `unknown` fallback; whether git is absent from the container images or its
  dubious-ownership check rejects the checkout was not pinned down, and the
  fix short-circuits both. The
  script now prefers `GITHUB_SHA`, which Actions always sets, and keeps git
  only for local invocation; the link carries the full SHA while the text
  shows the short form. When there is genuinely no commit to point at it
  emits plain text instead of a link, so the dead-link case cannot come back.

- `gemm-hipfile-bench` double-counted its device offset and aborted the whole
  rocjitsu report at block 16 of 32, reporting
  `warm hipFileRead block 16 returned -5022 of 1048576`.
  `hipFileRead`'s second
  argument is `buffer_base` and its fifth is `buffer_offset`, applied to that
  base; both read loops passed an already-advanced `dev + blk * kBlock` *and*
  the same increment again as the offset, so the effective destination was
  `dev + 2 * blk * kBlock`. The buffer is registered for 32 MiB, which makes
  block 15 the last one fully in range and block 16 the first to start past
  the registration, which the library reports as `-5022`,
  `hipFileInvalidValue`. Both loops now pass `dev`, the pointer
  `hipFileBufRegister` was given. Note that the benchmark's own correctness
  gate could not have caught this: it fails only when `failures` — elements
  exceeding `kTolerance`, 1e-3 — is non-zero, and `kBOffset` is 64 KiB, so
  both GEMM operands sit inside block 0, the one block the old arithmetic
  addressed correctly. Verified instead with an ad-hoc harness on the
  development VM, reading a 32 MiB random file from its NVMe and comparing
  device memory against `pread`: all 32 blocks match byte-for-byte. The
  benchmark then ran to completion for the first time — it had only ever been
  compile-verified before. Ten runs on that VM gave a `read_gbs` between 0.115
  and 0.151, which the report renders as 115–151 MB/s; note that the ~30%
  run-to-run spread is wider than the trend the badge is meant to show, so the
  single published figure should not be read as precise.

- Neither rocjitsu benchmark had ever compiled. `sgemm-bench.hip` and
  `gemm-hipfile-bench.hip` both used `goto cleanup` to reach a single cleanup
  block, and every one of those jumps crossed the initialisation of a variable
  declared later in the same scope — `block`, `grid`, `max_abs_error`,
  `start`, `seconds` and the rest. That is ill-formed C++, not a warning, and
  clang rejected it: 11 errors for `sgemm-bench.hip` against gfx1250. The
  failure stayed invisible for as long as the lane reported a blank line,
  because hipcc writes its diagnostics to stderr and the report script only
  captured stdout. Both files now release their device buffers, file
  descriptor, hipFile handle and driver through scope guards and return
  directly, so no jump crosses an initialisation. Guard declaration order is
  load-bearing: reverse-destruction order reproduces exactly what the
  `cleanup:` block did. Exit codes and every diagnostic string are unchanged.

- The hipFile benchmark reported "hipFile headers or library unavailable" on a
  guest that had the SDK installed. `run_rocjitsu_hipfile` probed only for the
  nested `include/hipfile/hipfile.h`, but TheRock — the stream
  `vm-rocjitsu.yml` uses — ships the header flat at `include/hipfile.h`, so the
  probe never matched and the lane exited before producing a `read_gbs`
  metric. The probe now accepts either layout, as the compile line below it and
  the benchmark's own `__has_include` already did.

- The rocjitsu guest never had the hipFile SDK in the first place, so fixing
  the probe alone would only have made it report the truth. `vm-rocjitsu.yml`
  listed `amdrocm-runtime-dev`, which depends on `amdrocm-sysdeps`,
  `amdrocm-runtime` and `amdrocm-llvm-dev` and nothing else — the development
  VM that the benchmark was proven on had `amdrocm-hipfile-dev` installed by
  hand. It is now in `rocm_setup_minimal_packages`, so a freshly generated
  image can run `gemm-hipfile-bench` and produce a `read_gbs` metric instead of
  aborting the report.

- The management and multicast NICs of one guest were given the same MAC.
  `_mcast_args` derived its address from the SSH port with the same
  arithmetic as `_mgmt_mac` rather than calling it, so `--mcast-group` put
  two interfaces on one address and the guest answered ARP for whichever
  came up first. The NIC index is now a byte of its own —
  `52:54:00:<index>:<hi>:<lo>` — so the management, multicast and data NICs
  cannot collide. The management NIC keeps the address it already had, so
  existing images that pin it still match. The data NIC gained an explicit
  MAC at the same time: it had none, which meant QEMU's built-in default
  `52:54:00:12:34:56` — exactly the address older guest images pin their
  netplan to, so a guest with both NICs could match its management netplan
  against the data NIC.

- The rocjitsu report lane reported its failures as a blank line. The
  guest-side benchmark scripts run under `set -euo pipefail`, and their
  `hipcc=$(command -v hipcc || ls /opt/rocm*/bin/hipcc ... | head -1)` probe
  fails the whole assignment when no `hipcc` exists: the unmatched glob makes
  `ls` exit non-zero and `pipefail` carries that out, so `set -e` killed the
  script one line above the `hipcc unavailable` guard written for exactly
  that case — with nothing printed, `ls` stderr being discarded. The
  `rocm=$(for ...; done | head -1)` hipFile probe had the same shape. Both
  now tolerate the empty result and reach their guard, and a benchmark that
  fails with no output at all is reported by name rather than as an empty
  `printf`. This is why `vm-report-rocjitsu` failed on its first run, took
  the `publish-pages` artifact with it, and left `/rocjitsu/` a 404 with two
  broken shields.io endpoint badges on the README.
- `scripts/release.sh` dropped every paragraph after the first in a
  multi-paragraph `CHANGELOG.md` bullet. A blank line ended the bullet, so
  the indented paragraph that followed matched the continuation rule but
  found an empty buffer and was discarded — silently, because the shortened
  stanza still parses. Blank lines no longer terminate a bullet, and a
  nested markdown item now becomes an entry of its own instead of being
  appended to its parent with a literal `- ` left mid-sentence.
- The wheel and sdist ship the MIT licence text. `license-files` was unset
  and `LICENSE` lives above the sdist root, so the PyPI artifacts carried
  only the `License: MIT` metadata string — not the text MIT requires be
  included in all copies. `qemu/LICENSE` is a symlink to the repo's, so
  there is no second copy to drift.
- `build-essential` is installed before `dpkg-buildpackage` in both
  `package.yml` and `release.yml`. `dpkg-checkbuilddeps` treats it as an
  implicit build dependency of every source package, even an arch-all
  Python one that compiles nothing, and the runner image does not ship it.
  Neither deb build had ever run on a GitHub runner to find out.
- The container jobs in `package.yml` pin `shell: bash`. The runner chose
  `sh -e` for the `ubuntu:24.04` container despite bash being installed
  there, and dash has no `set -o pipefail`, which every assertion step
  opens with.

- `vm-rocjitsu.yml` no longer stubs over the real gfx1250 firmware.
  `vfio_guest_firmware.py` changed contract: its default output is now the
  "gap" set -- `gc_12_1_0_imu.bin`, `gc_12_1_0_mes.bin`, `gc_12_1_0_mes1.bin`
  and `ip_discovery.bin`, the files no driver release ships -- which lands
  beside the packaged blobs instead of over them. As of amdgpu 31.60,
  `amdgpu-dkms-firmware` ships real `gc_12_1_0_mec.bin`,
  `gc_12_1_0_mec_1.bin`, `gc_12_1_0_rlc.bin`, `gc_12_1_0_rlc_1.bin`,
  `gc_12_1_0_uni_mes.bin` and `sdma_7_1_0.bin`, and `amdgpu-dkms` depends on
  it. That, plus `imu` from the generator, covers what upstream's
  `docs/qemu-vfio.md` calls for.

  Those blobs install under `/lib/firmware/updates/amdgpu/`, not
  `/lib/firmware/amdgpu/`. The kernel searches `updates/` first, so they take
  precedence over anything of the same name written into the base tree — which
  is why the gap set is safe to copy in alongside them.

  The playbook asserted on `gc_12_1_0_rlc_1.bin`, now deliberately absent, so a
  working generator failed the build. It asserts on `imu` instead, which is in
  both sets and in neither package. The separate `rj-ip-discovery` call is gone
  — the generator emits `ip_discovery.bin` itself, so that call overwrote what
  had just been produced. The `uni_mes` → `mes`/`mes1` copy is gone too, and
  had become actively wrong: it overwrote the generator's real `mes`/`mes1`
  stubs with the packaged `uni_mes` blob. A new guest-side check names any
  missing packaged blob, because the driver otherwise reports only `MES
  firmware reports incorrect version in ucode binary` or a bare `-2`. Set
  `vm_rocjitsu_firmware_set=full` for a guest on a pre-31.60 driver release.

  That check initially looked in `/lib/firmware/amdgpu/`, where it found only
  the stubs an earlier `--set full` run had left behind. It therefore passed on
  a guest carrying stale debris and failed on a freshly built one that was
  entirely correct. It now checks `/lib/firmware/updates/amdgpu/`, against the
  package's own file list.
- HIP kernels with a private segment (scratch) now complete on the emulated
  gfx1250 instead of hanging forever. `amdgpu-probe` passed
  `amdgpu.vramlimit=256`; upstream's `qemu-vfio.md` says 1024, and it is not a
  performance knob. ROCr provisions queue scratch out of that budget at the
  occupancy the device advertises, so a kernel with no private segment is
  unaffected — `vector_add` always passed in under a second — while one with a
  private segment waits on an allocation that never arrives. The dispatch never
  fails, so `hipDeviceSynchronize()` simply does not return and no ordinary
  timeout catches it; the wedged ROCr thread then sits in an uninterruptible
  KFD ioctl holding a KFD mutex for the rest of the boot, so only the first
  such result in a boot means anything.

  This is independent of `vram_aperture_bytes` in the rocjitsu config, which is
  the BAR window — upstream leaves that at 256 MiB alongside a 1 GiB
  `vramlimit`. Conflating the two sent this investigation down a dead end for
  some time, so it is worth stating plainly. The A/B is clean: the same server
  build, with only `vramlimit` changed, hangs at 256 and passes all four repro
  kernels at 1024.
- `amdgpu` now probes on the rocjitsu emulated device instead of oopsing the
  guest. Two independent faults, both of which took the machine down hard
  enough that `sudo amdgpu-probe` returned only `Killed`:
  - The device is exposed with `rombar=0`, so there is no option ROM and
    `amdgpu_device_init` takes its "VBIOS image optional" path, leaving
    `adev->mode_info.atom_context` NULL. `amdgpu_ras_init` then queries the RAS
    capability from the VBIOS and the atomfirmware helpers dereference that
    field unguarded, oopsing in `amdgpu_atom_parse_data_header`.
    `vm-rocjitsu.yml` now patches the DKMS source to return early when
    `atom_context` is NULL, matching the guard `amdgpu_ras_get_quirks` already
    has on the same field one function above.
  - `amdgpu-probe` passed `ip_block_mask=0x3f`, taken from upstream's
    `qemu-vfio.md`. That is correct for upstream's driver, where the compute IP
    blocks enumerate as indices 0..5 ending in `mes`; this DKMS build
    enumerates an extra `ras_v1_0` at index 5, so `mes_v12_1` lands at 6 and
    `0x3f` masks it off. `gfx_v12_1` needs MES to resume the command processor,
    so the probe oopsed in `gfx_v12_1_xcc_cp_resume`. Now `0x7f`.

  With both applied the probe completes, `/dev/kfd` appears, and `rocminfo`
  reports `gfx1250` with 32 CUs.
- `gen-vm` guests no longer lose networking when the MAC qemu-tool presents
  changes. The guest MAC is derived from the SSH port, and guests were ignoring
  the seed's `network-config` and falling back to cloud-init's own, which pins
  the interface to the MAC seen at creation time. Booting such an image again
  then left the NIC `unmanaged` with no address — on a different port, but also
  on the same one, including the default: before `4532f91` the slirp path
  passed no `mac=` at all, so QEMU's built-in `52:54:00:12:34:56` applied and
  got baked into the guest, while every later boot presents the derived
  address. A guest built with `--mgmt-tap` already got the derived MAC and so
  only broke when the port changed. In every case slirp's `hostfwd` still
  completed the TCP handshake, so SSH reported `Connection timed out during
  banner exchange` rather than `refused`, and `gen-vm` `--ansible-only` sat in
  `_wait_for_ssh` for its full 600 s without ever reaching the playbook. First
  boot now overwrites the rendered
  `/etc/netplan/50-cloud-init.yaml` with a `name: "en*"` match, so the MAC
  stops being load-bearing. Existing images keep the old pin; regenerate, or
  repoint netplan in the guest.
- `vm-rocjitsu.yml` now resolves the ROCm install prefix and writes
  `/etc/ld.so.conf.d/rocm.conf` and `/etc/profile.d/rocm.sh` against it.
  TheRock installs to a versioned `/opt/rocm/core-10.0`, but `rocm_setup`
  hardcodes the pre-TheRock `/opt/rocm/lib` and never sets `PATH`, so the
  linker resolved no ROCm libraries (`ldconfig -p | grep -c hsa-runtime` was 0)
  and `rocminfo` was `command not found` despite being installed. Apt then
  helpfully offers the universe 5.2.3 package, which the play already pins to
  priority `-1`. This is a workaround for
  [batesste-ansible#248](https://github.com/sbates130272/batesste-ansible/issues/248)
  and should be dropped once the role derives the prefix itself.
- The ernic configure play supplies `ernic_guest_vm_ip`, which
  `ernic_guest_setup` asserts on and defaults to empty. Derived as
  `192.168.200.<10 * vm_index>`, matching upstream `vm-register.yml`.
- `ernic_source_repo_version` is pinned to the same commit as the collection.
  It defaults to `main`, so the sources built in the guest could come from a
  different tree than the roles building them.
- `ansible-playbook-test-ernic` now triggers on `ansible/playbooks/roles/**`
  and `vars/ernic-pins.yml`. Both are inputs to `vm-ernic.yml`, but neither was
  in the workflow's `paths`, so a change to `ionic_image_prep` or to the kernel
  and source pins reported all checks green without the ernic lane having run
  at all.
- `ionic_image_prep` no longer corrupts `pci.ids`. hwdata already lists
  `1dd8:100a` (as `DSC Serial Port Controller` — the emulated NIC reuses a real
  pair), and the presence check grepped for our own entry text, so it never
  matched and appended a duplicate device id. pciutils refuses to parse a file
  containing one, which left `lspci` resolving no names for any device on the
  bus. The check is now a block-scoped `awk` scan for the device under its
  vendor, and a post-merge `lspci` parse check fails the play if a merge ever
  does break the file.

### Removed

- `qemu/gen-vm` and `qemu/run-vm`. `qemu-tool gen-vm` and `qemu-tool run-vm`
  have been the maintained path for some time and the two bash scripts had
  drifted; keeping both meant every flag had to be added twice. `shell-check`
  now lints only the two `libvirt/` scripts.
- The four per-stack `qemu/compose/*/env.example` files, superseded by
  `qemu/env.example`.
- The `smoke-test` workflow, which drove the removed bash scripts.

## [v1.3.0] - 2026-09-15

### Added

- `qemu-tool list` subcommand: lists every `qemu-system-*` process on the node,
  including VMs inside containers (attributed back to the container name), with
  vCPU count, memory, forwarded SSH port, KVM state, uptime and attached
  vfio-user sockets. `--json` for machine-readable output, `--qemu-tool-only`
  to filter.
- VM identity markers: `run-vm` and `gen-vm` now stamp each VM with
  `-name guest=<vm-name>,debug-threads=on` and a deterministic
  `-uuid <uuid5(namespace, "<role>:<vm-name>")>`. `list` recomputes the UUID
  from the guest name to identify qemu-tool's VMs exactly, and to distinguish a
  transient `gen-vm` build VM from a running `run-vm` VM. Both options are
  generic, so they work on amd64, arm64 and riscv64 alike.

### Changed

- `--vm-name` now rejects a comma. A comma terminates a QEMU option value, so
  it silently corrupted `-name`, `-uuid`, `-drive file=` and `-chardev path=`.

### Fixed

- The Debian package declared no dependencies beyond the Python interpreter.
  It now `Depends` on a QEMU system emulator and `qemu-utils`, `Recommends`
  `cloud-image-utils`, `openssh-client` and `wget` for `gen-vm`, and `Suggests`
  `ansible`, `dnsmasq-base`, `docker.io` and `iproute2` for the optional paths.

## [v1.2.0] - 2026-08-31

### Added

- `python3-qemu-tool` Debian package for system-wide installation via `dpkg`/`apt`
- Man page `qemu-tool(1)` installed to `/usr/share/man/man1/`
- `qemu/packages.d/packages-default`: package manifest moved into `qemu/` so it
  is self-contained within the deb build tree; installed to
  `/usr/share/qemu-tool/packages-default`
- CI: parallel `build-deb` job in `release.yml` uploads the `.deb` to GitHub Releases

### Changed

- Default image directory changed from `../images` (repo-relative) to
  `/var/lib/qemu-tool/images`; the installer creates the directory owned
  `root:kvm` with mode `2775` (setgid for kvm group inheritance)
- Default package manifest path changed from the repo-relative path to
  `/usr/share/qemu-tool/packages-default`

## [v1.0.0] - 2026-08-05

### Added

- `qemu/gen-vm`: cloud-init based VM creation for x86_64, ARM64, and RISC-V
- `qemu/run-vm`: flexible QEMU runner with NVMe, VFIO, CXL, VirtFS, QMP, and
  multicast NIC support
- `libvirt/virt-install-ubuntu`: libvirt VM creation via `virt-install`
- `libvirt/create-nvme`: helper to generate NVMe XML fragments for libvirt
- `packages.d/packages-default`: broad development and debugging package manifest
- `packages.d/packages-minimal`: minimal package manifest (`emacs-nox`, `fio`,
  `sysstat`, `tree`)
- `ansible/playbooks/vm-setup.yml`: post cloud-init Ansible playbook using the
  `sbates130272.batesste` Galaxy collection
- `udev/99-qemu-minimal-vfio.rules`: udev rules for VFIO device permissions
- `udev/install-vfio-rules`: installer script for the udev rules
- Shared backing-file workflow allowing multiple VMs to share a single
  read-only qcow2
- `RESTORE_IMAGE` mode to recreate a VM overlay from an existing backing file
- CI workflows: shell-check, smoke-test (x86/arm64, Noble/Resolute),
  ansible-setup-test, spell-check, and release
