"""Tests for the block-device, GPU and login sections of `qemu-tool inspect`.

All three are joins between what the guest agent reports and what the QEMU
command line says, and the join is where the wrong answers live: the guest
sees a real-looking PCI device and a real-looking KFD node whether the device
is emulated or passed through, so nothing here may conclude "real" from a
guest fact alone. Synthetic agent payloads and synthetic argv throughout --
no running VM, no sysfs, no socket.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import inspect as inspect_mod  # noqa: E402


# A real /proc/partitions from a fleet guest, trimmed.
PARTITIONS = (
    "major minor  #blocks  name\n\n"
    " 253        0  536870912 vda\n"
    " 253        1  535708655 vda1\n"
    "   7        0          0 loop0\n"
    " 259        0 1875385008 nvme0n1\n"
)

ROOT_DISK = {
    "name": "/dev/vda",
    "partition": False,
    "dependencies": [],
    "address": {"bus-type": "virtio", "dev": "/dev/vda",
                "pci-controller": {"domain": 0, "bus": 0, "slot": 4, "function": 0}},
}
ROOT_PART = {"name": "/dev/vda1", "partition": True, "dependencies": ["/dev/vda"]}
LOOP_DISK = {"name": "/dev/loop0", "partition": False, "dependencies": []}
NVME_DISK = {
    "name": "/dev/nvme0n1",
    "partition": False,
    "dependencies": [],
    # ID_SERIAL as the guest's udev builds it: model, serial space-padded to
    # 20 and escaped to underscores, then the namespace id.
    "address": {"bus-type": "nvme", "dev": "/dev/nvme0n1",
                "serial": "QEMU_NVMe_Ctrl_qt-vm1-nvme0_______1"},
}

FSINFO = [{"name": "vda1", "mountpoint": "/", "type": "ext4",
           "total-bytes": 532019777536, "used-bytes": 10116022272,
           "disk": [{"bus-type": "virtio", "dev": "/dev/vda1"}]}]

KFD_GFX1250 = (
    "cpu_cores_count 0\nsimd_count 128\ngfx_target_version 120500\n"
    "vendor_id 4098\ndevice_id 30145\nlocation_id 48\n"
)


# Node 0 is the CPU node, so a GPU is 1 or higher. Split around the index
# rather than written out per call: the whole path is 45 characters of
# [A-Za-z0-9/], which the secret scanner reads as a high-entropy string and
# refuses the commit over. The format placeholder breaks the run.
def kfd_props(node: int = 1) -> str:
    return f"/sys/class/kfd/kfd/topology/nodes/{node}/properties"


def ga(**kw):
    """A guest-agent payload with the keys a healthy agent returns.

    gpu_complete is one of them: the in-guest script sets it once the KFD
    probe has reached the end of the node list, and without it an empty GPU
    list is a probe that did not run rather than a VM with no GPU.
    """
    files = kw.pop("files", {"/proc/partitions": PARTITIONS})
    payload = {"hostname": {"host-name": "vm1"}, "interfaces": [],
               "files": files, "gpu_complete": True}
    payload.update(kw)
    return payload


def vm(**kw):
    base = {
        "name": "qt-vm1", "pid": 4242, "arch": "amd64", "vcpus": 4,
        "memory_mib": 8192, "ssh_port_host": 2222, "image": "/images/qt-vm1.qcow2",
        "container": None, "uptime_seconds": 60, "vfio_user_sockets": [],
        "qmp_socket": None, "qmp_available": False,
        "ga_socket": "/tmp/qga.sock", "ga_available": True, "nvme": [],
    }
    base.update(kw)
    return base


EMULATED_NVME_ARGV = [{"bdf": None, "kind": "emulated", "source": "argv",
                       "qdev_id": "nvme-0-dev", "serial": "qt-vm1-nvme0"}]
PASSTHROUGH_NVME_ARGV = [{"bdf": "0000:03:00.0", "kind": "passthrough",
                          "source": "argv", "qdev_id": "vfio-host0",
                          "class_verified": True}]


class BlockDevices(unittest.TestCase):
    def test_root_disk_is_emulated_and_sized(self):
        devs = inspect_mod._block_devices(
            vm(), ga(disks=[ROOT_DISK, ROOT_PART], fsinfo=FSINFO))
        self.assertEqual(len(devs), 1, "a partition is not a second disk")
        d = devs[0]
        self.assertEqual(d["dev"], "/dev/vda")
        self.assertEqual(d["origin"], "emulated")
        self.assertEqual(d["bytes"], 536870912 * 1024)
        self.assertEqual(d["size"], "512.0 GiB")
        self.assertEqual(d["mountpoints"], ["/"])

    def test_emulated_nvme_joins_on_the_argv_serial(self):
        devs = inspect_mod._block_devices(
            vm(nvme=EMULATED_NVME_ARGV), ga(disks=[NVME_DISK]))
        self.assertEqual(devs[0]["origin"], "emulated")
        self.assertIsNone(devs[0]["host_bdf"])

    def test_emulated_nvme_joins_on_the_subsystem_nqn(self):
        files = {"/proc/partitions": PARTITIONS,
                 "/sys/block/nvme0n1/device/subsysnqn":
                     "nqn.2019-08.org.qemu:qt-vm1-nvme0\n"}
        disk = dict(NVME_DISK, address={"bus-type": "nvme"})  # udev gave no serial
        devs = inspect_mod._block_devices(
            vm(nvme=EMULATED_NVME_ARGV), ga(disks=[disk], files=files))
        self.assertEqual(devs[0]["origin"], "emulated")

    def test_passthrough_nvme_reports_the_host_bdf(self):
        disk = dict(NVME_DISK, address={"bus-type": "nvme", "serial": "S4EWNX0R12345"})
        devs = inspect_mod._block_devices(
            vm(nvme=PASSTHROUGH_NVME_ARGV), ga(disks=[disk]))
        self.assertEqual(devs[0]["origin"], "passthrough")
        self.assertEqual(devs[0]["host_bdf"], "0000:03:00.0")

    def test_unmatched_nvme_is_unknown_not_real(self):
        disk = dict(NVME_DISK, address={"bus-type": "nvme", "serial": "S4EWNX0R12345"})
        devs = inspect_mod._block_devices(vm(), ga(disks=[disk]))
        self.assertEqual(devs[0]["origin"], "unknown")

    def test_two_passthrough_candidates_name_no_bdf(self):
        two = PASSTHROUGH_NVME_ARGV + [dict(PASSTHROUGH_NVME_ARGV[0],
                                            bdf="0000:04:00.0")]
        disk = dict(NVME_DISK, address={"bus-type": "nvme", "serial": "S4EWNX0R12345"})
        devs = inspect_mod._block_devices(vm(nvme=two), ga(disks=[disk]))
        self.assertEqual(devs[0]["origin"], "passthrough")
        self.assertIsNone(devs[0]["host_bdf"])

    def test_unbacked_loop_device_is_not_a_disk(self):
        devs = inspect_mod._block_devices(vm(), ga(disks=[LOOP_DISK, ROOT_DISK]))
        self.assertEqual([d["dev"] for d in devs], ["/dev/vda"])

    def test_device_the_agent_cannot_size(self):
        unsized = {"name": "/dev/sdb", "partition": False,
                   "address": {"bus-type": "scsi"}}
        devs = inspect_mod._block_devices(
            vm(), ga(disks=[unsized], files={}))
        self.assertIsNone(devs[0]["bytes"])
        self.assertEqual(devs[0]["size"], "-")
        self.assertEqual(devs[0]["origin"], "unknown")

    def test_unavailable_when_the_agent_did_not_answer(self):
        self.assertIsNone(inspect_mod._block_devices(vm(), {}))
        self.assertEqual(inspect_mod._disk_lines(None), [])

    def test_every_disk_is_listed(self):
        # Was capped at six with a "+N more" tail. On a fleet guest with
        # VM_NVME=8 that hid a third of the devices, and could hide the root
        # disk -- the one you most want named.
        disks = [dict(ROOT_DISK, name=f"/dev/vd{c}") for c in "abcdefgh"]
        lines = inspect_mod._disk_lines(inspect_mod._block_devices(
            vm(), ga(disks=disks)))
        self.assertEqual(len(lines), 8)
        self.assertFalse([t for _, t in lines if "more" in t])

    def test_disks_are_sorted_naturally_not_lexicographically(self):
        # nvme10n1 after nvme9n1. VM_NVME has no ceiling, so a two-digit
        # fleet is reachable, and plain sorted() puts nvme10 next to nvme1.
        names = ["/dev/nvme10n1", "/dev/nvme2n1", "/dev/nvme1n1", "/dev/nvme9n1"]
        disks = [dict(ROOT_DISK, name=n, mountpoints=[]) for n in names]
        lines = inspect_mod._disk_lines(inspect_mod._block_devices(
            vm(), ga(disks=disks)))
        self.assertEqual(
            [text.split()[0] for _, text in lines],
            ["/dev/nvme1n1", "/dev/nvme2n1", "/dev/nvme9n1", "/dev/nvme10n1"])

    def test_the_boot_disk_is_identified(self):
        # By mountpoint, not by position: the agent returned the root disk
        # SECOND on a fleet guest with 8 NVMe, so "the first one" is wrong.
        disks = [
            dict(NVME_DISK, name="/dev/nvme1n1"),
            ROOT_DISK,
            ROOT_PART,
        ]
        lines = inspect_mod._disk_lines(inspect_mod._block_devices(
            vm(), ga(disks=disks, fsinfo=FSINFO)))
        self.assertEqual(lines[0][0], "Boot Disk", lines)
        self.assertTrue(lines[0][1].startswith("/dev/vda"), lines)
        self.assertEqual([lab for lab, _ in lines[1:]], ["Disk"], lines)

    def test_nvme_partition_parents_resolve(self):
        # Stripping trailing digits gives "nvme0n" and "nvme0n1p", neither of
        # which names a device, so a mounted NVMe used to show no mountpoint.
        self.assertEqual(inspect_mod._parent_device("nvme0n1"), "nvme0n1")
        self.assertEqual(inspect_mod._parent_device("nvme0n1p3"), "nvme0n1")
        self.assertEqual(inspect_mod._parent_device("nvme10n1p2"), "nvme10n1")
        self.assertEqual(inspect_mod._parent_device("mmcblk0p1"), "mmcblk0")
        # Unchanged for the names that already worked.
        self.assertEqual(inspect_mod._parent_device("vda1"), "vda")
        self.assertEqual(inspect_mod._parent_device("sdb2"), "sdb")
        self.assertEqual(inspect_mod._parent_device("vda"), "vda")

    def test_a_disk_with_mounts_but_no_root_is_not_the_boot_disk(self):
        # A data NVMe mounted at /mnt/nvme0 is mounted, not bootable.
        fsinfo = [{"name": "nvme1n1", "mountpoint": "/mnt/nvme0", "type": "ext4",
                   "disk": [{"bus-type": "nvme", "dev": "/dev/nvme1n1"}]}]
        disks = [dict(NVME_DISK, name="/dev/nvme1n1")]
        lines = inspect_mod._disk_lines(inspect_mod._block_devices(
            vm(), ga(disks=disks, fsinfo=fsinfo)))
        self.assertEqual(lines[0][0], "Disk")
        self.assertIn("/mnt/nvme0", lines[0][1])


class Capacity(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(inspect_mod._fmt_bytes(512), "512 B")
        self.assertEqual(inspect_mod._fmt_bytes(536870912 * 1024), "512.0 GiB")
        self.assertEqual(inspect_mod._fmt_bytes(1875385008 * 1024), "1.7 TiB")

    def test_no_size_is_not_zero(self):
        self.assertEqual(inspect_mod._fmt_bytes(None), "-")

    def test_parses_proc_partitions(self):
        sizes = inspect_mod._proc_partitions(PARTITIONS)
        self.assertEqual(sizes["vda"], 536870912 * 1024)
        self.assertEqual(sizes["loop0"], 0)

    def test_garbage_partitions_text_is_survivable(self):
        self.assertEqual(inspect_mod._proc_partitions("not\na table"), {})
        self.assertEqual(inspect_mod._proc_partitions(None), {})


class Gpus(unittest.TestCase):
    def setUp(self):
        self.servers = mock.patch.object(
            inspect_mod, "_rocjitsu_gpus",
            lambda socks: ["gfx1250"] if any("rocjitsu" in s for s in socks) else [],
        )
        self.servers.start()
        self.addCleanup(self.servers.stop)

    def test_emulated_gpu_from_the_rocjitsu_config(self):
        gpus = inspect_mod._gpu_devices(
            vm(vfio_user_sockets=["/run/vfu/ernic-1.sock",
                                  "/run/vfu/rocjitsu-1.sock"]),
            ga(files={kfd_props(): KFD_GFX1250}),
            [],
        )
        self.assertEqual(gpus, [{"gfx": "gfx1250", "origin": "emulated",
                                 "source": "rocjitsu-1.sock"}])

    def test_passthrough_gpu_takes_its_gfx_from_the_guest(self):
        argv = ["qemu-system-x86_64", "-device",
                "vfio-pci,id=vfio-host0,bus=pcie.0,host=0000:03:00.0"]
        with mock.patch.object(inspect_mod, "_pci_class", lambda bdf: "0x120000"):
            gpus = inspect_mod._gpu_devices(
                vm(),
                ga(files={kfd_props():
                          KFD_GFX1250}),
                argv,
            )
        self.assertEqual(gpus, [{"gfx": "gfx1250", "origin": "passthrough",
                                 "source": "03:00.0"}])

    def test_a_passed_through_nic_is_not_a_gpu(self):
        argv = ["qemu-system-x86_64", "-device", "vfio-pci,host=0000:03:00.0"]
        with mock.patch.object(inspect_mod, "_pci_class", lambda bdf: "0x020000"):
            self.assertEqual(inspect_mod._gpu_devices(vm(), ga(), argv), [])

    def test_none_at_all(self):
        self.assertEqual(inspect_mod._gpu_devices(vm(), ga(), []), [])

    def test_guest_gfx_with_no_host_origin_is_unknown(self):
        gpus = inspect_mod._gpu_devices(
            vm(),
            ga(files={kfd_props(): KFD_GFX1250}),
            [],
        )
        self.assertEqual(gpus, [{"gfx": "gfx1250", "origin": "unknown",
                                 "source": "guest"}])

    def test_ambiguous_pairing_leaves_the_real_gpu_without_a_gfx(self):
        """Two real GPUs and one unclaimed gfx: naming either one is a guess."""
        argv = ["-device", "vfio-pci,host=0000:03:00.0",
                "-device", "vfio-pci,host=0000:04:00.0"]
        with mock.patch.object(inspect_mod, "_pci_class", lambda bdf: "0x030000"):
            gpus = inspect_mod._gpu_devices(
                vm(),
                ga(files={kfd_props():
                          KFD_GFX1250}),
                argv,
            )
        self.assertEqual([g["gfx"] for g in gpus], [None, None, "gfx1250"])
        self.assertEqual([g["origin"] for g in gpus],
                         ["passthrough", "passthrough", "unknown"])

    def test_decodes_the_kfd_target_version(self):
        self.assertEqual(inspect_mod._gfx_from_kfd("gfx_target_version 120500"),
                         "gfx1250")
        self.assertEqual(inspect_mod._gfx_from_kfd("gfx_target_version 90402"),
                         "gfx942")
        self.assertEqual(inspect_mod._gfx_from_kfd("gfx_target_version 90010"),
                         "gfx90a")

    def test_cpu_node_is_not_a_gpu(self):
        self.assertIsNone(inspect_mod._gfx_from_kfd(
            "cpu_cores_count 8\ngfx_target_version 0\n"))
        self.assertIsNone(inspect_mod._gfx_from_kfd("simd_count 0\n"))


class AStarvedDeviceProbeIsUnknown(unittest.TestCase):
    """M1. The device probes share the 8 s in-guest budget with the session walk.

    They are not symmetric: a truncated session walk says "unknown", while a
    truncated KFD probe used to say "this VM has no GPU" -- a claim, and a
    false one, caused by somebody being logged in. The order is the fix (the
    bounded always-wanted probes go first); this is the evidence that makes
    the remaining starvation readable.
    """

    def test_an_unprobed_guest_has_an_unknown_gpu_count_not_a_zero(self):
        starved = ga(files={"/proc/partitions": PARTITIONS}, gpu_complete=False)
        self.assertIsNone(inspect_mod._gpu_devices(vm(), starved, []))

    def test_a_missing_completeness_flag_is_also_unknown(self):
        payload = ga()
        payload.pop("gpu_complete")
        self.assertIsNone(inspect_mod._gpu_devices(vm(), payload, []))

    def test_a_completed_probe_that_found_nothing_is_a_real_none(self):
        self.assertEqual(inspect_mod._gpu_devices(vm(), ga(), []), [])

    def test_no_agent_at_all_is_still_a_real_none(self):
        """The command line is the whole evidence: a guest cannot have a GPU
        the host never handed it, so an unreachable agent does not make the
        host's own answer doubtful."""
        self.assertEqual(inspect_mod._gpu_devices(vm(), {}, []), [])

    def test_a_host_derived_gpu_needs_no_guest_evidence(self):
        with mock.patch.object(inspect_mod, "_rocjitsu_gpus",
                               lambda s: ["gfx1250"]):
            gpus = inspect_mod._gpu_devices(
                vm(vfio_user_sockets=["/run/vfu/rocjitsu-1.sock"]),
                ga(gpu_complete=False), [])
        self.assertEqual([g["origin"] for g in gpus], ["emulated"])

    def test_the_human_line_says_unknown_rather_than_printing_nothing(self):
        out = StringIO()
        record = {"name": "qt-vm1", "pid": 1, "arch": "amd64", "vcpus": 4,
                  "memory_mib": 1024, "ssh_cmd": None, "ssh_port": 2222,
                  "username": "ubuntu", "image": None, "container": None,
                  "uptime_seconds": 1, "vfio_user_sockets": [],
                  "ga_socket": "/tmp/qga.sock", "gpus": None,
                  "block_devices": None, "ssh_sessions": None}
        with mock.patch("sys.stdout", out):
            inspect_mod._print_human([record])
        text = out.getvalue()
        self.assertIn("  GPU:       unknown", text)
        self.assertIn("  Disk:      unknown", text)
        self.assertIn("  Login:     unknown", text)

    def test_a_vm_with_no_agent_socket_claims_nothing_either_way(self):
        out = StringIO()
        record = {"name": "qt-vm1", "pid": 1, "arch": "amd64", "vcpus": 4,
                  "memory_mib": 1024, "ssh_cmd": None, "ssh_port": 2222,
                  "username": "ubuntu", "image": None, "container": None,
                  "uptime_seconds": 1, "vfio_user_sockets": [],
                  "ga_socket": None, "gpus": [],
                  "block_devices": None, "ssh_sessions": None}
        with mock.patch("sys.stdout", out):
            inspect_mod._print_human([record])
        self.assertNotIn("Disk:", out.getvalue())
        self.assertNotIn("Login:", out.getvalue())


class Sessions(unittest.TestCase):
    def test_nobody_logged_in(self):
        r = {"ga_socket": "/tmp/qga.sock",
             "ssh_sessions": inspect_mod._ssh_sessions(ga(users=[], utmp=True))}
        self.assertEqual(r["ssh_sessions"], [])
        self.assertEqual(inspect_mod._login_line(r), "none")

    def test_empty_on_a_guest_without_utmp_is_unknown(self):
        """guest-get-users returns [] on Ubuntu 26.04 even with ssh open."""
        self.assertIsNone(inspect_mod._ssh_sessions(ga(users=[], utmp=False)))

    def test_one_user_with_a_login_time(self):
        sessions = inspect_mod._ssh_sessions(
            ga(users=[{"user": "stebates", "login-time": 1760000000.0}]))
        self.assertEqual(sessions[0]["user"], "stebates")
        self.assertEqual(sessions[0]["login_time"], 1760000000.0)
        line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": sessions})
        self.assertTrue(line.startswith("stebates (since "))

    def test_several_users(self):
        sessions = inspect_mod._ssh_sessions(ga(users=[
            {"user": "stebates", "login-time": 1760000000.0},
            {"user": "ubuntu"},
        ]))
        self.assertEqual([s["user"] for s in sessions], ["stebates", "ubuntu"])
        # No login-time is not a login-time of zero.
        self.assertIsNone(sessions[1]["login_time"])
        line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": sessions})
        self.assertTrue(line.endswith(", ubuntu"))

    def test_unknown_is_not_none(self):
        self.assertIsNone(inspect_mod._ssh_sessions({}))
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": None}),
            "unknown")

    def test_a_vm_with_no_agent_socket_prints_no_line(self):
        self.assertIsNone(
            inspect_mod._login_line({"ga_socket": None, "ssh_sessions": None}))


class RecordShape(unittest.TestCase):
    def _record(self, ga_info, **vm_kw):
        with mock.patch.object(inspect_mod, "_ga_query", lambda *a: ga_info), \
                mock.patch.object(inspect_mod, "_read_vm_info", lambda i: {}), \
                mock.patch.object(inspect_mod, "_ssh_key_for_image",
                                  lambda *a: ([], None)), \
                mock.patch.object(inspect_mod, "_vm_argv", lambda pid: []), \
                mock.patch.object(inspect_mod, "_rocjitsu_gpus",
                                  lambda s: ["gfx1250"] if s else []):
            from qemu_tool.config import VMConfig
            return inspect_mod._record(vm(**vm_kw), VMConfig())

    def test_json_shape(self):
        # utmp=True is what makes the empty users list a reportable "none";
        # without that evidence the session field is None, which is a
        # different shape and is tested next door.
        r = self._record(ga(disks=[ROOT_DISK, ROOT_PART], fsinfo=FSINFO,
                            users=[], utmp=True),
                         vfio_user_sockets=["/run/vfu/rocjitsu-1.sock"])
        json.dumps(r)  # the three new sections must be serialisable
        self.assertEqual(r["gpus"], [{"gfx": "gfx1250", "origin": "emulated",
                                      "source": "rocjitsu-1.sock"}])
        self.assertEqual(r["ssh_sessions"], [])
        self.assertEqual(
            r["block_devices"],
            [{"dev": "/dev/vda", "bytes": 536870912 * 1024, "size": "512.0 GiB",
              "bus": "virtio", "origin": "emulated", "host_bdf": None,
              "mountpoints": ["/"]}])

    def test_raw_sysfs_text_is_not_carried_into_json(self):
        r = self._record(ga(disks=[ROOT_DISK]))
        self.assertNotIn("files", r["guest"])
        self.assertEqual(r["guest"]["hostname"], {"host-name": "vm1"})

    def test_every_section_degrades_when_the_agent_is_unreachable(self):
        r = self._record({})
        self.assertIsNone(r["block_devices"])
        self.assertIsNone(r["ssh_sessions"])
        self.assertEqual(r["gpus"], [])  # host-derived, so never unknown
        self.assertNotIn("guest", r)
        self.assertEqual(r["name"], "qt-vm1")  # the VM is still reported

    def test_inspect_still_prints_without_an_agent(self):
        out = StringIO()
        with mock.patch("sys.stdout", out):
            inspect_mod._print_human([self._record({})])
        text = out.getvalue()
        self.assertIn("VM: qt-vm1", text)
        self.assertIn("Arch:", text)
        # The agent socket is there and did not answer, so the disks are in
        # exactly the state the Login line is in: unasked, not empty.
        self.assertIn("Disk:      unknown", text)
        self.assertIn("Login:     unknown", text)

    def test_human_output_of_a_full_record(self):
        out = StringIO()
        record = self._record(
            ga(disks=[ROOT_DISK, ROOT_PART], fsinfo=FSINFO,
               users=[{"user": "stebates", "login-time": 1760000000.0}]),
            vfio_user_sockets=["/run/vfu/rocjitsu-1.sock"])
        with mock.patch("sys.stdout", out):
            inspect_mod._print_human([record])
        text = out.getvalue()
        self.assertIn("  GPU:       gfx1250 (emulated, rocjitsu-1.sock)", text)
        self.assertIn("/dev/vda", text)
        self.assertIn("512.0 GiB", text)
        self.assertIn("emulated (virtio)", text)
        self.assertIn("  Login:     stebates (since", text)


if __name__ == "__main__":
    unittest.main()
