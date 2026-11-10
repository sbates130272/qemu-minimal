# vfio-user doorbell test rig — Docker Compose stack

One guest with two tiny vfio-user PCI devices from
[rocm-xio](https://github.com/ROCm/rocm-xio)'s `tools/vfu-doorbell`, for testing
how a write from an emulated device reaches another emulated device, and how
doorbell-style register semantics behave under different write patterns. There
is no GPU, NIC or NVMe model: a rocjitsu doorbell store takes the same
libvfio-user and QEMU call chain, and this strips everything else away.

| Service | What it serves | Socket |
|---|---|---|
| `ringee` | a device whose BAR0 carries doorbell overlays (`index`, `keyed`, `event`) and readable per-key counters | `/run/vfu/ringee.sock` |
| `ringer` | a device the guest programs to write a pattern of values to any guest physical address | `/run/vfu/ringer.sock` |
| `qemu` | the guest, with both attached, ringee first | — |

## Quick start

```sh
# 1. Build the binaries (needs the libvfio-user fork image). From a rocm-xio
#    checkout; they land in /tmp/vfu-doorbell/bin.
tools/vfu-doorbell/build-in-container.sh

# 2. The guest image (pulls the published qcow2 on first use).
qemu-tool gen-vm --env-file qemu/env/vfu-doorbell

# 3. The stack.
qemu-tool compose --stack vfu-doorbell-vm --env-file qemu/env/vfu-doorbell up -d

# 4. Copy the guest tool in and check both devices are there.
cd ansible && ansible-playbook -i inventory/vfu-doorbell.yml \
    playbooks/vfu-doorbell-guest.yml

# 5. Run the built-in scenarios (add --tags selftest to the playbook, or:)
qemu-tool ssh doorbell-1 sudo xio-doorbell selftest
```

Take it down with `qemu-tool compose --stack vfu-doorbell-vm --env-file
qemu/env/vfu-doorbell down`. Stop the stack before rebuilding the binaries: a
running binary on a bind mount cannot be overwritten, and `cp` fails with "text
file busy".

The guest tool is documented in `tools/vfu-doorbell/README.md` in rocm-xio. In
short: `xio-doorbell info`, `selftest`, `presets`, `ring` (the ringer writes the
ringee's BAR through `vfu_mmio_write`; `ring --ringer cpu` has the CPU write it
instead, the known-good baseline), `dump` and `log`.

`VM_QEMU_ARGS` in `qemu/env/vfu-doorbell` adds QEMU's own pci-testdev and an
NVMe controller to the guest, for `ring --ringee qemu-pci-testdev` and
`ring --ringee qemu-nvme,sq=free`. To see each write reach them, add
`--nvme-trace memory_region_ops_write` (the env file has the line to copy) and
read `docker logs vfu-doorbell-vm-qemu-1 | grep -E "pci-testdev|'nvme'"`. That
traces every MMIO write in the VM, mostly PCI config space at boot, so start the
stack with `up -d`. Empty the setting to run with only the ringee and the
ringer.

## What to know

- **The QEMU must be the fork.** The ringer's write arrives as a
  `VFIO_USER_DMA_WRITE` to an MMIO address, which only the fork's
  `vfio_user_dma_write` dispatches (it uses `address_space_write`). Stock QEMU
  rejects it and every ringer write fails with `ENODEV`. The image variables are
  `DOORBELL_`-prefixed so a shared `.env` cannot hand this stack a stock QEMU.
- **The servers need the libvfio-user fork** (`vfu_mmio_write`). They run in
  that image, with the binaries bind-mounted.
- **Do not poll the ringer over MMIO while a job runs.** The ringer's write
  waits for QEMU's reply on the socket that also carries the guest's accesses to
  it. A guest read at that moment holds QEMU's lock waiting on the ringer while
  the ringer waits on QEMU, and both stall until QEMU's message timeout. The
  guest tool avoids this by having the ringer store a completion word into guest
  RAM, which it polls. `xio-doorbell ring --ringer vfu-doorbell,poll-mmio ...` reproduces the stall
  on purpose: expect most writes to fail with `EPROTO` and the ringer to log
  "not a command req".
- **libvfio-user warns once about the missing twin socket.** The first
  server-initiated write prints "without the twin_socket feature enabled"
  because QEMU's client does not negotiate the second socket. That is the
  collision the previous bullet describes, not a fault in the rig.
- **The guest port is 2223**, not 2222, so this stack can run beside
  `vfio-user-xio-vm`. The guest has 4 GiB of RAM; `VM_SHM_SIZE` must stay at
  least `VM_VMEM`.
- **Do not restart one server.** A guest that has lost a vfio-user device does
  not get it back; recreate the stack.

## Behind a TLS-intercepting proxy

The guest is an overlay on a published qcow2, which gets no cloud-init seed, so
`gen-vm --ca-cert` (and `VM_CA_CERT_FILE` given to `gen-vm`) does not put a CA
in it. Export the path before running the playbook and it is installed there:

```sh
VM_CA_CERT_FILE=/path/to/ca.crt ansible-playbook -i inventory/vfu-doorbell.yml \
    playbooks/vfu-doorbell-guest.yml
```

The rig itself needs no guest network; this only matters if you add steps that
download. Pulling images and the backing qcow2 happens on the host and uses the
host's trust store.

## Settings

Set in `qemu/env/vfu-doorbell`; every variable is optional.

| Variable | Default | Notes |
|---|---|---|
| `DOORBELL_QEMU_IMAGE` | `…qemu-libvfio-user-sbates-fork:qemu.4cb9698-vfu.3719c3a` | must be the fork |
| `DOORBELL_SERVER_IMAGE` | `…libvfio-user-sbates-fork:vfu.3719c3a` | needs `vfu_mmio_write` |
| `DOORBELL_BIN_DIR` | `/tmp/vfu-doorbell/bin` | `vfu-doorbell` and `xio-doorbell` |
| `VM_NAME` | `doorbell-1` | `$VM_IMAGES_DIR/$VM_NAME.qcow2`, made by `gen-vm` |
| `VM_VCPUS` / `VM_VMEM` | `4` / `4096` | |
| `VM_SHM_SIZE` | `6g` | at least `VM_VMEM` |
| `VM_SSH_PORT` | `2223` | |
