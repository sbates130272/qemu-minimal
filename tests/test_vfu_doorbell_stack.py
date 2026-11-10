"""Tests for the vfu-doorbell-vm compose stack.

None of these asks Docker anything. They pin the properties of
qemu/compose/vfu-doorbell-vm/docker-compose.yml that fail silently when wrong:

  * the socket order handed to qemu, because it is the order the guest sees its
    devices in, and the guest tool finds the ringee and the ringer by their
    PCI ids but a reordering still changes which BDF is which in the logs;
  * that every socket qemu is told about is one some server actually makes, and
    that qemu waits for that server;
  * that no image variable is shared with the stock stacks, since a shared .env
    would hand this stack a stock QEMU whose vfio_user_dma_write cannot reach
    MMIO, and every ringer write would fail with ENODEV and say nothing else;
  * that nothing runs as root or holds a capability;
  * that the settings file and the compose file's inline defaults agree;
  * that the stack is registered everywhere a stack has to be: compose.py, the
    Debian package, and both CI lanes.

PyYAML is required. Under CI=true a missing PyYAML fails instead of skipping,
the same policy as tests/test_xio_stack.py.

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
STACK_NAME = "vfu-doorbell-vm"
STACK = REPO_ROOT / "qemu" / "compose" / STACK_NAME / "docker-compose.yml"
ENV = REPO_ROOT / "qemu" / "env" / "vfu-doorbell"

# The stock stacks' variable names. This stack must not read any of them.
STOCK_IMAGE_VARS = ("QEMU_IMAGE", "ERNIC_IMAGE", "ROCJITSU_IMAGE", "SPDK_IMAGE")


@unittest.skipIf(
    yaml is None and not os.environ.get("CI"), "PyYAML not installed"
)
class VfuDoorbellStack(unittest.TestCase):
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
        self.assertEqual(set(self.services), {"ringee", "ringer", "qemu"})

    def test_qemu_socket_order_is_the_device_order(self):
        self.assertEqual(
            self._qemu_sockets(),
            ["/run/vfu/ringee.sock", "/run/vfu/ringer.sock"],
        )

    def test_every_socket_qemu_names_is_made_by_a_server_it_waits_for(self):
        made = {
            "/run/vfu/ringee.sock": "ringee",
            "/run/vfu/ringer.sock": "ringer",
        }
        depends = self.services["qemu"]["depends_on"]
        for sock in self._qemu_sockets():
            server = made[sock]
            cmd = self.services[server]["command"]
            self.assertEqual(cmd[cmd.index("--socket") + 1], sock, server)
            self.assertEqual(
                depends[server]["condition"], "service_healthy",
                f"qemu must wait for {server}, which makes {sock}",
            )
            test = " ".join(self.services[server]["healthcheck"]["test"])
            self.assertIn(sock, test, f"{server}'s healthcheck is not {sock}")

    def test_each_server_runs_its_own_role(self):
        for name in ("ringee", "ringer"):
            cmd = self.services[name]["command"]
            self.assertEqual(cmd[cmd.index("--role") + 1], name)
            self.assertEqual(cmd[0], "/opt/vfu/vfu-doorbell")

    def test_the_binaries_are_mounted_read_only(self):
        for name in ("ringee", "ringer"):
            vols = self.services[name]["volumes"]
            self.assertTrue(
                any(v.endswith(":/opt/vfu:ro") for v in vols),
                f"{name} must mount the binaries read-only: {vols}",
            )

    def test_no_image_variable_is_shared_with_the_stock_stacks(self):
        for var in STOCK_IMAGE_VARS:
            self.assertNotRegex(
                self.text, rf"\$\{{{var}[:}}]",
                f"{var} would be read from a shared .env",
            )

    def test_images_are_pinned_forks_and_not_latest(self):
        # The servers run in the libvfio-user image (tag vfu.<sha>); qemu in the
        # QEMU image built against it (tag qemu.<sha>-vfu.<sha>).
        for name, marker, tag in (
            ("ringee", "libvfio-user-sbates-fork", r":vfu\.[0-9a-f]+\}$"),
            ("ringer", "libvfio-user-sbates-fork", r":vfu\.[0-9a-f]+\}$"),
            ("qemu", "qemu-libvfio-user-sbates-fork",
             r":qemu\.[0-9a-f]+-vfu\.[0-9a-f]+\}$"),
        ):
            image = self.services[name]["image"]
            self.assertIn(marker, image, name)
            self.assertNotIn(":latest", image, name)
            self.assertRegex(image, tag, name)

    def test_nothing_runs_as_root_or_holds_a_capability(self):
        for name, svc in self.services.items():
            self.assertIn("CONTAINER_USER:?", svc["user"], name)
            self.assertEqual(svc["cap_drop"], ["ALL"], name)
            self.assertIn("no-new-privileges:true", svc["security_opt"], name)
            self.assertNotIn("cap_add", svc, name)
            self.assertNotIn("privileged", svc, name)

    def test_the_servers_run_as_the_same_uid_as_qemu(self):
        # A vfio-user socket is created 0755 by its owner, so a qemu on another
        # uid cannot connect(2) to it.
        users = {svc["user"] for svc in self.services.values()}
        self.assertEqual(len(users), 1, users)

    def test_qemu_gets_the_kvm_group(self):
        self.assertIn("CONTAINER_KVM_GID:?", self.services["qemu"]["group_add"][0])

    def test_shm_covers_guest_ram_by_default(self):
        vmem = int(re.search(r"--vmem\s+\"\$\{VM_VMEM:-(\d+)\}\"",
                             self._qemu_script()).group(1))
        gib = int(re.search(r":-(\d+)g\}", self.services["qemu"]["shm_size"]).group(1))
        self.assertGreaterEqual(gib * 1024, vmem)

    def test_settings_file_agrees_with_the_compose_defaults(self):
        values = dict(
            line.split("=", 1)
            for line in ENV.read_text().splitlines()
            if re.match(r"^DOORBELL_[A-Z_]+=", line)
        )
        self.assertTrue(values)
        for key, value in values.items():
            m = re.search(r"\$\{" + key + r":-([^}]*)\}", self.text)
            self.assertIsNotNone(m, f"{key} is not a compose default")
            self.assertEqual(m.group(1), value, key)

    def test_the_guest_port_does_not_collide_with_the_xio_stack(self):
        # Both can be up at once only if they publish different SSH ports.
        mine = re.search(r"^VM_SSH_PORT=(\d+)", ENV.read_text(), re.M).group(1)
        xio = re.search(
            r"^VM_SSH_PORT=(\d+)",
            (REPO_ROOT / "qemu" / "env" / "xio").read_text(), re.M).group(1)
        self.assertNotEqual(mine, xio)
        self.assertIn(f"VM_SSH_PORT:-{mine}}}", self.text)

    def test_the_guest_name_does_not_collide_with_the_xio_stack(self):
        mine = re.search(r"^VM_NAME=(\S+)", ENV.read_text(), re.M).group(1)
        xio = re.search(
            r"^VM_NAME=(\S+)",
            (REPO_ROOT / "qemu" / "env" / "xio").read_text(), re.M).group(1)
        self.assertNotEqual(mine, xio)

    def test_the_inventory_host_is_the_vm_name(self):
        name = re.search(r"^VM_NAME=(\S+)", ENV.read_text(), re.M).group(1)
        inv = yaml.safe_load(
            (REPO_ROOT / "ansible" / "inventory" / "vfu-doorbell.yml").read_text())
        self.assertIn(name, inv["all"]["children"]["doorbell"]["hosts"])

    def test_the_playbook_checks_both_device_ids(self):
        play = (REPO_ROOT / "ansible" / "playbooks"
                / "vfu-doorbell-guest.yml").read_text()
        self.assertIn("1de0:0001", play)
        self.assertIn("1de0:0002", play)
        # The scenarios take a minute, so they run only when asked for.
        self.assertRegex(play, r"tags:\s*\[never,\s*selftest\]")

    def test_the_playbook_installs_the_ca_that_gen_vm_cannot(self):
        # A guest overlaid on the published qcow2 gets no cloud-init seed, so
        # VM_CA_CERT_FILE never reaches it through gen-vm.
        play = (REPO_ROOT / "ansible" / "playbooks"
                / "vfu-doorbell-guest.yml").read_text()
        self.assertIn("VM_CA_CERT_FILE", play)
        self.assertIn("update-ca-certificates", play)

    def test_the_settings_name_no_key_so_gen_vm_fetches_the_images(self):
        self.assertNotRegex(ENV.read_text(), r"(?m)^VM_SSH_KEY_FILE=")

    def test_the_stack_is_registered_everywhere_a_stack_has_to_be(self):
        compose_py = (REPO_ROOT / "qemu" / "qemu-tool" / "compose.py").read_text()
        stacks = re.search(r"_STACKS = \((.*?)\)", compose_py, re.S).group(1)
        self.assertIn(f'"{STACK_NAME}"', stacks)
        # Hand-written, not rendered by gen-compose.
        generated = re.search(r"_GENERATED_STACKS = frozenset\(\{(.*?)\}\)",
                              compose_py, re.S).group(1)
        self.assertNotIn(STACK_NAME, generated)
        install = (REPO_ROOT / "qemu" / "debian"
                   / "python3-qemu-tool.install").read_text()
        self.assertIn(f"compose/{STACK_NAME}/docker-compose.yml", install)
        self.assertIn(f"compose/{STACK_NAME}/README.md", install)
        dirs = (REPO_ROOT / "qemu" / "debian" / "python3-qemu-tool.dirs").read_text()
        self.assertIn(f"usr/share/qemu-tool/compose/{STACK_NAME}", dirs)
        for wf in ("qemu-minimal-gen-compose-check.yml",
                   "qemu-minimal-dry-run-qemu-tool.yml"):
            text = (REPO_ROOT / ".github" / "workflows" / wf).read_text()
            self.assertIn(STACK_NAME, text, wf)


if __name__ == "__main__":
    unittest.main()
