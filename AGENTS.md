# Agent Context for qemu-minimal

Practical facts for AI agents (and humans) working in this repo. Keep this
file up to date as infrastructure changes.

---

## Repository layout

- `qemu/` — qemu-tool Python package (installed as a Debian package or via pipx)
- `qemu/compose/vfio-user-ernic-vm/` — single-VM ernic-only compose stack
- `qemu/compose/vfio-user-rocjitsu-vm/` — single-VM rocjitsu-only compose stack
- `qemu/compose/vfio-user-ernic-rocjitsu-vm/` — single-VM ernic + rocjitsu compose stack
- `qemu/compose/vfio-user-ernic-2vm/` — two-VM ernic mesh stack; rocjitsu GPUs opt-in via `--profile rocjitsu-vm1/vm2`
- `images/` — qcow2 VM disk images (backing + overlay pairs, **not** `/var/lib/qemu-tool/images`)
- `ansible/` — Ansible playbooks for VM provisioning
- `rocm-ernic-enablement.md` — running log of rocm-ernic integration status and bugs

## VM images

All qcow2 images live at **`<repo-root>/images/`** (i.e.
`/home/stebates/Projects/qemu-minimal/images/`), not at the default
`/var/lib/qemu-tool/images`. Set `VM_IMAGES_DIR` accordingly in `qemu/.env`.

Active images for the 2-VM ernic stack:

| VM   | Overlay                      | Backing                       |
|------|------------------------------|-------------------------------|
| VM1  | `stebates-ernic-vm-1.qcow2`   | `stebates-ernic-base.qcow2`   |
| VM2  | `stebates-ernic-vm-2.qcow2`   | `stebates-ernic-base.qcow2`   |

Base image is 12G (compressed from 28G used filesystem, 95G virtual).
Both VMs share the same backing; only delta writes go into their overlays.
Kernel: `7.0.0-31-generic` (HWE). Recreate overlays with:
```bash
qemu-img create -f qcow2 -b stebates-ernic-base.qcow2 -F qcow2 stebates-ernic-vm-1.qcow2
qemu-img create -f qcow2 -b stebates-ernic-base.qcow2 -F qcow2 stebates-ernic-vm-2.qcow2
```

SSH access (when the stack is running):

```
ssh -p 2222 stebates@localhost   # VM1
ssh -p 2223 stebates@localhost   # VM2
```

Password: see cloud-init user-data (plain_text_passwd field). GPG key in VM
is not unlocked (no private key injected), so git-crypt and signed commits
are not available inside the VM.

## Two-VM compose stack

```bash
cp qemu/env.example qemu/.env
# Edit qemu/.env: set VM_IMAGES_DIR to the repo images/ absolute path
# Add --profile rocjitsu-vm1 --profile rocjitsu-vm2 to enable GPUs
qemu-tool compose --stack vfio-user-ernic-2vm \
  --profile rocjitsu-vm1 --profile rocjitsu-vm2 up -d
```

Key `qemu/.env` values that differ from the example defaults:

| Variable       | Correct value                                        |
|----------------|------------------------------------------------------|
| `VM_IMAGES_DIR`| `/home/stebates/Projects/qemu-minimal/images`        |
| `VM1_NAME`     | `stebates-ernic-vm-1`                                 |
| `VM2_NAME`     | `stebates-ernic-vm-2`                                 |

The ernic hub serves VM1 (`ernic-1.sock`); the worker serves VM2
(`ernic-2.sock`). VMs should be assigned IPs in `192.168.100.11/24` and
`192.168.100.12/24` on `enp1s0` — avoid `.1` (reserved as the hub DHCP
`server_ip`; ARP for `.1` is intercepted by the hub).

## rocm-ernic driver and userspace provider (in-VM)

**rocm-ernic is now ionic-based.** Upstream deleted `driver/` and `rdma-core/`
on 2026-09-16. There is no `rocm_ernic_eth`, no `rocm_ernic_rdma`, and no
patched rocm_ernic verbs provider any more; anything still referring to those
is describing a tree that no longer exists.

What replaces them:

| Was | Is |
|-----|-----|
| out-of-tree `rocm_ernic_eth` + `rocm_ernic_rdma` | upstream `ionic` + `ionic_rdma`, two AMD patches on top |
| DKMS package `rocm-ernic` | DKMS package `ionic-ernic`, built by `scripts/setup-ionic-dkms.sh` |
| rdma-core ≥ 62 + `apply-rocm-ernic-dv.sh` | stock rdma-core ≥ 61, whose `providers/ionic` is upstream |
| emulated device `1022:8000` | Pensando `1dd8:100a` |

`ionic_rdma` needs kernel ≥ 6.18 (`drivers/infiniband/hw/ionic` merged there)
and `ib_umem_get_va`, which landed after 7.0. No Ubuntu release ships one, so
the guest runs a pinned mainline kernel.

None of this is done by hand in this repo. Two pieces of automation own it:

- [`ansible/playbooks/roles/ionic_image_prep/`](ansible/playbooks/roles/ionic_image_prep/)
  prepares the guest image — installs the pinned mainline kernel from
  `kernel.ubuntu.com/mainline`, the build toolchain, the `1dd8:100a` pci.ids
  entry, and `/etc/modules-load.d/rocm-ernic-ionic.conf`. Upstream assumes a
  guest already prepared this way (their published `ionic` qcow2); this repo
  builds its own, so it owns the preparation.
- `sbates130272.rocm_ernic.ernic_guest_setup` from the collection builds and
  installs the DKMS package, the rdma-core provider and the NIC config.

The kernel ref is pinned once as `ernic_ionic_kernel_ref` in
[`vm-ernic.yml`](ansible/playbooks/vm-ernic.yml) and feeds
`ionic_image_kernel_ref` from it, so the sources and the guest kernel cannot
drift apart — `driver_ionic.yml` fails the build if they disagree on
major.minor.

Device names are unchanged: Ethernet `rocm-ernic0`, IB `rocm-rdma-ernic0`, both
still set by `99-rocm-ernic.rules` (see udev section below), now keyed on
`1dd8:100a` and `DRIVERS=="ionic|ionic_rdma"`.

## udev rules (in-VM)

`99-rocm-ernic.rules` gives the ernic devices stable names: Ethernet interface
→ `rocm-ernic0`, IB device → `rocm-rdma-ernic0`. Use these with
`ib_send_bw -d rocm-rdma-ernic0`.

There is nothing to install by hand. `ernic_guest_setup` ships the rules and
reloads udev, and both renames are confirmed working in CI — the ionic lane's
`Show NIC details` reports `rocm-ernic0` carrying `altname enp0s5np0`, which is
the pre-rename kernel name, and `ibv_devinfo` reports `hca_id: rocm-rdma-ernic0`.
The IB rename shells out to `/usr/bin/rdma`, which the gen-vm guest has.

If a guest somehow comes up with the kernel names instead, check that the rules
matched rather than re-copying them: they key on `ATTR{device/vendor}=="0x1dd8"`
and `ATTR{device/device}=="0x100a"`, so a guest still being served the old
`1022:8000` device will not rename anything.

## PCI ID (in-VM)

`ionic_image_prep` owns this; there is nothing to do by hand. Two traps make it
less straightforward than it looks.

**The emulated NIC reuses a real, vendor-assigned pair.** hwdata 0.379 already
lists `1dd8:100a` — as `DSC Serial Port Controller`. So on a current guest
nothing is missing: `lspci` names the device, misleadingly but it names it. The
"bare device number" case only arises on hwdata old enough to predate the entry.

**pciutils rejects a duplicate device id outright.** Not "ignores the second
entry" — it refuses to parse the file, and then resolves no names for *any*
device on the bus:

```
$ lspci -i /tmp/merged.ids -nn
lspci: Duplicate entry at /tmp/merged.ids, line 27685
$ echo $?
1
```

So the role's presence check is a block-scoped `awk` scan asking whether *this
vendor's block* already lists the device — a bare `grep 100a` matches the same
four hex digits under a dozen other vendors, and a `grep` for the entry text
misses the upstream spelling. On current hwdata the role therefore skips by
itself, which is the correct behaviour. When it does insert, the line goes
directly under the vendor line (`pci.ids` is parsed sequentially, so an append
at the end of the file lands under the class section and never matches), and a
post-merge `lspci` parse check fails the play rather than quietly breaking every
later `lspci`.

The entry goes under vendor `1dd8`, not `1022` — upstream moved the emulated
device off the AMD vendor id. hwdata spells the vendor `AMD Pensando Systems`,
and the vendor line has to match for the block scan to find it:

```
1dd8  AMD Pensando Systems
	100a  ROCm Emulated RDMA NIC (ionic)
```

`scripts/pci.ids.rocm-ernic` was deleted upstream along with the rest of the
pre-ionic tree, so the id is this repo's to maintain — see
`ionic_image_pciids_*` in
[`ionic_image_prep/defaults/main.yml`](ansible/playbooks/roles/ionic_image_prep/defaults/main.yml).
The 0.2.0 changelog calls pci.ids the guest image's business, but neither
`provision/ionic.sh` nor `packages/ionic.txt` in batesste-ci-images writes it.

**Note:** `update-pciids` will overwrite these files — re-apply after each run.

## Known issues / open bugs

See `rocm-ernic-enablement.md` for the full tracking list. Short version:

1. Worker `server_ip` ARP hijack when VM IP = `192.168.100.1` — use `.11`/`.12`
2. ARP log printf aliasing in `pvrdma_eth.c:483-493` (cosmetic, misleading)
3. Collection 0.2.0 is not on Galaxy — only 0.1.0 is published, and 0.1.0 is
   the pre-ionic collection. `requirements.yml` pins the upstream git SHA
   instead; move back to a Galaxy version pin once 0.2.0 ships there
4. `driver_ionic.yml` fail_msg still says to rebuild the golden image with
   `ernic_image_kernel_mainline=true` — a variable on `ernic_image_prep`,
   which 0.2.0 removed
5. *(was an `apply-rocm-ernic-dv.sh` / rdma-core / `docs/testing.rst` bug —
   all three files are gone with the pre-ionic tree)*
6. **Killing perftest mid-run breaks VM device context** — killing `ib_send_bw`
   or any perftest process mid-operation (SIGTERM/SIGKILL) leaves the guest
   RDMA driver in a broken state. `ibv_devinfo` reports "Failed to open device"
   or "wasn't found". `rmmod`/`modprobe` hangs for 2 min (DSR timeout) and the
   device does not recover. **Full stack restart required** (`docker compose down
   && up`). Avoid killing tests; let them run to completion or use `ib_send_bw
   --iters 100` to keep runs short.

7. **ernic-hub crash requires full stack restart** — if `ernic-hub` crashes and
   restarts (visible in `docker compose logs ernic-hub` as a re-initialization
   from the top), the guest driver enters a DSR initialization timeout
   (`-ETIMEDOUT`) on the next `modprobe ionic_rdma`. The vfio-user session
   held by QEMU becomes stale and cannot be recovered by reloading modules alone.
   **Fix: `docker compose down && docker compose up`** (all containers, not just
   qemu-1/qemu-2). Restarting only the QEMU containers is insufficient because
   the ernic-worker also needs a fresh TCP mesh connection to the new hub.

8. **perftest server dies if SSH session closes** — `nohup`/`disown` is not
   sufficient; the server exits with no output when the parent SSH session ends.
   Workaround: keep the SSH session alive (run server in foreground in a
   background job `&` and let the shell wait) or use `screen`/`tmux` inside
   the VM. Example:
   ```bash
   # terminal 1 — server (keep session open)
   ssh -p 2223 local-vm "ib_send_bw -d rocm-rdma-ernic0"
   # terminal 2 — client
   ssh -p 2222 local-vm "ib_send_bw -d rocm-rdma-ernic0 192.168.100.12"
   ```

9. **`ernic_source_repo_version` defaults to `main`** — so `ernic_source` clones
   upstream HEAD inside the guest even when `requirements.yml` installs the
   collection from a fixed SHA, and the roles can end up building sources from
   a different tree than they came from. Pinned locally in
   [`playbooks/vars/ernic-pins.yml`](ansible/playbooks/vars/ernic-pins.yml);
   the default is still upstream's.
10. **`ernic_nic_subnet` is not a role default** — it lives only in the upstream
    repo's `ansible/group_vars/all.yml`, which a consumer using the collection
    standalone never loads. Anything deriving a guest address from it has to
    repeat the value; `vm-ernic.yml`'s configure play does.
11. **The 4-vCPU floor is undocumented upstream** — `ionic_create_rdma_admin()`
    rejects fewer than `IONIC_EQ_COUNT_MIN` EQs with a bare `-EINVAL` that
    surfaces only as "Failed to register ibdev". There is no preflight assert
    in the collection and no mention in its docs.
12. **A rebooted ernic guest is device-present but not test-ready.** Only module
    loading persists (`ionic_image_prep` writes
    `/etc/modules-load.d/rocm-ernic-ionic.conf`). The address on `rocm-ernic0`,
    bringing the netdev up and the counters symlink are all run-time only, set
    by `ernic_guest_setup`, so a reboot brings back the RDMA device without the
    configuration around it. Re-run the configure play, or write a
    systemd-networkd unit for the address. `rocm_xio` is deliberately *not* in
    the `modules-load.d` drop-in — `rocm-xio.ko` is hand-copied into the running
    kernel's `extra/`, not DKMS-managed, so an entry would outlive the module
    across a kernel upgrade and leave `systemd-modules-load.service` failed.

## Git / GitHub

- Default GitHub account: `sbates130272` (verify with `gh auth status`)
- GPG signing required on all commits (`-S`), signoff required (`-s`)
- Main branch: `main`; current work branch: `feat/working-compose`
- Never use `--no-verify` or `--no-gpg-sign`

## Cutting a release

`scripts/release.sh <version>` from a clean `main`. It stamps the version in
`qemu/pyproject.toml`, generates the `qemu/debian/changelog` stanza from the
`CHANGELOG.md` `[Unreleased]` section, retitles that section as the new
version, commits signed-off, and makes a signed tag. Run it with `--dry-run`
first to see the generated stanza.

It never pushes; it prints the `git push origin main v<version>` that does.
Pushing the tag is what triggers `release.yml`, whose `verify-version` job
refuses to build anything unless the tag, `pyproject.toml` and
`debian/changelog` agree and `CHANGELOG.md` has a heading for the tag. That
gate exists because v1.3.0 shipped with no tag at all and nothing noticed.

`release.yml` then builds the wheel, the sdist and the `.deb`, attaches all
three to one GitHub Release, and publishes to PyPI last — a PyPI version can
never be reused, so it goes after everything repeatable has succeeded. PyPI
uses Trusted Publishing against the `pypi` environment, so there is no API
token anywhere in the repo.

## The published site

<https://sbates130272.github.io/qemu-minimal/> is served from the `gh-pages`
branch (Settings > Pages > Source = "Deploy from a branch", `gh-pages` /
(root)), not from a Pages artifact. `.github/workflows/publish-pages.yml` is
the only workflow that writes that branch; report lanes upload a named
artifact and nothing else.

That split is the whole point. `vm-report` and `vm-report-two-vms` each used
to call `actions/deploy-pages` with their own full `_site/`, and a Pages
deploy replaces the entire site, so whichever ran last won and the other
lane's report disappeared — with both workflows green.

`publish-pages` fetches each lane's latest artifact *by name*, not from the
run that triggered it, so a lane that has not run for a week still
contributes. Each lane's subtree is rsynced with its own scoped `--delete`,
so an expired artifact leaves that lane's last report in place and anything
else on the branch (a future `perf/`) survives untouched.

Adding a lane is three edits: upload an artifact under a new name, add a
`fetch <name> <dir>` line, and add a row to the landing page. Do **not** add
`.nojekyll` — the reports are markdown that GitHub's Jekyll build renders,
unlike rocm-ernic's pre-built Sphinx HTML.
