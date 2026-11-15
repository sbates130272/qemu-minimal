"""The published qcow2 artifact's format, shared by the puller and the pusher.

gen_vm pulls it (--backing-image) and publish_qcow pushes it. Both read the
names from here so that neither can drift from the other.

The main artifact is one zstd layer holding a standalone qcow2. A referrer
attached to it carries vm-info.json and the guest keypair, so a consumer can
fetch credentials without pulling the disk.
"""

from __future__ import annotations

VM_ARTIFACT_TYPE = "application/vnd.batesste.vm-image.v1"
VM_LAYER_MEDIA_TYPE = "application/vnd.batesste.vm-image.layer.v1+zstd"
VM_INFO_ARTIFACT_TYPE = "application/vnd.batesste.vm-info.v1"
VM_SSH_KEY_MEDIA_TYPE = "application/vnd.batesste.vm-ssh-key.v1"

# What consumers of vm-info.json branch on. Keys are only ever added.
VM_INFO_SCHEMA_VERSION = 9

# The referrer holds exactly these three files. gen_vm takes the private key
# as the first file that is not .pub, not .json and not empty, so a fourth
# file could be mistaken for it.
VM_INFO_NAME = "vm-info.json"
VM_KEY_NAME = "id_rsa"
VM_PUB_NAME = "id_rsa.pub"
