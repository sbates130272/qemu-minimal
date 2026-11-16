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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from .vm_artifact import VM_INFO_ARTIFACT_TYPE, VM_PUB_NAME
from .caps import qemu_binary
from .config import VMConfig
from .identity import identity_args
from .ports import AUTO_SSH_PORT_ATTEMPTS, ssh_hostfwd_refused
from .run_vm import (
    DISCARD_OPTS,
    _effective_mac,
    _ensure_mgmt_bridge,
    _mgmt_bridge_name,
    _mgmt_tap_name,
    _netdev_args,
    _teardown_mgmt_bridge,
    resolve_ssh_port,
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
        generated = _inherit_key(cfg, images / f"{cfg.vm_name}.json")
        _run_ansible(cfg, images, backing)
        _compact_backing(cfg, backing)
        overlay = images / f"{cfg.vm_name}.qcow2"
        if cfg.no_backing:
            backing.rename(overlay)
        else:
            _create_overlay(overlay, backing)
        _write_vm_info(cfg, images, generated)
        return

    if cfg.backing_image is not None:
        backing = _fetch_backing_image(cfg, images)
        _create_overlay(images / f"{cfg.vm_name}.qcow2", backing)
        pulled = _fetch_backing_keypair(cfg, images)
        if cfg.ssh_key_file is None:
            cfg.ssh_key_file = pulled
        _write_vm_info(cfg, images)
        return

    if cfg.backing_file is not None:
        _create_overlay(
            images / f"{cfg.vm_name}.qcow2",
            cfg.backing_file,
        )
        for sidecar in image_sidecars(Path(cfg.backing_file)):
            _inherit_key(cfg, sidecar)
        _write_vm_info(cfg, images)
        return

    # Before the download: a missing manifest should not cost a cloud image.
    _check_packages_exist(cfg)
    cloud_img_file, cloud_img_url = _resolve_cloud_image(cfg)
    _download_if_needed(cfg, images, cloud_img_file, cloud_img_url)

    backing = images / f"{cfg.vm_name}-backing.qcow2"
    # A marker left by an earlier pull under this name would label the bake
    # as a pulled image, which publish-qcow refuses.
    backing.with_suffix(".qcow2.image-ref").unlink(missing_ok=True)
    # Nor may the record of an earlier bake outlive the image it described:
    # publish-qcow would take a key from it that this image does not accept.
    (images / f"{cfg.vm_name}.json").unlink(missing_ok=True)
    (images / cloud_img_file).rename(backing) if False else None
    subprocess.run(
        ["cp", str(images / cloud_img_file), str(backing)], check=True
    )
    subprocess.run(
        ["qemu-img", "resize", str(backing), f"{cfg.size}G"], check=True
    )

    ssh_key, generated = image_key(cfg, images)

    packages = _load_packages(cfg)

    with ExitStack() as stack:
        cloud_cfg_path = Path(f"cloud-config-{cfg.vm_name}")
        net_cfg_path = Path(f"network-config-{cfg.vm_name}")
        seed_path = images / f"{cfg.vm_name}-seed.qcow2"
        stack.callback(_cleanup, cloud_cfg_path, net_cfg_path, seed_path)

        _write_cloud_config(cfg, packages, ssh_key, cloud_cfg_path)
        _check_cloud_config(cloud_cfg_path)
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
    _write_vm_info(cfg, images, generated)


# ---------------------------------------------------------------------------
# The image's SSH key
# ---------------------------------------------------------------------------

def generated_key_path(images: Path, vm_name: str) -> Path:
    """Private half of the keypair gen-vm generates for *vm_name*'s image.

    <images>/keys/ is where a pulled image's keypair goes too, and where
    inspect looks for a guest's key.
    """
    return images / "keys" / f"{vm_name}_id_rsa"


def image_key(cfg: VMConfig, images: Path) -> tuple[Path, bool]:
    """(public key a bake authorises, whether gen-vm generated it).

    A key somebody named is used as it is. Otherwise a keypair is generated
    for this image, replacing the one an earlier bake under the same name
    left: that bake's image has just been overwritten, so its key opens
    nothing any more. A named key removes that earlier pair for the same
    reason. cfg.ssh_key_file is set either way, which is what the Ansible
    run and the sidecar read.
    """
    # Resolved, so that the path the sidecar records means the same thing
    # from whatever directory publish-qcow is later run in.
    private = generated_key_path(images.resolve(), cfg.vm_name)
    made = (private, private.with_name(private.name + ".pub"))
    if cfg.ssh_key_file is not None:
        public = Path(cfg.ssh_key_file).expanduser()
        if not public.exists():
            sys.exit(f"Error: SSH key file {public} does not exist!")
        if public.resolve() != made[1]:
            for stale in made:
                stale.unlink(missing_ok=True)
        cfg.ssh_key_file = public
        return public, False

    public = made[1]
    private.parent.mkdir(parents=True, exist_ok=True)
    for stale in made:
        stale.unlink(missing_ok=True)
    try:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "rsa", "-N", "",
             "-C", f"qemu-tool {cfg.vm_name}", "-f", str(private)],
            check=True, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        sys.exit("Error: gen-vm needs 'ssh-keygen' to generate the image's "
                 "key; install it, or name a key with --ssh-key-file.")
    private.chmod(0o600)
    cfg.ssh_key_file = public
    print(f"Generated an SSH key for this image: {private} "
          f"('qemu-tool ssh {cfg.vm_name}' uses it)")
    return public, True


def image_sidecars(image: Path) -> list[Path]:
    """Where gen-vm would have recorded the key of the qcow2 *image*."""
    stem = image.name.removesuffix(".qcow2")
    return [image.with_name(f"{name}.json")
            for name in dict.fromkeys((stem.removesuffix("-backing"), stem))]


def _fingerprint(public: Path) -> str | None:
    """SHA256 fingerprint of *public*; None where it cannot be had."""
    try:
        out = subprocess.run(["ssh-keygen", "-lf", str(public)],
                             capture_output=True, text=True)
    except OSError:
        return None
    fields = out.stdout.split()
    return fields[1] if out.returncode == 0 and len(fields) > 1 else None


def _inherit_key(cfg: VMConfig, sidecar: Path) -> bool:
    """Take the key *sidecar* records, unless one was named; True if generated.

    For a guest that bakes nothing and so accepts whatever its image does.
    """
    if cfg.ssh_key_file is not None:
        return False
    try:
        info = json.loads(sidecar.read_text())
        recorded = info.get("ssh_key_file")
    except (OSError, ValueError, AttributeError):
        return False
    if not recorded:
        return False
    cfg.ssh_key_file = Path(recorded)
    return info.get("ssh_key_generated") is True


# ---------------------------------------------------------------------------
# VM info sidecar
# ---------------------------------------------------------------------------

def _write_vm_info(cfg: VMConfig, images: Path, generated: bool = False) -> None:
    """Write <vm-name>.json alongside the qcow2 for inspect to read.

    Records the username and SSH public key path *this gen-vm run was
    configured with*, so inspect does not need the settings file that was active
    here. That is not the same as what the image accepts: the restore and
    backing-file paths above write this file without creating any image
    content, and a guest booted from an overlay made by some earlier run, or
    from an image whose keys were injected by Ansible, accepts whatever is in
    its own authorized_keys. inspect therefore treats a live guest's answer as
    authoritative over this file -- printing this as fact once sent a user into
    three password prompts on a fleet guest that only takes the fleet key.
    """
    key = (Path(cfg.ssh_key_file).expanduser()
           if cfg.ssh_key_file is not None else None)
    info = {
        "username": cfg.username,
        "ssh_key_file": str(key) if key is not None else None,
        # The key itself, not only where it was: publish-qcow refuses a
        # pair that has been replaced at that path since.
        "ssh_key_fingerprint": _fingerprint(key) if key is not None else None,
        # True only on the image this run baked with a key it made itself:
        # it is what lets publish-qcow publish the key without being told
        # which. An overlay records false, whatever its backing image says.
        "ssh_key_generated": generated,
    }
    info_path = images / f"{cfg.vm_name}.json"
    info_path.write_text(json.dumps(info, indent=2) + "\n")
    print(f"VM info written: {info_path}")


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
    ref_file = backing.with_suffix(".qcow2.image-ref")

    if backing.exists() and not cfg.force:
        cached_ref = ref_file.read_text().strip() if ref_file.exists() else ""
        if cached_ref == cfg.backing_image:
            print(f"Backing image up to date: {backing}")
            return backing
        if cached_ref:
            print(f"Backing image tag changed ({cached_ref!r} -> {cfg.backing_image!r}), re-pulling")
        else:
            print(f"Backing image present but no tag recorded, re-pulling")
        # Remove overlays built on the old backing so they are recreated fresh.
        for stale in [images / f"{cfg.vm_name}.qcow2",
                      *sorted(images.glob(f"{cfg.vm_name}-nvme*.qcow2"))]:
            if stale.exists():
                print(f"Removing stale overlay: {stale.name}")
                stale.unlink()

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

    ref_file.write_text(cfg.backing_image + "\n")
    print(f"Backing image ready: {backing}")
    return backing


_VM_INFO_TYPE = VM_INFO_ARTIFACT_TYPE


def _fetch_backing_keypair(cfg: VMConfig, images: Path) -> Path | None:
    """Pull the guest keypair shipped as an ORAS referrer beside the qcow2.

    Returns the public key, or None when there is no keypair to be had.

    The keypair is placed in <images>/keys/ so inspect._local_key_candidates
    finds it via the glob of img.parent/keys/ and qemu-tool ssh can connect
    without a password prompt. Both the private and public halves are written:
    inspect only offers an identity when the private key file exists.
    """
    assert cfg.backing_image is not None
    keys_dir = images / "keys"
    ref_file = keys_dir / "image-ref"

    if ref_file.exists() and ref_file.read_text().strip() == cfg.backing_image:
        cached = sorted(keys_dir.glob("*.pub"))
        if cached:
            print(f"Guest keypair already at {keys_dir}")
            return _pulled_public_key(keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)

    if not _which("oras"):
        print(
            "Note: 'oras' not on PATH — cannot fetch the guest keypair. "
            "qemu-tool ssh will prompt for a password."
        )
        return None

    discovered = subprocess.run(
        ["oras", "discover", cfg.backing_image, "--format", "json"],
        capture_output=True, text=True,
    )
    if discovered.returncode != 0:
        print(
            f"Note: oras discover failed on {cfg.backing_image}; "
            "qemu-tool ssh will prompt for a password."
        )
        return None

    parsed = json.loads(discovered.stdout)
    referrers = parsed.get("referrers") or parsed.get("manifests", [])
    hits = [r["reference"] for r in referrers
            if r.get("artifactType") == _VM_INFO_TYPE]
    if not hits:
        print(
            f"Note: no {_VM_INFO_TYPE} referrer found on {cfg.backing_image}; "
            "qemu-tool ssh will prompt for a password."
        )
        return None

    with tempfile.TemporaryDirectory(dir=images) as tmp:
        subprocess.run(["oras", "pull", hits[0], "-o", tmp], check=True)
        private_src = next(
            (p for p in sorted(Path(tmp).iterdir()) if p.suffix != ".pub"
             and not p.name.endswith(".json") and p.stat().st_size > 0),
            None,
        )
        if private_src is None:
            print("Note: keypair referrer had no private key file; "
                  "qemu-tool ssh will prompt for a password.")
            return None

        stem = private_src.stem or private_src.name
        private = keys_dir / stem
        pub = keys_dir / f"{stem}.pub"

        import shutil
        shutil.copyfile(private_src, private)
        private.chmod(0o600)

        if not pub.is_file():
            derived = subprocess.run(
                ["ssh-keygen", "-y", "-f", str(private)],
                capture_output=True, text=True,
            )
            if derived.returncode == 0:
                pub.write_text(derived.stdout)

    ref_file.write_text(cfg.backing_image + "\n")
    print(f"Guest keypair ready: {keys_dir}")
    return pub if pub.is_file() else None


def _pulled_public_key(keys_dir: Path) -> Path | None:
    pub = keys_dir / VM_PUB_NAME
    return pub if pub.is_file() else None


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

def _as_yaml_list(manifest: str) -> str:
    """Normalise a package manifest to the YAML list cloud-config wants.

    Two formats exist. packages-default is already a YAML fragment, one
    "  - name" per line. rocm-golden.txt is one bare name per line. The text
    is pasted under "packages:" in the cloud-config, so a bare name there
    makes the whole document invalid YAML -- and cloud-init then ignores all
    of it, user and SSH key included, leaving a guest that boots to a login
    prompt nothing can get past.
    """
    out = []
    for line in manifest.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("-"):
            out.append(line)
        else:
            out.append(f"  - {text}")
    return "\n".join(out)


def _check_cloud_config(path: Path) -> None:
    """Refuse to boot a guest on user-data cloud-init will not parse.

    cloud-init does not fail on invalid YAML; it logs a warning inside the
    guest and applies none of it, so the bake waits on a guest with no user,
    no key and no reason to power off. Skipped where PyYAML is missing.
    """
    try:
        import yaml
    except ImportError:
        return
    try:
        parsed = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        sys.exit(f"Error: the generated cloud-config is not valid YAML: {exc}\n"
                 "Check the --packages manifest.")
    if not isinstance(parsed, dict):
        sys.exit("Error: the generated cloud-config is not a YAML mapping.")


def _missing(what: str, path: Path, sources: str) -> str:
    """The error for a file a flag or a fleet element named that is not there."""
    message = f"Error: {what} {path} does not exist!"
    if not path.is_absolute():
        message += (
            f"\n       It is relative, and {sources} are resolved against "
            f"the current directory ({Path.cwd()})."
        )
    return message


def _check_packages_exist(cfg: VMConfig) -> None:
    if cfg.packages is None or cfg.packages == "none":
        return
    if not Path(cfg.packages).exists():
        sys.exit(_missing("package manifest", Path(cfg.packages),
                          "--packages and <bake-packages>"))


def _load_packages(cfg: VMConfig) -> str:
    _check_packages_exist(cfg)
    if cfg.packages is None or cfg.packages == "none":
        pkgs = ""
    else:
        pkgs = _as_yaml_list(Path(cfg.packages).read_text())

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
  - systemctl mask multipathd.service multipathd.socket
  - systemctl mask iscsid.service iscsid.socket
  - systemctl mask snapd.service snapd.socket snapd.seeded.service
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
  - path: /etc/systemd/system/multipathd.service.d/99-timeout.conf
    content: |
      [Service]
      TimeoutStopSec=5s
    owner: root:root
    permissions: '0644'
    defer: false
  - path: /etc/systemd/system/iscsid.service.d/99-timeout.conf
    content: |
      [Service]
      TimeoutStopSec=5s
    owner: root:root
    permissions: '0644'
    defer: false
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
    #
    # With KVM a first boot takes minutes, and it still gets a ceiling: a
    # guest whose cloud-init rejected the user-data never powers itself off,
    # and waiting on it for ever looks exactly like a slow bake.
    boot_timeout = 1800 if cfg.kvm else 3300  # 55 min; job ceiling is 60 min
    try:
        subprocess.run(cmd, check=True, timeout=boot_timeout)
    except subprocess.TimeoutExpired:
        sys.exit(
            f"Error: first boot timed out after {boot_timeout}s on "
            f"{cfg.arch} ({'KVM' if cfg.kvm else 'no KVM'}). cloud-init did "
            f"not complete and power the guest off. Without KVM, check "
            f"rootfs expansion (growpart/resize2fs) in the QEMU console "
            f"log; with it, check that cloud-init accepted the user-data."
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
        # aia=none keeps the PLIC interrupt controller regardless of QEMU
        # version. QEMU 9.0 changed the virt machine default from none to
        # aplic-imsic; U-Boot 2025.10 does not support AIA IMSIC for EFI
        # boot, causing "Failed to load '/'" and a 55-minute cloud-init
        # timeout when ubuntu-latest moved to Ubuntu 26 (QEMU 10.x).
        #
        # -cpu max: QEMU ≥ 9.1 dropped the V (vector) and B (bit-manipulation)
        # extensions from the default rv64 CPU profile. Ubuntu 26.04 (resolute)
        # ships userspace binaries (ld-linux-riscv64-lp64d.so.1 and others)
        # that use both; without them they SIGILL on the first executed insn,
        # killing init and panicking the kernel before cloud-init can run.
        # "max" enables all extensions the emulator supports, which is the
        # right choice for an emulated guest with no physical hardware to match.
        machine = f"virt,aia=none{kvm}" if kvm else "virt,aia=none"
        return [
            "-machine", machine,
            "-cpu", "max",
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
        sys.exit(_missing("ansible playbook", playbook,
                          "--ansible-playbook and <bake-playbook>"))
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

    print(f"Booting {backing} for Ansible setup...")
    start: int | None = None
    for attempt in range(1, AUTO_SSH_PORT_ATTEMPTS + 1):
        # The port this boot really has; cfg itself when it names one.
        boot = resolve_ssh_port(cfg, start)
        if boot.mgmt_tap:
            _ensure_mgmt_bridge(boot.ssh_port)
        watch = cfg.ssh_port_auto and not cfg.mgmt_tap
        qemu_proc = _start_qemu(boot, backing, watch)
        try:
            vm_host = "localhost"
            vm_port = boot.ssh_port
            if boot.mgmt_tap:
                vm_host = _discover_vm_ip(
                    _mgmt_bridge_name(boot.ssh_port),
                    _effective_mac(boot),
                    timeout,
                )
                vm_port = 22
            ready = _wait_for_ssh(boot, timeout, host=vm_host, port=vm_port,
                                  proc=qemu_proc)
        except Exception:
            qemu_proc.kill()
            qemu_proc.wait()
            if boot.mgmt_tap:
                _teardown_mgmt_bridge(boot.ssh_port)
            raise
        if ready:
            break
        qemu_proc.kill()
        qemu_proc.wait()
        if not (watch and qemu_proc.hostfwd_refused()):
            sys.exit("Error: VM did not accept SSH in time.")
        if attempt == AUTO_SSH_PORT_ATTEMPTS:
            sys.exit(
                f"Error: --ssh-port auto: QEMU was refused {attempt} ports "
                f"in a row that had just probed free, the last {vm_port}; "
                "something is taking them as they are tried. Name a port "
                "with --ssh-port <number>."
            )
        print(f"--ssh-port auto: port {vm_port} was taken before QEMU "
              "bound it; trying the next free one.", file=sys.stderr)
        start = vm_port + 1

    try:
        print(f"Running Ansible playbook {playbook}...")
        _restore_blocking_stdio()
        _ensure_jmespath()

        private_key_extra = _key_extra_vars(cfg)
        ap_cmd = ["ansible-playbook", "-i", str(inventory), str(playbook),
                  "-e", f"ansible_host={vm_host}",
                  "-e", f"ansible_port={vm_port}",
                  "-e", f"ansible_user={cfg.username}",
                  "-e", f"username={cfg.username}",
                  "-e", f"vm_username={cfg.username}",
                  "-e", f"vm_root_user={cfg.username}",
                  *private_key_extra]
        if extra_args:
            # shlex, so a quoted value survives: -e 'k="a b"' is one argument.
            ap_cmd += shlex.split(extra_args)

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
            _teardown_mgmt_bridge(boot.ssh_port)
        print("Ansible post-setup complete.")
    except Exception:
        qemu_proc.kill()
        qemu_proc.wait()
        if cfg.mgmt_tap:
            _teardown_mgmt_bridge(boot.ssh_port)
        raise


class _WatchedQemu(subprocess.Popen):
    """QEMU with its stderr passed through and read for a refused SSH port."""

    def __init__(self, cmd: list[str], ssh_port: int, **kw) -> None:
        super().__init__(cmd, stderr=subprocess.PIPE, **kw)
        self._refusal = ssh_hostfwd_refused(ssh_port)
        self._refused = False
        self._relay = threading.Thread(target=self._pump, daemon=True)
        self._relay.start()

    def _pump(self) -> None:
        out = getattr(sys.stderr, "buffer", None)
        for line in self.stderr:
            if self._refusal in line:
                self._refused = True
            if out is not None:
                out.write(line)
                out.flush()
        self.stderr.close()

    def hostfwd_refused(self) -> bool:
        """Whether QEMU exited on its SSH hostfwd. Call once it has exited."""
        self._relay.join(timeout=5)
        return self._refused


def _start_qemu(boot: VMConfig, backing: Path, watch: bool) -> subprocess.Popen:
    cmd = [
        qemu_binary(boot),
        *identity_args("gen-vm", boot.vm_name),
        *_arch_args_for_gen(boot, ",accel=kvm" if boot.kvm else ""),
        "-smp", f"cpus={boot.vcpus}",
        "-m", str(boot.vmem),
        "-nographic",
        "-drive", f"if=virtio,format=qcow2,file={backing}{DISCARD_OPTS}",
        *_netdev_args(boot),
    ]
    if watch:
        return _WatchedQemu(cmd, boot.ssh_port)
    return subprocess.Popen(cmd)


def _key_extra_vars(cfg: VMConfig) -> list[str]:
    """The ansible-playbook -e pairs that carry the image's key.

    sbates130272.batesste.user_setup authorises a public key read from the
    controller, by default ~/.ssh/id_rsa.pub. Left alone that puts the
    caller's own key in an image baked with another, so it is pointed at
    the key this bake authorised.
    """
    extra: list[str] = []
    id_args = _ssh_identity_args(cfg)
    if id_args:
        extra += ["-e", f"ansible_ssh_private_key_file={id_args[1]}"]
    if cfg.ssh_key_file is not None:
        # abspath, not resolve: the private half sits beside the name the
        # caller gave, not beside whatever that name links to.
        public = Path(os.path.abspath(Path(cfg.ssh_key_file).expanduser()))
        if public.suffix == ".pub":
            extra += ["-e", f"user_setup_ssh_key_src_dir={public.parent}",
                      "-e", f"user_setup_ssh_key_name={public.stem}"]
        else:
            print(f"Note: {public} does not end in .pub, so a playbook that "
                  "runs user_setup cannot be pointed at it and authorises "
                  "~/.ssh/id_rsa.pub as well.")
    return extra


def _ssh_identity_args(cfg: VMConfig) -> list[str]:
    if cfg.ssh_key_file is None:
        return []
    key_pub = Path(cfg.ssh_key_file).expanduser()
    # Strip .pub to get the private key; fall back to no -i if missing.
    private_key = key_pub.parent / key_pub.stem
    if private_key.exists():
        return ["-i", str(private_key)]
    return []


def _qemu_gone(proc: subprocess.Popen | None) -> bool:
    """Whether QEMU has exited, saying so unless the caller will retry."""
    if proc is None or proc.poll() is None:
        return False
    if isinstance(proc, _WatchedQemu) and proc.hostfwd_refused():
        # The caller starts it again on another port and says so.
        return True
    print(
        f"Error: QEMU exited with status {proc.returncode} before the "
        "VM accepted SSH.\n"
        "       Its message is above; a hostfwd collision on the host "
        "is the usual cause.",
        file=sys.stderr,
    )
    return True


def _wait_for_ssh(
    cfg: VMConfig, timeout: int, host: str = "localhost", port: int | None = None,
    proc: subprocess.Popen | None = None,
) -> bool:
    p = port if port is not None else cfg.ssh_port
    print(f"Waiting for VM to accept SSH at {host}:{p}...")
    id_args = _ssh_identity_args(cfg)
    elapsed = 0
    while elapsed < timeout:
        # QEMU exiting is not something to wait out. It refuses to start on a
        # hostfwd whose host port is taken -- which a fleet settings file
        # invites, because the fleet runs QEMU inside a per-VM netns where
        # every guest can bind the same port, while gen-vm runs it on the
        # host where that port may already be a service. Without this check
        # the real error scrolls past and ten minutes later the run fails
        # with "VM did not accept SSH in time", which blames the guest.
        if _qemu_gone(proc):
            return False
        answered = False
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
            answered = r.returncode == 0
        except subprocess.TimeoutExpired:
            # SSH connected (ConnectTimeout=1 would have fired otherwise)
            # but background login processes kept the channel open.
            # The VM is up.
            answered = True
        if answered:
            # Whatever answered was not this guest if QEMU died meanwhile:
            # it was the listener that cost QEMU the port.
            if _qemu_gone(proc):
                return False
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
