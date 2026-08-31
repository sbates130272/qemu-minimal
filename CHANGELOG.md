# Changelog

All notable changes to this project will be documented in this file.

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
