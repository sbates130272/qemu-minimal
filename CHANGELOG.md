# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- `ionic_image_prep`, a repo-local Ansible role that prepares a guest image for
  rocm-ernic's ionic mode: a pinned mainline kernel from `kernel.ubuntu.com`,
  the DKMS and rdma-core toolchain, the `1dd8:100a` `pci.ids` entry, the
  `modules-load.d` drop-in and the rdma-core provider stamp. Collection 0.2.0
  removed `ernic_image_prep` and now assumes an already-prepared guest; this
  repo builds its own with `gen-vm`, so it owns that preparation.
- `smoke-test-rocm-two-vms` and `vm-report-two-vms` workflows, replacing
  `2vm-smoke-test` and `2vm-vm-report`. Guest vCPU and memory are derived from
  the runner rather than fixed, with a floor of 4 vCPUs per guest.
- `ansible/playbooks/vars/ernic-pins.yml`: the mainline kernel ref and the
  rocm-ernic source commit, each written once and read by both plays.

### Changed

- **rocm-ernic is now ionic-based.** Upstream deleted the out-of-tree
  `rocm_ernic_eth`/`rocm_ernic_rdma` drivers and the patched verbs provider on
  2026-09-16. The guest now builds upstream `ionic` + `ionic_rdma` as the
  `ionic-ernic` DKMS package and uses stock rdma-core's `providers/ionic`; the
  emulated device moved from `1022:8000` to Pensando `1dd8:100a`.
- `ansible-playbook-test` split into per-profile workflows (`-rocm`,
  `-rocjitsu`, `-ernic`, `-ernic-rocjitsu`) sharing a `setup-vm-job` action,
  and every workflow renamed to a consistent `qemu-minimal - <name>` form.
- `requirements.yml` pins the rocm-ernic collection to an upstream git SHA.
  Galaxy publishes only 0.1.0, which is the pre-ionic collection, so the
  previous `>=0.1.0` could never have resolved to 0.2.0.
- ernic guests now get 4 vCPUs. `ionic_lif_size()` derives its EQ count from
  `num_online_cpus()` and `ionic_create_rdma_admin()` rejects fewer than
  `IONIC_EQ_COUNT_MIN`, so at 2 vCPUs `ionic_rdma` could never probe.

### Fixed

- The ernic configure play supplies `ernic_guest_vm_ip`, which
  `ernic_guest_setup` asserts on and defaults to empty. Derived as
  `192.168.200.<10 * vm_index>`, matching upstream `vm-register.yml`.
- `ernic_source_repo_version` is pinned to the same commit as the collection.
  It defaults to `main`, so the sources built in the guest could come from a
  different tree than the roles building them.
- `ionic_image_prep` no longer corrupts `pci.ids`. hwdata already lists
  `1dd8:100a` (as `DSC Serial Port Controller` — the emulated NIC reuses a real
  pair), and the presence check grepped for our own entry text, so it never
  matched and appended a duplicate device id. pciutils refuses to parse a file
  containing one, which left `lspci` resolving no names for any device on the
  bus. The check is now a block-scoped `awk` scan for the device under its
  vendor, and a post-merge `lspci` parse check fails the play if a merge ever
  does break the file.

## [v1.3.0] - 2026-09-15

### Added

- `qemu-tool list` subcommand: lists every `qemu-system-*` process on the node,
  including VMs inside containers (attributed back to the container name), with
  vCPU count, memory, forwarded SSH port, KVM state, uptime and attached
  vfio-user sockets. `--json` for machine-readable output, `--qemu-tool-only`
  to filter.
- VM identity markers: `run-vm` and `gen-vm` now stamp each VM with
  `-name guest=<vm-name>,debug-threads=on` and a deterministic
  `-uuid <uuid5(namespace, "<role>:<vm-name>")>`. `list` recomputes the UUID
  from the guest name to identify qemu-tool's VMs exactly, and to distinguish a
  transient `gen-vm` build VM from a running `run-vm` VM. Both options are
  generic, so they work on amd64, arm64 and riscv64 alike.

### Changed

- `--vm-name` now rejects a comma. A comma terminates a QEMU option value, so
  it silently corrupted `-name`, `-uuid`, `-drive file=` and `-chardev path=`.

### Fixed

- The Debian package declared no dependencies beyond the Python interpreter.
  It now `Depends` on a QEMU system emulator and `qemu-utils`, `Recommends`
  `cloud-image-utils`, `openssh-client` and `wget` for `gen-vm`, and `Suggests`
  `ansible`, `dnsmasq-base`, `docker.io` and `iproute2` for the optional paths.

## [v1.2.0] - 2026-08-31

### Added

- `python3-qemu-tool` Debian package for system-wide installation via `dpkg`/`apt`
- Man page `qemu-tool(1)` installed to `/usr/share/man/man1/`
- `qemu/packages.d/packages-default`: package manifest moved into `qemu/` so it
  is self-contained within the deb build tree; installed to
  `/usr/share/qemu-tool/packages-default`
- CI: parallel `build-deb` job in `release.yml` uploads the `.deb` to GitHub Releases

### Changed

- Default image directory changed from `../images` (repo-relative) to
  `/var/lib/qemu-tool/images`; the installer creates the directory owned
  `root:kvm` with mode `2775` (setgid for kvm group inheritance)
- Default package manifest path changed from the repo-relative path to
  `/usr/share/qemu-tool/packages-default`

## [v1.0.0] - 2026-08-05

### Added

- `qemu/gen-vm`: cloud-init based VM creation for x86_64, ARM64, and RISC-V
- `qemu/run-vm`: flexible QEMU runner with NVMe, VFIO, CXL, VirtFS, QMP, and
  multicast NIC support
- `libvirt/virt-install-ubuntu`: libvirt VM creation via `virt-install`
- `libvirt/create-nvme`: helper to generate NVMe XML fragments for libvirt
- `packages.d/packages-default`: broad development and debugging package manifest
- `packages.d/packages-minimal`: minimal package manifest (`emacs-nox`, `fio`,
  `sysstat`, `tree`)
- `ansible/playbooks/vm-setup.yml`: post cloud-init Ansible playbook using the
  `sbates130272.batesste` Galaxy collection
- `udev/99-qemu-minimal-vfio.rules`: udev rules for VFIO device permissions
- `udev/install-vfio-rules`: installer script for the udev rules
- Shared backing-file workflow allowing multiple VMs to share a single
  read-only qcow2
- `RESTORE_IMAGE` mode to recreate a VM overlay from an existing backing file
- CI workflows: shell-check, smoke-test (x86/arm64, Noble/Resolute),
  ansible-setup-test, spell-check, and release
