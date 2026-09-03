# rocm-ernic Enablement Tracking

Items needed to fully enable rocm-ernic in the vfio-user compose stack,
split by where the fix belongs.

---

## VM disk image (qcow2 / cloud-init)

- [ ] **Kernel headers pre-installed** (`linux-headers-$(uname -r)`) so the
  rocm-ernic driver can be built inside the VM without a manual apt step.
  Target package: `linux-headers-generic` (tracks the running kernel).

- [ ] **rocm-ernic driver installed via DKMS**. The `dkms.conf` in `driver/`
  of the rocm-ernic repo already supports this. Manual steps until automated:

  ```bash
  # On host — copy driver source to VM
  scp -P 2222 -r ~/Projects/rocm-ernic/driver stebates@localhost:/tmp/rocm-ernic-driver

  # On VM — install kernel headers and DKMS
  sudo apt-get install -y linux-headers-$(uname -r) dkms

  # On VM — register and install via DKMS
  VER=$(grep '^PACKAGE_VERSION=' /tmp/rocm-ernic-driver/dkms.conf | cut -d= -f2 | tr -d '"')
  MOD=$(grep '^PACKAGE_NAME=' /tmp/rocm-ernic-driver/dkms.conf | cut -d= -f2 | tr -d '"')
  sudo mkdir -p /usr/src/${MOD}-${VER}
  sudo cp -r /tmp/rocm-ernic-driver/. /usr/src/${MOD}-${VER}/
  sudo dkms add ${MOD}/${VER}
  sudo dkms build ${MOD}/${VER}
  sudo dkms install ${MOD}/${VER}
  ```

  DKMS installs both `rocm_ernic_eth.ko.zst` and `rocm_ernic_rdma.ko.zst`
  to `/lib/modules/$(uname -r)/updates/dkms/` and rebuilds on kernel upgrade.

  Long-term: either ship the driver source in the VM image and build on first
  boot via cloud-init `runcmd`, or have the ernic container image provide a
  pre-built tarball pushed to the VM via an `ernicctl driver-push` command.

- [x] **ib_core and ernic modules loaded on boot** — done via
  `/etc/modules-load.d/rocm-ernic.conf`:
  ```
  ib_core
  rocm_ernic_eth
  rocm_ernic_rdma
  ```
  DKMS builds for all installed kernels on upgrade. Two source patches required
  for kernel ≥ 7.0.0-31 compatibility (applied locally, not yet upstream):
  - `rocm_ernic_misc.c`: add `#if __has_include(<rdma/iter.h>) #include <rdma/iter.h> #endif` — `rdma/iter.h` was split out from `rdma/ib_umem.h` in 7.0.0-31.
  - `rocm_ernic_main.c`: add `__maybe_unused` to `rocm_ernic_alloc_hw_port_stats` and `rocm_ernic_get_hw_stats` — `-Werror=unused-function` promoted in 7.0.0-31 build env.
  - `rocm_ernic_misc.c`: replace `goto exit` / `exit:` pattern with direct `return ret` / `return 0` — prevents `-Werror=return-type` false positive.

- [x] **libibverbs / perftest installed** — rdma-core v62 installed
  system-wide (`CMAKE_INSTALL_PREFIX=/usr -DNO_MAN_PAGES=1`), replacing
  system v50. IB device enumerates as `rocm-rdma-ernic0` (stable name via
  udev rule). System `perftest` (`ib_send_bw` etc.) works directly.

- [x] **udev rules installed** — `99-rocm-ernic.rules` from
  `rocm-ernic/udev/` installed at `/etc/udev/rules.d/`. Renames Ethernet
  interface to `rocm-ernic0` and IB device to `rocm-rdma-ernic0` on boot.

- [x] **PCI ID installed** — `scripts/pci.ids.rocm-ernic` applied to
  `/usr/share/misc/pci.ids` and `/usr/share/hwdata/pci.ids`. `lspci` now
  shows `ROCm Emulated RDMA NIC` for device `1022:8000`. Note:
  `update-pciids` will overwrite — re-apply after each run.

- [x] **Unique MAC per VM — driver pick-up confirmed**. The compose file
  passes `-m` to each ernic instance; the driver correctly reads the MAC from
  BAR1. IP assignment persisted via `/etc/netplan/60-ernic.yaml` (`.11`/`.12`).

---

## Container images (batesste-ci-images)

### `batesste-ci-images-ubuntu-rocm-ernic`

- [x] **Ethernet relay between VMs is working**. Ping between
  `192.168.100.11` (VM1/hub) and `192.168.100.12` (VM2/worker) succeeds
  with ~4–6 ms RTT. Key findings from debugging:

  - The BAR1 write path, TCP relay, and frame inject paths are all correctly
    implemented. Earlier suspicions about missing register handling were wrong.
  - The `WARN: rdma: Ethernet RX not enabled` at startup is a red herring —
    it fires before the vfio-user client connects and does not affect
    steady-state operation.
  - **Worker ARP hijack** (`pvrdma_eth.c:469-474`): the worker hardcodes
    `server_ip = inet_addr("192.168.100.1")` (fallback when no DHCP server).
    VMs must not use `.1` as their IP or the worker intercepts ARPs for VM1.
    Current workaround: use `.11`/`.12`. Fix: stop the worker from claiming
    `server_ip` when it has no DHCP server, or add a CLI flag to pin it.
  - **ARP log printf aliasing bug** (`pvrdma_eth.c:483-493`): `inet_ntoa()`
    is called three times in one `rdma_info_report()` argument list. All three
    `%s` fields print the last-evaluated address (target IP) due to the
    static buffer. Fix: `strncpy` each result into a local buffer before
    formatting (the ICMP/DHCP paths at lines 756-760 and 872-878 already do
    this correctly).

- [ ] **`ERNIC_DEBUG_MESH` passthrough in compose** — added `environment:`
  block to both `ernic-hub` and `ernic-worker` in `docker-compose.yml` so
  setting `ERNIC_DEBUG_MESH=1` in `.env` enables mesh debug logging without
  rebuilding. Remove from `.env` (or set to `0`) for normal operation.

- [ ] **Expose verbose logging flag** (`-v` / `ERNIC_LOG_LEVEL`). Not yet
  implemented; `ERNIC_DEBUG_MESH` partially fills this gap for mesh paths.

- [x] **RDMA userspace provider built and installed**. The rocm-ernic repo
  ships a userspace verbs provider in `rdma-core/providers/rocm_ernic/`
  with an `apply-rocm-ernic-dv.sh` script to graft it into an rdma-core
  source tree. One bug fixed locally, one upstream clarification:

  1. **`apply-rocm-ernic-dv.sh` omits `dc.c` and `rocm_ernic_dc.h`** — both
     files are required by `CMakeLists.txt` but the script's per-file copy
     list excluded them. Fixed by replacing the per-file `cp -v` lines with
     `cp -rv "${PROVIDER_SRC}/." "${DEST}/"` so future additions are not
     silently missed. Fixed in `rocm-ernic-dv/apply-rocm-ernic-dv.sh`.

  2. **`verbs.c:277` — 8-arg `ibv_cmd_reg_dmabuf_mr`** is correct for
     rdma-core ≥62 (CI builds against 62–64). The v50 system libibverbs
     has the 7-arg form and rejects the build — this is a version
     requirement, not a bug. Do **not** change `verbs.c`; build against
     rdma-core ≥62 as below.

  **Build procedure (manual steps until automated):**

  ```bash
  # On host — copy provider source to VM
  scp -P 2222 -r ~/Projects/rocm-ernic/rdma-core \
    stebates@localhost:/tmp/rocm-ernic-rdma-core

  # On VM
  sudo apt-get install -y cmake ninja-build pkg-config \
    libibverbs-dev librdmacm-dev ibverbs-utils
  cd /tmp && git clone --depth 1 --branch v50.0 \
    https://github.com/linux-rdma/rdma-core.git rdma-core-src
  bash /tmp/rocm-ernic-rdma-core/rocm-ernic-dv/apply-rocm-ernic-dv.sh \
    /tmp/rdma-core-src
  cd /tmp/rdma-core-src
  cmake -B build-provider -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr -DNO_PYVERBS=1 -DENABLE_STATIC=0
  ninja -C build-provider rocm_ernic
  # v62 uses the -rdmav59 ABI suffix; install the real .so under that name
  sudo cp build-provider/lib/librocm_ernic.so.1.0.62.0 \
    /usr/lib/x86_64-linux-gnu/librocm_ernic-rdmav59.so
  sudo bash -c 'echo "driver /usr/lib/x86_64-linux-gnu/librocm_ernic" \
    > /etc/libibverbs.d/rocm_ernic.driver'
  sudo ldconfig
  ```

  After install: `ibv_devices` enumerates `rocm_ernic0` with unique GUIDs
  (`01006dfeff636f70` / `02006dfeff636f70`) on both VMs with no warnings,
  provided the system `libibverbs.so.1` remains at v50 (do not replace it
  with the v62 build — that breaks the system perftest and ibverbs-providers
  packages which require `IBVERBS_PRIVATE_34`).

- [x] **RDMA verbs bandwidth and latency tests** — complete. Install rdma-core
  62.0 with `CMAKE_INSTALL_PREFIX=/usr -DNO_MAN_PAGES=1` (replacing system v50;
  safe in rocm_ernic-only guests, system perftest is unaffected). After install
  `ibv_devices` shows `rocm-rdma-ernic0` (stable name via udev rule) with
  `IBVERBS_PRIVATE_59`. Run perftest directly — no `LD_LIBRARY_PATH` needed.

  Results (kernel 7.0.0-30 ↔ 7.0.0-31, RC transport, 65536 B default message):

  | Test            | Peak BW      | Avg BW / Latency       |
  |-----------------|-------------|------------------------|
  | `ib_send_bw`    | 337 MB/s    | 245 MB/s               |
  | `ib_write_bw`   | 367 MB/s    | 335 MB/s               |
  | `ib_read_bw`    | 419 MB/s    | 319 MB/s               |
  | `ib_send_lat`   | 395 µs min  | 603 µs avg             |
  | `ib_write_lat`  | 227 µs min  | 397 µs avg             |

  These numbers reflect emulated vfio-user + TCP relay overhead, not wire speed.

- [x] **TCP throughput testing** — iperf3 installed on both VMs. Tested
  with 4 parallel streams: ~16 Mbit/s aggregate, zero retransmits. Use
  `systemd-run` to keep the server alive past SSH session teardown
  (`KillUserProcesses=yes` is set in the VM image).

### `batesste-ci-images-ubuntu-qemu-libvfio-user`

- [x] **pipx and python3-setuptools pre-installed** — done; qemu-tool now
  skips pip install if already in PATH.

- [x] **qemu-tool pre-installed** — done via pipx in updated image.

---

## qemu-minimal compose stack

- [x] Distinct MACs per ernic instance via `ERNIC1_MAC` / `ERNIC2_MAC`
  env vars.

- [x] **ARP/Ethernet connectivity between VMs** — working. Use IPs outside
  the `192.168.100.1` address (reserved as hub `server_ip`) e.g. `.11`/`.12`.
  Persisted via `/etc/netplan/60-ernic.yaml` in each VM.

- [x] **Driver load on VM boot** — persistent via DKMS +
  `/etc/modules-load.d/rocm-ernic.conf`. Survives kernel upgrades.

- [ ] **RDMA verbs end-to-end test** (`ib_write_bw`, `ib_send_bw`) between
  the two VMs over the rocm-ernic RDMA device.

- [ ] **TCP throughput test** (`iperf3`) over the ernic Ethernet interface
  between the two VMs.

- [ ] **Automate driver install in VM image** — currently requires manual
  DKMS registration after each fresh VM. Target: cloud-init `runcmd` that
  fetches and installs the driver on first boot, or ship via Ansible role.
