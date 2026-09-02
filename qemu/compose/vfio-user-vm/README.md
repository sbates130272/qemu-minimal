# vfio-user GPU VM — Docker Compose stack

Runs rocm-ernic and rocjitsu vfio-user GPU servers alongside a `qemu-system` VM
that attaches them as emulated PCIe devices via the `vfio-user-pci` driver.

## Prerequisites

- Docker with Compose v2 (`docker compose version`)
- `/dev/kvm` accessible on the host (strongly recommended; omit the `devices` section for CI)
- A qemu-tool VM image (see [qemu-tool gen-vm](../../qemu-tool/))

## Quick start

```sh
# 1. Configure
cp env.example .env
$EDITOR .env          # set image tags, VM_IMAGE path, GPU counts

# 2. Build the VM image (skip if you already have one)
qemu-tool gen-vm --vm-name qemu-minimal --images /var/lib/qemu-tool/images

# 3. Start the stack
docker compose up

# 4. Connect to the guest
ssh -p 2222 ubuntu@localhost
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ERNIC_IMAGE` | `…-rocm-ernic:latest` | rocm-ernic vfio-user server image |
| `ROCJITSU_IMAGE` | `…-rocm-rocjitsu:latest` | rocjitsu vfio-user server image |
| `QEMU_IMAGE` | `…-qemu-libvfio-user:august-11-2026` | qemu-system image (must include qemu-tool) |
| `ERNIC_COUNT` | `1` | Number of ernic GPU replicas |
| `ROCJITSU_COUNT` | `1` | Number of rocjitsu GPU replicas |
| `ROCJITSU_CONFIG` | `gfx1250_mi455x.json` | Config filename under `/usr/local/share/rocjitsu/configs/` |
| `VM_IMAGE` | `/var/lib/qemu-tool/images/qemu-minimal.qcow2` | Absolute path to the qcow2 image |
| `VM_NAME` | `qemu-minimal` | Must match the qcow2 basename without extension |
| `VM_VCPUS` | `4` | Guest vCPU count |
| `VM_VMEM` | `8192` | Guest RAM in MiB |
| `VM_SSH_PORT` | `2222` | Host port forwarded to guest SSH |
| `VM_SHM_SIZE` | `8g` | Container shared memory (must be ≥ `VM_VMEM` MiB) |

## How it works

1. `ernic` and `rocjitsu` services start their respective vfio-user servers, each writing a
   Unix socket to the `vfu-sockets` tmpfs volume at `/run/vfu/<prefix>-<N>.sock`.
2. The `qemu` service waits for all GPU sockets to appear (healthchecks), then calls
   `qemu-tool run-vm --vfio-userdev <sockets>` which adds a shared-memory memfd backend and
   attaches each socket as a `vfio-user-pci` device.
3. The guest sees the emulated GPUs as standard PCIe devices.

## Known issues

**KVM recommended**: The `qemu` service requests `/dev/kvm` via `devices` in
the compose file. Without KVM the VM will run in software emulation and be
significantly slower. Verify your user is in the `kvm` group (`groups | grep kvm`)
and that `/dev/kvm` exists on the host for best performance.

**amdgpu driver panic at boot**: The guest amdgpu kernel driver will probe the
rocjitsu vfio-user PCIe device and panic. Until rocjitsu gains full driver
compatibility, blacklist the driver in the VM image before booting with this
compose stack:

```sh
# Boot the VM once without vfio-user devices, then:
ssh -p 2222 ubuntu@localhost \
  "echo 'blacklist amdgpu' | sudo tee /etc/modprobe.d/blacklist-amdgpu.conf \
   && sudo update-initramfs -u && sudo shutdown -h now"
```

## GPU server socket contract

Each GPU server container writes a Unix domain socket into the shared `vfu-sockets`
tmpfs volume at `/run/vfu`. The socket paths used by each service are:

| Service | Socket path | CLI flag |
|---|---|---|
| `ernic` | `/run/vfu/ernic-1.sock` | `rocm-ernic -s /run/vfu/ernic-1.sock` |
| `rocjitsu` | `/run/vfu/rocjitsu-1.sock` | `rocjitsu --vfio-socket /run/vfu/rocjitsu-1.sock` |

The compose healthcheck polls for socket existence; `qemu` will not start until
both are healthy. The `qemu` service globs `/run/vfu/*.sock` at startup and passes
all matches as a comma-separated list to `--vfio-userdev`. Socket filesystem order
determines PCIe slot assignment inside the guest.

When `ERNIC_COUNT` or `ROCJITSU_COUNT` is greater than 1 you will need to
customise the service commands to write distinct socket names (e.g. `ernic-2.sock`)
and update the healthchecks accordingly.

## Installed location

When installed from the Debian package this compose stack is at:

```
/usr/share/qemu-tool/compose/vfio-user-vm/
```
