# QEMU and Libvirt Tooling

## Summary

This repository provides a modern, cloud-init based
environment for running and testing QEMU-based and
Libvirt-based VMs. It's particularly well-suited for NVMe,
PCIe device passthrough, and libvfio-user testing, but can
be used for general-purpose VM creation for development and
testing.

**Key Features:**
- Fast VM creation using Ubuntu cloud images and cloud-init
- NVMe device emulation with tracing support
- PCIe device passthrough (VFIO)
- CXL (Compute Express Link) device emulation
- Multi-architecture support (x86_64, ARM64, RISC-V)
- Declarative package management via manifests
- KVM acceleration support

## Quick Start (QEMU)

```bash
cd qemu
./gen-vm
```

This creates an Ubuntu Resolute VM named `qemu-minimal`
with default settings. To run the VM:

```bash
./run-vm
```

SSH into the VM:

```bash
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
    gen-vm          Create VM disk images via cloud-init
    run-vm          Run a VM created by gen-vm
  libvirt/
    virt-install-ubuntu  Create VMs via libvirt
    create-nvme          Generate NVMe XML for libvirt
  packages.d/
    packages-default     Default cloud-init package set
    packages-minimal     Minimal cloud-init package set
  images/                VM images (created at runtime)
```

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

Select a manifest via the `PACKAGES` environment variable:

```bash
PACKAGES=../packages.d/packages-minimal ./gen-vm
```

Set `PACKAGES=none` to skip package installation entirely.

## Environment Variables (gen-vm)

| Variable | Default | Description |
|----------|---------|-------------|
| `VM_NAME` | `qemu-minimal` | Name for the VM |
| `ARCH` | `amd64` | Architecture (`amd64`, `arm64`, `riscv64`) |
| `RELEASE` | `resolute` | Ubuntu release codename |
| `SIZE` | `64` | Disk size in GB |
| `VCPUS` | `2` | Number of vCPUs |
| `VMEM` | `4096` | Memory in MB |
| `PACKAGES` | `../packages.d/packages-default` | Package manifest file or `none` |
| `KVM` | `enable` | Set to anything else to disable KVM |
| `QEMU_PATH` | (empty) | Prefix path to QEMU binaries |
| `SSH_PORT` | `2222` | Host port forwarded to guest SSH |
| `USERNAME` | `ubuntu` | Guest username |
| `PASS` | `password` | Guest password |
| `SSH_KEY_FILE` | `$HOME/.ssh/id_rsa.pub` | SSH public key to inject |
| `FORCE` | `false` | Force re-download of cloud image |
| `NO_BACKING` | `false` | Create image without backing file |
| `RESTORE_IMAGE` | `false` | Recreate image from existing backing file |
| `BACKING_FILE` | (empty) | Path to existing backing qcow2 for overlay creation |

### RESTORE_IMAGE

When `RESTORE_IMAGE=true`, `gen-vm` skips cloud-init and
only recreates the final qcow2 from the existing backing
file. This is useful when the VM image is corrupted or
deleted but the backing file remains. The script verifies
that the backing file exists and that QEMU is not currently
using it before restoring.

### BACKING_FILE

When `BACKING_FILE` is set to a path, `gen-vm` skips the
cloud image download, cloud-init provisioning, and first
boot entirely. Instead it creates `${VM_NAME}.qcow2` as a
thin qcow2 overlay on top of the specified backing file.
This allows multiple VMs to share a single read-only
backing image, with per-VM diffs written to their own
overlays. See [Shared Backing Files](#shared-backing-files)
for a full workflow example.

## Environment Variables (run-vm)

| Variable | Default | Description |
|----------|---------|-------------|
| `VM_NAME` | `qemu-minimal` | Name of the VM to run |
| `ARCH` | `amd64` | Architecture |
| `VCPUS` | `2` | Number of vCPUs |
| `VMEM` | `4096` | Memory in MB |
| `KVM` | `enable` | KVM acceleration |
| `QEMU_PATH` | (empty) | Prefix path to QEMU binaries |
| `IMAGES` | `../images` | Directory containing VM images |
| `SSH_PORT` | `2222` | Host port forwarded to guest SSH |
| `FILESYSTEM` | `none` | Host directory to share via 9p/VirtFS |
| `NVME` | `none` | NVMe config (count, string, or negative for null_blk) |
| `NVME_TRACE` | `none` | NVMe tracing (`doorbell`, `all`, or event name) |
| `NVME_TRACE_FILE` | (empty) | Redirect trace output to a file |
| `PCI_HOSTDEV` | `none` | Comma-separated PCI addresses for VFIO passthrough |
| `VFIO_USERDEV` | `none` | libvfio-user sockets (each under a root-port) |
| `PCI_MMIO_BRIDGE` | `none` | pci-mmio-bridge for CXL-style testing |
| `PCI_TESTDEV` | `none` | Enable pci-testdev |
| `DATA_NIC_QUEUES` | `0` | Multi-queue virtio-net TAP NIC (queue count) |
| `MCAST_GROUP` | `none` | Multicast socket NIC (`230.0.0.1:1234`) |
| `QMP_SOCKET` | `false` | QMP socket (`true` for default, or path) |
| `DRY_RUN` | `none` | Print QEMU command instead of executing |
| `BACKING_SHARED` | `false` | Disable image locking for shared backing files |

## Shared Backing Files

Multiple VMs can share a single read-only backing file.
Each VM gets its own thin overlay where all writes land,
so the backing file is never modified after provisioning.

1. Create the base VM (runs cloud-init and provisions the
   backing file):

```bash
cd qemu
VM_NAME=base ./gen-vm
```

2. Create per-VM overlays from the shared backing file:

```bash
BACKING_FILE=../images/base-backing.qcow2 \
  VM_NAME=vm1 ./gen-vm
BACKING_FILE=../images/base-backing.qcow2 \
  VM_NAME=vm2 ./gen-vm
```

3. Run each VM with `BACKING_SHARED=true` and a unique
   `SSH_PORT` so QEMU disables file locking on the
   backing chain:

```bash
BACKING_SHARED=true VM_NAME=vm1 \
  SSH_PORT=2222 ./run-vm &
BACKING_SHARED=true VM_NAME=vm2 \
  SSH_PORT=2223 ./run-vm &
```

`BACKING_SHARED=true` adds `file.locking=off` and
`backing.file.locking=off` to the root disk `-drive`.
The first disables QEMU's OFD locking on the overlay;
the second disables it on the backing file that QEMU
opens internally. Both are needed to prevent lock
conflicts across instances sharing the same backing
file. Each VM must still use a distinct `VM_NAME` (and
therefore a distinct overlay) to avoid data corruption.
