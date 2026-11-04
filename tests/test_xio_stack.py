"""Tests for the vfio-user-xio-vm compose stack.

None of these asks Docker anything. They pin the properties of
qemu/compose/vfio-user-xio-vm/docker-compose.yml that fail silently when
wrong -- the kind where the stack still comes up and a later test, or a later
person, pays for it:

  * the SPDK namespaces. Beside a KV namespace an LBA one is invisible to a
    Linux guest ("Command set not reported for nsid:1"), so the default is KV
    only and adding one has to be a decision, not an accident;
  * the socket order handed to qemu, because it is the order the guest sees
    its devices in, and the GPU/NIC/NVMe pairing is defined by it;
  * that every socket qemu is told about is one some server actually makes;
  * that no image variable is shared with the stock stacks, since a shared
    .env would hand this stack a QEMU with no pci-mmio-bridge;
  * that nothing runs as root or holds a capability.

PyYAML is required. Under CI=true a missing PyYAML fails instead of skipping,
the same policy as tests/test_ernic_identity.py: a guard that skips where it
is meant to run is a guard that is off.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
STACK = REPO_ROOT / "qemu" / "compose" / "vfio-user-xio-vm" / "docker-compose.yml"

# The stock stacks' variable names. This stack must not read any of them.
STOCK_IMAGE_VARS = ("QEMU_IMAGE", "ERNIC_IMAGE", "ROCJITSU_IMAGE", "SPDK_IMAGE")


@unittest.skipIf(
    yaml is None and not os.environ.get("CI"), "PyYAML not installed"
)
class XioStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if yaml is None:
            raise AssertionError("PyYAML is required under CI=true")
        cls.text = STACK.read_text()
        cls.doc = yaml.safe_load(cls.text)
        cls.services = cls.doc["services"]

    def _qemu_script(self) -> str:
        return self.services["qemu"]["entrypoint"][-1]

    def _qemu_sockets(self) -> list[str]:
        m = re.search(r"--vfio-userdev\s+(\S+)", self._qemu_script())
        self.assertIsNotNone(m, "qemu has no --vfio-userdev")
        return m.group(1).split(",")

    def test_service_set(self):
        self.assertEqual(
            set(self.services),
            {"rocjitsu-1", "rocjitsu-2", "ernic", "spdk-nvme", "qemu"},
        )

    def test_two_gpus_are_two_services_not_replicas(self):
        # Replicas share one volume and one command, so two of them would bind
        # one socket path. See the comment above rocjitsu-1 in the stack.
        for name, svc in self.services.items():
            self.assertNotIn("replicas", svc.get("deploy", {}), name)
        sockets = []
        for name in ("rocjitsu-1", "rocjitsu-2"):
            cmd = self.services[name]["command"]
            sockets.append(cmd[cmd.index("--vfio-socket") + 1])
        self.assertEqual(len(set(sockets)), 2, sockets)

    def test_both_gpus_share_one_config(self):
        configs = []
        for name in ("rocjitsu-1", "rocjitsu-2"):
            cmd = self.services[name]["command"]
            configs.append(cmd[cmd.index("--config") + 1])
        self.assertEqual(configs[0], configs[1])

    def test_spdk_serves_only_a_kv_namespace_by_default(self):
        env = self.services["spdk-nvme"]["environment"]["NVME_NAMESPACES"]
        default = re.search(r":-([^}]+)\}", env).group(1)
        kinds = [s.split(":")[0] for s in default.split(",")]
        self.assertEqual(kinds, ["kv"], f"{default!r}: see the comment above spdk-nvme")

    def test_spdk_serial_is_set_and_a_valid_nvme_serial(self):
        env = self.services["spdk-nvme"]["environment"]["SPDK_SERIAL"]
        serial = re.search(r":-([^}]+)\}", env).group(1)
        # NVMe serial numbers are at most 20 ASCII characters.
        self.assertLessEqual(len(serial), 20, serial)
        self.assertTrue(serial.isascii() and serial.strip() == serial, serial)

    def test_settings_file_agrees_with_the_compose_defaults(self):
        # qemu/env/xio and the compose file's inline fallbacks hold the same
        # pins; one edited without the other drifts with no error.
        env_file = REPO_ROOT / "qemu" / "env" / "xio"
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if re.match(r"^XIO_[A-Z_]+=", line)
        )
        self.assertTrue(values)
        for key, value in values.items():
            m = re.search(r"\$\{" + key + r":-([^}]*)\}", self.text)
            self.assertIsNotNone(m, f"{key} is not a compose default")
            self.assertEqual(m.group(1), value, key)

    def test_the_playbook_names_both_nvme_devices(self):
        play = (REPO_ROOT / "ansible" / "playbooks" / "xio-guest.yml").read_text()
        self.assertIn('SYMLINK+="xio-kv"', play)
        blk = [l for l in play.splitlines() if 'SYMLINK+="xio-blk"' in l]
        self.assertEqual(len(blk), 1)
        # Whole disks only: without DEVTYPE the rule also names partitions,
        # and the second one to match takes the link from the first.
        self.assertIn('ENV{DEVTYPE}=="disk"', blk[0])

    def test_rocm_xio_is_pinned_off_main(self):
        # main's nvme-ep has an s_waitcnt that fails instruction selection on
        # gfx1250, so a guest built from it never gets an xio-tester.
        env = (REPO_ROOT / "qemu" / "env" / "xio").read_text()
        ref = re.search(r"^ROCM_XIO_REF=(\S+)", env, re.M)
        self.assertIsNotNone(ref, "ROCM_XIO_REF is not set")
        self.assertNotIn(ref.group(1), ("main", "master", "HEAD"))
        self.assertRegex(env, r"(?m)^ROCM_XIO_REPO=\S+")

    def test_the_playbook_installs_the_module_without_make_install(self):
        # `make install` rebuilds as root and leaves root-owned files in the
        # user's source tree.
        play = (REPO_ROOT / "ansible" / "playbooks" / "xio-guest.yml").read_text()
        self.assertIn("modules_install", play)
        self.assertNotRegex(play, r"(?m)^\s*(cmd:\s*)?make install\b")

    def test_the_playbook_sets_the_ernic_address_directly(self):
        # The guest has no systemd-networkd, so a netplan file alone leaves the
        # NIC with no address.
        play = (REPO_ROOT / "ansible" / "playbooks" / "xio-guest.yml").read_text()
        self.assertIn("ip addr add {{ xio_ernic_address }}", play)

    def test_the_stack_does_not_depend_on_quick_start(self):
        # Its guest, key, settings and playbook are its own. A comment may
        # mention quick-start; reading its paths or running it may not.
        for f in (STACK, REPO_ROOT / "qemu" / "env" / "xio",
                  REPO_ROOT / "ansible" / "inventory" / "xio.yml",
                  REPO_ROOT / "ansible" / "playbooks" / "xio-guest.yml"):
            text = f.read_text()
            self.assertNotIn("quickstart-key", text, f.name)
            self.assertNotRegex(text, r"qemu-tool\s+quick-start", f.name)
            self.assertNotIn("env/quickstart", text, f.name)

    def test_the_settings_name_no_key_so_gen_vm_fetches_the_images(self):
        # Naming one would make gen-vm boot the overlay to install it, which
        # the stack does not need: the image already accepts its builder's.
        env = (REPO_ROOT / "qemu" / "env" / "xio").read_text()
        self.assertNotRegex(env, r"(?m)^VM_SSH_KEY_FILE=")

    def test_the_inventory_host_is_the_vm_name(self):
        # The sidecar the key path comes from is named for VM_NAME.
        env = (REPO_ROOT / "qemu" / "env" / "xio").read_text()
        name = re.search(r"^VM_NAME=(\S+)", env, re.M).group(1)
        inv = yaml.safe_load(
            (REPO_ROOT / "ansible" / "inventory" / "xio.yml").read_text())
        self.assertIn(name, inv["all"]["children"]["xio"]["hosts"])

    def test_spdk_socket_dir_matches_what_qemu_attaches(self):
        env = self.services["spdk-nvme"]["environment"]
        self.assertEqual(env["VFIO_USER_SOCKET_DIR"], "/run/vfu/nvme")
        self.assertIn("/run/vfu/nvme/cntrl", self._qemu_sockets())

    def test_qemu_socket_order_is_the_device_order(self):
        self.assertEqual(
            self._qemu_sockets(),
            [
                "/run/vfu/rocjitsu-1.sock",
                "/run/vfu/rocjitsu-2.sock",
                "/run/vfu/ernic-1.sock",
                "/run/vfu/nvme/cntrl",
            ],
        )

    def test_every_socket_qemu_names_is_made_by_a_server_it_waits_for(self):
        made = {
            "/run/vfu/rocjitsu-1.sock": "rocjitsu-1",
            "/run/vfu/rocjitsu-2.sock": "rocjitsu-2",
            "/run/vfu/ernic-1.sock": "ernic",
            "/run/vfu/nvme/cntrl": "spdk-nvme",
        }
        depends = self.services["qemu"]["depends_on"]
        for sock in self._qemu_sockets():
            server = made[sock]
            self.assertEqual(
                depends[server]["condition"], "service_healthy",
                f"qemu must wait for {server}, which makes {sock}",
            )
            # And the healthcheck must look for the same socket it serves.
            test = " ".join(self.services[server]["healthcheck"]["test"])
            self.assertIn(sock, test, f"{server}'s healthcheck is not {sock}")

    def test_qemu_has_its_own_nvme_and_the_bridge(self):
        script = self._qemu_script()
        self.assertRegex(script, r"--nvme\s+1\b")
        self.assertIn("--pci-mmio-bridge", script)

    def test_images_are_pinned_forks_and_not_latest(self):
        for name, marker in (
            ("rocjitsu-1", "rocm-rocjitsu-sbates-fork"),
            ("rocjitsu-2", "rocm-rocjitsu-sbates-fork"),
            ("spdk-nvme", "spdk-libvfio-user-sbates-fork"),
            ("qemu", "qemu-libvfio-user-sbates-fork"),
        ):
            image = self.services[name]["image"]
            self.assertIn(marker, image, name)
            self.assertNotIn(":latest", image, name)
            self.assertRegex(image, r":[^:}]+\}$|:[A-Za-z0-9._-]+-vfu\.", name)

    def test_no_image_variable_is_shared_with_the_stock_stacks(self):
        for var in STOCK_IMAGE_VARS:
            self.assertNotRegex(
                self.text, rf"\$\{{{var}[:}}]",
                f"{var} would be read from a shared .env",
            )

    def test_nothing_runs_as_root_or_holds_a_capability(self):
        for name, svc in self.services.items():
            self.assertIn("CONTAINER_USER:?", svc["user"], name)
            self.assertEqual(svc["cap_drop"], ["ALL"], name)
            self.assertIn("no-new-privileges:true", svc["security_opt"], name)
            self.assertNotIn("cap_add", svc, name)
            self.assertNotIn("privileged", svc, name)

    def test_the_servers_run_as_the_same_uid_as_qemu(self):
        # rocjitsu and SPDK leave their sockets 0755 owned by their creator, so
        # a qemu on any other uid cannot connect(2) to them.
        users = {svc["user"] for svc in self.services.values()}
        self.assertEqual(len(users), 1, users)

    def test_qemu_gets_the_kvm_group(self):
        self.assertIn("CONTAINER_KVM_GID:?", self.services["qemu"]["group_add"][0])

    def test_shm_covers_guest_ram_by_default(self):
        # QEMU refuses to allocate the memfd backend when /dev/shm is smaller
        # than guest RAM, so the two defaults have to stay in step.
        vmem = int(re.search(r"--vmem\s+\"\$\{VM_VMEM:-(\d+)\}\"",
                             self._qemu_script()).group(1))
        shm = self.services["qemu"]["shm_size"]
        gib = int(re.search(r":-(\d+)g\}", shm).group(1))
        self.assertGreaterEqual(gib * 1024, vmem)


if __name__ == "__main__":
    unittest.main()
