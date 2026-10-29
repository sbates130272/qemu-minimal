# QEMU and Libvirt Tooling

[![GitHub Stars](https://img.shields.io/github/stars/sbates130272/qemu-minimal?label=Stars&style=flat-square)](https://github.com/sbates130272/qemu-minimal/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/sbates130272/qemu-minimal?label=Issues&style=flat-square)](https://github.com/sbates130272/qemu-minimal/issues)
[![Last Commit](https://img.shields.io/github/last-commit/sbates130272/qemu-minimal?label=Last%20Commit&style=flat-square)](https://github.com/sbates130272/qemu-minimal/commits/main)
[![Platform](https://img.shields.io/badge/Platform-x86__64%20%7C%20ARM64%20%7C%20RISC--V-blue?style=flat-square)](https://github.com/sbates130272/qemu-minimal)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-Noble%20%7C%20Resolute-orange?style=flat-square&logo=ubuntu)](https://releases.ubuntu.com/noble/)
[![GitHub Release](https://img.shields.io/github/v/release/sbates130272/qemu-minimal?label=Release&style=flat-square)](https://github.com/sbates130272/qemu-minimal/releases/latest)

<!-- Reports. The blank lines around this block and the performance one below
     are load-bearing: a blank line is what makes GitHub start a new paragraph,
     and therefore a new row of badges. A <br> inside the paragraph does not.
     The vm-* report lanes run on main (push and a daily 08:00 cron), so they
     are pinned with ?branch=main. report-for-quickstart and report-for-mi355x are pinned: both are
     daily and path-filtered on push to main, like the vm-* lanes.

     The date badge is the odd one out: it is not a workflow status. It is
     computed during the Pages publish from the reports actually on the site,
     and shows the last date on which every one of them was both stamped
     `pass` and freshly generated. A lane can be red while the site still
     serves its last good report, and that distinction is the whole point --
     see scripts/render-site-perf.py's update_green(). -->
[![VM Reports](https://img.shields.io/badge/VM%20Reports-Live-blue?style=flat-square)](https://sbates130272.github.io/qemu-minimal/)
[![Basic Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-vm-basic.yml?branch=main&label=report-for-vm-basic&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-basic.yml)
[![Rocjitsu Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-vm-rocjitsu.yml?branch=main&label=report-for-vm-rocjitsu&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-rocjitsu.yml)
[![Ernic Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-vm-ernic.yml?branch=main&label=report-for-vm-ernic&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-ernic.yml)
[![Lmcache Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-lmcache.yml?branch=main&label=report-for-lmcache&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-lmcache.yml)
[![Quickstart Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-quickstart.yml?branch=main&label=report-for-quickstart&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-quickstart.yml)
[![Mi355x Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-mi355x.yml?branch=main&label=report-for-mi355x&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-mi355x.yml)
[![All Reports Green](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-all-green.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)

<!-- Performance. These are shields.io *endpoint* badges: the label, value and
     colour all come from JSON that the Pages publish writes, so the colour is
     a verdict rather than decoration. Green means the latest reading is within
     5% of the mean of the previous five distinct readings, amber down to 20%
     below it, red past that, and blue means there is nothing to compare
     against yet. They used to be fixed AMD/NVIDIA/Intel brand colours, which
     meant GEMM rendered red on its best day. -->
[![Rocjitsu GEMM](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![Rocjitsu HipFile](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![HipFile Fio](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![Ernic P4 FP8 BW](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-ernic-p4-bw.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)

<!-- quickstart-boot and the four other PR-only lanes (dry-run, ansible,
     shell-check, spell-check) carry no ?branch=main: they have no
     push-to-main and no cron trigger, so they have no run on main and
     shields renders "no status" for a pinned badge. The two smoke-test lanes
     in this row do run on main and stay pinned, as do the report lanes in
     their own row above.

     quickstart-boot is the only one of the six that boots a guest. It is
     deliberately NOT a report lane -- it publishes nothing and uploads no
     artifact, so publish-pages.yml neither knows nor needs to know about
     it. -->
[![Qemu-tool Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-smoke-test-qemu-tool.yml?branch=main&label=qemu-tool-smoke&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-smoke-test-qemu-tool.yml)
[![Quickstart Boot](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-quickstart-boot.yml?label=quickstart-boot&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-quickstart-boot.yml)
[![Dry-Run Tests](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-dry-run-qemu-tool.yml?label=dry-run&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-dry-run-qemu-tool.yml)
[![Ansible Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-ansible-setup-test.yml?label=ansible&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-ansible-setup-test.yml)
[![Shell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-shell-check.yml?label=shell-check&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-shell-check.yml)
[![Spell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-spell-check.yml?label=spell-check&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-spell-check.yml)
[![Rocm-ernic Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-smoke-test-rocm-ernic.yml?branch=main&label=smoke-test-rocm-ernic&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-smoke-test-rocm-ernic.yml)

<!-- The two Ansible collections ansible/requirements.yml installs. Exact
     pins, both of them, and neither is a range: sbates130272.batesste
     follows a breaking 3.x line, and the rocm-ernic one is a git SHA because
     Galaxy only ever published 0.1.0, which is the pre-ionic collection.

     rocm-ernic's shortrev is load-bearing beyond the collection itself. SRQ
     ships in two halves -- the device emulation in the ernic server
     container and the guest-side driver built from this tree -- so this
     badge and the rocm-ernic one in the row below have to agree. If they
     ever disagree, the guest cannot create an SRQ at all. See AGENTS.md
     issue 20.

     "The row below" means the FLEET row, the one tracking qemu/env/scale-out
     -- that is where LMCache and therefore SRQ live. The quick-start badge
     is deliberately ahead of both and is not part of this pairing; it boots
     no LMCache and creates no SRQ. -->
[![Batesste Collection](https://img.shields.io/badge/sbates130272.batesste-3.1.0-9cf?style=flat-square)](https://galaxy.ansible.com/ui/repo/published/sbates130272/batesste/)
[![Rocm-ernic Collection](https://img.shields.io/badge/sbates130272.rocm__ernic-61b934a-9cf?style=flat-square)](https://github.com/ROCm/rocm-ernic)

<!-- The batesste-ci-images this repo pins. STATIC badges, hand-edited with
     the tags: shields has no endpoint for "what does this repo pin", and a
     dynamic Docker Hub badge would report that registry's newest tag, which
     is the opposite of what these are for -- the point is which build the
     stacks are on, not which exists.

     WHICH FILE THESE TRACK: qemu/env/scale-out, the fleet settings. That is
     the pin a long soak runs on and the one the committed stack is generated
     from. Within it the five share one <date>.<ci-sha> stamp by
     construction: gen_compose.py's _check_image_coherence refuses a mixed
     set, because a guest driver and a host server from different builds
     wedge at the vfio-user/DSR boundary. So when one moves they all move,
     and these move with them. The stamp is the first badge; the rest carry
     the component revision that distinguishes the build.

     The quick-start badge is separate BECAUSE it is separate. The coherence
     check is per settings file, not across them, and qemu/env/quickstart
     runs ahead of the fleet on purpose -- it is the shop window and should
     show the current build. One badge for it, carrying its own stamp; do not
     try to fold it into the row above, and do not "fix" the two to agree.

     sbates-fork is the odd one out in the other direction: it is pinned only
     by the qemu/env/overlay-mi100 overlay, for vfio-user P2P DMA, and
     carries a QEMU patch the stock image does not. See AGENTS.md issue 19. -->
[![CI image build](https://img.shields.io/badge/ci--images-20261021.g742e8e2-informational?style=flat-square&logo=docker)](https://hub.docker.com/u/sbates130272)
[![Qemu](https://img.shields.io/badge/qemu-11.1.2-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qemu-libvfio-user/tags)
[![Libvfio-user](https://img.shields.io/badge/libvfio--user-8039244-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qemu-libvfio-user/tags)
[![Rocm-ernic](https://img.shields.io/badge/rocm--ernic-31d3114-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-rocm-ernic/tags)
[![Rocjitsu](https://img.shields.io/badge/rocjitsu-84fb06b-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-rocm-rocjitsu/tags)
[![Guest Qcow2](https://img.shields.io/badge/guest-resolute%20%7C%20qm.b4eb552-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qcow2-gen-ernic-rocjitsu/tags)
[![Qemu Fork](https://img.shields.io/badge/sbates--fork-qemu.aa4344e-blue?style=flat-square)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork/tags)

<!-- What `qemu-tool quick-start` boots, from qemu/env/quickstart. One badge,
     carrying that file's own build stamp; the ernic and rocjitsu revisions
     it implies are the ones in that stamp's tags. -->
[![Quick-start Build](https://img.shields.io/badge/quick--start-20261022.gc533d98-success?style=flat-square&logo=docker)](https://hub.docker.com/r/sbates130272/batesste-ci-images-ubuntu-qcow2-gen-ernic-rocjitsu/tags)
<!-- The LMCache ref qemu/docker/lmcache/Dockerfile builds from. Not a
     batesste-ci-image: this one is built here and referenced by tag, so it
     carries no build stamp and sits outside the coherence check above. It
     still moves with that row in practice, because the thing it talks to is
     the ernic. Green rather than blue to say "ours, not theirs". -->
[![LMCache](https://img.shields.io/badge/LMCache-v0.5.6-success?style=flat-square)](https://github.com/LMCache/LMCache/releases/tag/v0.5.6)

## Summary

This repository provides a modern, cloud-init based
environment for running and testing QEMU-based and
Libvirt-based VMs. It's particularly well-suited for NVMe,
PCIe device passthrough, and libvfio-user testing, but can
be used for general-purpose VM creation for development and
testing.

**Key Features:**
- Fast VM creation using Ubuntu cloud images and cloud-init (Noble and Resolute)
- `qemu-tool` Python CLI with `run-vm`, `gen-vm`, `gen-compose`, `compose`,
  `ernic-stats` and `list` subcommands
- Bidirectional libvirt domain XML support (`--domain` input, `--convert-to-libvirt` output)
- NVMe device emulation with tracing support
- PCIe device passthrough (VFIO)
- CXL (Compute Express Link) device emulation
- Multi-architecture support (x86_64, ARM64, RISC-V)
- Declarative package management via manifests
- KVM acceleration support

## Quick Start: two GPU + RDMA VMs

The fastest way to something real: two guests, each with an emulated gfx1250,
an emulated NVMe and an emulated rocm-ernic RDMA NIC, with Prometheus and
Grafana beside them, plus a GPU test and an RDMA test across them. Nothing is
built — the guest comes from a published qcow2 that already carries the
mainline kernel, the ionic driver, rdma-core, perftest and ROCm 10.1, and the
device servers come from tagged CI images.

You need KVM, Docker with compose, Ansible, [`oras`](https://oras.land/) ≥ 1.3.4,
8 cores, 16 GB of RAM and about 30 GB of disk. Then:

```bash
uv tool install -e ./qemu                                  # or pipx, or the .deb -- see below
ansible-galaxy collection install -r ansible/requirements.yml  # needs git
qemu-tool check                                            # optional; see below
qemu-tool quick-start
```

`qemu-tool check` asks every one of those questions and a few more —
the Docker daemon and your access to it, the uid the containers will run as,
which settings file is in force, whether the images directory has room — and
prints a line per answer. It writes nothing and needs no `sudo`, so it is
safe on a host you have not decided to use yet, and it exits non-zero if
anything would stop a stack starting. You do not have to run it: it is the
same code `quick-start` runs in its own preflight, so skipping it only means
finding out a few seconds later.

One command, and it sizes the guests for whatever machine you ran it on
rather than asking you to. It shows you the plan — vCPU, RAM, where the
images are going, how much disk that needs — and asks before it does
anything; anything it cannot work out on its own it asks for too. Pass
`--yes` to take every default, which is also what it does when stdin is not a
terminal, so nothing hangs in a script.

The daily
[report-for-quickstart](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-quickstart.yml)
lane runs that same command — so what is documented here is what CI proves
every morning. It:

1. checks this machine: KVM actually usable (the ioctl, not just the device
   node), the tools on `PATH`, a writable images directory, free disk, and
   nothing already sitting on the ports it wants;
2. pulls the guest image (~2.5 GB, unpacking to ~9 GB — the long step) and
   its SSH keypair, which
   ships beside it as an ORAS referrer;
3. creates two overlays of one backing file and generates a two-VM stack from
   [`qemu/env/quickstart`](qemu/env/quickstart);
4. brings the stack up — per guest a `rocm-ernic` NIC server, a `rocjitsu` GPU
   server and a QEMU container, with the two NICs joined by a TCP mesh, plus
   Prometheus and Grafana;
5. stages the per-build `ip_discovery.bin`, loads `amdgpu` and gives each guest
   its address on the ernic L2 segment;
6. runs `vector_add` on each guest's GPU and `ib_send_bw` from guest 1 to
   guest 2, then checks the NIC's own byte counter moved — a passing
   `ib_send_bw` proves the verbs worked, only the counter proves the bytes
   crossed the mesh.

Measured on a 32-core workstation: **2 m 47 s** end to end once the backing
image is cached, and a first run is that plus however long pulling ~20 GB
takes you (about 3 minutes here). When it finishes it prints how to get in,
which is:

```bash
qemu-tool ssh quickstart-1                  # port, account and key resolved for you
qemu-tool ssh quickstart-1 -- ibv_devinfo   # or run one command and come back

# the same thing spelled out, if you would rather
ssh -i /var/lib/qemu-tool/quickstart-key/id_rsa -p 2222 ubuntu@localhost
```

and where to watch it:

| Service | Where |
|---|---|
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3000> — anonymous admin, so no login |

The ernic dashboard is provisioned from files and is under **Dashboards** in
the Grafana sidebar; its data is the stats dump each `rocm-ernic` writes,
scraped by the exporter. Both ports move automatically if something on your
machine already has them. `--no-metrics` leaves the pair out.

Useful variations — `qemu-tool quick-start <verb>`:

| Verb | What it does |
|---|---|
| `preflight` | check this machine and touch nothing |
| `up` | stop once the guests are booted |
| `test` | configure and test a stack that is already up |
| `down` | power the guests off and stop the stack, keeping the overlays |

`qemu-tool quick-start --help` has the rest, and `qemu-tool man` has all of
it. For more than two guests see [Scale-out fleet](#scale-out-fleet), which is
the same stack generator with a larger `VM_COUNT`.

## System-wide Install (Debian/Ubuntu)

Download the `.deb` from the [GitHub Releases](../../releases) page and install:

```bash
sudo apt install --no-install-recommends \
  ./python3-qemu-tool_*.deb \
  cloud-image-utils openssh-client wget
```

Use `apt` rather than `dpkg -i`: the package depends on a QEMU system emulator
and `qemu-utils`, and `dpkg` will not resolve those for you.

`--no-install-recommends` is worth the extra typing. Every `qemu-system-*`
package *recommends* `qemu-system-gui`, so a plain `apt install` drags in GTK,
SDL and the rest of a desktop display stack — 293 packages instead of 73 — none
of which a headless VM needs. No package can decline another package's
recommends, so the flag is the only way to say no. The three named packages are
what the flag would otherwise skip and `gen-vm` genuinely needs: `cloud-localds`,
`ssh` and `wget`.

Add `ansible` too if you intend to use `gen-vm --ansible-playbook`.

This installs `qemu-tool` to `/usr/bin/qemu-tool` and creates
`/var/lib/qemu-tool/images`. Where a `kvm` group exists — the normal case on a
host with QEMU installed — that directory is owned `root:kvm` with mode `2775`,
so the setgid bit keeps new images group-writable. Add yourself to the group if
you have not already:

```bash
sudo usermod -aG kvm $USER
# re-login for the group to take effect
```

On a system with no `kvm` group the install still succeeds, but the directory is
left `root`-owned and not group-writable, and the package says so. Once a `kvm`
group exists, apply the intended ownership yourself:

```bash
sudo chown root:kvm /var/lib/qemu-tool/images
sudo chmod 2775 /var/lib/qemu-tool/images
```

## Quick Start (qemu-tool)

Install from PyPI with [pipx](https://pipx.pypa.io/), which puts `qemu-tool` on
your `PATH` in its own isolated virtualenv:

```bash
sudo apt install -y pipx
pipx ensurepath          # adds ~/.local/bin to PATH; open a new shell after
pipx install qemu-tool
```

That gives you a working `gen-vm` and `run-vm`: both package manifests,
`env/example`, the man page and the compose stacks all ship inside the wheel,
so none of those depend on a checkout.

`qemu-tool compose` is the exception. The stacks are there, but each one
bind-mounts a qemu-tool source tree into its container and builds the tool
inside it, and neither a wheel nor the `.deb` is a source tree. To bring a
stack up, point `QEMU_TOOL_SRC` at a checkout:

```bash
QEMU_TOOL_SRC=/path/to/qemu-minimal qemu-tool compose --vm-name myvm up
```

To work on the tool itself, install the checkout in editable mode instead, so
edits take effect without reinstalling:

```bash
pipx install -e ./qemu   # from the repo root
```

What a pipx install still does not get is the parts that are install-tree
artifacts by nature — `/etc/qemu-tool/env`, `/var/lib/qemu-tool/images` and a
man page on your `MANPATH`. Those defaults fall back to per-user locations
rather than failing:

| Default | `.deb` | pipx / uv / venv |
|---|---|---|
| Settings file | `/etc/qemu-tool/env` | `$XDG_CONFIG_HOME/qemu-tool/env` |
| Images directory | `/var/lib/qemu-tool/images` | `$XDG_DATA_HOME/qemu-tool/images` |
| Package manifest | `/usr/share/qemu-tool/packages-default` | bundled in the wheel |
| Compose stacks | `/usr/share/qemu-tool/compose/` | bundled in the wheel |
| Manual page | `/usr/share/man/man1/` | bundled; read it with `qemu-tool man` |

The manual page is the one of those a fallback path cannot fix. Neither pipx
nor uv has any mechanism for installing onto `MANPATH` — the page ships as
package data either way — so `man qemu-tool` works only from the `.deb`.
`qemu-tool man` opens the bundled copy regardless of how the tool was
installed, and `qemu-tool man --path` prints where it is.

A per-user settings file **beats** the system one, as XDG expects: if you
wrote `$XDG_CONFIG_HOME/qemu-tool/env` under a pipx install and later install
the `.deb`, your file still wins and edits to `/etc/qemu-tool/env` are
ignored. Delete the per-user copy, or pass `--env-file`, to hand control back
to the system file. The images directory works the other way round but has
its own catch: the system path is preferred only when it is *writable*, so if
you are not in the `kvm` group you quietly get the per-user path instead of a
permission error. Pass `--images` to be explicit.

A plain virtualenv works too, if you prefer to activate it explicitly:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./qemu
```

If a build fails with `Cannot update time stamp of directory
'qemu_tool.egg-info'`, a previous root-owned build left an artifact behind.
Delete the `qemu/qemu_tool.egg-info` directory as root and retry.

Generate and run a Noble VM:

```bash
qemu-tool gen-vm --vm-name myvm --release resolute
qemu-tool run-vm --vm-name myvm
ssh -p 2222 ubuntu@localhost
```

Generate and run a Resolute VM:

```bash
qemu-tool gen-vm --vm-name myvm --release resolute
qemu-tool run-vm --vm-name myvm
```

Convert options to a libvirt domain XML:

```bash
qemu-tool run-vm --vm-name myvm --nvme 2 --convert-to-libvirt myvm.xml
virsh define myvm.xml && virsh start myvm
```

## Quick Start (Libvirt)

```bash
./libvirt/virt-install-ubuntu
```

## Directory Layout

```
qemu-minimal/
  qemu/
    pyproject.toml  Python package manifest for qemu-tool
    qemu_tool/      Python package (qemu-tool CLI)
    packages.d/     Cloud-init package manifests
    env/            Settings files -- `example` is the template, copy to qemu/.env
  libvirt/
    virt-install-ubuntu  Create VMs via libvirt
    create-nvme          Generate NVMe XML for libvirt
  ansible/
    playbooks/vm-basic.yml  Post cloud-init Ansible playbook
    requirements.yml        Galaxy collection requirements
  udev/
    99-qemu-minimal-vfio.rules  VFIO device permissions
    install-vfio-rules            Install the udev rules
  images/                VM images (created at runtime)
```

## VFIO PCI Passthrough

Bind the host device to `vfio-pci`, then pass its PCI address
to `run-vm` via `PCI_HOSTDEV`. QEMU must be able to open the
IOMMU group device under `/dev/vfio/N`; by default those
nodes are root-only.

Install the udev rules once (requires sudo). Your user must
be in the `kvm` group:

```bash
./udev/install-vfio-rules
```

Re-login after group changes. `qemu-tool run-vm` checks VFIO access
and prints this path if permissions are still wrong.

## Docker Compose: vfio-user GPU VM

For testing with emulated AMD GPUs over libvfio-user — specifically
[rocm-ernic][rocm-ernic] and [rocjitsu][rocjitsu] — Docker Compose
stacks under `qemu/compose/` start the GPU server containers and the
`qemu-system` VM in one command. Available stacks:

| Stack | VMs | ernic | rocjitsu |
|---|---|---|---|
| `vfio-user-ernic-vm/` | 1 | yes | no |
| `vfio-user-rocjitsu-vm/` | 1 | no | yes |
| `vfio-user-ernic-rocjitsu-vm/` | 1 | yes | yes |
| `vfio-user-ernic-2vm/` | 2 | yes (mesh) | opt-in per VM via `--profile` |
| `vfio-user-ernic-rocjitsu-scale-out/` | N (generated) | yes (mesh) | yes |

The first four are hand-written and committed. The last is generated from a
settings file by `qemu-tool gen-compose` — see [Scale-out fleet](#scale-out-fleet)
below.

This is the recommended path when you do not have a physical GPU to pass
through but need a guest that sees PCIe GPU devices.

```bash
# build a VM image first if you do not already have one
qemu-tool gen-vm --vm-name qemu-minimal

qemu-tool compose --vm-name qemu-minimal up
ssh -p 2222 ubuntu@localhost

qemu-tool compose --vm-name qemu-minimal down
```

`qemu-tool compose` is a wrapper around `docker compose` that sets
`VM_NAME` and `VM_IMAGES_DIR` automatically and locates the compose
stack whether running from source or an installed `.deb` package.
Pass any `docker compose` subcommand after the `qemu-tool` flags
(`up`, `down`, `ps`, `logs ernic`, etc.).

### Scale-out fleet

`qemu-tool gen-compose` generates an N-VM rocm-ernic mesh — one emulated RDMA
NIC and one rocjitsu GPU per VM — from the fleet keys in a settings file. Same
topology as `vfio-user-ernic-2vm`, sized by a variable rather than by hand.

```bash
qemu-tool gen-compose --env-file qemu/env/scale-out
qemu-tool compose --env-file qemu/env/scale-out \
    --stack vfio-user-ernic-rocjitsu-scale-out --profile metrics up -d
```

`qemu/env/scale-out` is the one authoritative fleet settings file. A host or a
CI lane that needs different values adds a small committed overlay on top of it
rather than a whole fourth copy to hand-sync — `--env-file` is repeatable and
later files win key by key:

```bash
qemu-tool gen-compose --env-file qemu/env/scale-out --env-file qemu/env/overlay-mi100 \
    --output-dir /var/lib/qemu-tool/stacks/mi100-fleet
qemu-tool compose --stack-dir /var/lib/qemu-tool/stacks/mi100-fleet \
    --env-file qemu/env/scale-out --env-file qemu/env/overlay-mi100 \
    --profile metrics up -d
```

Every later command against that fleet needs none of those flags. The
generated `docker-compose.yml` already records the layers it was rendered
from, so `compose` reads them back:

```bash
cd /var/lib/qemu-tool/stacks/mi100-fleet && qemu-tool compose ps
# or, from anywhere:
export QEMU_TOOL_STACK=/var/lib/qemu-tool/stacks/mi100-fleet
qemu-tool compose ps
```

A stack is found three ways, in this order: `--stack-dir`, then
`$QEMU_TOOL_STACK`, then the current directory when it is itself a rendered
stack — recognised by the generator's provenance header, not by the presence
of a `docker-compose.yml`, so an unrelated docker project is never adopted.
There is deliberately no walk-up: `docker compose` can walk up because what it
finds is what it runs, whereas here a walk-up from inside this checkout
reaches `qemu/compose/<default>/` and would drive the committed 8-VM stack
under the name of a 32-VM fleet.

`--env-file` still wins over the remembered layers, and the layers are
resolved honestly: the header records them as typed, so a relative name is
tried against the stack directory and the current directory and nothing else.
A layer that is not at either, or a generated stack whose header names no
layers at all, is an error naming the path and where it was looked for —
never a quiet fall-back to the search path, which is how a 32-VM fleet ends up
driven by an 8-VM `qemu/.env`.

Keys the overlay does not mention keep the golden file's value, so the overlay
is short enough to read in a diff. `compose` passes the same layers through to
`docker compose --env-file` in the same order, which applies the same last-wins
rule, so the tool and compose never see different values.

An overlay render needs both directory flags, and omitting either fails
quietly. `gen-compose` defaults its output to the committed stack directory, so
without `--output-dir` the overlay's fleet is written over the golden 8-VM one
and `Gen Compose Check` fails on the next push. And `VM_COUNT` and the VM names
are baked into the generated YAML rather than interpolated from the settings
file at run time, so without `--stack-dir` `compose` brings up that committed
8-VM stack whatever layers it is given.

Per-VM values are comma-lists, not numbered variables: `VM1_NAME`/`VM2_NAME`
does not survive eight VMs, let alone the mesh's sixty-four. A bare value
broadcasts to every VM, a list of length `VM_COUNT` applies positionally, and
any other length is an error naming the key and both lengths.

The exception matters, because getting it backwards fails silently: a key
naming a field that is *already* a list — `VM_PCI_HOSTDEV`, `VM_VFIO_USERDEV`,
`VM_EXTRA_HOSTFWD` — uses its commas for several values belonging to **one**
VM, so the whole value goes to **every** VM. `VM_EXTRA_HOSTFWD=a,b` is two
hostfwd rules per guest, not one rule each for two guests. Which case applies
is read off `VMConfig`'s own type hints rather than a hand-kept list of names.

`VM_VCPUS` must not drop below 4 on an ernic stack. `ionic_lif_size()` derives
its EQ count from `num_online_cpus()` and `ionic_create_rdma_admin()` rejects
fewer than `IONIC_EQ_COUNT_MIN`, so `ionic_rdma` never probes; the only symptom
is "Failed to register ibdev" in the guest log.

The generated `docker-compose.yml` records the hash of the settings file it was
built from, and `qemu-tool compose` warns on stderr when they disagree. It
never blocks: `down` against a stale tree is how you recover from a bad edit.
`gen-compose --check` turns the same comparison into a CI gate.

With `--profile metrics` the stack also brings up Prometheus and
`qemu-tool ernic-stats`, which serves the per-QP counters each `rocm-ernic -S`
writes. Full detail in
[the stack README](qemu/compose/vfio-user-ernic-rocjitsu-scale-out/README.md)
and [`qemu/env/scale-out`](qemu/env/scale-out), with the committed overlays in
[`qemu/env/overlay-ci`](qemu/env/overlay-ci) and
[`qemu/env/overlay-mi100`](qemu/env/overlay-mi100).

### Settings file

`qemu/env/example` is the single settings template for the whole tool. Copy it
to `qemu/.env` and edit; `gen-vm`, `run-vm` and `compose` all read the same
file, and `compose` hands it straight to `docker compose --env-file`.

```bash
cp qemu/env/example qemu/.env
$EDITOR qemu/.env
```

Every `VM_*` key maps to the flag of the same name -- `VM_VCPUS` is `--vcpus`,
`VM_IMAGES_DIR` is `--images`. Values are read lowest to highest from the
built-in defaults, a `--domain` XML, this file, then explicit CLI flags, so a
flag always wins over the file. One-shot actions (`--dry-run`, `--force`,
`--restore-image`, `--ansible-only`, `--nvme-recreate`) are deliberately not
readable from it.

The file is searched for in this order, first hit wins:

| Location | Used for |
|---|---|
| `--env-file FILE` | explicit, per invocation; repeatable, later files win key by key |
| `$QEMU_TOOL_ENV` | explicit, per shell |
| `./.env` | a per-project override |
| `qemu/.env` | a source checkout |
| `$XDG_CONFIG_HOME/qemu-tool/env` | a pipx or venv install (`~/.config` by default) |
| `/etc/qemu-tool/env` | an installed `.deb` |

The stack `README.md` files document the variables specific to each stack.

## Images Directory

When installed system-wide via the `.deb` package, `gen-vm` and `run-vm`
default to `/var/lib/qemu-tool/images` for storing VM disk images.
The directory is created by the package installer. Where a `kvm` group
exists it is given `root:kvm` ownership and mode `2775` (setgid), so any
member of that group can read and write images without `sudo`. On a host
with no `kvm` group the installer leaves it `root:root` and mode `755`
and prints the two commands to run once the group appears.

When using a source checkout (`pipx install -e ./qemu`), override the
default with `--images`:

```bash
qemu-tool gen-vm --vm-name myvm --images ./images
```

Cloud images are downloaded into the images directory, and the generated
qcow2 files (backing and final) are stored there.

## Package Manifests

The `packages.d/` directory contains YAML-formatted package
lists consumed by cloud-init during VM creation. Two
manifests are provided:

- **packages-default** -- a broad set of development and
  debugging packages. Installed to `/usr/share/qemu-tool/packages-default`
  by the `.deb` package and used as the default.
- **packages-minimal** -- a smaller set with just `emacs-nox`,
  `fio`, `sysstat`, and `tree`. Installed to
  `/usr/share/qemu-tool/packages-minimal` by the `.deb` package.

Both also ship inside the wheel, so a pipx install resolves the default
manifest without a checkout or a system install. `/usr/share/qemu-tool` wins
when it exists, so editing the installed copy still takes effect.

Select a manifest via the `--packages` flag:

```bash
# system install — use the installed minimal manifest or a custom path
qemu-tool gen-vm --packages /usr/share/qemu-tool/packages-minimal

# source checkout
qemu-tool gen-vm --packages qemu/packages.d/packages-minimal
```

Set `--packages none` to skip package installation entirely.

## Ansible Post-Setup

After cloud-init first boot, `gen-vm` can optionally run an
Ansible playbook from the top-level `ansible/` directory against
the backing image. This installs roles from the
[sbates130272.batesste][batesste-galaxy] Galaxy collection
(user setup, favourite packages, git configuration, and more).

The host must have `ansible`, `ansible-galaxy`, and the Python
`jmespath` module for the same interpreter as `ansible-playbook`
(install with `pip install jmespath` inside the venv when needed). When the collection is not already installed, `gen-vm` runs
`ansible-galaxy collection install --no-deps -r ansible/requirements.yml`.
`requirements.yml` lists every collection the playbooks need, including the
ones that would otherwise arrive as transitive dependencies, so `--no-deps`
skips roughly 10 MiB of collections this repository never uses.

One exception, temporary: `sbates130272.rocm_ernic` is pulled from the
upstream [rocm-ernic][rocm-ernic] git tree at a pinned SHA rather than from
Galaxy. Galaxy publishes only 0.1.0, the pre-ionic collection, and the ionic
work needs 0.2.0. This reverts to a normal Galaxy version pin as soon as
rocm-ernic publishes its next collection.

### Available playbooks

The playbooks are layered rather than independent: `vm-basic.yml` is the base
for every guest, and `vm-rocm.yml` is the base for every guest that needs ROCm.
Each one imports its parent, so the shared work has exactly one definition.

```text
vm-basic.yml                 user setup, favourite packages, git config
  |-- vm-rocm.yml            + apt pins, ROCm stack            [ROCm base]
  |     `-- vm-rocjitsu.yml  + mainline kernel, amdgpu patches, firmware
  `-- vm-ernic.yml           + ionic driver and RDMA prerequisites

vm-slim.yml                  imported as the last line of every leaf above
```

Every leaf ends by importing `vm-slim.yml`, which deletes the scratch the bake
left behind — the apt cache and package lists, DKMS build trees, archived
journals, `/tmp` and `/var/tmp` — and then `fstrim`s it back out of the qcow2.
Every playbook that *imports* another passes `image_slim_enable: false` on that
import, so exactly one slim pass runs per bake and it is the last play in the
run. The flag propagates through nested imports, so a leaf never has to know
how deep its own chain goes.

| Playbook | Imports | Description |
|---------|---------|-------------|
| `vm-basic.yml` | — | User setup, favourite packages, git config |
| `vm-rocm.yml` | `vm-basic.yml` | As above, plus the ROCm stack. The base for any ROCm guest |
| `vm-ernic.yml` | `vm-basic.yml` | [rocm-ernic][rocm-ernic] RDMA NIC prerequisites; `--tags configure` for post-boot NIC setup. Installs no ROCm |
| `vm-rocjitsu.yml` | `vm-rocm.yml` | ROCm + rocjitsu GPU firmware and driver prerequisites |
| `vm-ernic-rocjitsu.yml` | both of the above | Both ernic and rocjitsu prerequisites combined |
| `vm-slim.yml` | — | Reclaim bake scratch and `fstrim` it back to the qcow2. Imported last by every leaf; also runnable on its own against a baked guest |

```bash
qemu-tool gen-vm \
  --vm-name base \
  --ansible-playbook ansible/playbooks/vm-basic.yml
```

Ansible changes are written into the backing qcow2, so overlays
created with `--backing-file` inherit them. Ansible is skipped when
`--restore-image` or `--backing-file` is set.

## qemu-tool CLI Reference

`qemu-tool` is the primary interface. Both subcommands accept `--domain
<file.xml>` to load a libvirt domain XML as base configuration (CLI
flags take precedence over XML values).

### check flags

Checks the host and prints a report grouped by area — tools, Docker,
virtualisation, container identity, settings, capacity, images. Every line is
marked `ok`, `warn`, `FAIL` or `skip`. A `FAIL` stops a stack starting and
sets the exit status to 1; a `warn` limits what the host can do without
blocking it, and does not fail the command — a checker that cried wolf would
stop being run.

| Flag | Default | Description |
|------|---------|-------------|
| `--env-file FILE` | (search path) | Settings file to check against, repeatable, later files winning key by key. Which file the search path found is itself part of the report |

The container identity line is printed even when it is fine. "Which uid did
my fleet run as" used to have a surprising answer — every stack ran as root
until `CONTAINER_USER` became unconditional — so stating it is the point. See
[Settings file](#settings-file).

### down flags

Gracefully shuts down guests through the QEMU guest agent, then optionally
tears down the containing stack. `run-vm` execs QEMU, so `docker compose down`
is a power cut — `qemu-tool down` is what keeps guest filesystems clean.

| Flag | Default | Description |
|------|---------|-------------|
| `--all` | off | Power off all guests then tear down every owned stack. `sudo qemu-tool down --all` takes every stack on the system |
| `--dry-run` | off | Show what would happen without doing it |
| `--yes` | off | Skip the teardown confirmation. The prompt never defaults to yes, so this is the only way to proceed unattended |
| `--all-users` | off | With `--all`: include other users' stacks. Needs root |
| `--timeout SECONDS` | 120 | How long to wait for guests to power off |

Stack identity and ownership come from what compose records on each container,
so neither is a guess. **Is it a qemu-tool stack?** — the compose file carries
the `gen-compose` provenance header, or sits in one of the stack directories
this tool ships. **Is it yours?** — the uid its containers actually run as.

A stack started before the containers ran as the caller has every container as
root, so a non-root run sees it as root's and skips it; those are listed with
the reason rather than passed over. Run under `sudo` to include them.

### Shared flags (run-vm and gen-vm)

| Flag | Default | Description |
|------|---------|-------------|
| `--vm-name NAME` | `qemu-minimal` | VM name |
| `--arch {amd64,arm64,riscv64}` | `amd64` | Target architecture |
| `--vcpus N` | `2` | vCPU count |
| `--vmem MiB` | `4096` | Memory in MiB |
| `--images DIR` | `/var/lib/qemu-tool/images` | Image directory |
| `--ssh-port PORT` | `2222` | Host port forwarded to guest SSH |
| `--mac ADDR` | (from `--ssh-port`) | Management NIC MAC; set it for an image that pins its netplan to one, including images this repo built before `3969f07` |
| `--kvm / --no-kvm` | kvm | KVM acceleration |
| `--qemu-path PATH` | (system) | Directory containing QEMU binaries |
| `--domain FILE` | — | Libvirt XML base config (`-` for stdin) |

### gen-vm flags

| Flag | Default | Description |
|------|---------|-------------|
| `--release NAME` | `resolute` | Ubuntu codename (`resolute`, `noble`) or `XX.YY` |
| `--size GB` | `64` | Disk size in GB |
| `--username USER` | `ubuntu` | Guest username |
| `--password PASS` | `password` | Guest password |
| `--user-id UID` | `1000` | Guest UID |
| `--ssh-key-file FILE` | `~/.ssh/id_rsa.pub` | SSH public key to inject |
| `--packages FILE` | `packages.d/packages-default` | Package manifest or `none` |
| `--force` | off | Force re-download of cloud image |
| `--compact / --no-compact` | compact | Recompress the backing image after the bake |
| `--no-backing` | off | Create flat image without a backing file |
| `--restore-image` | off | Recreate overlay from existing backing file |
| `--backing-file FILE` | — | Create overlay on top of this existing qcow2 |
| `--backing-image OCI_REF` | — | Pull a published backing qcow2 with `oras`, then overlay on it |
| `--ansible-playbook FILE` | — | Path to an Ansible playbook to run against the VM image after cloud-init |

### run-vm flags

| Flag | Default | Description |
|------|---------|-------------|
| `--nvme VALUE` | — | NVMe: positive int=count, negative=null_blk, string=literal |
| `--nvme-trace EVENT` | — | NVMe tracing: `doorbell`, `all`, or event name |
| `--nvme-trace-file FILE` | — | Redirect trace output to file |
| `--nvme-lbaf-mask HEX` | — | 16-bit hex LBA format mask (e.g. `0x1f`) |
| `--nvme-recreate` | off | Delete and recreate NVMe qcow2 files on start |
| `--filesystem DIR` | — | Host directory to share via 9p/VirtFS |
| `--pci-testdev` | off | Enable `pci-testdev` |
| `--pci-hostdev BDF[,BDF]` | — | VFIO PCI passthrough (repeatable or comma-sep) |
| `--vram-dev-index N` | — | 1-based index into `--pci-hostdev` for VRAM DMA |
| `--vram-bar N` | `0` | BAR index on the VRAM device |
| `--vfio-userdev SOCK[,SOCK]` | — | libvfio-user socket paths |
| `--pci-mmio-bridge` | off | Enable `pci-mmio-bridge` for CXL-style testing |
| `--data-nic-queues N` | `0` | Multi-queue TAP NIC (queue count) |
| `--mcast-group IP:PORT` | — | Multicast socket NIC |
| `--qmp-socket [PATH]` | — | QMP socket (bare flag = auto path) |
| `--no-qemu-guest-agent` | off | Omit guest agent channel |
| `--backing-shared` | off | Disable image locking for shared backing files |
| `--extra-hostfwd RULE` | — | Extra hostfwd rule e.g. `tcp::9150-:9100` (repeatable) |
| `--dry-run` | off | Print QEMU command instead of running |
| `--convert-to-libvirt [FILE]` | — | Emit libvirt domain XML to FILE (default `<vm>.xml`) |

### gen-compose flags

| Flag | Default | Description |
|------|---------|-------------|
| `--stack NAME` | `vfio-user-ernic-rocjitsu-scale-out` | Stack to generate |
| `--env-file FILE` | search path | Settings file (see `qemu/env/scale-out`). Repeatable: later files override keys set by earlier ones |
| `--output-dir DIR` | the stack directory | Where to write |
| `--check` | off | Exit non-zero if regenerating would change anything; writes nothing |
| `--dry-run` | off | Print the generated files instead of writing them |

`--output-dir` will not default into `/usr/share/qemu-tool/compose`; that tree
belongs to the Debian package, so overwriting it has to be asked for by name.
In a checkout it defaults to the committed stack directory instead, so any
render that layers an overlay must name an `--output-dir` of its own or it
overwrites the committed fleet and breaks `--check`.

### compose flags

| Flag | Default | Description |
|------|---------|-------------|
| `--stack NAME` | `vfio-user-ernic-rocjitsu-vm` | Committed stack to run |
| `--stack-dir DIR` | `$QEMU_TOOL_STACK`, else the cwd if it is a rendered stack | Run a stack rendered by `gen-compose --output-dir DIR`. Mutually exclusive with `--stack` |
| `--vm-name NAME` | from settings | Sets `VM_NAME`/`VM1_NAME` |
| `--vm2-name NAME` | from settings | Sets `VM2_NAME`, for the 2-VM stacks |
| `--images DIR` | from settings | Sets `VM_IMAGES_DIR` |
| `--env-file FILE` | the stack's recorded layers, else the search path | Settings file, passed through to `docker compose --env-file`. Repeatable: later files win |

Everything else on the line goes to `docker compose` in the order typed, so
`--profile metrics up -d` needs no separator. A `--` before the subcommand is
still accepted and dropped, because `docker compose` reads one as end-of-options
and would silently ignore the profile that followed it.

Two things `compose` deliberately does not do, both of which a fleet wrapper
script used to:

- **It never regenerates, and never edits a settings file.** Resizing a fleet
  is `gen-compose --output-dir` after editing `VM_COUNT` yourself. Those files
  are tracked and hand-written, with the reasoning for each value in the
  comments above it; a tool that rewrites `VM_COUNT` as a side effect of `up`
  can write it into `qemu/env/scale-out` and break `Gen Compose Check` from a
  command that looks like a start.
- **It is not a command runner.** `ssh`/`each` over a fleet is

  ```bash
  ansible -i <stack>/fleet-inventory.yml fleet -f <N> -m raw \
      --private-key <key> -a '…'
  ```

  `gen-compose` renders that inventory beside the compose file, with every
  host, port and user, and ansible runs the fleet in parallel with real exit
  codes. `--private-key` is not optional: the generated inventory carries the
  host, port and user but deliberately no key path, because the key is not a
  property of the fleet — without it ansible reports every guest `UNREACHABLE`.
  For one guest, `qemu-tool inspect --vm-name <name>` prints a complete `ssh`
  command, with the key it verified the guest actually accepts.
- **Readiness is partly covered, and the gap is worth knowing.**
  `qemu-tool inspect --vm-name <name>` reports whether the guest agent answers
  and lists the guest's addressed interfaces, which is enough to tell a booted
  guest from a running QEMU. It does **not** identify the ernic: it prints
  whatever `guest-network-get-interfaces` returns, so the ernic appears only
  once it has an address, and only under whatever name udev gave it. "RDMA
  ready" — `rocm-ernic0` present, addressed, and `rocm-rdma-ernic0`
  `PORT_ACTIVE` — is not something the tool asserts today. The read-only
  guest-agent boundary is what makes what it does report safe to run against
  someone else's live fleet.

`down -v` is worth typing in full: without `-v` a killed run leaves its
sockets in the shared `/run/vfu` volume and the next `up` dies on
`cannot serve /run/vfu/rocjitsu-1.sock: Address already in use`, surfacing as
`dependency failed to start: container … is unhealthy`.

### ernic-stats flags

| Flag | Default | Description |
|------|---------|-------------|
| `--stats-dir DIR` | `/run/ernic-stats` | Directory of `*.stats` files |
| `--port PORT` | `9840` | Listen port |
| `--addr ADDR` | `0.0.0.0` | Listen address |

Runs inside the generated stack; rarely invoked by hand. Metric and label names
match rocm-ernic's own `prometheus/ernic-exporter`, so that project's Grafana
dashboard works against this endpoint unmodified.

### list flags

| Flag | Default | Description |
|------|---------|-------------|
| `--json` | off | Emit JSON instead of a table |
| `--qemu-tool-only` | off | Omit VMs not started by qemu-tool |

`qemu-tool list` shows every `qemu-system-*` process on the node — including
VMs running inside containers, which it attributes back to the container name —
and says which ones qemu-tool started:

```console
$ qemu-tool list
PID    NAME         SOURCE            ARCH   VCPU  MEM    SSH   KVM  UPTIME  CONTAINER                     NVME      VFIO-USER
37374  rocjitsu-vm  external          amd64  4     8192M  2222  yes  53m     vfio-user-rocjitsu-vm-qemu-1  00:05.0   rocjitsu-1.sock
79805  marker-test  qemu-tool/run-vm  amd64  1     1024M  2223  yes  0m      -                             -         -
```

#### The NVME column

Every NVMe attached to the VM, emulated or passed through, comma-joined, with
identical labels collapsed as `label*N` (`VM_NVME=2` is ordinary, and two
identical labels say no more than one plus a count). `-` when the VM has none.

| Entry | Meaning |
|-------|---------|
| `00:05.0` | **Guest** BDF of an emulated NVMe, read from the VM's QMP monitor. The `0000:` domain is dropped for table width; `--json` keeps the canonical form |
| `emul` | An emulated NVMe whose guest BDF is not knowable — the VM has no QMP socket, or the query failed |
| `host:03:00.0` | A passed-through device, by its **host** BDF |
| `host:03:00.0?` | The same, with no sysfs entry on this host to confirm it is an NVMe |

Examples: `emul*2`, `emul,host:03:00.0`, `00:05.0,00:06.0`, `-`.

**A host BDF and a guest BDF are different address spaces**, which is why a
passthrough entry is labelled rather than printed bare: `host:03:00.0` is the
address on *this* machine, the address `lspci` inside the guest reports is a
different one, and sending someone to the wrong namespace's device is worse
than telling them nothing. Only the guest BDF is actionable inside the VM, and
it is only available when the VM was started with `--qmp-socket` — without a
monitor to ask, an emulated NVMe is reported as `emul` and nothing more,
because `run-vm` emits `-device nvme,...` with no `addr=` and QEMU picks the
slot at realize time, so the guest address never appears on the command line.

Passthrough entries come from `-device vfio-pci,host=...`, which is also how a
GPU or a NIC is passed through, so the device class is checked in
`/sys/bus/pci/devices/<bdf>/class` and only `0x0108xx` (NVM Express) is
claimed. A BDF this host has no sysfs entry for is reported with a trailing
`?` rather than dropped — dropping would silently hide a real NVMe.

`--json` adds the disk image path, full vfio-user socket paths, owning user and
uptime in seconds, plus an `nvme` array of objects per VM — `bdf` (canonical
`dddd:bb:dd.f`, or `null` for an emulated device whose guest BDF is unknown),
`kind` (`emulated` or `passthrough`), `source` (`argv` or `qmp`, i.e. whether
the BDF was upgraded from the monitor), `qdev_id`, and `serial` or
`class_verified` respectively:

```bash
# PID of a VM by name
qemu-tool list --json | jq -r '.[] | select(.name=="myvm") | .pid'
```

#### How VMs are identified

qemu-tool stamps every VM it launches with two generic QEMU options:

```
-name guest=<vm-name>,debug-threads=on
-uuid <uuid5(namespace, "<role>:<vm-name>")>
```

`list` recomputes the UUID from the parsed guest name and compares it against
the `-uuid` on the command line. An exact match identifies the VM as
qemu-tool's and reveals which subcommand started it, so a transient `gen-vm`
image-build VM is distinguishable from a running `run-vm` VM.

Both options are generic rather than machine-specific, so they work on all
supported architectures — including riscv64, where `-smbios` would not.

VMs shown as `external` were either started by something other than qemu-tool,
or by a version predating these markers; restart such a VM for it to be
identified. This is operator convenience, not a security boundary — the markers
are ordinary command-line arguments and anyone can set them.

### Libvirt XML round-trip

`--convert-to-libvirt` emits a domain XML that can be fed back as
`--domain` input to reproduce the identical QEMU command. QEMU-specific
features not natively representable in libvirt (NVMe emulation, tracing,
libvfio-user, pci-mmio-bridge) are stored as `<qemu:commandline>` entries
and round-trip hint attributes.

```bash
# Emit XML
qemu-tool run-vm --vm-name myvm --nvme 2 \
  --convert-to-libvirt myvm.xml

# Parse XML back and run
qemu-tool run-vm --domain myvm.xml

# Define and start in libvirt instead
virsh define myvm.xml && virsh start myvm
```

### --restore-image

When `--restore-image` is set, `qemu-tool gen-vm` skips cloud-init and
only recreates the final qcow2 from the existing backing file. Useful when
the VM image is corrupted or deleted but the backing file remains.

### --backing-file

When `--backing-file FILE` is set, `qemu-tool gen-vm` creates
`<vm-name>.qcow2` as a thin overlay on top of the specified file. Cloud-init
provisioning is skipped — the backing file must already be provisioned (e.g.
by a prior `gen-vm` run). See [Shared Backing Files](#shared-backing-files)
for a full workflow example.

## Shared Backing Files

Multiple VMs can share a single read-only backing file.
Each VM gets its own thin overlay where all writes land,
so the backing file is never modified after provisioning.

1. Create the base VM (runs cloud-init and provisions the
   backing file):

```bash
qemu-tool gen-vm --vm-name base
```

2. Create per-VM overlays from the shared backing file:

```bash
qemu-tool gen-vm --vm-name vm1 \
  --backing-file images/base-backing.qcow2
qemu-tool gen-vm --vm-name vm2 \
  --backing-file images/base-backing.qcow2
```

3. Run each VM with `--backing-shared` and a unique
   `--ssh-port` so QEMU disables file locking on the
   backing chain:

```bash
qemu-tool run-vm --vm-name vm1 \
  --ssh-port 2222 --backing-shared &
qemu-tool run-vm --vm-name vm2 \
  --ssh-port 2223 --backing-shared &
```

`--backing-shared` adds `file.locking=off` and
`backing.file.locking=off` to the root disk `-drive`.
The first disables QEMU's OFD locking on the overlay;
the second disables it on the backing file that QEMU
opens internally. Both are needed to prevent lock
conflicts across instances sharing the same backing
file. Each VM must still use a distinct `--vm-name` (and
therefore a distinct overlay) to avoid data corruption.

<!-- References -->

[batesste-galaxy]: https://galaxy.ansible.com/ui/repo/published/sbates130272/batesste/
[rocm-ernic]: https://github.com/ROCm/rocm-ernic
[rocjitsu]: https://github.com/sbates130272/batesste-ci-images
