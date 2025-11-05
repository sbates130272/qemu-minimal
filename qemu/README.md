# QEMU Scripts

This directory contains the modern QEMU VM management scripts for the
qemu-minimal repository.

## Scripts Overview

### gen-vm - VM Generation Script

Creates Ubuntu-based VMs using QEMU and cloud-init. This is the recommended way
to create new VMs.

**Basic Usage:**
```bash
./gen-vm
```

This creates a VM named `qemu-minimal` using Ubuntu Noble with default settings.

**Common Options:**

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `VM_NAME` | `qemu-minimal` | Name of the VM |
| `RELEASE` | `noble` | Ubuntu release (noble, jammy, focal) |
| `ARCH` | `amd64` | Architecture (amd64, arm64, riscv64) |
| `SIZE` | `64` | Disk size in GB |
| `VCPUS` | `2` | Number of virtual CPUs |
| `VMEM` | `4096` | Memory in MB |
| `USERNAME` | `ubuntu` | Username for the VM |
| `PASS` | `password` | Password for the user |
| `SSH_KEY_FILE` | `~/.ssh/id_rsa.pub` | SSH public key to install |
| `PACKAGES` | `../packages.d/packages-default` | Package manifest file |
| `KVM` | `enable` | Use KVM acceleration (set to `none` to disable) |
| `FORCE` | `false` | Force re-download of cloud image |
| `NO_BACKING` | `false` | Don't use backing file |
| `RESTORE_IMAGE` | `false` | Restore image from backing file only |

**Examples:**

Create a VM with custom name and more resources:
```bash
VM_NAME=dev-vm VCPUS=4 VMEM=8192 ./gen-vm
```

Create an ARM64 VM:
```bash
ARCH=arm64 VM_NAME=arm-test ./gen-vm
```

Create a RISC-V VM:
```bash
ARCH=riscv64 VM_NAME=riscv-test ./gen-vm
```

Create a VM with minimal packages:
```bash
PACKAGES=../packages.d/packages-minimal ./gen-vm
```

Restore a corrupted VM image from its backing file:
```bash
VM_NAME=my-vm RESTORE_IMAGE=true ./gen-vm
```

**Image Structure:**
- Creates `${IMAGES}/${VM_NAME}-backing.qcow2` from Ubuntu cloud image
- Creates `${IMAGES}/${VM_NAME}.qcow2` as a qcow2 overlay
- Backing file approach saves disk space and allows quick resets

---

### run-vm - VM Execution Script

Runs a VM that was created with `gen-vm`. Supports NVMe emulation, PCIe device
passthrough, CXL, and more.

**Basic Usage:**
```bash
./run-vm
```

**Common Options:**

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `VM_NAME` | `qemu-minimal` | Name of the VM to run |
| `ARCH` | `amd64` | Architecture (amd64, arm64, riscv64) |
| `VCPUS` | `2` | Number of virtual CPUs |
| `VMEM` | `4096` | Memory in MB |
| `SSH_PORT` | `2222` | Host port for SSH forwarding |
| `KVM` | `enable` | Use KVM acceleration |
| `NVME` | `none` | NVMe device configuration |
| `NVME_TRACE` | `none` | Enable NVMe tracing |
| `NVME_TRACE_FILE` | empty | File for trace output |
| `FILESYSTEM` | `none` | Host filesystem to share with guest |
| `PCI_HOSTDEV` | `none` | PCIe devices for passthrough |
| `PCI_TESTDEV` | `none` | Enable pci-testdev |

**NVMe Configuration:**

The `NVME` variable has three modes:

1. **Numeric**: Number of NVMe drives to create
   ```bash
   NVME=4 ./run-vm  # Creates 4 NVMe drives
   ```

2. **Boolean**: Use null_blk backed NVMe
   ```bash
   NVME=true ./run-vm
   ```
Requires null_blk module loaded on host:
   ```bash
   sudo modprobe null_blk queue_mode=2 gb=1024
   ```

3. **Custom String**: Pass custom NVMe arguments
   ```bash
   NVME="-drive file=/path/to/nvme.img,if=none,id=nvme1 -device nvme,..." ./run-vm
   ```

**NVMe Tracing:**

Enable detailed NVMe tracing for debugging:

```bash
# Trace doorbell writes only
NVME=4 NVME_TRACE=doorbell ./run-vm

# Trace all NVMe events
NVME=4 NVME_TRACE=all ./run-vm

# Trace to a file instead of stderr
NVME=4 NVME_TRACE=doorbell NVME_TRACE_FILE=/tmp/nvme.log ./run-vm

# Trace specific events
NVME=4 NVME_TRACE=pci_nvme_read ./run-vm
```

**PCIe Device Passthrough:**

Pass through physical PCIe devices to the VM using VFIO-PCI:

```bash
# Single device
PCI_HOSTDEV=0000:03:00.0 ./run-vm

# Multiple devices (comma-separated)
PCI_HOSTDEV=0000:03:00.0,0000:04:00.0 ./run-vm
```

**Requirements:**
- Devices must be bound to `vfio-pci` driver (use `./vfio-setup`)
- IOMMU must be enabled in BIOS/kernel

**Filesystem Sharing:**

Share a host directory with the guest:

```bash
FILESYSTEM=/home/user/Projects ./run-vm
```

Mount in guest:
```bash
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostfs /mnt/hostfs
```

Or add to `/etc/fstab`:
```
hostfs /mnt/hostfs 9p trans=virtio,version=9p2000.L,nofail 0 1
```

**Complete Examples:**

Development VM with NVMe and shared filesystem:
```bash
VM_NAME=dev-vm VCPUS=4 VMEM=8192 NVME=2 \
  FILESYSTEM=/home/user/Projects ./run-vm
```

Testing with NVMe tracing:
```bash
VM_NAME=test-vm NVME=4 NVME_TRACE=doorbell \
  NVME_TRACE_FILE=/tmp/nvme-trace.log ./run-vm
```

PCIe device passthrough for NVMe testing:
```bash
VM_NAME=pcie-test PCI_HOSTDEV=0000:03:00.0 ./run-vm
```

---

### run-vm-cxl-nvme - CXL Device Emulation

Runs a VM with Compute Express Link (CXL) emulation and NVMe devices on the CXL
bus.

**Basic Usage:**
```bash
./run-vm-cxl-nvme
```

**Common Options:**

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `VM_NAME` | `qemu-minimal` | Name of the VM |
| `VCPUS` | `2` | Number of virtual CPUs |
| `MEMORY` | `4G` | Memory size |
| `NVME_SIZE` | `64G` | Size of NVMe device |
| `FS_PATH` | `/home/$USER/Projects` | Shared filesystem path |
| `SSH_PORT` | `2224` | Host SSH port |
| `KERNEL_PATH` | `../kernels` | Path to kernel |
| `KERNEL_NAME` | `bzImage-cxl-nvme` | Kernel image name |
| `QEMU_DIR` | empty | Custom QEMU binary directory |

**Notes:**
- CXL emulation typically requires TCG (not KVM)
- Requires recent kernel with CXL support
- Creates CXL memory device and NVMe on CXL bus
- Useful for testing CXL-attached storage devices

**Example:**
```bash
VM_NAME=cxl-test VCPUS=4 MEMORY=8G ./run-vm-cxl-nvme
```

---

### vfio-setup - VFIO Device Management

Helper script to bind/unbind PCIe devices to/from vfio-pci driver for device
passthrough.

**Prerequisites:**
```bash
# Install driverctl
sudo apt install driverctl

# Enable IOMMU in BIOS and kernel
# Intel: Add intel_iommu=on to kernel command line
# AMD: Add amd_iommu=on to kernel command line

# Create udev rules for permissions
sudo tee /etc/udev/rules.d/70-vfio-group.rules << EOF
SUBSYSTEM=="vfio", GROUP="kvm", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

**Basic Usage:**

Bind device to vfio-pci:
```bash
sudo HOST_ADDR=0000:03:00.0 ./vfio-setup
```

Check current driver:
```bash
sudo HOST_ADDR=0000:03:00.0 CHECK_ONLY=true ./vfio-setup
```

List all vfio-pci bound devices:
```bash
sudo LIST_ONLY=true ./vfio-setup
```

Unbind from vfio-pci:
```bash
sudo HOST_ADDR=0000:03:00.0 ./vfio-setup
# Then manually bind to original driver
```

**Options:**

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `HOST_ADDR` | `none` | PCIe bus address (e.g., 0000:03:00.0) |
| `BIND_DRIVER` | `vfio-pci` | Driver to bind to |
| `CHECK_ONLY` | `false` | Only check current driver |
| `LIST_ONLY` | `false` | List vfio-pci devices |

---

## Workflow

### Creating and Running a New VM

1. **Generate the VM:**
   ```bash
   VM_NAME=myvm VCPUS=4 VMEM=8192 ./gen-vm
   ```

2. **Run the VM:**
   ```bash
   VM_NAME=myvm NVME=2 ./run-vm
   ```

3. **SSH into the VM:**
   ```bash
   ssh -p 2222 ubuntu@localhost
   ```

### Testing NVMe with Tracing

1. **Create VM with default packages (includes nvme-cli):**
   ```bash
   ./gen-vm
   ```

2. **Run with NVMe tracing enabled:**
   ```bash
   NVME=4 NVME_TRACE=doorbell NVME_TRACE_FILE=/tmp/trace.log ./run-vm
   ```

3. **In the VM, generate NVMe traffic:**
   ```bash
   sudo nvme list
   sudo fio --name=test --filename=/dev/nvme0n1 --size=1G --rw=randwrite
   ```

4. **On host, analyze traces:**
   ```bash
   grep "pci_nvme_mmio_doorbell" /tmp/trace.log
   ```

### Hardware Device Passthrough

1. **Find device PCI address:**
   ```bash
   lspci | grep NVMe
   # Example output: 03:00.0 Non-Volatile memory controller: ...
   ```

2. **Bind to vfio-pci:**
   ```bash
   sudo HOST_ADDR=0000:03:00.0 ./vfio-setup
   ```

3. **Run VM with device:**
   ```bash
   PCI_HOSTDEV=0000:03:00.0 ./run-vm
   ```

4. **Verify in VM:**
   ```bash
   lspci | grep NVMe
   sudo nvme list
   ```

---

## Architecture Support

All scripts support multiple architectures:

- **amd64/x86_64**: Full KVM support on Intel/AMD hosts
- **arm64/aarch64**: Requires UEFI firmware (`qemu-efi-aarch64`)
- **riscv64**: Requires U-Boot (`u-boot-qemu`)

Set `ARCH` variable to target architecture:
```bash
ARCH=arm64 ./gen-vm
ARCH=arm64 ./run-vm
```

---

## Troubleshooting

### VM Won't Boot

- Check that cloud-init completed: images should show user-data applied
- Verify SSH key is correct: check `~/.ssh/id_rsa.pub`
- Try without KVM: `KVM=none ./gen-vm` and `KVM=none ./run-vm`

### NVMe Not Showing Up

- Verify images created: `ls ../images/*-nvme*.qcow2`
- Check QEMU output for errors
- Ensure using recent QEMU version

### PCIe Passthrough Fails

- Verify IOMMU enabled: `dmesg | grep -i iommu`
- Check device bound to vfio-pci: `lspci -k -s 03:00.0`
- Ensure udev rules applied: `ls -l /dev/vfio/`
- Check IOMMU grouping: `find /sys/kernel/iommu_groups/ -name devices`

### Slow Performance

- Enable KVM if on bare metal: `KVM=enable` (default)
- Increase CPU/Memory: `VCPUS=8 VMEM=16384`
- Use virtio drivers in guest

---

## Related Documentation

- [Main README](../README.md) - Repository overview
- [Package Manifests](../packages.d/) - Available package lists
- [CI Workflows](../.github/workflows/) - Automated testing

---

## Migration from Legacy Scripts

If you were using the old `runqemu` script:

**Old:**
```bash
./runqemu -i images/myvm.qcow2 -m 4096 -n kernels/bzImage
```

**New:**
```bash
# Create VM once
VM_NAME=myvm VMEM=4096 ./gen-vm

# Run VM
VM_NAME=myvm NVME=2 ./run-vm
```

Key differences:
- Cloud-init based setup (cleaner, more maintainable)
- Separate generation and execution steps
- Modern Ubuntu releases (Noble, Jammy vs old Jessie)
- Better architecture support
- NVMe tracing capabilities
- PCIe passthrough support

