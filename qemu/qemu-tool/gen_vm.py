"""VM image generation using cloud-init.

This module exclusively uses the Ubuntu cloud-init provisioning workflow.
No legacy disk-injection methods (virt-sysprep, guestfish, chroot, etc.)
are supported. All three operating modes (normal, restore-image,
backing-file) build on images that were originally provisioned by
cloud-init.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from .caps import qemu_binary
from .config import VMConfig

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

    images = Path(cfg.images)
    images.mkdir(parents=True, exist_ok=True)

    if cfg.restore_image:
        _restore_image(cfg, images)
        return

    if cfg.ansible_only:
        if cfg.ansible_profile is None:
            sys.exit("Error: --ansible-only requires --ansible-profile")
        backing = images / f"{cfg.vm_name}-backing.qcow2"
        if not backing.exists():
            sys.exit(f"Error: --ansible-only requires an existing backing image at {backing}")
        _prepare_ansible(cfg)
        _run_ansible(cfg, images, backing)
        overlay = images / f"{cfg.vm_name}.qcow2"
        if cfg.no_backing:
            backing.rename(overlay)
        else:
            _create_overlay(overlay, backing)
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

    _prepare_ansible(cfg)
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


def _create_overlay(overlay: Path, backing: Path) -> None:
    ts = _save_timestamp(backing)
    subprocess.run(
        ["qemu-img", "create", "-F", "qcow2", "-b", str(backing), "-f", "qcow2",
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
        subprocess.run(["wget", "-P", str(images), url], check=True)


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
    indented_key = key_content.replace("\n", "\n      ")
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
    ssh_authorized_keys: |
      {indented_key}
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
timezone:
  America/Edmonton
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
    out.write_text("""\
version: 2
ethernets:
  eth0:
    match:
      name: en*
    dhcp4: true
    # default libvirt network
    gateway4: 192.168.122.1
    nameservers:
      addresses: [ 192.168.122.1,8.8.8.8 ]
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
        "-drive", f"if=virtio,format=qcow2,file={backing}",
        "-drive", f"if=virtio,format=qcow2,file={seed}",
        "-netdev", "user,id=net0",
        "-device", "virtio-net-pci,netdev=net0",
    ]
    subprocess.run(cmd, check=True)


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
        return [
            "-machine", f"virt,{kvm}",
            "-kernel", "/usr/lib/u-boot/qemu-riscv64_smode/uboot.elf",
        ]
    sys.exit(f"Error: no ARCH mapping for '{cfg.arch}'")


# ---------------------------------------------------------------------------
# Ansible
# ---------------------------------------------------------------------------

def _parse_ansible_profile(profile: Path) -> dict[str, str]:
    """Extract KEY=value or KEY=${KEY:-default} assignments from a shell profile."""
    result: dict[str, str] = {}
    for line in profile.read_text().splitlines():
        line = line.strip()
        m = re.match(r'^([A-Z_]+)=\$\{[A-Z_]+:-(.+?)\}$', line)
        if m:
            val = m.group(2).strip('"').strip("'")
            if not val.startswith("$"):
                result[m.group(1)] = val
            continue
        m = re.match(r'^([A-Z_]+)=(.+)$', line)
        if m:
            val = m.group(2).strip('"').strip("'")
            if not val.startswith("$"):
                result[m.group(1)] = val
    return result


def _prepare_ansible(cfg: VMConfig) -> None:
    if cfg.ansible_profile is None:
        return
    profile = Path(cfg.ansible_profile)
    if not profile.exists():
        sys.exit(f"Error: ANSIBLE_PROFILE {profile} does not exist!")
    for tool in ("ansible-playbook", "ansible-galaxy"):
        if not _which(tool):
            sys.exit(f"Error: {tool} not found (required for --ansible-profile)!")


def _run_ansible(cfg: VMConfig, images: Path, backing: Path) -> None:
    if cfg.ansible_profile is None:
        return

    profile = Path(cfg.ansible_profile)
    env = _parse_ansible_profile(profile)

    script_dir = Path(__file__).parent.parent  # qemu/
    ansible_dir = Path(env.get("ANSIBLE_DIR", "../ansible"))
    if not ansible_dir.is_absolute():
        ansible_dir = (script_dir / ansible_dir).resolve()

    playbook = env.get("ANSIBLE_PLAYBOOK", "playbooks/vm-setup.yml")
    inventory = env.get("ANSIBLE_INVENTORY", "inventory/qemu-vm.yml")
    tags = env.get("ANSIBLE_TAGS", "")
    extra_args = env.get("ANSIBLE_EXTRA_ARGS", "")
    ansible_username = env.get("ANSIBLE_USERNAME", cfg.username)
    timeout = int(env.get("ANSIBLE_TIMEOUT", "600"))

    for p, label in (
        (ansible_dir / playbook, "playbook"),
        (ansible_dir / inventory, "inventory"),
        (ansible_dir / "requirements.yml", "requirements"),
    ):
        if not p.exists():
            sys.exit(f"Error: Ansible {label} {p} does not exist!")

    _ensure_jmespath()
    _ensure_ansible_collection(ansible_dir)

    print(f"Booting {backing} for Ansible setup...")
    kvm = ",accel=kvm" if cfg.kvm else ""
    arch_args = _arch_args_for_gen(cfg, kvm)
    qemu_proc = subprocess.Popen([
        qemu_binary(cfg),
        *arch_args,
        "-smp", f"cpus={cfg.vcpus}",
        "-m", str(cfg.vmem),
        "-nographic",
        "-drive", f"if=virtio,format=qcow2,file={backing}",
        "-netdev", f"user,id=net0,hostfwd=tcp::{cfg.ssh_port}-:22",
        "-device", "virtio-net-pci,netdev=net0",
    ])

    try:
        if not _wait_for_ssh(cfg, timeout):
            qemu_proc.kill()
            qemu_proc.wait()
            sys.exit("Error: VM did not accept SSH in time.")

        print(f"Running Ansible playbook {playbook}...")
        _restore_blocking_stdio()
        _ensure_jmespath()

        ap_cmd = ["ansible-playbook", "-i", inventory, playbook,
                  "-e", f"ansible_port={cfg.ssh_port}",
                  "-e", f"ansible_user={cfg.username}",
                  "-e", f"username={ansible_username}",
                  "-e", f"vm_username={ansible_username}",
                  "-e", f"vm_root_user={cfg.username}"]
        if tags:
            ap_cmd += ["--tags", tags]
        if extra_args:
            ap_cmd += extra_args.split()

        r = subprocess.run(
            ap_cmd,
            cwd=str(ansible_dir),
            env={**os.environ, "ANSIBLE_CONFIG": str(ansible_dir / "ansible.cfg")},
        )
        if r.returncode != 0:
            qemu_proc.kill()
            qemu_proc.wait()
            sys.exit("Error: Ansible playbook failed.")

        print("Powering off VM after Ansible setup...")
        subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-p", str(cfg.ssh_port),
             f"{cfg.username}@localhost", "sudo poweroff"],
            capture_output=True,
        )
        qemu_proc.wait()
        print("Ansible post-setup complete.")
    except Exception:
        qemu_proc.kill()
        qemu_proc.wait()
        raise


def _wait_for_ssh(cfg: VMConfig, timeout: int) -> bool:
    print(f"Waiting for VM to accept SSH on port {cfg.ssh_port}...")
    elapsed = 0
    while elapsed < timeout:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=1",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-p", str(cfg.ssh_port),
             f"{cfg.username}@localhost", "true"],
            capture_output=True,
        )
        if r.returncode == 0:
            print(f"VM ready for Ansible after {elapsed} seconds.")
            return True
        time.sleep(2)
        elapsed += 2
    return False


def _ensure_ansible_collection(ansible_dir: Path) -> None:
    _restore_blocking_stdio()
    print("Installing/upgrading Ansible collections from requirements.yml...")
    subprocess.run(
        ["ansible-galaxy", "collection", "install", "--upgrade", "--pre",
         "-r", str(ansible_dir / "requirements.yml")],
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
    # Warn on unknown releases; XX.YY version strings are always accepted.
    if (
        cfg.backing_file is None
        and not cfg.restore_image
        and not re.match(r"^\d+\.\d+$", cfg.release)
        and cfg.release not in KNOWN_RELEASES
    ):
        print(
            f"WARNING: release '{cfg.release}' is not a known tested release "
            f"({', '.join(sorted(KNOWN_RELEASES))}). Proceeding anyway.",
            file=sys.stderr,
        )
