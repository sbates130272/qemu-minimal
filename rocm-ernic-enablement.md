# rocm-ernic Enablement Tracking

Items needed to fully enable rocm-ernic in the vfio-user compose stack,
split by where the fix belongs.

---

## VM disk image (qcow2 / cloud-init)

- [ ] **Kernel headers pre-installed** (`linux-headers-$(uname -r)`) so the
  rocm-ernic driver can be built inside the VM without a manual apt step.
  Target package: `linux-headers-generic` (tracks the running kernel).

- [ ] **rocm-ernic driver pre-built and installed**. Either:
  - Ship the `.ko` files (built for the target kernel) and a `modprobe.d`
    entry so they load on boot, **or**
  - Use DKMS (`dkms.conf` is already in the rocm-ernic driver directory)
    so the driver rebuilds automatically on kernel upgrades.
  Source: `driver/` in the rocm-ernic repo.

- [ ] **ib_core loaded on boot** (`/etc/modules` or a `systemd` unit).
  `rocm_ernic_rdma.ko` depends on `ib_core`; without it the RDMA module
  fails with "Unknown symbol" errors.

- [ ] **rdma-core / libibverbs installed** in the VM for RDMA userspace
  testing (`ibv_devices`, `ib_write_bw`, `perftest`, etc.).

- [ ] **Unique MAC per VM**. Currently the compose file passes `-m` to each
  ernic instance but the VM image has no awareness of this. Once the driver
  picks up the MAC from the device, ARP should work. Confirm MAC is read
  correctly from BAR1 device registers.

---

## Container images (batesste-ci-images)

### `batesste-ci-images-ubuntu-rocm-ernic`

- [ ] **Fix Ethernet RX enable handling in the server**. The TCP mesh forms
  correctly (hub registers worker, topology broadcast to 2 nodes). The guest
  driver writes the RX enable bit to BAR1 and logs
  `Ethernet IMR enabled for RX packets`, but the server logs
  `WARN: rdma: Ethernet RX not enabled` and drops all frames. The server's
  BAR1 register write handler is not honoring the Ethernet RX enable bit
  (`⚠ Full command processing (in progress)` in the feature list). Fix
  the BAR1 write handler in `rocm_ernic_compat.c` / `rocm_ernic_server.c`
  to set an internal `eth_rx_enabled` flag when the driver writes that bit.
  Until this is fixed, no Ethernet frames (including ARP) flow between VMs.

- [ ] **Expose verbose logging flag**. No `-v`/`ERNIC_LOG_LEVEL` env var is
  implemented yet; add one to help debug the Ethernet relay path.

### `batesste-ci-images-ubuntu-qemu-libvfio-user`

- [x] **pipx and python3-setuptools pre-installed** — done; qemu-tool now
  skips pip install if already in PATH.

- [x] **qemu-tool pre-installed** — done via pipx in updated image.

---

## qemu-minimal compose stack

- [x] Distinct MACs per ernic instance via `ERNIC1_MAC` / `ERNIC2_MAC`
  env vars.

- [ ] **ARP/Ethernet connectivity between VMs** — blocked on the
  `TCP_MSG_ETH_FRAME` relay issue above.

- [ ] **RDMA module loading** — `rocm_ernic_rdma.ko` fails with unknown
  `ib_*` symbols even after `modprobe ib_core`. May need additional RDMA
  stack modules (`ib_uverbs`, `rdma_cm`, `ib_umem` etc.) or a kernel build
  with `CONFIG_INFINIBAND=y`.

- [ ] **Automate driver load on VM boot** — currently requires manual
  `insmod` after each VM restart. A `systemd` unit in the VM image or a
  cloud-init runcmd would fix this.
