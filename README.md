# QEMU and Libvirt Tooling

[![GitHub Stars](https://img.shields.io/github/stars/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/issues)
[![Last Commit](https://img.shields.io/github/last-commit/sbates130272/qemu-minimal?style=flat-square)](https://github.com/sbates130272/qemu-minimal/commits/main)
[![Platform](https://img.shields.io/badge/platform-x86__64%20%7C%20ARM64%20%7C%20RISC--V-blue?style=flat-square)](https://github.com/sbates130272/qemu-minimal)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-Noble%20%7C%20Resolute-orange?style=flat-square&logo=ubuntu)](https://releases.ubuntu.com/noble/)

## Summary

This repository provides a modern, cloud-init based
environment for running and testing QEMU-based and
Libvirt-based VMs. It's particularly well-suited for NVMe,
PCIe device passthrough, and libvfio-user testing, but can
be used for general-purpose VM creation for development and
testing.

**Key Features:**
- Fast VM creation using Ubuntu cloud images and cloud-init (Noble and Resolute)
- `qemu-tool` Python CLI with `run-vm` and `gen-vm` subcommands
- Bidirectional libvirt domain XML support (`--domain` input, `--convert-to-libvirt` output)
- NVMe device emulation with tracing support
- PCIe device passthrough (VFIO)
- CXL (Compute Express Link) device emulation
- Multi-architecture support (x86_64, ARM64, RISC-V)
- Declarative package management via manifests
- KVM acceleration support

## Quick Start (qemu-tool)

Install the tool (from the repo root):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./qemu
```

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

## Quick Start (Legacy bash scripts)

> **Deprecated:** `gen-vm` and `run-vm` bash scripts emit a deprecation
> warning and will be removed in a future release. Use `qemu-tool` instead.

```bash
cd qemu
./gen-vm
./run-vm
ssh -p 2222 ubuntu@localhost
# Password: password (or use SSH key)
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
    gen-vm          Legacy bash script (deprecated)
    run-vm          Legacy bash script (deprecated)
  libvirt/
    virt-install-ubuntu  Create VMs via libvirt
    create-nvme          Generate NVMe XML for libvirt
  packages.d/
    packages-default     Default cloud-init package set
    packages-minimal     Minimal cloud-init package set
  ansible/
    playbooks/vm-setup.yml  Post cloud-init Ansible playbook
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

Re-login after group changes. `run-vm` checks VFIO access and
prints this path if permissions are still wrong.

## Images Directory

Both `gen-vm` and `run-vm` default to `../images` (relative
to `qemu/`) for storing and locating VM disk images. This
directory is created automatically by `gen-vm` and is
gitignored. Cloud images are downloaded here, and the
generated qcow2 files (backing and final) are stored here.

## Package Manifests

The `packages.d/` directory contains YAML-formatted package
lists consumed by cloud-init during VM creation. Two
manifests are provided:

- **packages-default** -- a broad set of development and
  debugging packages.
- **packages-minimal** -- a smaller set with just `emacs-nox`,
  `fio`, `sysstat`, and `tree`.

Select a manifest via the `--packages` flag:

```bash
qemu-tool gen-vm --packages ../packages.d/packages-minimal
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
`ansible-galaxy collection install -r ansible/requirements.yml`.

Customize the default role list in
[`ansible/playbooks/vm-setup.yml`](ansible/playbooks/vm-setup.yml).

```bash
qemu-tool gen-vm \
  --vm-name base \
  --ansible-profile ../ansible/profiles/vm-setup
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
| `--images DIR` | `../images` | Image directory |
| `--ssh-port PORT` | `2222` | Host port forwarded to guest SSH |
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
| `--packages FILE` | `../packages.d/packages-default` | Package manifest or `none` |
| `--force` | off | Force re-download of cloud image |
| `--no-backing` | off | Create flat image without a backing file |
| `--restore-image` | off | Recreate overlay from existing backing file |
| `--backing-file FILE` | — | Create overlay on top of this existing qcow2 |
| `--ansible-profile FILE` | — | Path to Ansible profile file for post-setup |

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
