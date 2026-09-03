# Agent Context for qemu-minimal

Practical facts for AI agents (and humans) working in this repo. Keep this
file up to date as infrastructure changes.

---

## Repository layout

- `qemu/` — qemu-tool Python package (installed as a Debian package or via pipx)
- `qemu/compose/vfio-user-vm/` — single-VM vfio-user compose stack
- `qemu/compose/vfio-user-2vm/` — two-VM rocm-ernic mesh stack (primary dev target)
- `images/` — qcow2 VM disk images (backing + overlay pairs, **not** `/var/lib/qemu-tool/images`)
- `ansible/` — Ansible playbooks and profiles for VM provisioning
- `rocm-ernic-enablement.md` — running log of rocm-ernic integration status and bugs

## VM images

All qcow2 images live at **`<repo-root>/images/`** (i.e.
`/home/stebates/Projects/qemu-minimal/images/`), not at the default
`/var/lib/qemu-tool/images`. Set `VM_IMAGES_DIR` accordingly in
`qemu/compose/vfio-user-2vm/.env`.

Active images for the 2-VM ernic stack:

| VM   | Overlay                      | Backing                       |
|------|------------------------------|-------------------------------|
| VM1  | `stebates-ernic-vm-1.qcow2`   | `stebates-ernic-base.qcow2`   |
| VM2  | `stebates-ernic-vm-2.qcow2`   | `stebates-ernic-base.qcow2`   |

Base image is 12G (compressed from 28G used filesystem, 95G virtual).
Both VMs share the same backing; only delta writes go into their overlays.
Kernel: `7.0.0-31-generic` (HWE). Recreate overlays with:
```bash
qemu-img create -f qcow2 -b stebates-ernic-base.qcow2 -F qcow2 stebates-ernic-vm-1.qcow2
qemu-img create -f qcow2 -b stebates-ernic-base.qcow2 -F qcow2 stebates-ernic-vm-2.qcow2
```

SSH access (when the stack is running):

```
ssh -p 2222 stebates@localhost   # VM1
ssh -p 2223 stebates@localhost   # VM2
```

Password: see cloud-init user-data (plain_text_passwd field). GPG key in VM
is not unlocked (no private key injected), so git-crypt and signed commits
are not available inside the VM.

## Two-VM compose stack

```bash
cd qemu/compose/vfio-user-2vm
cp env.example .env
# Edit .env: set VM_IMAGES_DIR to the repo images/ absolute path
docker compose up -d
```

Key `.env` values that differ from the example defaults:

| Variable       | Correct value                                        |
|----------------|------------------------------------------------------|
| `VM_IMAGES_DIR`| `/home/stebates/Projects/qemu-minimal/images`        |
| `VM1_NAME`     | `stebates-ernic-vm-1`                                 |
| `VM2_NAME`     | `stebates-ernic-vm-2`                                 |

The ernic hub serves VM1 (`ernic-1.sock`); the worker serves VM2
(`ernic-2.sock`). VMs should be assigned IPs in `192.168.100.11/24` and
`192.168.100.12/24` on `enp1s0` — avoid `.1` (reserved as the hub DHCP
`server_ip`; ARP for `.1` is intercepted by the hub).

## rocm-ernic driver (in-VM, post-boot)

The DKMS kernel driver (`rocm_ernic_eth` + `rocm_ernic_rdma`) is installed
persistently in the VM images via:

```bash
# On host — copy driver source
scp -P 2222 -r ~/Projects/rocm-ernic/driver stebates@localhost:/tmp/rocm-ernic-driver

# On VM — DKMS install
VER=$(grep '^PACKAGE_VERSION=' /tmp/rocm-ernic-driver/dkms.conf | cut -d= -f2 | tr -d '"')
MOD=$(grep '^PACKAGE_NAME='    /tmp/rocm-ernic-driver/dkms.conf | cut -d= -f2 | tr -d '"')
sudo apt-get install -y linux-headers-$(uname -r) dkms
sudo mkdir -p /usr/src/${MOD}-${VER}
sudo cp -r /tmp/rocm-ernic-driver/. /usr/src/${MOD}-${VER}/
sudo dkms add ${MOD}/${VER} && sudo dkms build ${MOD}/${VER} && sudo dkms install ${MOD}/${VER}
```

Persistent module loading: `/etc/modules-load.d/rocm-ernic.conf` contains:
```
ib_core
rocm_ernic_eth
rocm_ernic_rdma
```

After reboot the Ethernet interface is renamed to `rocm-ernic0` and the IB
device to `rocm-rdma-ernic0` by the udev rule (see udev section below).

**Known kernel compatibility patches** (applied to the local driver source,
not yet upstream): `rocm_ernic_misc.c` needs `#if __has_include(<rdma/iter.h>)`
for kernels ≥ 7.0.0-31 where `rdma/iter.h` was split out; `rocm_ernic_main.c`
needs `__maybe_unused` on `rocm_ernic_alloc_hw_port_stats` and
`rocm_ernic_get_hw_stats` to suppress `-Werror=unused-function`.

## rocm-ernic userspace provider (in-VM)

The userspace verbs provider requires rdma-core **≥ 62.0** (Ubuntu 24.04
ships v50 — insufficient). Install by cloning rdma-core, applying the
rocm-ernic provider patch, and replacing the system libibverbs:

```bash
# On host
scp -P 2222 -r ~/Projects/rocm-ernic/rdma-core stebates@localhost:/tmp/rocm-ernic-rdma-core

# On VM
sudo apt-get install -y cmake ninja-build pkg-config libibverbs-dev librdmacm-dev
cd ~ && git clone --depth 1 --branch v62.0 \
  https://github.com/linux-rdma/rdma-core.git rdma-core-v62
bash /tmp/rocm-ernic-rdma-core/rocm-ernic-dv/apply-rocm-ernic-dv.sh ~/rdma-core-v62
cmake -S ~/rdma-core-v62 -B ~/rdma-core-v62/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
  -DNO_PYVERBS=1 -DENABLE_STATIC=0 -DNO_MAN_PAGES=1
ninja -C ~/rdma-core-v62/build -j$(nproc)
sudo ninja -C ~/rdma-core-v62/build install
sudo ldconfig
```

`-DNO_MAN_PAGES=1` avoids a pandoc prebuilt-file install error on systems
without pandoc. After install `ibv_devices` shows `rocm-rdma-ernic0`
(`IBVERBS_PRIVATE_59`). System `perftest` (`ib_send_bw` etc.) works without
`LD_LIBRARY_PATH` — safe in rocm_ernic-only VMs with no mlx5/efa hardware.

Note: `docs/testing.rst` in the rocm-ernic repo references
`LD_LIBRARY_PATH=/opt/rdma-core-ernic/lib` — that path does not exist;
the real install prefix is `/usr`. Ignore that instruction.

## udev rules (in-VM)

Install `99-rocm-ernic.rules` from `~/Projects/rocm-ernic/udev/` to give
the ernic devices stable names:

```bash
# On host
scp -P 2222 ~/Projects/rocm-ernic/udev/99-rocm-ernic.rules stebates@localhost:/tmp/

# On VM
sudo cp /tmp/99-rocm-ernic.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

After the next device event (or reboot): Ethernet interface → `rocm-ernic0`,
IB device → `rocm-rdma-ernic0`. Use these names with `ib_send_bw -d rocm-rdma-ernic0`.

## PCI ID (in-VM)

Install the rocm-ernic PCI ID so `lspci` shows `ROCm Emulated RDMA NIC`
instead of `Device 8000`:

```bash
# On host
scp -P 2222 ~/Projects/rocm-ernic/scripts/pci.ids.rocm-ernic stebates@localhost:/tmp/

# On VM — patch both pci.ids files lspci may use
for IDS in /usr/share/misc/pci.ids /usr/share/hwdata/pci.ids; do
  [ -f "$IDS" ] || continue
  grep -q '8000  ROCm Emulated RDMA NIC' "$IDS" && continue
  sudo sed -i '/^1022  Advanced Micro Devices/a\\t8000  ROCm Emulated RDMA NIC' "$IDS"
done
```

**Note:** `update-pciids` will overwrite these files — re-apply after each run.

## Known issues / open bugs

See `rocm-ernic-enablement.md` for the full tracking list. Short version:

1. Worker `server_ip` ARP hijack when VM IP = `192.168.100.1` — use `.11`/`.12`
2. ARP log printf aliasing in `pvrdma_eth.c:483-493` (cosmetic, misleading)
3. `apply-rocm-ernic-dv.sh` omitted `dc.c`/`rocm_ernic_dc.h` — fixed locally
4. rdma-core version inconsistency in rocm-ernic repo (62 vs 64 in different files)
5. `docs/testing.rst` LD_LIBRARY_PATH points to non-existent path

## Git / GitHub

- Default GitHub account: `sbates130272` (verify with `gh auth status`)
- GPG signing required on all commits (`-S`), signoff required (`-s`)
- Main branch: `main`; current work branch: `feat/two-vm-compose`
- Never use `--no-verify` or `--no-gpg-sign`
