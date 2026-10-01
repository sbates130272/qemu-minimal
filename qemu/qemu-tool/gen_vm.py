"""VM image generation using cloud-init.

This module exclusively uses the Ubuntu cloud-init provisioning workflow.
No legacy disk-injection methods (virt-sysprep, guestfish, chroot, etc.)
are supported. All three operating modes (normal, restore-image,
backing-file) build on images that were originally provisioned by
cloud-init.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from .caps import qemu_binary
from .config import VMConfig
from .identity import identity_args
from .run_vm import (
    DISCARD_OPTS,
    _effective_mac,
    _ensure_mgmt_bridge,
    _mgmt_bridge_name,
    _mgmt_tap_name,
    _netdev_args,
    _teardown_mgmt_bridge,
)

# Supported Ubuntu release codenames. Other codenames or XX.YY version
# strings are also accepted by _resolve_cloud_image() but are untested.
KNOWN_RELEASES = {"noble", "resolute"}

_ARCH_MAP = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "riscv64": "riscv64",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(cfg: VMConfig) -> None:
    _validate(cfg)
    _prepare_ansible(cfg)

    images = Path(cfg.images)
    images.mkdir(parents=True, exist_ok=True)

    if cfg.restore_image:
        _restore_image(cfg, images)
        return

    if cfg.ansible_only:
        if cfg.ansible_playbook is None:
            sys.exit("Error: --ansible-only requires --ansible-playbook")
        backing = images / f"{cfg.vm_name}-backing.qcow2"
        if not backing.exists():
            sys.exit(f"Error: --ansible-only requires an existing backing image at {backing}")
        _run_ansible(cfg, images, backing)
        _compact_backing(cfg, backing)
        overlay = images / f"{cfg.vm_name}.qcow2"
        if cfg.no_backing:
            backing.rename(overlay)
        else:
            _create_overlay(overlay, backing)
        return

    if cfg.backing_image is not None:
        backing = _fetch_backing_image(cfg, images)
        _create_overlay(images / f"{cfg.vm_name}.qcow2", backing)
        return

    if cfg.backing_file is not None:
        _create_overlay(
            images / f"{cfg.vm_name}.qcow2",
            cfg.backing_file,
        )
        return

    cloud_img_file, cloud_img_url = _resolve_cloud_image(cfg)
    _download_if_needed(cfg, images, cloud_img_file, cloud_img_url)

    backing = images / f"{cfg.vm_name}-backing.qcow2"
    (images / cloud_img_file).rename(backing) if False else None
    subprocess.run(
        ["cp", str(images / cloud_img_file), str(backing)], check=True
    )
    subprocess.run(
        ["qemu-img", "resize", str(backing), f"{cfg.size}G"], check=True
    )

    ssh_key = Path(cfg.ssh_key_file).expanduser()
    if not ssh_key.exists():
        sys.exit(f"Error: SSH key file {ssh_key} does not exist!")

    packages = _load_packages(cfg)

    with ExitStack() as stack:
        cloud_cfg_path = Path(f"cloud-config-{cfg.vm_name}")
        net_cfg_path = Path(f"network-config-{cfg.vm_name}")
        seed_path = images / f"{cfg.vm_name}-seed.qcow2"
        stack.callback(_cleanup, cloud_cfg_path, net_cfg_path, seed_path)

        _write_cloud_config(cfg, packages, ssh_key, cloud_cfg_path)
        _write_network_config(cfg, net_cfg_path)
        _create_seed_iso(cfg, images, cloud_cfg_path, net_cfg_path)
        _first_boot(cfg, images, backing)

    _run_ansible(cfg, images, backing)
    _compact_backing(cfg, backing)

    overlay = images / f"{cfg.vm_name}.qcow2"
    if cfg.no_backing:
        backing.rename(overlay)
    else:
        _create_overlay(overlay, backing)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _restore_image(cfg: VMConfig, images: Path) -> None:
    if cfg.no_backing:
        sys.exit("Error: --restore-image and --no-backing cannot both be set.")
    backing = images / f"{cfg.vm_name}-backing.qcow2"
    if not backing.exists():
        sys.exit(f"Error: Backing file {backing} does not exist!")
    overlay = images / f"{cfg.vm_name}.qcow2"
    if overlay.exists():
        _check_not_in_use(overlay)
    print(f"Creating new image with backing file: {overlay}")
    _create_overlay(overlay, backing)
    print(f"Successfully created {overlay} with backing file {backing}")


def _fetch_backing_image(cfg: VMConfig, images: Path) -> Path:
    """Pull a published backing qcow2 from a registry, decompressed.

    The artifact is not a runnable image -- artifactType
    application/vnd.batesste.vm-image.v1, one zstd layer -- so it needs `oras`,
    not `docker pull`. Same job as _download_if_needed(), different source.
    """
    backing = images / f"{cfg.vm_name}-backing.qcow2"
    if backing.exists() and not cfg.force:
        print(f"Backing image already present: {backing} (use --force to refetch)")
        return backing

    for tool in ("oras", "zstd"):
        if not _which(tool):
            sys.exit(
                f"Error: --backing-image needs '{tool}', which is not on PATH.\n"
                "Install it, or fetch the qcow2 yourself and use --backing-file."
            )

    with tempfile.TemporaryDirectory(dir=images) as tmp:
        tmpdir = Path(tmp)
        print(f"Pulling {cfg.backing_image}")
        subprocess.run(["oras", "pull", cfg.backing_image, "-o", str(tmpdir)], check=True)

        compressed = sorted(tmpdir.glob("*.qcow2.zst"))
        plain = sorted(tmpdir.glob("*.qcow2"))
        if compressed:
            src = compressed[0]
            print(f"Decompressing {src.name}")
            # Straight to the final name: -o writes the output path itself, so
            # there is no second full-size copy of a ~12G image.
            subprocess.run(
                ["zstd", "-d", "-f", str(src), "-o", str(backing)], check=True
            )
        elif plain:
            subprocess.run(["cp", str(plain[0]), str(backing)], check=True)
        else:
            found = ", ".join(p.name for p in sorted(tmpdir.iterdir())) or "nothing"
            sys.exit(
                f"Error: {cfg.backing_image} has no .qcow2 or .qcow2.zst layer "
                f"(pulled: {found})."
            )

    print(f"Backing image ready: {backing}")
    return backing


def _create_overlay(overlay: Path, backing: Path) -> None:
    ts = _save_timestamp(backing)
    subprocess.run(
        ["qemu-img", "create", "-F", "qcow2", "-b", str(backing.resolve()), "-f", "qcow2",
         str(overlay)],
        check=True,
    )
    if ts is not None:
        os.utime(backing, (ts, ts))


def _save_timestamp(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _allocated_bytes(path: Path) -> int:
    """Bytes the image actually occupies on disk, not its virtual size.

    st_blocks is in 512-byte units on every platform regardless of the
    filesystem block size. This is the number that matters here: a baked
    backing image is 64G virtual and ~20G allocated, so a free-space check
    against the virtual size would refuse on any sane disk.
    """
    return path.stat().st_blocks * 512


def _gib(nbytes: int) -> str:
    return f"{nbytes / (1 << 30):.2f} GiB"


def _compact_backing(cfg: VMConfig, backing: Path) -> None:
    """Recompress the freshly baked backing image in place.

    -c (zlib) is right *because* this is a backing file: in normal use every
    guest write lands in the overlay, so nothing rewrites a compressed cluster
    and the usual "compressed qcow2 is slow to write" objection does not apply.
    A ROCm tree is mostly ELF, which compresses well. If decompression ever
    shows up in guest boot time, -o compression_type=zstd is the next lever.

    One path does write to it: --ansible-only re-enters _run_ansible on an
    existing backing and boots it as the writable root disk, with no overlay
    in front. If that image was compacted by an earlier bake, the run pays for
    it -- a sub-cluster write read-decompresses its 64 KiB cluster and
    allocates a fresh uncompressed one, so the image inflates while Ansible
    runs. The call below is what recovers it: the mode compacts again at the
    end, and --no-compact is there for a rebuild loop that would rather keep
    the image uncompressed throughout.

    This flattens nothing. The backing image is a byte copy of the downloaded
    cloud image (see run()) and Ubuntu cloud images are standalone qcow2, so
    there is no chain underneath to collapse — the convert is a pure
    recompress and the result depends on exactly what it depended on before.
    (--backing-file, which *is* handed a caller-supplied image that may have a
    chain, returns from run() long before here and is never rewritten.)

    A failure must never fail the bake: by this point an hour of Ansible is
    already on disk and an uncompacted image is perfectly usable. Every path
    warns, removes the temporary file and leaves the original untouched.
    """
    if not cfg.compact:
        return
    if cfg.no_backing:
        # This image is about to *become* the overlay, so the guest will write
        # to it. Compressing clusters that are going to be rewritten is the
        # one case where -c is actively the wrong choice.
        return

    before = _allocated_bytes(backing)
    free = shutil.disk_usage(backing.parent).free
    if free < before:
        print(f"Warning: skipping compaction of {backing.name}: the convert "
              f"holds both copies at once, needing up to {_gib(before)}, and "
              f"only {_gib(free)} is free.")
        return

    # Sibling temp file, so os.replace() is an atomic same-filesystem rename
    # and an interrupted convert cannot destroy an image that took an hour to
    # bake.
    tmp = backing.parent / f"{backing.name}.compact"
    ts = _save_timestamp(backing)
    print(f"Compacting {backing.name} ({_gib(before)} allocated)...")
    try:
        subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", "-c",
             str(backing), str(tmp)],
            check=True,
        )
        # The cheap post-condition: qemu-img info both proves the result parses
        # as qcow2 and gives us the virtual size to compare. A full qemu-img
        # check would read every referenced cluster of a 20G image for a
        # failure mode convert does not have.
        if _virtual_size(tmp) != _virtual_size(backing):
            raise ValueError("virtual size changed across the convert")
        os.replace(tmp, backing)
        if ts is not None:
            os.utime(backing, (ts, ts))
    except FileNotFoundError:
        print("Error: qemu-img not found; leaving "
              f"{backing.name} uncompacted.")
        return
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        print(f"Error: compaction of {backing.name} failed ({exc}); the "
              "original image is intact and unchanged.")
        return
    finally:
        tmp.unlink(missing_ok=True)

    after = _allocated_bytes(backing)
    ratio = before / after if after else 0.0
    print(f"Compacted {backing.name}: {_gib(before)} -> {_gib(after)} "
          f"({ratio:.2f}x)")


def _virtual_size(path: Path) -> int:
    r = subprocess.run(
        ["qemu-img", "info", "--output=json", "-U", str(path)],
        capture_output=True, text=True, check=True,
    )
    return int(json.loads(r.stdout)["virtual-size"])


def _check_not_in_use(path: Path) -> None:
    for cmd in (["fuser", str(path)], ["lsof", str(path)]):
        try:
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0:
                sys.exit(
                    f"Error: {path} is currently in use by another process!"
                )
            return
        except FileNotFoundError:
            continue
    print(f"Warning: cannot check if {path} is in use (fuser/lsof not found).")


def _cleanup(cloud_cfg: Path, net_cfg: Path, seed: Path) -> None:
    for p in (cloud_cfg, net_cfg, seed):
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Cloud image resolution + download
# ---------------------------------------------------------------------------

def _resolve_cloud_image(cfg: VMConfig) -> tuple[str, str]:
    release = cfg.release
    arch = cfg.arch
    if re.match(r"^\d+\.\d+$", release):
        fname = f"ubuntu-{release}-server-cloudimg-{arch}.img"
        url = f"https://cloud-images.ubuntu.com/releases/{release}/release/{fname}"
    else:
        fname = f"{release}-server-cloudimg-{arch}.img"
        url = f"https://cloud-images.ubuntu.com/{release}/current/{fname}"
    return fname, url


def _download_if_needed(
    cfg: VMConfig, images: Path, fname: str, url: str
) -> None:
    target = images / fname
    if cfg.force or not target.exists():
        target.unlink(missing_ok=True)
        subprocess.run(
            ["wget", "--timeout=60", "--tries=3", "--waitretry=10",
             "-P", str(images), url],
            check=True,
        )


# ---------------------------------------------------------------------------
# Cloud-config + seed ISO
# ---------------------------------------------------------------------------

def _load_packages(cfg: VMConfig) -> str:
    if cfg.packages is None or cfg.packages == "none":
        pkgs = ""
    elif Path(cfg.packages).exists():
        pkgs = Path(cfg.packages).read_text()
    else:
        sys.exit(f"Error: package manifest {cfg.packages} does not exist!")

    if not re.search(r"^\s*-\s*qemu-guest-agent\s*$", pkgs, re.MULTILINE):
        if pkgs:
            pkgs += "\n"
        pkgs += "  - qemu-guest-agent"
    return pkgs


def _ca_cert_fragment(cfg: VMConfig) -> tuple[str, str]:
    """Return (write_files_entry, runcmd_entry) for an extra CA cert, or ('', '')."""
    if cfg.ca_cert_file is None:
        return "", ""
    cert_path = Path(cfg.ca_cert_file).expanduser()
    if not cert_path.exists():
        sys.exit(f"Error: --ca-cert file {cert_path} does not exist!")
    cert_b64 = base64.b64encode(cert_path.read_bytes()).decode()
    cert_name = cert_path.name
    write_entry = f"""\
  - path: /usr/local/share/ca-certificates/{cert_name}
    encoding: b64
    content: {cert_b64}
    owner: root:root
    permissions: '0644'"""
    runcmd_entry = "  - update-ca-certificates"
    return write_entry, runcmd_entry


def _write_cloud_config(
    cfg: VMConfig, packages: str, ssh_key: Path, out: Path
) -> None:
    key_content = ssh_key.read_text().rstrip()
    key_list = "\n".join(
        f"      - {k}" for k in key_content.splitlines() if k.strip()
    )
    ca_write, ca_runcmd = _ca_cert_fragment(cfg)
    out.write_text(f"""\
#cloud-config
hostname: {cfg.vm_name}
disable_root: true
ssh_pwauth: true
users:
  - name: {cfg.username}
    plain_text_passwd: '{cfg.password}'
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    uid: {cfg.user_id}
    groups: users, admin
    shell: /bin/bash
    ssh_authorized_keys:
{key_list}
apt:
  conf: |
    APT::Install-Recommends "false";
    APT::Install-Suggests "false";
ntp:
  enabled: true
packages:
{packages}
runcmd:
  - systemctl disable openipmi.service
  - systemctl mask openipmi.service
  - loginctl enable-linger {cfg.username}
{ca_runcmd}
power_state:
  delay: now
  mode: poweroff
  message: Shutting down
  timeout: 2
  condition: true
timezone: America/Edmonton
write_files:
{ca_write}
  - path: /etc/sysctl.d/10-kernel-hardening.conf
    content: 'kernel.dmesg_restrict = 0'
    owner: root:root
    permissions: 0o644
    append: true
    defer: true
  - path: /etc/systemd/logind.conf.d/99-kill-user-processes.conf
    content: |
      [Login]
      KillUserProcesses=yes
    owner: root:root
    permissions: 0o644
    defer: true
  - path: /etc/systemd/system.conf.d/99-timeout.conf
    content: |
      [Manager]
      DefaultTimeoutStopSec=15s
    owner: root:root
    permissions: 0o644
    defer: true
  - path: /etc/netplan/50-cloud-init.yaml
    content: |
      network:
        version: 2
        ethernets:
          qemu-tool-en:
            match:
              name: "en*"
            dhcp4: true
    owner: root:root
    permissions: 0o600
    append: false
    defer: true
  - path: /home/{cfg.username}/.emacs
    content: |
      ;; enable syntax highlighting
      (global-font-lock-mode 1)
      ;; show line and column numbers in mode line
      (line-number-mode 1)
      (column-number-mode 1)
      ;; force emacs to always use spaces instead of tab characters
      (setq-default indent-tabs-mode nil)
      ;; set default tab width to 4 spaces
      (setq default-tab-width 4)
      (setq tab-width 4)
      ;; default to showing trailing whitespace
      (setq-default show-trailing-whitespace t)
      ;; default to auto-fill-mode on in all major modes
      (setq-default auto-fill-function 'do-auto-fill)
    owner: {cfg.username}:{cfg.username}
    permissions: 0o644
    append: false
    defer: true
""")


def _write_network_config(cfg: VMConfig, out: Path) -> None:
    # Guests have been observed ignoring this and falling back to cloud-init's
    # own generated config, which pins the interface to the MAC it saw at
    # creation time. run-vm derives that MAC from --ssh-port, so changing the
    # port later leaves the NIC unmanaged with no address and the VM
    # unreachable. The write_files entry above overwrites the rendered
    # 50-cloud-init.yaml late in first boot with a name match, which is what
    # actually takes effect; this file is kept because a guest that does honour
    # it gets the right config from the start.
    out.write_text("""\
version: 2
ethernets:
  eth0:
    match:
      name: en*
    dhcp4: true
""")


def _create_seed_iso(
    cfg: VMConfig, images: Path, cloud_cfg: Path, net_cfg: Path
) -> None:
    seed = images / f"{cfg.vm_name}-seed.qcow2"
    subprocess.run(
        ["cloud-localds", "-d", "qcow2", str(seed),
         str(cloud_cfg), str(net_cfg)],
        check=True,
    )


# ---------------------------------------------------------------------------
# First boot (cloud-init)
# ---------------------------------------------------------------------------

def _first_boot(cfg: VMConfig, images: Path, backing: Path) -> None:
    kvm = ",accel=kvm" if cfg.kvm else ""
    qarch = _ARCH_MAP[cfg.arch]
    qemu = qemu_binary(cfg)
    arch_args = _arch_args_for_gen(cfg, kvm)
    seed = images / f"{cfg.vm_name}-seed.qcow2"
    cmd = [
        qemu,
        *arch_args,
        "-smp", f"cpus={cfg.vcpus}",
        "-m", str(cfg.vmem),
        "-nographic",
        # The seed drive deliberately does not get DISCARD_OPTS: _cleanup
        # deletes it, so there is nothing to reclaim.
        "-drive", f"if=virtio,format=qcow2,file={backing}{DISCARD_OPTS}",
        "-drive", f"if=virtio,format=qcow2,file={seed}",
        "-netdev", "user,id=net0",
        "-device", f"virtio-net-pci,netdev=net0,mac={_effective_mac(cfg)}",
    ]
    # Without KVM, emulation is slow enough that cloud-init's rootfs expand
    # (growpart + resize2fs) can take 30+ minutes on arm64/riscv64. Cap the
    # wait so a hung guest produces a clear error rather than silently burning
    # the CI job timeout.
    boot_timeout = None if cfg.kvm else 3300  # 55 min; job ceiling is 60 min
    try:
        subprocess.run(cmd, check=True, timeout=boot_timeout)
    except subprocess.TimeoutExpired:
        sys.exit(
            f"Error: first boot timed out after {boot_timeout}s on "
            f"{cfg.arch} (no KVM). cloud-init did not complete — "
            f"check rootfs expansion (growpart/resize2fs) in the "
            f"QEMU console log."
        )


def _arch_args_for_gen(cfg: VMConfig, kvm: str) -> list[str]:
    if cfg.arch == "amd64":
        return ["-machine", f"q35{kvm}"]
    if cfg.arch == "arm64":
        return [
            "-machine", f"virt,gic-version=max{kvm}",
            "-cpu", "max",
            "-bios", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
        ]
    if cfg.arch == "riscv64":
        # Avoid a trailing comma in the machine string when kvm is empty.
        machine = f"virt{kvm}" if kvm else "virt"
        return [
            "-machine", machine,
            "-kernel", "/usr/lib/u-boot/qemu-riscv64_smode/uboot.elf",
        ]
    sys.exit(f"Error: no ARCH mapping for '{cfg.arch}'")


# ---------------------------------------------------------------------------
# Ansible
# ---------------------------------------------------------------------------

def _prepare_ansible(cfg: VMConfig) -> None:
    if cfg.ansible_playbook is None:
        return
    playbook = Path(cfg.ansible_playbook)
    if not playbook.exists():
        sys.exit(f"Error: ansible playbook {playbook} does not exist!")
    for tool in ("ansible-playbook", "ansible-galaxy"):
        if not _which(tool):
            sys.exit(f"Error: {tool} not found (required for --ansible-playbook)!")


def _discover_vm_ip(bridge: str, mac: str, timeout: int = 120) -> str:
    print(f"Waiting for VM (MAC {mac}) to appear on {bridge}...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["ip", "neigh", "show", "dev", bridge],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if mac.lower() in line.lower():
                ip = line.split()[0]
                print(f"VM IP discovered: {ip}")
                return ip
        time.sleep(2)
    sys.exit(f"Error: VM with MAC {mac} did not appear on {bridge} within {timeout}s.")


def _run_ansible(cfg: VMConfig, images: Path, backing: Path) -> None:
    if cfg.ansible_playbook is None:
        return

    playbook = Path(cfg.ansible_playbook).resolve()
    # Derive ansible_dir as the grandparent of the playbook
    # (ansible/playbooks/foo.yml → ansible/)
    ansible_dir = playbook.parent.parent
    inventory = ansible_dir / "inventory/qemu-minimal-vms.yml"
    extra_args = os.environ.get("ANSIBLE_EXTRA_ARGS", "")
    timeout = 600

    for p, label in (
        (playbook, "playbook"),
        (inventory, "inventory"),
        (ansible_dir / "requirements.yml", "requirements"),
    ):
        if not p.exists():
            sys.exit(f"Error: Ansible {label} {p} does not exist!")

    _ensure_jmespath()
    _ensure_ansible_collection(ansible_dir)

    if cfg.mgmt_tap:
        _ensure_mgmt_bridge(cfg.ssh_port)

    print(f"Booting {backing} for Ansible setup...")
    kvm = ",accel=kvm" if cfg.kvm else ""
    arch_args = _arch_args_for_gen(cfg, kvm)
    qemu_proc = subprocess.Popen([
        qemu_binary(cfg),
        *identity_args("gen-vm", cfg.vm_name),
        *arch_args,
        "-smp", f"cpus={cfg.vcpus}",
        "-m", str(cfg.vmem),
        "-nographic",
        "-drive", f"if=virtio,format=qcow2,file={backing}{DISCARD_OPTS}",
        *_netdev_args(cfg),
    ])

    try:
        vm_host = "localhost"
        vm_port = cfg.ssh_port
        if cfg.mgmt_tap:
            vm_ip = _discover_vm_ip(
                _mgmt_bridge_name(cfg.ssh_port),
                _effective_mac(cfg),
                timeout,
            )
            vm_host = vm_ip
            vm_port = 22

        if not _wait_for_ssh(cfg, timeout, host=vm_host, port=vm_port):
            qemu_proc.kill()
            qemu_proc.wait()
            sys.exit("Error: VM did not accept SSH in time.")

        print(f"Running Ansible playbook {playbook}...")
        _restore_blocking_stdio()
        _ensure_jmespath()

        id_args = _ssh_identity_args(cfg)
        private_key_extra = (
            ["-e", f"ansible_ssh_private_key_file={id_args[1]}"]
            if id_args else []
        )
        ap_cmd = ["ansible-playbook", "-i", str(inventory), str(playbook),
                  "-e", f"ansible_host={vm_host}",
                  "-e", f"ansible_port={vm_port}",
                  "-e", f"ansible_user={cfg.username}",
                  "-e", f"username={cfg.username}",
                  "-e", f"vm_username={cfg.username}",
                  "-e", f"vm_root_user={cfg.username}",
                  *private_key_extra]
        if extra_args:
            ap_cmd += extra_args.split()

        ansible_env = {
            k: v for k, v in os.environ.items()
            if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        }
        ansible_env["ANSIBLE_CONFIG"] = str(ansible_dir / "ansible.cfg")
        r = subprocess.run(ap_cmd, cwd=str(ansible_dir), env=ansible_env)
        if r.returncode != 0:
            qemu_proc.kill()
            qemu_proc.wait()
            sys.exit("Error: Ansible playbook failed.")

        print("Powering off VM after Ansible setup...")
        subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             *_ssh_identity_args(cfg),
             "-p", str(vm_port),
             f"{cfg.username}@{vm_host}", "sudo poweroff"],
            capture_output=True,
        )
        qemu_proc.wait()
        if cfg.mgmt_tap:
            _teardown_mgmt_bridge(cfg.ssh_port)
        print("Ansible post-setup complete.")
    except Exception:
        qemu_proc.kill()
        qemu_proc.wait()
        if cfg.mgmt_tap:
            _teardown_mgmt_bridge(cfg.ssh_port)
        raise


def _ssh_identity_args(cfg: VMConfig) -> list[str]:
    key_pub = Path(cfg.ssh_key_file).expanduser()
    # Strip .pub to get the private key; fall back to no -i if missing.
    private_key = key_pub.parent / key_pub.stem
    if private_key.exists():
        return ["-i", str(private_key)]
    return []


def _wait_for_ssh(
    cfg: VMConfig, timeout: int, host: str = "localhost", port: int | None = None
) -> bool:
    p = port if port is not None else cfg.ssh_port
    print(f"Waiting for VM to accept SSH at {host}:{p}...")
    id_args = _ssh_identity_args(cfg)
    elapsed = 0
    while elapsed < timeout:
        try:
            r = subprocess.run(
                ["ssh",
                 "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=1",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 *id_args,
                 "-p", str(p),
                 f"{cfg.username}@{host}", "true"],
                capture_output=True,
                timeout=5,
            )
            if r.returncode == 0:
                print(f"VM ready for Ansible after {elapsed} seconds.")
                return True
        except subprocess.TimeoutExpired:
            # SSH connected (ConnectTimeout=1 would have fired otherwise)
            # but background login processes kept the channel open.
            # The VM is up.
            print(f"VM ready for Ansible after {elapsed} seconds.")
            return True
        time.sleep(2)
        elapsed += 2
    return False


def _ensure_ansible_collection(ansible_dir: Path) -> None:
    _restore_blocking_stdio()
    print("Installing Ansible collections from requirements.yml...")
    subprocess.run(
        # --no-deps: requirements.yml names every collection the playbooks
        # reach, so galaxy must not also pull the unused ones our roles'
        # collection happens to declare. See the comment in requirements.yml.
        #
        # No --upgrade: with it, galaxy contacts galaxy.ansible.com on every
        # gen-vm even when the installed collections already satisfy the
        # requirements, which defeats both the CI cache and anything baked
        # into the CI image, and puts a network service on the critical path
        # of an otherwise local build. Without it, a satisfied requirements
        # file is an offline no-op; an unsatisfied one still resolves and
        # installs a matching version. Bump the floors in requirements.yml to
        # pull a newer collection, or run ansible-galaxy --upgrade by hand.
        ["ansible-galaxy", "collection", "install", "--pre",
         "--no-deps", "-r", str(ansible_dir / "requirements.yml")],
        check=True,
    )


def _ensure_jmespath() -> None:
    py = _ansible_python()
    r = subprocess.run([py, "-c", "import jmespath"], capture_output=True)
    if r.returncode == 0:
        return
    for flag in ("--break-system-packages", "--user"):
        r2 = subprocess.run([py, "-m", "pip", "install", flag, "jmespath"],
                            capture_output=True)
        r3 = subprocess.run([py, "-c", "import jmespath"], capture_output=True)
        if r3.returncode == 0:
            print(f"Installed jmespath via pip ({flag}).")
            return
    sys.exit(
        f"Error: {py} cannot import jmespath.\n"
        f"Install with: {py} -m pip install --break-system-packages jmespath"
    )


def _ansible_python() -> str:
    import shutil
    ap = shutil.which("ansible-playbook")
    if ap:
        first_line = Path(ap).read_text().splitlines()[0]
        if first_line.startswith("#!"):
            py = first_line[2:].strip()
            if Path(py).is_file() and os.access(py, os.X_OK):
                return py
    return "python3"


def _restore_blocking_stdio() -> None:
    for fd in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass


def _which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


def _validate(cfg: VMConfig) -> None:
    if cfg.restore_image and cfg.no_backing:
        sys.exit("Error: --restore-image and --no-backing cannot both be set.")
    if cfg.backing_file is not None and cfg.no_backing:
        sys.exit("Error: --backing-file and --no-backing cannot both be set.")
    if cfg.backing_file is not None and not Path(cfg.backing_file).exists():
        sys.exit(f"Error: --backing-file {cfg.backing_file} does not exist!")
    if cfg.backing_image is not None and cfg.backing_file is not None:
        sys.exit("Error: --backing-image and --backing-file cannot both be set.")
    if cfg.backing_image is not None and cfg.no_backing:
        sys.exit("Error: --backing-image and --no-backing cannot both be set.")
    # Warn on unknown releases; XX.YY version strings are always accepted.
    if (
        cfg.backing_file is None
        and cfg.backing_image is None
        and not cfg.restore_image
        and not re.match(r"^\d+\.\d+$", cfg.release)
        and cfg.release not in KNOWN_RELEASES
    ):
        print(
            f"WARNING: release '{cfg.release}' is not a known tested release "
            f"({', '.join(sorted(KNOWN_RELEASES))}). Proceeding anyway.",
            file=sys.stderr,
        )
