"""Publish a baked guest qcow2 as an OCI artifact.

The other half of gen_vm's --backing-image: what this pushes is what that
pulls. See vm_artifact.py for the format.

What is published is the file named by --image and nothing derived from a
boot of it. A caller that tests the guest before publishing does so on an
overlay, so module loads, test output and anything specific to one host's
devices stay out of the artifact.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .fleet_xml import FleetConfig, FleetXml
from .gen_vm import generated_key_path, image_sidecars
from .vm_artifact import (
    VM_ARTIFACT_TYPE,
    VM_INFO_ARTIFACT_TYPE,
    VM_INFO_NAME,
    VM_INFO_SCHEMA_VERSION,
    VM_KEY_NAME,
    VM_LAYER_MEDIA_TYPE,
    VM_PUB_NAME,
    VM_SSH_KEY_MEDIA_TYPE,
)

# Level 12, not 19: on a multi-GB disk the top levels cost several times the
# wall clock for a few percent, and this runs on a 4-vCPU runner. --long=27
# is a 128 MiB window, which is what pays on a disk image, where the same
# page repeats further apart than the default window reaches. It is also the
# largest window `zstd -d` accepts without being told, which is how gen_vm
# decompresses.
ZSTD_ARGS = ["-T0", "-12", "--long=27"]

# The identifying subset repeated on the referrer, so that one discovered
# without its subject in hand still says what it belongs to.
_REFERRER_ANNOTATIONS = (
    "org.opencontainers.image.description",
    "org.opencontainers.image.revision",
    "org.opencontainers.image.created",
    "vm.flavour",
    "vm.release",
    "vm.kernel_release",
)


def _run_json(cmd: list[str]) -> dict:
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def image_facts(image: Path) -> dict:
    """Format and sizes of *image*, refusing one that is not standalone."""
    info = _run_json(["qemu-img", "info", "--output=json", str(image)])
    if info.get("format") != "qcow2":
        sys.exit(f"Error: {image} is {info.get('format')}, not qcow2.")
    if info.get("backing-filename"):
        sys.exit(
            f"Error: {image} is an overlay on {info['backing-filename']}. "
            "Publish the baked base, not an overlay a guest has run on."
        )
    return info


def key_fingerprint(public_key: Path) -> str:
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(public_key)],
        check=True, capture_output=True, text=True,
    )
    # "<bits> <fingerprint> <comment> (<type>)"
    return out.stdout.split()[1]


def _key_material(public_key: Path) -> tuple[str, str] | None:
    """(type, base64) of an OpenSSH public key file; None if it has no key."""
    try:
        fields = public_key.read_text().split()
    except OSError:
        return None
    return (fields[0], fields[1]) if len(fields) >= 2 else None


def _derived_key_material(private_key: Path) -> tuple[tuple[str, str] | None, str]:
    """((type, base64) of the public key *private_key* holds, ssh-keygen's error)."""
    out = subprocess.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(private_key)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    fields = out.stdout.split()
    if out.returncode != 0 or len(fields) < 2:
        return None, out.stderr.strip()
    return (fields[0], fields[1]), ""


def _check_pair(private_key: Path, public_key: Path) -> None:
    """Refuse a private key that is not the other half of *public_key*.

    The guest authorises the public half and the fingerprint published is
    the public half's; a private key from another pair beside it would be
    published under both.
    """
    derived, why = _derived_key_material(private_key)
    if derived is not None and derived == _key_material(public_key):
        return
    sys.exit(
        f"Error: {private_key} is not the private half of {public_key}"
        + (f"\n       (ssh-keygen: {why})" if why else "") + ".\n"
        "       Nothing is published. A key with a passphrase cannot be "
        "published either."
    )


def _sidecar(image: Path) -> Path:
    """The record gen-vm wrote for *image*.

    Found from the image file and never from --vm-name, which is only the
    name to publish under: another image's record names another image's key.
    """
    candidates = image_sidecars(image)
    return next((c for c in candidates if c.is_file()), candidates[0])


def _generated_key(sidecar: Path, image: Path) -> Path:
    """The private key gen-vm generated for *image*, which *sidecar* describes.

    Only a key the record says gen-vm made is published without being
    named. One a user chose is usually a personal key, and no record says
    nothing about whose it is; neither is guessed at, and nothing here
    looks in ~/.ssh.

    The record is trusted for the claim and not for the path: the key is
    looked for where gen-vm puts one, and the record has to agree, down to
    the fingerprint of what was baked in.
    """
    try:
        info = json.loads(sidecar.read_text())
        recorded = info.get("ssh_key_file")
        generated = info.get("ssh_key_generated") is True
        fingerprint = info.get("ssh_key_fingerprint")
    except (OSError, ValueError, AttributeError):
        sys.exit(
            f"Error: no record of the key this image was baked with "
            f"({sidecar} is missing or unreadable).\n"
            "       Pass --key with the private key the guest authorises. "
            "It is PUBLISHED with the image."
        )
    if not generated or not isinstance(recorded, str) or not recorded:
        sys.exit(
            f"Error: {sidecar} does not record a key gen-vm generated for "
            f"this image (it names {recorded or 'no key'}).\n"
            "       A key somebody chose is not published unless it is "
            "named: pass --key with its\n"
            "       private half, which is PUBLISHED with the image. To "
            "publish a key made for\n"
            "       the image instead, re-bake without --ssh-key-file and "
            "<vm-ssh-key-file>."
        )

    def refuse(why: str) -> None:
        sys.exit(
            f"Error: {sidecar} says gen-vm generated this image's key, but "
            f"{why}.\n"
            "       Nothing is published on that record. Re-bake, or pass "
            "--key with the private key\n"
            "       the guest authorises, which is PUBLISHED with the image."
        )

    private = generated_key_path(image.parent, sidecar.stem)
    public = private.with_name(private.name + ".pub")
    for key in (private, public):
        if key.is_symlink() or key.resolve() != key:
            refuse(f"{key} is a symbolic link, or is reached through one")
    if Path(recorded).resolve() != public:
        refuse(f"it names {recorded}, and gen-vm generates {public}")
    for key in (private, public):
        if not key.is_file():
            refuse(f"{key} does not exist")
    if not isinstance(fingerprint, str) or not fingerprint:
        refuse("it records no fingerprint for it")
    if key_fingerprint(public) != fingerprint:
        refuse(f"{public} is not the key that was baked in "
               f"({fingerprint} was)")
    print(f"Publishing the key gen-vm generated for this image: {private}")
    return private


def _check_baked_key(sidecar: Path, public_key: Path) -> None:
    """Refuse a key that is not the one gen-vm authorised in this image.

    gen-vm records the public key it baked in beside the image, by
    fingerprint and by path. Publishing a different pair ships a private key
    the guest does not accept, with its fingerprint recorded as the guest's.
    Nothing recorded means nothing to compare, and is not an error.
    """
    try:
        info = json.loads(sidecar.read_text())
        recorded = info.get("ssh_key_file")
        fingerprint = info.get("ssh_key_fingerprint")
    except (OSError, ValueError, AttributeError):
        return
    if isinstance(fingerprint, str) and fingerprint:
        matches = key_fingerprint(public_key) == fingerprint
    else:
        # A record with a path and no fingerprint: compare with what is at
        # the path now, which is all there is.
        baked = (_key_material(Path(recorded))
                 if isinstance(recorded, str) and recorded else None)
        matches = baked is None or baked == _key_material(public_key)
    if matches:
        return
    sys.exit(
        f"Error: {public_key} is not the key this image authorises.\n"
        f"       {sidecar} records that it was baked with "
        f"{recorded or fingerprint}.\n"
        f"       Re-bake with 'gen-vm --bake --ssh-key-file {public_key}', "
        "or pass the --key that matches."
    )


def build_vm_info(args: argparse.Namespace, facts: dict, qcow2_name: str,
                  size_bytes: int, fingerprint: str, created: str) -> dict:
    """The vm-info.json a consumer reads instead of booting the image.

    The field names are a contract with every consumer of the published
    guests; add to them, never rename. The guest's own stamp is carried
    whole under "guest", so the pins it records are stated once, there.
    """
    guest = json.loads(args.guest_stamp.read_text()) if args.guest_stamp else {}
    return {
        "schema_version": VM_INFO_SCHEMA_VERSION,
        "vm_name": args.vm_name,
        "flavour": args.flavour,
        "username": args.username,
        "password": args.password,
        "image_path": f"/output/{qcow2_name}",
        "image_format": "qcow2",
        "image_size_bytes": size_bytes,
        "image_virtual_size_bytes": facts["virtual-size"],
        "release": args.release,
        "architecture": args.architecture,
        "kernel_release": guest.get("booted_kernel", ""),
        "backing_file": False,
        "ssh_keys": {
            "private_key_path": f"/output/{VM_KEY_NAME}",
            "public_key_path": f"/output/{VM_PUB_NAME}",
            "fingerprint": fingerprint,
        },
        "rocm": {
            "path": guest.get("rocm_path", ""),
            "version": guest.get("amdrocm_runtime_dev_version", ""),
        },
        "provisioning": {
            "vm_playbook": args.playbook,
            "kernel_ref": guest.get("ionic_kernel_pin", ""),
            "kernel_debs": " ".join(guest.get("kernel_debs", [])),
            "ernic_commit": guest.get("ernic_source_pin", ""),
            "sdxi_commit": guest.get("sdxi_source_ref", ""),
            "amdgpu_dkms_version": guest.get("amdgpu_dkms_version", ""),
            "unattended_upgrades": "purged",
        },
        "guest": guest,
        "build_info": {
            "qemu_minimal_commit": args.commit,
            "build_timestamp": created,
        },
    }


def build_annotations(args: argparse.Namespace, info: dict, version: str) -> dict:
    """Annotations for the main artifact.

    They let `oras manifest fetch` answer "what is this" without a multi-GB
    pull. The password is in vm-info.json, which a consumer fetches on
    purpose, and never here.
    """
    annotations = {
        "org.opencontainers.image.title": f"{info['vm_name']}.qcow2.zst",
        "org.opencontainers.image.description": (
            f"{info['flavour']} guest qcow2 ({info['release']}, "
            f"{info['architecture']}, kernel {info['kernel_release']})"
        ),
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": args.commit,
        "org.opencontainers.image.created": info["build_info"]["build_timestamp"],
        "org.opencontainers.image.source": args.source,
        "vm.name": info["vm_name"],
        "vm.flavour": info["flavour"],
        "vm.release": info["release"],
        "vm.architecture": info["architecture"],
        "vm.kernel_release": info["kernel_release"],
        "vm.username": info["username"],
        "vm.image_size_bytes": info["image_size_bytes"],
        "vm.playbook": info["provisioning"]["vm_playbook"],
        "vm.qemu_minimal_commit": args.commit,
    }
    return {k: str(v) for k, v in annotations.items() if v not in ("", None)}


def _annotation_args(annotations: dict) -> list[str]:
    return [a for k, v in annotations.items() for a in ("--annotation", f"{k}={v}")]


def resolve(args: argparse.Namespace) -> None:
    """Fill the flags left unset from the --xml layers, then the defaults.

    Only an explicit --xml is read. An ambient fleet.xml must not decide
    what goes to a registry.
    """
    fleet = FleetXml.parse_layers(FleetXml.find(args.xml)) if args.xml else None
    have = fleet or FleetConfig()

    def fill(attr: str, from_xml: str, default: str | None = None) -> None:
        if getattr(args, attr) is None:
            setattr(args, attr, from_xml or default)

    fill("ref", have.publish_ref)
    fill("vm_name", have.bake_name)
    fill("flavour", have.publish_flavour, "rocm-golden")
    fill("username", have.bake_username, "ubuntu")
    fill("password", have.bake_password, "password")
    fill("release", have.bake_release, "resolute")
    fill("playbook", have.bake_playbook, "")

    if args.image is None and fleet is not None and args.vm_name:
        # The name gen-vm gives the image it bakes.
        args.image = (Path(fleet.vm_images_dir).expanduser()
                      / f"{args.vm_name}-backing.qcow2")
    # --key is not filled in from a layer. <vm-ssh-key-file> is a fleet's
    # own login key, usually somebody's ~/.ssh one, and this key is
    # published; run() takes the image's generated key from its sidecar.

    missing = [
        hint for attr, hint in (
            ("ref", "--ref, or <publish-ref> in an --xml layer"),
            ("vm_name", "--vm-name, or <bake-name> in an --xml layer"),
            ("image", "--image, or --xml with <bake-name> (or --vm-name) to "
                      "find it under <vm-images-dir>"),
        ) if not getattr(args, attr)
    ]
    if missing:
        sys.exit("Error: publish-qcow needs:\n  " + "\n  ".join(missing))


def run(args: argparse.Namespace) -> None:
    resolve(args)
    tools = ["qemu-img", "zstd", "ssh-keygen"] + ([] if args.dry_run else ["oras"])
    for tool in tools:
        if shutil.which(tool) is None:
            sys.exit(f"Error: publish-qcow needs '{tool}', which is not on PATH.")

    image = args.image.resolve()
    if not image.is_file():
        sys.exit(f"Error: {image} does not exist.")
    pulled = image.with_name(image.name + ".image-ref")
    if pulled.is_file():
        sys.exit(
            f"Error: {image} was pulled from {pulled.read_text().strip()} by "
            "gen-vm, not baked here;\n"
            "       publishing it would republish that image as a new bake. "
            "Bake with 'gen-vm --bake' first."
        )
    # Before the key is looked for: an overlay shares its backing image's
    # record, and is refused for what it is.
    facts = image_facts(image)
    sidecar = _sidecar(image)
    private_key = (args.key.resolve() if args.key
                   else _generated_key(sidecar, image))
    public_key = private_key.with_name(private_key.name + ".pub")
    for key in (private_key, public_key):
        if not key.is_file():
            sys.exit(f"Error: {key} does not exist.")
    _check_baked_key(sidecar, public_key)
    _check_pair(private_key, public_key)
    if args.guest_stamp and not args.guest_stamp.is_file():
        sys.exit(f"Error: {args.guest_stamp} does not exist.")
    if not args.tag:
        sys.exit("Error: at least one --tag is required.")

    created = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    qcow2_name = f"{args.vm_name}.qcow2"

    # Beside the image by default: /tmp is often a tmpfs, and this holds a
    # full copy of the disk.
    work_parent = args.work_dir or image.parent
    with tempfile.TemporaryDirectory(dir=work_parent, prefix="publish-qcow-") as tmp:
        work = Path(tmp)
        qcow2 = work / qcow2_name

        # A fresh copy, not the file itself: convert drops the clusters the
        # bake freed and writes none compressed. zstd does the compression;
        # compressed clusters inside the image slow every consumer's reads.
        print(f"Converting {image.name} to a standalone qcow2")
        subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", str(image), str(qcow2)],
            check=True,
        )
        size_bytes = qcow2.stat().st_size

        shutil.copyfile(private_key, work / VM_KEY_NAME)
        (work / VM_KEY_NAME).chmod(0o600)
        shutil.copyfile(public_key, work / VM_PUB_NAME)

        info = build_vm_info(
            args, facts, qcow2_name, size_bytes,
            key_fingerprint(public_key), created,
        )
        (work / VM_INFO_NAME).write_text(json.dumps(info, indent=2) + "\n")

        print(f"Compressing {qcow2_name} ({size_bytes} bytes)")
        subprocess.run(
            ["zstd", *ZSTD_ARGS, "--rm", "-q", "-f", str(qcow2)], check=True,
        )
        blob = qcow2.with_name(qcow2_name + ".zst")
        print(f"Compressed to {blob.stat().st_size} bytes")

        annotations = build_annotations(args, info, args.tag[0])
        referrer = {k: annotations[k] for k in _REFERRER_ANNOTATIONS
                    if k in annotations}
        first = f"{args.ref}:{args.tag[0]}"

        if args.dry_run:
            for tag in args.tag:
                print(f"Would push {args.ref}:{tag}")
            print(f"Would attach {VM_INFO_NAME}, {VM_KEY_NAME}, {VM_PUB_NAME}")
            for key, value in annotations.items():
                print(f"  {key} = {value}")
            if args.keep:
                kept = args.keep.resolve()
                kept.mkdir(parents=True, exist_ok=True)
                for name in (blob.name, VM_INFO_NAME):
                    shutil.move(str(work / name), str(kept / name))
                print(f"Kept {blob.name} and {VM_INFO_NAME} in {kept}")
            return

        # One push carries every tag; the referrer attaches to the first.
        subprocess.run(
            ["oras", "push", "--artifact-type", VM_ARTIFACT_TYPE,
             *_annotation_args(annotations),
             f"{args.ref}:{','.join(args.tag)}",
             f"{blob.name}:{VM_LAYER_MEDIA_TYPE}"],
            cwd=work, check=True,
        )
        subprocess.run(
            ["oras", "attach", "--artifact-type", VM_INFO_ARTIFACT_TYPE,
             *_annotation_args(referrer), first,
             f"{VM_INFO_NAME}:application/json",
             f"{VM_KEY_NAME}:{VM_SSH_KEY_MEDIA_TYPE}",
             f"{VM_PUB_NAME}:{VM_SSH_KEY_MEDIA_TYPE}"],
            cwd=work, check=True,
        )

    digest = _run_json(
        ["oras", "manifest", "fetch", "--descriptor", first]).get("digest", "")
    for tag in args.tag:
        print(f"Pushed {args.ref}:{tag}")
    print(f"Digest {args.ref}@{digest}")
    if args.digest_file:
        args.digest_file.write_text(f"{args.ref}@{digest}\n")
