# Migration Guide

This guide helps users transition from legacy scripts to the modern qemu-minimal
workflow.

## Overview

The qemu-minimal repository has evolved from Debian Jessie-based manual VM
creation to Ubuntu cloud-init based automation. The modern approach is simpler,
more maintainable, and supports multiple architectures.

## What Changed?

### Archived Scripts

The following scripts have been archived (removed from working tree, preserved
in git history):

**Replaced by `qemu/gen-vm` and `qemu/run-vm`:**
- `runqemu` - Legacy VM runner
- `run-cxl-x86_64` - Replaced by `qemu/run-vm-cxl-nvme`
- `scripts/run-arm`, `scripts/run-ppc64el`, `scripts/run-riscv64`, etc. -
  Architecture support now in `qemu/gen-vm`

**Debian-based VM Creation (superseded by cloud-init):**
- `scripts/create` - Manual debootstrap approach
- `scripts/setup` - Manual configuration
- `scripts/shrink` - Image shrinking

**Legacy Testing Scripts:**
- `scripts/busybox` - Initramfs creation
- `scripts/simple` - Simple initramfs
- `scripts/nvmf-host`, `scripts/nvmf-target` - NVMf over Soft-RoCE (kernel 4.8
  era)

**Other:**
- `scripts/cross` - Cross-compilation via multistrap
- `scripts/logan/*` - Personal kernel build scripts
- Old kernel images (4.6, 4.8) and configs
- Old libvirt kickstart files (CentOS 7, etc.)

## Migration Scenarios

### Scenario 1: You Were Using `runqemu`

**Old Workflow:**
```bash
# Create image manually with scripts/create
sudo scripts/create images/myvm.qcow2

# Setup image
sudo scripts/setup images/myvm.qcow2

# Run with runqemu
./runqemu -i images/myvm.qcow2 -m 4096 -n kernels/bzImage
```

**New Workflow:**
```bash
# Generate VM (once)
cd qemu
VM_NAME=myvm VMEM=4096 ./gen-vm

# Run VM
VM_NAME=myvm ./run-vm

# SSH into VM
ssh -p 2222 ubuntu@localhost
```

**Benefits:**
- Cloud-init automates user creation, package installation, and configuration
- Ubuntu cloud images are maintained and updated regularly
- Backing file approach allows easy resets
- No manual partitioning or chroot setup needed

### Scenario 2: You Were Using Architecture-Specific Scripts

**Old Workflow:**
```bash
# Different script for each architecture
./scripts/run-arm <options>
./scripts/run-riscv64 <options>
```

**New Workflow:**
```bash
# Single script, specify architecture
ARCH=arm64 ./gen-vm
ARCH=arm64 ./run-vm

# Or for RISC-V
ARCH=riscv64 ./gen-vm
ARCH=riscv64 ./run-vm
```

### Scenario 3: You Were Creating Cross-Architecture Images

**Old Workflow:**
```bash
# Used multistrap
sudo scripts/cross
```

**New Workflow:**
```bash
# Cloud-init handles cross-architecture
ARCH=arm64 ./gen-vm    # Creates ARM64 VM on x86 host
ARCH=riscv64 ./gen-vm  # Creates RISC-V VM on x86 host
```

Note: Cloud images are pre-built for each architecture, no cross-compilation
needed!

### Scenario 4: Custom Package Installation

**Old Workflow:**
```bash
# Manually chroot and install packages
sudo chroot /mnt/vm-root apt install ...
```

**New Workflow:**
```bash
# Create package manifest file
cat > my-packages << EOF
  - build-essential
  - cmake
  - git
  - fio
EOF

# Use with gen-vm
PACKAGES=my-packages ./gen-vm
```

Or use existing manifests:
```bash
PACKAGES=../packages.d/packages-default ./gen-vm
PACKAGES=../packages.d/packages-minimal ./gen-vm
```

### Scenario 5: NVMe Device Testing

**Old Workflow:**
```bash
# Limited NVMe options in runqemu
./runqemu -u -i myvm.qcow2 kernels/bzImage
```

**New Workflow:**
```bash
# Flexible NVMe configuration
NVME=4 ./run-vm  # 4 NVMe devices

# With tracing
NVME=4 NVME_TRACE=doorbell ./run-vm

# Using host null_blk
sudo modprobe null_blk queue_mode=2 gb=1024
NVME=true ./run-vm
```

### Scenario 6: CXL Testing

**Old Workflow:**
```bash
./run-cxl-x86_64 -i images/cxl-vm.qcow2 kernels/bzImage
```

**New Workflow:**
```bash
VM_NAME=cxl-vm ./gen-vm
VM_NAME=cxl-vm ./run-vm-cxl-nvme
```

## Common Tasks

### Creating a Clean Development VM

```bash
cd qemu
VM_NAME=dev VCPUS=4 VMEM=8192 SIZE=128 \
  PACKAGES=../packages.d/packages-default ./gen-vm
VM_NAME=dev NVME=2 FILESYSTEM=/home/$USER/Projects ./run-vm
```

### Resetting a VM to Clean State

**Old Approach:** Delete snapshot, recreate from backing file manually

**New Approach:**
```bash
# Simple deletion and restore
rm ../images/myvm.qcow2
VM_NAME=myvm RESTORE_IMAGE=true ./gen-vm
```

### Testing Different Ubuntu Releases

```bash
# Noble (24.04 LTS - default)
RELEASE=noble VM_NAME=noble-test ./gen-vm

# Jammy (22.04 LTS)
RELEASE=jammy VM_NAME=jammy-test ./gen-vm

# Focal (20.04 LTS)
RELEASE=focal VM_NAME=focal-test ./gen-vm
```

### Accessing Archived Files

If you need an archived script or old kernel:

```bash
# List archived files
git log --all --pretty=format: --name-only --diff-filter=D | sort -u

# View content of archived file
git show HEAD~1:runqemu  # Or appropriate commit

# Restore specific file temporarily
git show HEAD~1:runqemu > /tmp/runqemu
chmod +x /tmp/runqemu

# Restore permanently (not recommended without good reason)
git checkout <commit-before-deletion> -- runqemu
```

## Key Differences Summary

| Aspect | Old Approach | New Approach |
|--------|-------------|--------------|
| **Base OS** | Debian Jessie | Ubuntu Noble/Jammy |
| **VM Creation** | Manual debootstrap | Cloud-init automation |
| **Configuration** | Manual chroot scripts | Cloud-init user-data |
| **Images** | Manual qcow2 creation | Ubuntu cloud images |
| **Architecture** | Separate scripts | Single script + ARCH variable |
| **Packages** | Manual apt install | Package manifest files |
| **Reset** | Manual snapshot management | RESTORE_IMAGE mode |
| **NVMe** | Limited hardcoded options | Flexible configuration + tracing |
| **Documentation** | Scattered in README | Dedicated qemu/README.md |

## Environment Variables Reference

### gen-vm

| Variable | Old Equivalent | Description |
|----------|---------------|-------------|
| `VM_NAME` | `-i` parameter | Name of VM |
| `RELEASE` | N/A (fixed Jessie) | Ubuntu release |
| `ARCH` | Different scripts | Architecture |
| `SIZE` | `SIZE` env var | Disk size |
| `VCPUS` | `CORES` | CPU count |
| `VMEM` | `-m` parameter | Memory |
| `USERNAME` | Hardcoded | Username |
| `PACKAGES` | Manual install | Package list |
| `KVM` | `-k` flag (inverted) | KVM enable/disable |

### run-vm

| Variable | Old Equivalent | Description |
|----------|---------------|-------------|
| `VM_NAME` | `-i` parameter | VM to run |
| `NVME` | `-n` flag | NVMe configuration |
| `NVME_TRACE` | N/A | NVMe tracing (new feature) |
| `SSH_PORT` | `-s` parameter | SSH port forward |
| `FILESYSTEM` | `-f` flag (inverted) | Shared filesystem |
| `PCI_HOSTDEV` | N/A | PCIe passthrough (new) |

## Troubleshooting

### "I can't find the old scripts!"

They're in git history. See "Accessing Archived Files" above.

### "My old VM images don't work!"

Old Debian Jessie images should still boot with `run-vm`, but consider creating
fresh Ubuntu-based VMs:
- Modern software versions
- Better hardware support
- Security updates
- Simpler maintenance

### "The new approach is too different!"

Start small:
1. Create one test VM: `./gen-vm`
2. Run it: `./run-vm`
3. SSH in: `ssh -p 2222 ubuntu@localhost`
4. Once comfortable, migrate your workflows

### "I need a feature from the old scripts!"

Let us know! File an issue. Many "missing" features are actually available:
- Custom packages: Use `PACKAGES` variable
- Custom config: Modify `gen-vm` cloud-config section
- Special networking: Set in `run-vm` environment
- Old kernels: Boot with `-kernel` flag (see run-vm-cxl-nvme for example)

## Getting Help

- Read [qemu/README.md](qemu/README.md) for detailed documentation
- Check the main [README.md](README.md) for repository overview
- Look at [packages.d/](packages.d/) for package manifest examples
- Review archived scripts in git history for reference
- File issues on GitHub for bugs or feature requests

## Philosophy

The migration represents a shift from **manual imperative** configuration to
**declarative** infrastructure:

- **Old:** "Run these commands to configure the VM"
- **New:** "The VM should have these properties" (cloud-init applies them)

This approach is:
- More reproducible
- Easier to maintain
- Better documented
- Industry standard
- Widely supported

Welcome to modern qemu-minimal! 🚀

