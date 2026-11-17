"""The published qcow2 artifact's format, shared by the puller and the pusher.

gen_vm pulls it (--backing-image) and publish_qcow pushes it. Both read the
names from here so that neither can drift from the other.

The main artifact is one zstd layer holding a standalone qcow2. A referrer
attached to it carries vm-info.json and the guest keypair, so a consumer can
fetch credentials without pulling the disk.
"""

from __future__ import annotations

import re

# The tags an image is published under. The dated one, <YYYYMMDD>.g<sha7>, is
# the day of the publish and the qemu-minimal commit the guest was baked from;
# it is the one a fleet pins. "latest" and qm.<sha7> are moved onto each new
# publish. publish-qcow-prune deletes only the dated shape, and gen-compose
# accepts only that shape from a published <backing-ref>, so both read it
# from here. [0-9], not \d, which also matches other scripts' digits.
VM_DATED_TAG = re.compile(r"[0-9]{8}\.g[0-9a-f]{7}")
VM_COMMIT_TAG = re.compile(r"qm\.[0-9a-f]{7}")

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
