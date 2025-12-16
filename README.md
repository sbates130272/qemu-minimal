# QEMU and Libvirt Tooling

## Summary

This repository provides a modern, cloud-init based environment for running and
testing QEMU-based and Libvirt-based VMs. It's particularly well-suited for
NVMe, PCIe device passthrough, and libvfio-user testing, but can be used for
general-purpose VM creation for development and testing.

**Key Features:**
- 🚀 Fast VM creation using Ubuntu cloud images and cloud-init
- 💾 NVMe device emulation with tracing support
- 🔌 PCIe device passthrough (VFIO)
- 🔧 CXL (Compute Express Link) device emulation
- 🏗️ Multi-architecture support (x86_64, ARM64, RISC-V)
- 📦 Declarative package management via manifests
- ⚡ KVM acceleration support

## Quick Start (QEMU)
```bash
cd qemu
./qemu/gen-vm
```
This creates an Ubuntu Noble VM named `qemu-minimal` with default settings. To
run the VM:
```bash
./qemu/run-vm
```
SSH into the VM
```bash
ssh -p 2222 ubuntu@localhost
# Password: password (or use SSH key)
```

## Quick Start (Libvirt)
```bash
./libvirt/virt-install-ubuntu
```
