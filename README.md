# QEMU and Libvirt Tooling

[![GitHub Stars](https://img.shields.io/github/stars/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/issues)
[![Last Commit](https://img.shields.io/github/last-commit/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/commits/main)
[![Platform](https://img.shields.io/badge/platform-x86__64%20%7C%20ARM64%20%7C%20RISC--V-blue?style=flat-square)](https://github.com/sbates130272/qemu-minimal)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-Noble%20%7C%20Resolute-orange?style=flat-square&logo=ubuntu)](https://releases.ubuntu.com/noble/)
[![GitHub Release](https://img.shields.io/github/v/release/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/releases/latest)
[![VM Report](https://img.shields.io/badge/VM%20Report-live-blue?style=flat-square)](https://sbates130272.github.io/qemu-minimal/)
[![rocjitsu GEMM](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-gemm.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![rocjitsu hipFile](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![hipFile fio](https://img.shields.io/endpoint?url=https%3A%2F%2Fsbates130272.github.io%2Fqemu-minimal%2Fperf%2Fbadge-hipfile-fio.json&style=flat-square)](https://sbates130272.github.io/qemu-minimal/perf/)
[![qemu-tool Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-smoke-test-qemu-tool.yml?branch=main&label=qemu-tool-smoke&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-smoke-test-qemu-tool.yml)
[![Dry-Run Tests](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-dry-run-qemu-tool.yml?branch=main&label=dry-run&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-dry-run-qemu-tool.yml)
[![Ansible Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-ansible-setup-test.yml?branch=main&label=ansible&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-ansible-setup-test.yml)
[![Shell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-shell-check.yml?branch=main&label=shell-check&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-shell-check.yml)
[![Spell Check](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-spell-check.yml?branch=main&label=spell-check&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-spell-check.yml)
[![rocm-ernic Smoke Test](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-smoke-test-rocm-ernic.yml?branch=main&label=smoke-test-rocm-ernic&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-smoke-test-rocm-ernic.yml)
[![ernic Report](https://img.shields.io/github/actions/workflow/status/sbates130272/qemu-minimal/qemu-minimal-report-for-vm-ernic.yml?branch=main&label=report-for-vm-ernic&style=flat-square)](https://github.com/sbates130272/qemu-minimal/actions/workflows/qemu-minimal-report-for-vm-ernic.yml)

## Summary

This repository provides a modern, cloud-init based
environment for running and testing QEMU-based and
Libvirt-based VMs. It's particularly well-suited for NVMe,
PCIe device passthrough, and libvfio-user testing, but can
be used for general-purpose VM creation for development and
testing.

**Key Features:**
- Fast VM creation using Ubuntu cloud images and cloud-init (Noble and Resolute)
- `qemu-tool` Python CLI with `run-vm`, `gen-vm`, `compose`, and `list` subcommands
- Bidirectional libvirt domain XML support (`--domain` input, `--convert-to-libvirt` output)
- NVMe device emulation with tracing support
- PCIe device passthrough (VFIO)
- CXL (Compute Express Link) device emulation
- Multi-architecture support (x86_64, ARM64, RISC-V)
- Declarative package management via manifests
- KVM acceleration support

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
`env.example`, the man page and the compose stacks all ship inside the wheel,
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

| Default | `.deb` | pipx / venv |
|---|---|---|
| Settings file | `/etc/qemu-tool/env` | `$XDG_CONFIG_HOME/qemu-tool/env` |
| Images directory | `/var/lib/qemu-tool/images` | `$XDG_DATA_HOME/qemu-tool/images` |
| Package manifest | `/usr/share/qemu-tool/packages-default` | bundled in the wheel |
| Compose stacks | `/usr/share/qemu-tool/compose/` | bundled in the wheel |

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
qemu-tool gen-vm --vm-name myvm --release noble
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
    env.example     Settings template -- copy to qemu/.env
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

### Settings file

`qemu/env.example` is the single settings template for the whole tool. Copy it
to `qemu/.env` and edit; `gen-vm`, `run-vm` and `compose` all read the same
file, and `compose` hands it straight to `docker compose --env-file`.

```bash
cp qemu/env.example qemu/.env
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
| `--env-file FILE` | explicit, per invocation |
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

| Playbook | Description |
|---------|-------------|
| `vm-basic.yml` | User setup, favourite packages, git config |
| `vm-rocm.yml` | As above, plus ROCm stack |
| `vm-ernic.yml` | ROCm + [rocm-ernic][rocm-ernic] RDMA NIC prerequisites; `--tags configure` for post-boot NIC setup |
| `vm-rocjitsu.yml` | ROCm + rocjitsu GPU firmware and driver prerequisites |
| `vm-ernic-rocjitsu.yml` | Both ernic and rocjitsu prerequisites combined |

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
| `--release NAME` | `noble` | Ubuntu codename (`noble`, `resolute`) or `XX.YY` |
| `--size GB` | `64` | Disk size in GB |
| `--username USER` | `ubuntu` | Guest username |
| `--password PASS` | `password` | Guest password |
| `--user-id UID` | `1000` | Guest UID |
| `--ssh-key-file FILE` | `~/.ssh/id_rsa.pub` | SSH public key to inject |
| `--packages FILE` | `packages.d/packages-default` | Package manifest or `none` |
| `--force` | off | Force re-download of cloud image |
| `--no-backing` | off | Create flat image without a backing file |
| `--restore-image` | off | Recreate overlay from existing backing file |
| `--backing-file FILE` | — | Create overlay on top of this existing qcow2 |
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
PID    NAME         SOURCE            ARCH   VCPU  MEM    SSH   KVM  UPTIME  CONTAINER                     VFIO-USER
37374  rocjitsu-vm  external          amd64  4     8192M  2222  yes  53m     vfio-user-rocjitsu-vm-qemu-1  rocjitsu-1.sock
79805  marker-test  qemu-tool/run-vm  amd64  1     1024M  2223  yes  0m      -                             -
```

`--json` adds the disk image path, full vfio-user socket paths, owning user and
uptime in seconds:

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
