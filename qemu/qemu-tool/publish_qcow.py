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


def run(args: argparse.Namespace) -> None:
    tools = ["qemu-img", "zstd", "ssh-keygen"] + ([] if args.dry_run else ["oras"])
    for tool in tools:
        if shutil.which(tool) is None:
            sys.exit(f"Error: publish-qcow needs '{tool}', which is not on PATH.")

    image = args.image.resolve()
    if not image.is_file():
        sys.exit(f"Error: {image} does not exist.")
    private_key = args.key.resolve()
    public_key = private_key.with_name(private_key.name + ".pub")
    for key in (private_key, public_key):
        if not key.is_file():
            sys.exit(f"Error: {key} does not exist.")
    if args.guest_stamp and not args.guest_stamp.is_file():
        sys.exit(f"Error: {args.guest_stamp} does not exist.")
    if not args.tag:
        sys.exit("Error: at least one --tag is required.")

    facts = image_facts(image)
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
