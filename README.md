# Minimal QEMU Environment

## Summary

This repository provides a modern, cloud-init based environment for running and
testing QEMU VMs. It's particularly well-suited for NVMe, PCIe device
passthrough, and CXL emulation testing, but can be used for general-purpose VM
management.

**Key Features:**
- 🚀 Fast VM creation using Ubuntu cloud images and cloud-init
- 💾 NVMe device emulation with tracing support
- 🔌 PCIe device passthrough (VFIO)
- 🔧 CXL (Compute Express Link) device emulation
- 🏗️ Multi-architecture support (x86_64, ARM64, RISC-V)
- 📦 Declarative package management via manifests
- ⚡ KVM acceleration support

## Quick Start

### Creating Your First VM

```bash
cd qemu
./gen-vm
```

This creates an Ubuntu Noble VM named `qemu-minimal` with default settings.

### Running the VM

```bash
./run-vm
```

### SSH into the VM

```bash
ssh -p 2222 ubuntu@localhost
# Password: password (or use SSH key)
```

That's it! 🎉

## Documentation

- **[qemu/README.md](qemu/README.md)** - Comprehensive guide to gen-vm, run-vm,
  and related scripts
- **[MIGRATION.md](MIGRATION.md)** - Migration guide from legacy scripts
- **[CLEANUP_PROPOSAL.md](CLEANUP_PROPOSAL.md)** - Repository cleanup details
  and rationale

## Modern Workflow

### VM Generation with gen-vm

The `qemu/gen-vm` script creates VMs using Ubuntu cloud images and cloud-init:

```bash
cd qemu

# Basic VM
./gen-vm

# Custom configuration
VM_NAME=dev VCPUS=4 VMEM=8192 SIZE=128 \
  PACKAGES=../packages.d/packages-default ./gen-vm

# Different architecture
ARCH=arm64 ./gen-vm
ARCH=riscv64 ./gen-vm

# Different Ubuntu release
RELEASE=jammy ./gen-vm
```

### VM Execution with run-vm

The `qemu/run-vm` script runs VMs with flexible hardware configuration:

```bash
# Basic execution
./run-vm

# With NVMe devices
NVME=4 ./run-vm

# With NVMe tracing
NVME=4 NVME_TRACE=doorbell NVME_TRACE_FILE=/tmp/trace.log ./run-vm

# With shared filesystem
FILESYSTEM=/home/$USER/Projects ./run-vm

# With PCIe device passthrough
PCI_HOSTDEV=0000:03:00.0 ./run-vm
```

See [qemu/README.md](qemu/README.md) for complete documentation.

## NVMe Testing

### Emulated NVMe Devices

Create multiple NVMe SSDs for testing:

```bash
cd qemu
VM_NAME=nvme-test ./gen-vm
NVME=4 VM_NAME=nvme-test ./run-vm
```

In the VM:
```bash
sudo nvme list
# Shows 4 NVMe devices
```

### NVMe Tracing

Enable detailed tracing for debugging:

```bash
# Trace doorbell operations
NVME=4 NVME_TRACE=doorbell ./run-vm

# Trace to file for analysis
NVME=4 NVME_TRACE=doorbell NVME_TRACE_FILE=/tmp/nvme.log ./run-vm

# Trace all NVMe events
NVME=4 NVME_TRACE=all ./run-vm
```

### Hardware NVMe Passthrough

Pass through real NVMe devices to the VM:

```bash
# Find device
lspci | grep NVMe
# Example: 03:00.0 Non-Volatile memory controller

# Bind to vfio-pci
cd qemu
sudo HOST_ADDR=0000:03:00.0 ./vfio-setup

# Pass to VM
PCI_HOSTDEV=0000:03:00.0 ./run-vm
```

## CXL Device Emulation

Test CXL (Compute Express Link) devices:

```bash
cd qemu
VM_NAME=cxl-test ./gen-vm
VM_NAME=cxl-test ./run-vm-cxl-nvme
```

This creates a VM with:
- CXL bus topology
- CXL-attached memory device
- NVMe device on CXL fabric

## Architecture Support

Create VMs for different architectures:

```bash
cd qemu

# ARM64 / AArch64
ARCH=arm64 VM_NAME=arm-test ./gen-vm
ARCH=arm64 VM_NAME=arm-test ./run-vm

# RISC-V 64-bit
ARCH=riscv64 VM_NAME=riscv-test ./gen-vm
ARCH=riscv64 VM_NAME=riscv-test ./run-vm

# x86_64 (default)
ARCH=amd64 ./gen-vm
```

Architecture-specific notes:
- **x86_64**: Full KVM support
- **ARM64**: Requires UEFI firmware (qemu-efi-aarch64)
- **RISC-V**: Requires U-Boot (u-boot-qemu)

## Package Management

Customize installed packages using manifest files:

```bash
# Use default packages (development tools, fio, nvme-cli, etc.)
PACKAGES=../packages.d/packages-default ./gen-vm

# Use minimal packages
PACKAGES=../packages.d/packages-minimal ./gen-vm

# Create custom manifest
cat > my-packages << EOF
  - build-essential
  - git
  - fio
  - nvme-cli
EOF
PACKAGES=my-packages ./gen-vm
```

Available manifests:
- `packages.d/packages-default` - Full development environment
- `packages.d/packages-minimal` - Minimal set of tools

## VM Management

### Resetting a VM

VMs use a backing file approach for easy resets:

```bash
# Delete the overlay
rm ../images/myvm.qcow2

# Restore from backing file
VM_NAME=myvm RESTORE_IMAGE=true ./gen-vm
```

The backing file preserves the clean, post-cloud-init state.

### Multiple VMs

Run multiple VMs simultaneously with different SSH ports:

```bash
# VM 1
VM_NAME=vm1 SSH_PORT=2222 ./run-vm &

# VM 2
VM_NAME=vm2 SSH_PORT=2223 ./run-vm &

# VM 3
VM_NAME=vm3 SSH_PORT=2224 ./run-vm &
```

## Shared Filesystems

Share host directories with guests using VirtFS:

```bash
# Run VM with shared filesystem
FILESYSTEM=/home/$USER/Projects ./run-vm
```

In the guest, mount it:

```bash
# One-time mount
sudo mkdir -p /mnt/hostfs
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostfs /mnt/hostfs

# Persistent mount (add to /etc/fstab)
echo "hostfs /mnt/hostfs 9p trans=virtio,version=9p2000.L,nofail 0 1" | sudo tee -a /etc/fstab
```

## Kernel Debugging

QEMU's GDB support is available for kernel debugging:

```bash
# Add -s -S to QEMU args by modifying run-vm temporarily
# Or use qemu-system-x86_64 directly with -s -S

# In another terminal
gdb vmlinux
(gdb) target remote :1234
(gdb) break start_kernel
(gdb) continue
```

For kernel modules:

```bash
(gdb) add-symbol-file /path/to/module.ko <text_addr> \
      -s .data <data_addr> -s .bss <bss_addr>
```

Get addresses from `/sys/module/<module>/sections/` in the running kernel.

## Continuous Integration

The repository includes GitHub Actions workflows:

- **smoke-test.yml** - Tests VM generation and execution for x86_64, ARM64, and
  RISC-V
- **spell-check.yml** - Validates documentation spelling

The smoke tests verify:
- VM generation with gen-vm
- RESTORE_IMAGE mode
- NVME_TRACE functionality
- Multi-architecture support

## libvirt Scripts

The `libvirt/` directory contains scripts for libvirt-based VM management:

- `virt-install-ubuntu` - Create VMs using virt-install and cloud-init
- `create-gcp-nested` - GCP nested virtualization setup
- `create-nvme` - NVMe-specific libvirt configuration
- `create-raid` - RAID configuration helpers
- `virt-clone-many` - Batch VM cloning

These are alternative approaches when libvirt management is preferred over
direct QEMU.

## Migrating from Legacy Scripts

If you previously used the `runqemu` script or other legacy tools, see
[MIGRATION.md](MIGRATION.md) for a comprehensive migration guide.

**Quick comparison:**

| Task | Old Approach | New Approach |
|------|-------------|--------------|
| Create VM | `sudo scripts/create ...` | `./gen-vm` |
| Configure VM | `sudo scripts/setup ...` | Automatic via cloud-init |
| Run VM | `./runqemu -i ... -m ...` | `./run-vm` |
| NVMe devices | Limited options | `NVME=4 ./run-vm` |
| Architecture | Different scripts | `ARCH=arm64 ./gen-vm` |

## Repository Structure

```
qemu-minimal/
├── qemu/                   # Main scripts
│   ├── gen-vm             # VM generation
│   ├── run-vm             # VM execution
│   ├── run-vm-cxl-nvme    # CXL emulation
│   ├── vfio-setup         # PCIe passthrough setup
│   └── README.md          # Detailed documentation
├── libvirt/               # libvirt-based tools
├── packages.d/            # Package manifests
├── kernels/               # Kernel configs
├── images/                # VM images (gitignored)
├── .github/workflows/     # CI configuration
├── README.md              # This file
├── MIGRATION.md           # Migration guide
└── CLEANUP_PROPOSAL.md    # Cleanup documentation
```

## Requirements

### Host System

**Minimum:**
- Linux system (Ubuntu, Debian, Fedora, etc.)
- QEMU installed (`qemu-system-x86`, `qemu-system-arm`, `qemu-system-misc`)
- `cloud-image-utils` (for cloud-localds)
- SSH client

**For optimal experience:**
- KVM support (bare metal or nested virtualization)
- 16GB+ RAM
- 50GB+ disk space for VM images

**For specific features:**
- **ARM64 VMs**: `qemu-efi-aarch64` package
- **RISC-V VMs**: `u-boot-qemu` package
- **PCIe passthrough**: IOMMU enabled, `driverctl` installed
- **Shared filesystems**: VirtFS support in QEMU and guest kernel

### Installation

On Ubuntu/Debian:

```bash
# Basic requirements
sudo apt update
sudo apt install qemu-system-x86 qemu-system-arm qemu-system-misc \
                 qemu-utils cloud-image-utils

# For ARM64 VMs
sudo apt install qemu-efi-aarch64

# For RISC-V VMs
sudo apt install u-boot-qemu

# For PCIe passthrough
sudo apt install driverctl
```

## Contributing

Contributions welcome! Areas of interest:

- Additional architecture support
- More package manifests
- CI enhancements
- Documentation improvements
- Bug fixes

Please ensure:
- Scripts pass shellcheck
- Documentation is updated
- CI tests pass

## License

This repository is for educational and testing purposes. Please check individual
component licenses (QEMU, Ubuntu, etc.) for your use case.

## History

This repository evolved from a Debian Jessie-based manual VM creation
environment to the current Ubuntu cloud-init based approach. The legacy scripts
have been archived but remain in git history for reference.

## Related Projects

- [QEMU](https://www.qemu.org/) - The emulator this project wraps
- [cloud-init](https://cloud-init.io/) - Cloud instance initialization
- [Ubuntu Cloud Images](https://cloud-images.ubuntu.com/) - Base images used by
  gen-vm

## Acknowledgments

- Original creation by Logan and contributors
- Continued development by Stephen Bates and community
- Built on top of QEMU, cloud-init, and Ubuntu projects

---

**Getting Started:** Read [qemu/README.md](qemu/README.md) for detailed usage
instructions.

**Need Help?** Check [MIGRATION.md](MIGRATION.md) if migrating from old scripts,
or file an issue on GitHub.
