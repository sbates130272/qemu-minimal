# vfio-user rocm-xio development VM — Docker Compose stack

One guest with everything [rocm-xio](https://github.com/ROCm/rocm-xio)'s
accelerator-initiated IO paths need, so a GPU kernel can be pointed at each of
them in turn:

| Service | What it serves | Socket |
|---|---|---|
| `rocjitsu-1` | emulated gfx1250 GPU (first GPU in the guest) | `/run/vfu/rocjitsu-1.sock` |
| `rocjitsu-2` | emulated gfx1250 GPU (second GPU) | `/run/vfu/rocjitsu-2.sock` |
| `ernic` | one emulated RDMA NIC, `loopback` backend | `/run/vfu/ernic-1.sock` |
| `spdk-nvme` | SPDK NVMe controller: one NVMe-KV namespace (NSID 1) | `/run/vfu/nvme/cntrl` |
| `qemu` | the guest, plus QEMU's own NVMe drive and `pci-mmio-bridge` | — |

The four xio paths this exists for: GPU to GPU, GPU to the ernic, GPU to QEMU's
NVMe, GPU to the SPDK NVMe. **None of them has been run on this stack yet.**
What has been checked is that the four servers come up healthy as non-root with
no capabilities, that SPDK reports `nsid map: 1=kv`, and that the compose
file's invariants hold (`tests/test_xio_stack.py`). Whether a guest boots with
all of it attached, and whether any xio path works, is for the first run to
find out.

## Quick start

Settings live in `qemu/env/xio`: the guest image (`VM_BACKING_IMAGE`), sizes,
and the pinned server images.

```sh
# 1. The guest image: pulls the published ernic-rocjitsu qcow2 (~2.5 GB), makes
#    the overlay, and fetches the key the image already accepts into
#    <images>/keys/. Add --ssh-key-file <pub> to install your own instead (one
#    30 s boot; created if it does not exist).
qemu-tool gen-vm --env-file qemu/env/xio

# 2. The stack.
qemu-tool compose --stack vfio-user-xio-vm --env-file qemu/env/xio up -d

# 3. Make the guest ready and check every device is there.
cd ansible && ansible-playbook -i inventory/xio.yml playbooks/xio-guest.yml

# 4. Log in with the key gen-vm installed.
qemu-tool ssh xio-1
```

Step 3 installs the corporate CA from `$VM_CA_CERT_FILE` (when set), reloads
`ionic` if the wrong one is running, installs the GPU firmware, addresses the
ernic, and loads `amdgpu`. It then clones rocm-xio (`ROCM_XIO_REF` in
`qemu/env/xio`), builds `xio-tester`, installs and loads the `rocm-xio` kernel
module, and fails unless it finds 2 GPUs and 2 NVMe controllers. The build takes
a while the first time and is skipped when `~/rocm-xio/build/xio-tester`
exists. Skip it with `--skip-tags build`; redo it with `-e xio_rebuild=true`.
An existing `~/rocm-xio` checkout is never updated or touched. It has not been
run against a guest yet.

Take it down with `qemu-tool compose --stack vfio-user-xio-vm --env-file
qemu/env/xio down`. Do not restart a single server: a guest that has lost a
vfio-user device does not get it back, and the ernic wedges the guest's RDMA
driver until the whole stack is recreated (AGENTS.md issues 6 and 7).

Then run `xio-tester` from `~/rocm-xio/build`. rocm-xio's own `local-setup.md`
(commit `0c18d2d`) covers the guest side. ROCm is under `/opt/rocm/core-<version>`, not `/opt/rocm`. The IB
device keeps its kernel name (for example `rocep1s0`); read it from
`ibv_devinfo -l`.

## The two NVMe controllers

They are not interchangeable, and rocm-xio treats them differently.

**QEMU's own NVMe** (`--nvme 1`, a sparse qcow2 under `VM_IMAGES_DIR`). Its BAR0
lives in QEMU's address space, which a GPU store cannot reach. `nvme-ep` has to
be run with `--pci-mmio-bridge`, which is why the stack passes that flag and why
it needs the fork QEMU image.

**The SPDK controller** has one namespace, **NSID 1, NVMe-KV**:

- rocm-xio takes `--namespace 1`. KV Store and Retrieve are opcodes `0x01` and
  `0x02`, the same as block Write and Read, so a KV command sent to an LBA
  namespace would run as a block write.
- A Linux guest makes no block device for a KV namespace. `nvme list` shows it
  with no capacity, and `dmesg` says `block device for nsid 1 not supported
  (csi 1)`. That is normal. KV traffic goes through the generic node
  (`/dev/ng*n1`) or a passthrough ioctl that names the NSID.
- **Controller names are not stable** (`nvme0` and `nvme1` swap between boots).
  The playbook installs udev rules that name them: the SPDK controller is
  **`/dev/xio-kv`** (matches `XIO_SPDK_SERIAL`) and QEMU's NVMe disk is
  **`/dev/xio-blk`** (matches the model `QEMU NVMe Ctrl`).
- **A KV value must be 128 KiB or smaller.** The vfio-user transport rejects
  anything larger with status `0x06`. rocm-xio's `--value-size` follows
  `--data-buffer-size` and defaults to 1 MiB, so pass a smaller one.
- KV is memory-backed only (`kv:mem`). There is no file-backed KV namespace.
- To see what actually reached SPDK, from the host:
  `qemu-tool compose --stack vfio-user-xio-vm exec spdk-nvme rpc.py
  kvdev_mem_get_entry KvMem0 <key>`. A non-zero `code` means the command did not
  arrive or failed.

There is no LBA namespace on purpose. With one beside the KV namespace, the
guest kernel logs `Command set not reported for nsid:1` and makes no block
device for it (measured). Use QEMU's NVMe when you need a block device.

## Settings

Set in `qemu/env/xio`; every variable is optional. The image variables are `XIO_`-prefixed on
purpose: the other stacks read `QEMU_IMAGE`, `ERNIC_IMAGE` and `ROCJITSU_IMAGE`,
and `qemu/env/example` pins those to the **stock** images. A shared `.env` would
hand this stack a QEMU with no `pci-mmio-bridge`, and nothing would say so until
a test failed.

| Variable | Default | Notes |
|---|---|---|
| `XIO_QEMU_IMAGE` | `…qemu-libvfio-user-sbates-fork:qemu.f91f120-vfu.3719c3a` | must be the fork |
| `XIO_ROCJITSU_IMAGE` | `…rocm-rocjitsu-sbates-fork:rocjitsu.3141e64-vfu.3719c3a` | both GPUs |
| `XIO_SPDK_IMAGE` | `…spdk-libvfio-user-sbates-fork:spdk.18d1d8d-vfu.3719c3a` | NVMe-KV target; includes the host doorbell fd |
| `XIO_ERNIC_IMAGE` | `…rocm-ernic:20261022.gc533d98-ernic.151379b-vfu.8039244` | the quickstart's |
| `XIO_ROCJITSU_CONFIG` | `gfx1250_mi455x.json` | one config for both GPUs |
| `XIO_NVME_NAMESPACES` | `kv:mem` | see above before adding an LBA namespace |
| `XIO_SPDK_SERIAL` | `XIO-KV` | what `/dev/xio-kv` matches on; max 20 chars |
| `VM_NAME` | `xio-1` | the guest image, `$VM_IMAGES_DIR/$VM_NAME.qcow2`; must already exist |
| `VM_VCPUS` / `VM_VMEM` | `4` / `8192` | `VM_VCPUS` must stay at least 4 or `ionic_rdma` never probes |
| `VM_SHM_SIZE` | `12g` | must be at least `VM_VMEM` |
| `VM_SSH_PORT` | `2222` | |
| `VM_NVME_TRACE` | off | `doorbell` or `all`; writes a line per doorbell to the log |

## Things this stack does not establish

- **Peer-to-peer DMA between vfio-user devices needs the fork QEMU**, which this
  stack uses, but nothing in this repo has run a P2P workload against it
  (AGENTS.md issue 19). Do not read "it boots" as "GPU to NVMe works".
- **The topology is flat** (one root port per device). A shared PCIe switch is a
  different P2P path; `run-vm`'s `--vfio-user-switch-width` builds one, and this
  stack does not pass it yet.
- **The ernic is loopback only.** One NIC and no peer, so a GPU-to-ernic run
  proves the verbs and never the mesh.
- **The two ernic images differ from the others in libvfio-user version** (the
  ernic is built against `vfu.8039244`, the fork images against `vfu.3719c3a`).
  QEMU is the client and implements the protocol itself, so this should not
  matter; it has not been tested beyond the servers starting.
