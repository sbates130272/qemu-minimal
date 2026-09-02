# vfio-user 2-VM mesh — Docker Compose stack

Spins up two VMs connected via a rocm-ernic TCP manager/worker mesh, each
with its own rocjitsu GPU emulator. The ernic-hub relays L2 Ethernet
frames between VMs so they share a private network; RDMA operations go
peer-to-peer over the same TCP connections.

## Architecture

```
ernic-hub  (tcp:manager:listen:6320)  ──── VM 1 (qemu-1, SSH :2222)
      │                                         └─ ernic-1.sock + rocjitsu-1.sock
      │  TCP mesh
ernic-worker   (tcp:worker:ernic-hub) ──── VM 2 (qemu-2, SSH :2223)
                                               └─ ernic-2.sock + rocjitsu-2.sock
```

## Prerequisites

- Docker with Compose v2
- `/dev/kvm` on the host (recommended)
- Two qcow2 VM images — each VM must have its own overlay (they can share a
  backing file; see below)

## Quick start

```sh
# 1. Configure
cp env.example .env
$EDITOR .env   # set VM1_NAME, VM2_NAME, VM_IMAGES_DIR

# 2. Create a second VM overlay if you only have one image
#    (both VMs need distinct qcow2 files)
qemu-tool gen-vm --vm-name qemu-minimal-2 --backing-file \
  /var/lib/qemu-tool/images/qemu-minimal-backing.qcow2

# 3. Start the stack
qemu-tool compose --stack vfio-user-2vm --vm-name qemu-minimal up

# 4. Connect to each VM
ssh -p 2222 ubuntu@localhost   # VM 1
ssh -p 2223 ubuntu@localhost   # VM 2
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `VM1_NAME` | `qemu-minimal` | VM 1 image basename (no extension) |
| `VM2_NAME` | `qemu-minimal-2` | VM 2 image basename (no extension) |
| `VM_IMAGES_DIR` | `/var/lib/qemu-tool/images` | Directory containing both VM images |
| `VM1_SSH_PORT` | `2222` | Host port forwarded to VM 1 SSH |
| `VM2_SSH_PORT` | `2223` | Host port forwarded to VM 2 SSH |
| `VM_VCPUS` | `4` | vCPU count (shared by both VMs) |
| `VM_VMEM` | `8192` | RAM in MiB (shared by both VMs) |
| `ERNIC_TCP_PORT` | `6320` | TCP port for ernic manager/worker mesh |
| `ROCJITSU_CONFIG` | `gfx1250_mi455x.json` | rocjitsu GPU config |
| `VM_SHM_SIZE` | `8g` | Container shared memory (must be >= VM_VMEM MiB) |

## VM image requirements

Each VM needs its **own qcow2 overlay** — two VMs cannot safely share a
single overlay file. They can however share the same backing file:

```sh
# Create VM2's overlay from VM1's existing backing file
qemu-img create -f qcow2 -b /path/to/vm1-backing.qcow2 \
  -F qcow2 /path/to/qemu-minimal-2.qcow2
```

## Known issues

See [../vfio-user-vm/README.md](../vfio-user-vm/README.md) for the amdgpu
driver panic workaround — apply it to both VM images before starting the
stack.
