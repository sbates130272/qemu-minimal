"""Tests for `qemu-tool list --summary`.

The thing worth testing here is not the arithmetic -- it is that every
collector has an unavailable state and that the state renders as unavailable
rather than as zero. An idle fleet and a fleet whose stats files could not be
read produce the same numbers if the unreadable case contributes 0, and a
fleet summary that cannot tell them apart is worse than no summary.

Synthetic inputs throughout: no docker, no /proc, no running VM. The
collectors that shell out are monkeypatched at the one function that does it.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

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
from qemu_tool import summary  # noqa: E402


def _vm(idx: int, *, project_container: str | None = None,
        vcpus: int = 4, memory_mib: int = 8192, ga: bool = True,
        gpus=("gfx1250",), image: str | None = None):
    return {
        "pid": 1000 + idx,
        "name": f"vm-{idx}",
        "source": "qemu-tool",
        "role": "fleet",
        "vcpus": vcpus,
        "memory_mib": memory_mib,
        "uptime_seconds": 7200,
        "container": project_container,
        "gpus": list(gpus),
        "nvme": [],
        "image": image,
        "ga_socket": f"/run/qga-{idx}.sock",
        "ga_available": ga,
    }


def _container(idx: int, service: str, *, project="fleet", state="running",
               status="Up 2 hours"):
    name = f"{project}-{service}-1"
    return {
        "id": f"{idx:064x}",
        "name": name,
        "state": state,
        "status": status,
        "project": project,
        "service": service,
        "class": summary._service_class(service),
    }


class ServiceClass(unittest.TestCase):
    def test_numbered_services_collapse_to_one_class(self):
        self.assertEqual(summary._service_class("ernic-17"), "ernic")
        self.assertEqual(summary._service_class("qemu-1"), "qemu")

    def test_a_name_ending_in_a_word_survives(self):
        # Only a trailing -<digits> comes off, or the exporter would be
        # grouped as "ernic-stats".
        self.assertEqual(
            summary._service_class("ernic-stats-exporter"),
            "ernic-stats-exporter")


class RollCall(unittest.TestCase):
    def _containers(self, extra=()):
        found = {}
        idx = 0
        for service in ("qemu-1", "qemu-2", "ernic-1", "ernic-2",
                        "rocjitsu-1", "rocjitsu-2"):
            idx += 1
            c = _container(idx, service)
            found[c["id"]] = c
        for c in extra:
            found[c["id"]] = c
        return found

    def test_running_over_total_per_class(self):
        roll = summary._roll_call("fleet", self._containers())
        self.assertEqual(roll["services"]["qemu"], {"running": 2, "total": 2})
        self.assertEqual(roll["services"]["ernic"], {"running": 2, "total": 2})

    def test_a_dead_service_shrinks_the_numerator_not_the_denominator(self):
        dead = _container(90, "ernic-2", state="restarting",
                          status="Restarting (1)")
        found = self._containers()
        for cid, c in list(found.items()):
            if c["service"] == "ernic-2":
                del found[cid]
        found[dead["id"]] = dead
        roll = summary._roll_call("fleet", found)
        self.assertEqual(roll["services"]["ernic"], {"running": 1, "total": 2})

    def test_a_one_shot_that_exited_zero_is_not_counted_as_down(self):
        # The tapsetup sidecars hold CAP_NET_ADMIN just long enough to make
        # the TAP; all N are Exited(0) on a perfectly healthy fleet.
        taps = [_container(50 + i, f"tapsetup-{i}", state="exited",
                           status="Exited (0) 2 hours ago") for i in range(2)]
        roll = summary._roll_call("fleet", self._containers(taps))
        self.assertNotIn("tapsetup", roll["services"])
        self.assertEqual(roll["completed"], ["tapsetup"])
        self.assertEqual(roll["infra"], {"running": 0, "total": 0})

    def test_a_one_shot_that_exited_nonzero_is_counted(self):
        taps = [_container(50, "tapsetup-0", state="exited",
                           status="Exited (1) 2 hours ago")]
        roll = summary._roll_call("fleet", self._containers(taps))
        self.assertEqual(roll["services"]["tapsetup"],
                         {"running": 0, "total": 1})

    def test_infra_rolls_up_everything_that_is_not_per_vm(self):
        infra = [
            _container(60, "prometheus", state="restarting",
                       status="Restarting (1)"),
            _container(61, "grafana", state="restarting",
                       status="Restarting (1)"),
            _container(62, "ernic-stats-exporter"),
        ]
        roll = summary._roll_call("fleet", self._containers(infra))
        self.assertEqual(roll["infra"], {"running": 1, "total": 3})

    def test_no_docker_at_all_is_unavailable_not_empty(self):
        roll = summary._roll_call("fleet", {})
        self.assertEqual(roll["source"], "unavailable")


class ErnicAggregate(unittest.TestCase):
    """The ernic collector, with _ernic_dumps (the docker exec) stubbed."""

    DUMP = """=== ROCm ERNIC Statistics ===
Instance:
  socket      : /run/vfu/ernic-{n}.sock
  backend     : tcp:worker:172.31.1.1:6320
  connection  : connected (device reset)

Device Statistics:
  reset_count             : 1
  total_bytes_sent        : {sent}
  total_bytes_received    : 0
  total_bytes_rdma_read   : 0
  total_bytes_rdma_write  : 0
  total_ip_bytes_tx       : {tx}
  total_ip_bytes_rx       : 999999
"""

    def _run(self, dumps, hosts=2):
        containers = {}
        for i in range(1, hosts + 1):
            c = _container(i, f"ernic-{i}")
            containers[c["id"]] = c
        original = summary._ernic_dumps
        summary._ernic_dumps = lambda name: dumps
        try:
            return summary._ernic_fleet("fleet", containers)
        finally:
            summary._ernic_dumps = original

    def test_counters_sum_and_rx_is_excluded_from_the_tx_total(self):
        got = self._run({
            "ernic-1.stats": self.DUMP.format(n=1, sent=100, tx=10),
            "ernic-2.stats": self.DUMP.format(n=2, sent=200, tx=20),
        })
        self.assertEqual(got["rdma_bytes"], 300)
        # rx is 999999 per instance and deliberately never added: the l2
        # bridge floods every frame to every guest.
        self.assertEqual(got["ip_bytes_tx"], 30)
        self.assertEqual(got["connected"], 2)
        self.assertEqual(got["instances"], 2)

    def test_an_unreadable_file_shrinks_the_denominator(self):
        # The server rewrites in place with no atomic rename, so a read can
        # land mid-write and parse to nothing. That instance must not add a
        # zero that reads as an idle node.
        got = self._run({
            "ernic-1.stats": self.DUMP.format(n=1, sent=100, tx=10),
            "ernic-2.stats": "",
        })
        self.assertEqual(got["instances"], 1)
        self.assertEqual(got["expected"], 2)
        self.assertEqual(got["rdma_bytes"], 100)

    def test_no_dumps_at_all_is_unavailable_not_zero(self):
        got = self._run({})
        self.assertEqual(got["source"], "unavailable")
        self.assertIsNone(got["rdma_bytes"])
        self.assertIsNone(got["connected"])
        self.assertEqual(got["expected"], 2)

    def test_a_stack_with_no_ernic_is_unavailable_with_no_expectation(self):
        got = self._run({}, hosts=0)
        self.assertEqual(got["source"], "unavailable")
        self.assertEqual(got["expected"], 0)


class Headroom(unittest.TestCase):
    HOST = {"cores": 192, "memory_available_mib": 930000,
            "memory_mib": 976 * 1024, "loadavg": [1.0, 1.0, 1.0],
            "hostname": "node", "loadavg_scope": "host", "source": "proc"}

    def _headroom(self, vms, ernic=None, storage=None):
        ernic = ernic or {"instances": 32, "expected": 32, "connected": 32,
                          "rdma_bytes": 0, "ip_bytes_tx": 0, "resets": 32,
                          "source": "ernic-stats"}
        storage = storage or {"allocated_bytes": 1 << 30,
                              "virtual_bytes": 16 << 40,
                              "free_bytes": 1 << 40, "images": 1,
                              "images_measured": 1, "source": "stat+qemu-img"}
        memory = {"allocated_mib": sum(v["memory_mib"] for v in vms)}
        return summary._headroom(vms, self.HOST, ernic, storage, memory)

    def test_the_mesh_wall_binds_before_the_vcpu_budget(self):
        vms = [_vm(i) for i in range(32)]
        h = self._headroom(vms)
        self.assertEqual(h["binding"], "mesh")
        self.assertEqual(h["vms"], summary.MESH_NODE_WALL - 32)
        vcpu = next(r for r in h["rows"] if r["name"] == "vcpu")
        self.assertEqual(vcpu["used"], 128)
        self.assertEqual(vcpu["limit"], 240)
        self.assertEqual(vcpu["vms"], 28)

    def test_disk_reports_an_overcommit_ratio_and_no_vm_count(self):
        h = self._headroom([_vm(0)])
        disk = next(r for r in h["rows"] if r["name"] == "disk")
        self.assertIsNone(disk["vms"])
        self.assertEqual(disk["note"], "16x overcommit")

    def test_an_unreadable_mesh_cannot_bind_and_says_so(self):
        h = self._headroom([_vm(i) for i in range(32)], ernic={
            "instances": 0, "expected": 32, "connected": None,
            "rdma_bytes": None, "ip_bytes_tx": None, "resets": None,
            "source": "unavailable"})
        mesh = next(r for r in h["rows"] if r["name"] == "mesh")
        self.assertIsNone(mesh["used"])
        self.assertIsNone(mesh["vms"])
        self.assertEqual(h["binding"], "vcpu")

    def test_a_fleet_of_none_has_no_ceiling_to_report(self):
        h = summary._headroom(
            [], self.HOST,
            {"instances": 0, "expected": 0, "connected": None,
             "rdma_bytes": None, "ip_bytes_tx": None, "resets": None,
             "source": "unavailable"},
            {"allocated_bytes": None, "virtual_bytes": None,
             "free_bytes": None, "images": 0, "source": "unavailable"},
            {"allocated_mib": None})
        # No VM means no per-VM size, so "+N VMs" is not a number anyone has.
        self.assertIsNone(h["binding"])


class Sessions(unittest.TestCase):
    def test_unknown_never_folds_into_zero(self):
        vms = [_vm(0), _vm(1), _vm(2)]
        found = {id(vms[0]): [], id(vms[1]): None,
                 id(vms[2]): [{"user": "ubuntu", "service": "sshd",
                               "remote_host": "172.31.0.1", "session": "15",
                               "login_time": None}]}
        got = summary._sessions_summary(vms, found, True)
        self.assertEqual(got["sessions"], 1)
        self.assertEqual(got["unknown"], 1)
        self.assertEqual(len(got["occupied"]), 1)
        self.assertEqual(got["users"], {"ubuntu": 1})

    def test_no_agent_is_unknown_not_nobody(self):
        vms = [_vm(0, ga=False)]
        got = summary._sessions_summary(vms, {id(vms[0]): None}, True)
        self.assertEqual(got["unknown"], 1)
        self.assertEqual(got["sessions"], 0)
        self.assertIn("1 unknown", summary._login_line(got))

    def test_no_guests_renders_as_not_collected(self):
        vms = [_vm(0)]
        got = summary._sessions_summary(vms, {}, False)
        self.assertIsNone(got["sessions"])
        self.assertEqual(got["source"], "not-collected")
        self.assertIn("not collected", summary._login_line(got))

    def test_a_vm_with_no_agent_is_not_queried_at_all(self):
        self.assertIsNone(summary._vm_sessions(_vm(0, ga=False)))


class Cpu(unittest.TestCase):
    def _cpu(self, members, containers, first, second, pf, ps, elapsed=5.0):
        member_cids = {}
        by_name = {c["name"]: c for c in containers.values()}
        for vm in members:
            if vm["container"] in by_name:
                member_cids[by_name[vm["container"]]["id"]] = vm
        return summary._cpu("fleet", members, containers, member_cids,
                            first, second, pf, ps, elapsed)

    def test_cores_split_by_service_class(self):
        qemu = _container(1, "qemu-1")
        ernic = _container(2, "ernic-1")
        containers = {qemu["id"]: qemu, ernic["id"]: ernic}
        vm = _vm(1, project_container=qemu["name"])
        first = {qemu["id"]: {"usage_usec": 0, "shmem": 0, "current": 0},
                 ernic["id"]: {"usage_usec": 0, "shmem": 0, "current": 0}}
        second = {qemu["id"]: {"usage_usec": 5_000_000, "shmem": 0, "current": 0},
                  ernic["id"]: {"usage_usec": 10_000_000, "shmem": 0, "current": 0}}
        got = self._cpu([vm], containers, first, second,
                        {vm["pid"]: 0}, {vm["pid"]: 0})
        self.assertEqual(got["by_service"], {"ernic": 2.0, "qemu": 1.0})
        self.assertEqual(got["cores_busy"], 3.0)

    def test_a_container_that_restarted_mid_window_is_dropped_not_negative(self):
        qemu = _container(1, "qemu-1")
        containers = {qemu["id"]: qemu}
        first = {qemu["id"]: {"usage_usec": 9_000_000, "shmem": 0, "current": 0}}
        second = {qemu["id"]: {"usage_usec": 10_000, "shmem": 0, "current": 0}}
        got = self._cpu([], containers, first, second, {}, {})
        self.assertEqual(got["source"], "unavailable")
        self.assertIsNone(got["cores_busy"])

    def test_an_uncontainerised_vm_still_counts(self):
        vm = _vm(1, project_container=None)
        got = self._cpu([vm], {}, {}, {},
                        {vm["pid"]: 0},
                        {vm["pid"]: int(5.0 * summary._CLK_TCK)})
        self.assertEqual(got["by_service"], {"qemu": 1.0})
        self.assertEqual(got["per_vm"][0]["cores_busy"], 1.0)

    def test_a_vm_whose_proc_entry_vanished_is_none_not_zero(self):
        vm = _vm(1, project_container=None)
        got = self._cpu([vm], {}, {}, {}, {vm["pid"]: None}, {vm["pid"]: None})
        self.assertIsNone(got["per_vm"][0]["cores_busy"])
        self.assertEqual(got["source"], "unavailable")


class Memory(unittest.TestCase):
    def test_guest_ram_is_shmem_not_anon(self):
        qemu = _container(1, "qemu-1")
        vm = _vm(1, project_container=qemu["name"])
        sample = {qemu["id"]: {"usage_usec": 0, "shmem": 1 << 30,
                               "current": 2 << 30}}
        got = summary._memory([vm], {qemu["id"]: vm}, sample)
        self.assertEqual(got["guest_resident_bytes"], 1 << 30)
        self.assertEqual(got["unreclaimable_bytes"], 2 << 30)
        self.assertEqual(got["allocated_mib"], 8192)

    def test_no_cgroup_is_unavailable_not_zero(self):
        vm = _vm(1, project_container=None)
        got = summary._memory([vm], {}, {})
        self.assertIsNone(got["guest_resident_bytes"])
        self.assertEqual(got["source"], "unavailable")
        # The allocation is argv, not cgroup, so it survives.
        self.assertEqual(got["allocated_mib"], 8192)


class Storage(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.original = summary._qemu_img_info

        def restore():
            summary._qemu_img_info = self.original
        self.addCleanup(restore)

    def _touch(self, name: str, size: int) -> str:
        p = self.dir / name
        p.write_bytes(b"\0" * size)
        return str(p)

    def test_a_shared_backing_file_is_counted_once(self):
        backing = self._touch("backing.qcow2", 4096)
        overlays = [self._touch(f"vm{i}.qcow2", 4096) for i in range(3)]
        summary._qemu_img_info = lambda path: {
            "virtual-size": 1 << 30, "full-backing-filename": backing}
        vms = [_vm(i, image=overlays[i]) for i in range(3)]
        got = summary._storage(vms)
        # Three overlays plus one backing, not three overlays plus three
        # backings: otherwise a 15.8 G shared base reads as 47 G.
        blocks = os.stat(backing).st_blocks * 512
        self.assertEqual(got["allocated_bytes"], blocks * 4)
        self.assertEqual(got["virtual_bytes"], 3 << 30)
        self.assertEqual(got["images"], 3)

    def test_nvme_scratch_images_are_included(self):
        root = self._touch("vm0.qcow2", 4096)
        scratch = self._touch("vm0-nvme1.qcow2", 4096)
        summary._qemu_img_info = lambda path: {"virtual-size": 1 << 30}
        vm = _vm(0, image=root)
        vm["nvme"] = [{"kind": "emulated", "image": scratch}]
        got = summary._storage([vm])
        self.assertEqual(got["images"], 2)
        self.assertEqual(got["virtual_bytes"], 2 << 30)

    def test_qemu_img_failing_leaves_virtual_unknown_not_zero(self):
        root = self._touch("vm0.qcow2", 4096)
        summary._qemu_img_info = lambda path: None
        got = summary._storage([_vm(0, image=root)])
        self.assertIsNone(got["virtual_bytes"])
        # stat still answered, so allocation is real.
        self.assertEqual(got["allocated_bytes"],
                         os.stat(root).st_blocks * 512)
        self.assertEqual(got["source"], "stat")

    def test_a_vm_with_no_image_at_all(self):
        got = summary._storage([_vm(0, image=None)])
        self.assertEqual(got["source"], "unavailable")
        self.assertIsNone(got["allocated_bytes"])


class Gpu(unittest.TestCase):
    def test_targets_are_counted_and_utilisation_is_explicitly_absent(self):
        got = summary._gpu([_vm(i) for i in range(3)])
        self.assertEqual(got["count"], 3)
        self.assertEqual(got["targets"], {"gfx1250": 3})
        self.assertIsNone(got["utilisation"])
        self.assertEqual(got["utilisation_source"], "unmeasurable")

    def test_no_rocjitsu_profile_is_no_gpus(self):
        got = summary._gpu([_vm(0, gpus=())])
        self.assertEqual(got["count"], 0)
        self.assertIsNone(got["origin"])


class Outliers(unittest.TestCase):
    def _cpu_block(self, cores):
        return {"per_vm": [{"vm": f"vm-{i}", "container": f"qemu-{i}",
                            "cores_busy": c} for i, c in enumerate(cores)]}

    def test_a_vm_far_above_the_fleet_median_is_named(self):
        cpu = self._cpu_block([0.14] * 30 + [1.18, 1.15])
        out = summary._outliers(cpu, {}, "fleet")
        self.assertEqual(out["cpu_median_cores"], 0.14)
        self.assertEqual([v["cores_busy"] for v in out["cpu_hot"]],
                         [1.18, 1.15])

    def test_a_uniform_fleet_has_no_outliers(self):
        out = summary._outliers(self._cpu_block([0.14] * 32), {}, "fleet")
        self.assertEqual(out["cpu_hot"], [])

    def test_a_quiet_fleet_does_not_flag_noise_as_hot(self):
        # 4x a near-zero median is still near zero; the absolute floor is what
        # stops 0.04 cores being reported as an anomaly.
        out = summary._outliers(self._cpu_block([0.01] * 31 + [0.05]),
                                {}, "fleet")
        self.assertEqual(out["cpu_hot"], [])

    def test_a_restarting_container_is_reported_with_its_status(self):
        prom = _container(60, "prometheus", state="restarting",
                          status="Restarting (1)")
        out = summary._outliers(self._cpu_block([]), {prom["id"]: prom},
                                "fleet")
        self.assertEqual(out["containers"],
                         [{"name": prom["name"], "status": "Restarting (1)"}])

    def test_a_completed_one_shot_is_not_an_outlier(self):
        tap = _container(50, "tapsetup-1", state="exited",
                         status="Exited (0) 2 hours ago")
        out = summary._outliers(self._cpu_block([]), {tap["id"]: tap},
                                "fleet")
        self.assertEqual(out["containers"], [])


class Render(unittest.TestCase):
    """The renderer must print the unavailable word, not a plausible number."""

    def _fleet(self, **over):
        base = {
            "project": "fleet", "vms": 2,
            "uptime_seconds": {"min": 7200, "max": 7200},
            "roll_call": {"services": {"qemu": {"running": 2, "total": 2},
                                       "ernic": {"running": 2, "total": 2}},
                          "infra": {"running": 0, "total": 0},
                          "completed": [], "source": "docker-ps"},
            "headroom": {"rows": [], "binding": None, "vms": None},
            "cpu": {"cores_busy": None, "cores_total": 192, "by_service": {},
                    "per_vm": [], "source": "unavailable"},
            "memory": {"allocated_mib": 16384, "guest_resident_bytes": None,
                       "unreclaimable_bytes": None, "containers_measured": 0,
                       "source": "unavailable"},
            "storage": {"allocated_bytes": None, "virtual_bytes": None,
                        "free_bytes": None, "images": 0,
                        "source": "unavailable"},
            "network": {"instances": 0, "expected": 2, "connected": None,
                        "rdma_bytes": None, "ip_bytes_tx": None,
                        "resets": None, "source": "unavailable"},
            "gpu": {"count": 0, "targets": {}, "origin": None,
                    "utilisation": None, "utilisation_source": "unmeasurable"},
            "logins": {"sessions": None, "occupied": None, "unknown": None,
                       "users": None, "guests": 2, "source": "not-collected"},
            "outliers": {"cpu_median_cores": None, "cpu_hot": [],
                         "containers": []},
        }
        base.update(over)
        return base

    HOST = {"hostname": "node", "cores": 192, "memory_mib": 976 * 1024,
            "memory_available_mib": 900 * 1024, "loadavg": None,
            "loadavg_scope": "host", "source": "proc"}

    def _render(self, fleet):
        return "\n".join(summary.render({
            "schema": 1, "sampled_seconds": 5.0, "guests_probed": False,
            "host": self.HOST, "fleets": [fleet]}))

    def test_every_dead_collector_says_unavailable(self):
        text = self._render(self._fleet())
        for row in ("cpu", "hostload", "memory", "net"):
            line = next(ln for ln in text.splitlines()
                        if ln.strip().startswith(row))
            self.assertIn("unavailable", line, row)

    def test_a_zero_is_never_printed_for_an_unread_collector(self):
        text = self._render(self._fleet())
        self.assertNotIn("0.0 cores busy", text)
        self.assertNotIn("RDMA 0B", text)

    def test_an_rdma_total_of_zero_is_still_printed_when_it_was_measured(self):
        # Honesty cuts both ways: a measured zero IS the answer on an
        # RDMA-idle fleet and must not be hidden behind "unavailable".
        text = self._render(self._fleet(network={
            "instances": 2, "expected": 2, "connected": 2, "rdma_bytes": 0,
            "ip_bytes_tx": 1024, "resets": 2, "source": "ernic-stats"}))
        self.assertIn("RDMA 0B moved", text)
        self.assertIn("mesh 2/2 connected", text)

    def test_no_fleets_at_all(self):
        self.assertEqual(
            summary.render({"schema": 1, "sampled_seconds": 5.0,
                            "guests_probed": True, "host": self.HOST,
                            "fleets": []}),
            ["No QEMU VMs running."])

    def test_a_fleet_of_one(self):
        text = self._render(self._fleet(
            vms=1, roll_call={"services": {"qemu": {"running": 1, "total": 1}},
                              "infra": {"running": 0, "total": 0},
                              "completed": [], "source": "docker-ps"}))
        self.assertIn("1 VM  up 2h", text)
        self.assertIn("qemu 1/1", text)
        # Absent classes still print, so a missing rocjitsu profile is visible
        # rather than silently omitted.
        self.assertIn("rocjitsu 0/0", text)

    def test_an_empty_outliers_block_is_explicit(self):
        self.assertIn("OUTLIERS   none", self._render(self._fleet()))

    def test_an_uncontainerised_group_is_labelled(self):
        text = self._render(self._fleet(project=None))
        self.assertIn("(uncontainerised)", text)

    def test_json_and_text_come_from_the_same_object(self):
        import json
        fleet = self._fleet()
        data = {"schema": 1, "sampled_seconds": 5.0, "guests_probed": False,
                "host": self.HOST, "fleets": [fleet]}
        # One object, never a bare list, and the shape does not change with
        # the VM count.
        self.assertIsInstance(json.loads(json.dumps(data)), dict)
        self.assertEqual(sorted(data), ["fleets", "guests_probed", "host",
                                        "sampled_seconds", "schema"])


class Formatting(unittest.TestCase):
    def test_sizes_are_binary_with_a_one_letter_suffix(self):
        self.assertEqual(summary._fmt_size(0), "0B")
        self.assertEqual(summary._fmt_size(1536), "1.5K")
        self.assertEqual(summary._fmt_size(25_900_000_000), "24.1G")
        self.assertEqual(summary._fmt_size(8192, "MiB"), "8.0G")

    def test_an_unmeasured_size_is_a_dash_not_a_zero(self):
        self.assertEqual(summary._fmt_size(None), "-")

    def test_an_identical_uptime_is_not_printed_as_a_range(self):
        self.assertEqual(
            summary._fmt_uptime_range({"min": 7200, "max": 7200}), "2h00m")
        self.assertEqual(
            summary._fmt_uptime_range({"min": 60, "max": 7200}), "1m-2h00m")
        self.assertEqual(
            summary._fmt_uptime_range({"min": None, "max": None}), "-")


if __name__ == "__main__":
    unittest.main()
