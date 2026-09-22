# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

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
- `gen-vm` guests no longer lose networking when `--ssh-port` changes. The
  guest MAC is derived from the SSH port, and guests were ignoring the seed's
  `network-config` and falling back to cloud-init's own, which pins the
  interface to the MAC seen at creation time. Booting the same image on a
  different port then left the NIC `unmanaged` with no address: slirp's
  `hostfwd` still completed the TCP handshake, so SSH reported `Connection
  timed out during banner exchange` rather than `refused`, and `gen-vm`
  `--ansible-only` sat in `_wait_for_ssh` for its full 600 s without ever
  reaching the playbook. First boot now overwrites the rendered
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
