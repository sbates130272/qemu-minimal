"""A guest is always an AMD guest unless very clearly told otherwise.

AGENTS.md, "The guest is an AMD machine", is the rule; this file holds the
code and the committed files to it.

Under KVM QEMU gives every CPU model the HOST's CPUID vendor
(target/i386/host-cpu.c, host_cpu_instance_init), so on an Intel host a bare
`-cpu EPYC` guest is GenuineIntel family 23, and the guest kernel
(drivers/pci/p2pdma.c, cpu_supports_p2pdma) then refuses P2PDMA between
devices that share no upstream bridge. run-vm therefore states the vendor, and
only two things may change it: <vm-cpu-vendor> in a fleet layer and
--cpu-vendor on the command line.

No Intel host runs these tests. What the vendor does there is asked of a
booted guest by scripts/check-guest-cpu.sh, in the quickstart-boot lane.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "qemu" / "stacks"
COMPOSE = REPO_ROOT / "qemu" / "compose"
GITHUB = REPO_ROOT / ".github"
SCRIPT = REPO_ROOT / "scripts" / "check-guest-cpu.sh"
PACKAGE = REPO_ROOT / "qemu" / "qemu-tool"


def _load_package() -> None:
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", PACKAGE / "__init__.py",
        submodule_search_locations=[str(PACKAGE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)


_load_package()
from qemu_tool import (  # noqa: E402
    cli, fleet_xml, gen_compose, gen_vm, identity, run_vm, settings,
)
from qemu_tool.caps import QemuCaps  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_files import inline_overlays  # noqa: E402

AMD = "AuthenticAMD"
INTEL = "GenuineIntel"


def _fleet(elements: str) -> str:
    return f'<fleet xmlns="http://qemu-tool/fleet/1.0">\n{elements}\n</fleet>\n'


def _layers(*bodies: str) -> FleetConfig:
    """rocm-golden.xml with each body laid over it, in order."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = [STACKS / "rocm-golden.xml"]
        for index, body in enumerate(bodies):
            path = Path(tmp) / f"layer-{index}.xml"
            path.write_text(_fleet(body))
            paths.append(path)
        return FleetXml.parse_layers(paths)


def _problems(body: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.xml"
        path.write_text(_fleet(body))
        return FleetXml.problems(path)


def _cpu(cmd: list[str]) -> list[str]:
    """The value of every -cpu on a command line, in order."""
    return [cmd[i + 1] for i, word in enumerate(cmd) if word == "-cpu"]


def _command(**fields) -> list[str]:
    cfg = VMConfig(vm_name="t", dry_run=True, **fields)
    return run_vm.build_command(cfg, run_vm.QemuCaps())


class RunVm(unittest.TestCase):
    """The -cpu run-vm writes for an amd64 guest, in each shape."""

    def test_the_default_states_the_amd_vendor(self):
        self.assertEqual(VMConfig().cpu_vendor, AMD)
        self.assertEqual(identity.AMD_CPU_VENDOR, AMD)
        self.assertEqual(_cpu(_command()), ["EPYC,vendor=AuthenticAMD"])

    def test_with_phys_bits(self):
        self.assertEqual(_cpu(_command(cpu_phys_bits=42)),
                         ["EPYC,vendor=AuthenticAMD,phys-bits=42"])

    def test_with_vfio_user_devices(self):
        # That path writes its own -machine, and the -cpu is still this one.
        for extra, want in (({}, "EPYC,vendor=AuthenticAMD"),
                            ({"cpu_phys_bits": 41},
                             "EPYC,vendor=AuthenticAMD,phys-bits=41")):
            cmd = _command(vfio_userdev=["/run/vfu/rocjitsu-1.sock"], **extra)
            self.assertEqual(_cpu(cmd), [want])
            self.assertEqual(cmd[1:3], ["-cpu", want])

    def test_without_kvm_too(self):
        # No host detection of any kind: TCG gets the same words.
        self.assertEqual(_cpu(_command(kvm=False)),
                         ["EPYC,vendor=AuthenticAMD"])

    def test_another_vendor_when_told(self):
        self.assertEqual(_cpu(_command(cpu_vendor=INTEL, cpu_phys_bits=42)),
                         ["EPYC,vendor=GenuineIntel,phys-bits=42"])

    def test_an_empty_vendor_states_none(self):
        self.assertEqual(_cpu(_command(cpu_vendor="")), ["EPYC"])
        self.assertEqual(_cpu(_command(cpu_vendor="", cpu_phys_bits=42)),
                         ["EPYC,phys-bits=42"])

    def test_a_vendor_qemu_would_refuse_is_refused_here(self):
        for bad in ("AMD", "AuthenticAMD ", "Authentic,AMD", "AuthenticAM\n"):
            with self.subTest(vendor=bad), self.assertRaises(SystemExit) as ctx:
                _command(cpu_vendor=bad)
            self.assertIn("12 printable ASCII", str(ctx.exception))
            self.assertIn(AMD, str(ctx.exception))
            self.assertIn(INTEL, str(ctx.exception))

    def test_other_architectures_are_untouched(self):
        for arch in ("arm64", "riscv64"):
            cmd = _command(arch=arch, kvm=False)
            self.assertNotIn("vendor=", " ".join(cmd))
            self.assertNotIn("EPYC", " ".join(cmd))
            self.assertEqual(
                _command(arch=arch, kvm=False, cpu_vendor=INTEL), cmd)

    def test_a_cpu_in_qemu_args_is_after_run_vms_own(self):
        # QEMU takes the LAST -cpu and nothing of an earlier one (run on
        # 10.2.1 and on the pinned fork), so this is how other CPU
        # properties are set, and why it has to restate the vendor.
        words = ["-cpu", "EPYC-Milan,vendor=AuthenticAMD"]
        cmd = _command(qemu_args=words, cpu_phys_bits=42)
        self.assertEqual(_cpu(cmd), ["EPYC,vendor=AuthenticAMD,phys-bits=42",
                                     "EPYC-Milan,vendor=AuthenticAMD"])

    def test_the_cli_takes_the_flag_empty_included(self):
        parser = cli._build_parser()
        for words, want in ((["--cpu-vendor", INTEL], INTEL),
                            (["--cpu-vendor", ""], ""),
                            (["--cpu-vendor="], "")):
            ns = parser.parse_args(["run-vm", "--vm-name", "t", *words])
            self.assertEqual(ns.cpu_vendor, want)
            self.assertEqual(
                cli._extract_cli_overrides(ns, "run-vm")["cpu_vendor"], want)
        ns = parser.parse_args(["run-vm", "--vm-name", "t"])
        self.assertNotIn("cpu_vendor", cli._extract_cli_overrides(ns, "run-vm"))

    def _run(self, **fields) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        cfg = VMConfig(vm_name="t", dry_run=True, **fields)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            run_vm.run(cfg, run_vm.QemuCaps())
        return out.getvalue(), err.getvalue()

    def test_an_amd_guest_is_started_without_a_word(self):
        out, err = self._run()
        self.assertIn("-cpu EPYC,vendor=AuthenticAMD ", out)
        self.assertEqual(err, "")

    def test_a_guest_that_is_not_amd_gets_one_line_on_stderr(self):
        for vendor, shown in ((INTEL, "'GenuineIntel'"),
                              ("", "the host's CPU vendor")):
            out, err = self._run(cpu_vendor=vendor)
            self.assertEqual(len(err.splitlines()), 1, err)
            self.assertIn("not presented as AMD", err)
            self.assertIn(shown, err)
            self.assertIn("P2PDMA", err)
            self.assertIn("share no upstream bridge", err)
            # Two of the four stacks put devices below PCIe switches, so
            # "on different root ports" is not what the kernel refuses.
            self.assertNotIn("root port", err)
            self.assertNotIn("not presented", out)

    def test_no_vendor_and_no_kvm_is_an_amd_guest_and_gets_no_note(self):
        # Only KVM puts the host's vendor in place of the model's own
        # (QEMU, target/i386/kvm/kvm-cpu.c calls host_cpu_instance_init):
        # under TCG a bare -cpu EPYC is AuthenticAMD.
        out, err = self._run(cpu_vendor="", kvm=False)
        self.assertIn("-cpu EPYC ", out)
        self.assertEqual(err, "")
        # A stated vendor that is not AMD is announced with or without KVM.
        _, err = self._run(cpu_vendor=INTEL, kvm=False)
        self.assertIn("not presented as AMD", err)
        # And a render, which cannot know, always says so.
        self.assertIsNotNone(identity.cpu_vendor_note("", "<vm-cpu-vendor>"))

    def test_the_libvirt_converter_says_the_domain_has_the_hosts_cpu(self):
        # <cpu mode='host-passthrough'/>: no vendor setting reaches it, so
        # every amd64 conversion is announced, on every host.
        for words in ([], ["--cpu-vendor", INTEL]):
            out, err = io.StringIO(), io.StringIO()
            # The conversion asks the QEMU binary what it can do, and a
            # host without one (the Gen Compose Check runner) stops there.
            # What is asked here is the note, which no capability decides.
            with mock.patch.object(cli, "probe_caps",
                                   return_value=QemuCaps()), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                cli.main(["run-vm", "--vm-name", "t", *words,
                          "--convert-to-libvirt", "-"])
            self.assertIn("host-passthrough", out.getvalue())
            notes = [line for line in err.getvalue().splitlines()
                     if "convert-to-libvirt" in line]
            self.assertEqual(notes, [identity.LIBVIRT_CPU_NOTE], err.getvalue())
            self.assertIn("not presented as AMD", notes[0])
            self.assertIn("do not reach it", notes[0])
            self.assertNotIn("note:", out.getvalue())


class Bake(unittest.TestCase):
    """gen-vm's bake boots pass no -cpu at all, on purpose.

    AGENTS.md, "The CPU the tools are built for": a bake on QEMU's default
    model is the portable direction, and -cpu EPYC there made perftest
    build AVX2.
    """

    def test_the_bake_boots_pass_no_cpu(self):
        for kvm in ("", ",accel=kvm"):
            args = gen_vm._arch_args_for_gen(VMConfig(arch="amd64"), kvm)
            self.assertEqual(args, ["-machine", f"q35{kvm}"])

    def test_gen_vm_names_no_cpu_model_for_amd64(self):
        source = (PACKAGE / "gen_vm.py").read_text()
        self.assertNotIn("EPYC", source)
        self.assertNotIn("vendor=", source)

    def test_a_bake_does_not_take_the_fleets_vendor(self):
        fleet = _layers("<vm-cpu-vendor>GenuineIntel</vm-cpu-vendor>")
        self.assertEqual(settings.vm_overrides(fleet)["cpu_vendor"], INTEL)
        self.assertNotIn("cpu_vendor", settings.bake_overrides(fleet))


class FleetElement(unittest.TestCase):
    """<vm-cpu-vendor>: absent is AMD, empty is the host's, and no other."""

    def test_absent_is_amd(self):
        self.assertEqual(FleetConfig().vm_cpu_vendor, AMD)
        self.assertEqual(_layers().vm_cpu_vendor, AMD)
        self.assertEqual(settings.vm_overrides(_layers())["cpu_vendor"], AMD)

    def test_an_empty_element_is_a_value_not_the_default(self):
        for body in ("<vm-cpu-vendor/>", "<vm-cpu-vendor></vm-cpu-vendor>"):
            fleet = _layers(body)
            self.assertEqual(fleet.vm_cpu_vendor, "")
            self.assertEqual(settings.vm_overrides(fleet)["cpu_vendor"], "")

    def test_a_later_layer_wins_either_way(self):
        self.assertEqual(_layers(
            "<vm-cpu-vendor/>",
            "<vm-cpu-vendor>AuthenticAMD</vm-cpu-vendor>").vm_cpu_vendor, AMD)
        self.assertEqual(_layers(
            "<vm-cpu-vendor>GenuineIntel</vm-cpu-vendor>",
            "<vm-cpu-vendor/>").vm_cpu_vendor, "")
        # A layer that does not name it leaves the earlier one's.
        self.assertEqual(_layers(
            "<vm-cpu-vendor>GenuineIntel</vm-cpu-vendor>",
            "<vm-count>1</vm-count>").vm_cpu_vendor, INTEL)

    def test_twelve_characters_kept_as_written(self):
        for vendor in (AMD, INTEL, "HygonGenuine", "  Shanghai  "):
            self.assertEqual(_layers(
                f"<vm-cpu-vendor>{vendor}</vm-cpu-vendor>").vm_cpu_vendor,
                vendor)

    def test_anything_else_is_a_parse_problem_naming_both_vendors(self):
        for bad in ("AMD", "amd", "1", " AuthenticAMD ", "\nAuthenticAMD\n",
                    "AuthenticAMD1", "Authentic,AMD", "Authentic\tAMD",
                    "Authenticé"):
            with self.subTest(vendor=bad):
                problems = _problems(
                    f"<vm-cpu-vendor>{bad}</vm-cpu-vendor>")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("vm-cpu-vendor", problems[0])
                self.assertIn(AMD, problems[0])
                self.assertIn(INTEL, problems[0])

    def test_the_form_is_qemus(self):
        # x86_cpuid_set_vendor() takes exactly CPUID_VENDOR_SZ, 12,
        # characters; a comma would end the value on -cpu.
        self.assertIsNone(identity.cpu_vendor_problem(AMD, "x"))
        self.assertIsNone(identity.cpu_vendor_problem("", "x"))
        self.assertIsNone(identity.cpu_vendor_problem(" " * 12, "x"))
        for bad in ("A" * 11, "A" * 13, "A" * 11 + ",", "A" * 11 + "\x7f"):
            self.assertIsNotNone(identity.cpu_vendor_problem(bad, "x"), bad)

    def test_emit_and_parse_round_trip_the_empty_value(self):
        setting = fleet_xml._BY_TAG["vm-cpu-vendor"]
        # Not sparse: emit() leaves a sparse element out when it is empty,
        # and an empty vendor would then come back as AuthenticAMD.
        self.assertFalse(setting.sparse)
        for vendor in (AMD, INTEL, ""):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "f.xml"
                path.write_text(FleetXml.emit(FleetConfig(vm_cpu_vendor=vendor)))
                self.assertEqual(FleetXml.parse(path).vm_cpu_vendor, vendor)


class Render(unittest.TestCase):
    """gen-compose says --cpu-vendor only when a layer says otherwise."""

    def _render(self, *bodies: str) -> tuple[str, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = gen_compose.render_compose(
                gen_compose.plan(_layers(*bodies)), ["e"], "d")
        return out, err.getvalue()

    def _run_vm_words(self, out: str, guest: int = 1) -> list[str]:
        import yaml
        script = yaml.safe_load(out)["services"][f"qemu-{guest}"]["entrypoint"][-1]
        return shlex.split(script[script.index("run-vm"):].replace("$$", "$"))

    def test_the_default_renders_nothing(self):
        for bodies in ((), ("<vm-cpu-vendor>AuthenticAMD</vm-cpu-vendor>",)):
            out, err = self._render(*bodies)
            self.assertNotIn("cpu-vendor", out)
            self.assertNotIn("not presented as AMD", err)

    def test_no_committed_stack_carries_the_option(self):
        files = sorted(COMPOSE.glob("*/docker-compose.yml"))
        self.assertEqual(len(files), 4)
        for path in files:
            text = path.read_text()
            self.assertNotIn("cpu-vendor", text, path)
            # run-vm writes the -cpu; a stack never does.
            self.assertNotIn("-cpu ", text, path)

    def test_another_vendor_renders_on_every_guest_with_a_note(self):
        out, err = self._render("<vm-cpu-vendor>GenuineIntel</vm-cpu-vendor>")
        self.assertEqual(out.count("          --cpu-vendor='GenuineIntel' \\\n"), 2)
        for guest in (1, 2):
            self.assertIn("--cpu-vendor=GenuineIntel",
                          self._run_vm_words(out, guest))
        notes = [line for line in err.splitlines()
                 if "not presented as AMD" in line]
        self.assertEqual(len(notes), 1, err)
        self.assertIn("<vm-cpu-vendor>", notes[0])
        self.assertIn("P2PDMA", notes[0])

    def test_an_empty_element_renders_an_empty_value(self):
        out, err = self._render("<vm-cpu-vendor/>")
        self.assertEqual(out.count("          --cpu-vendor='' \\\n"), 2)
        words = self._run_vm_words(out)
        self.assertIn("--cpu-vendor=", words)
        # And run-vm reads that word as the empty vendor.
        ns = cli._build_parser().parse_args(
            ["run-vm", "--vm-name", "t", "--cpu-vendor="])
        self.assertEqual(ns.cpu_vendor, "")
        self.assertIn("the host's CPU vendor", err)

    def test_a_vendor_the_shell_would_read_survives_it(self):
        vendor = "a b$c'd\"e;f|"
        self.assertEqual(len(vendor), 12)
        out, _ = self._render(
            "<vm-cpu-vendor>a b$c'd\"e;f|</vm-cpu-vendor>")
        self.assertIn(f"--cpu-vendor={vendor}", self._run_vm_words(out))


class NothingImplicit(unittest.TestCase):
    """Only an explicit setting changes the vendor.

    No host detection, no fallback, and nothing committed that sets it.
    """

    def test_no_shipped_fleet_file_sets_another_vendor(self):
        files = sorted(STACKS.glob("*.xml"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            self.assertEqual(FleetXml.parse(path).vm_cpu_vendor, AMD, path)
            # Not even restated: the default is the rule.
            self.assertNotIn("vm-cpu-vendor", path.read_text(), path)

    def _github_files(self) -> list[Path]:
        return sorted(p for p in GITHUB.rglob("*")
                      if p.suffix in (".yml", ".yaml"))

    def test_no_workflow_overlay_sets_the_vendor(self):
        seen = 0
        for path in self._github_files():
            for document in inline_overlays(path.read_text()):
                seen += 1
                self.assertNotIn("vm-cpu-vendor", document, path)
        self.assertGreater(seen, 3)

    def test_nothing_committed_passes_the_flag(self):
        places = self._github_files() + sorted(
            (REPO_ROOT / "scripts").rglob("*")) + sorted(
            (REPO_ROOT / "ansible").rglob("*.yml"))
        for path in places:
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(errors="replace")
            self.assertNotIn("--cpu-vendor", text, path)
            self.assertNotIn("vm-cpu-vendor", text, path)

    def test_a_test_overlay_with_the_vendor_would_be_caught(self):
        text = ("cat > o.xml <<'XML'\n"
                '<fleet xmlns="http://qemu-tool/fleet/1.0">\n'
                "  <vm-cpu-vendor>GenuineIntel</vm-cpu-vendor>\n"
                "</fleet>\nXML\n")
        self.assertIn("vm-cpu-vendor", inline_overlays(text)[0])

    def test_the_code_does_not_look_at_the_hosts_cpu(self):
        # The vendor is never derived: not from /proc/cpuinfo, not from
        # CPUID, not from platform. check_system.py may report on a host;
        # these four decide what a guest is.
        for name in ("run_vm.py", "gen_compose.py", "identity.py",
                     "settings.py", "fleet_xml.py", "config.py"):
            source = (PACKAGE / name).read_text()
            for word in ("cpuinfo", "platform.processor", "lscpu",
                         "GenuineIntel\" if", "is_intel", "host_vendor"):
                self.assertNotIn(word, source, f"{name}: {word}")

    def test_agents_md_states_the_rule(self):
        text = (REPO_ROOT / "AGENTS.md").read_text()
        self.assertIn("## The guest is an AMD machine", text)
        self.assertIn(
            "A guest is always an AMD guest unless very clearly told "
            "otherwise", " ".join(text.split()))


@unittest.skipUnless(shutil.which("sh") and shutil.which("awk"),
                     "needs sh and awk")
class CheckScript(unittest.TestCase):
    """scripts/check-guest-cpu.sh, against a stand-in for the guest."""

    AMD_CPUINFO = ("processor\t: 0\nvendor_id\t: AuthenticAMD\n"
                   "cpu family\t: 23\nmodel\t\t: 1\n"
                   "model name\t: AMD EPYC Processor\n")
    INTEL_HOST = ("processor\t: 0\nvendor_id\t: GenuineIntel\n"
                  "cpu family\t: 6\nmodel name\t: INTEL(R) XEON(R) "
                  "PLATINUM 8573C\n")
    DMESG = "[    0.000000] Linux version 7.2.6\n[    1.0] pci 0000:00:00.0\n"
    REFUSAL = ("[   50.1] nvme 0000:00:07.0: cannot be used for peer-to-peer "
               "DMA as the client and provider (0000:02:00.0) do not share "
               "an upstream bridge or whitelisted host bridge\n")

    def _check(self, cpuinfo: str, dmesg: str | None,
               host: str | None = None) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cpuinfo").write_text(cpuinfo)
            (root / "host").write_text(host or self.INTEL_HOST)
            if dmesg is not None:
                (root / "dmesg").write_text(dmesg)
            guest = root / "guest"
            # The guest: prints a file for each of the two lines it is sent.
            guest.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                f'  "cat /proc/cpuinfo") cat "{root}/cpuinfo" ;;\n'
                f'  "sudo -n dmesg") cat "{root}/dmesg" ;;\n'
                '  *) echo "unexpected: $1" >&2; exit 64 ;;\n'
                "esac\n")
            guest.chmod(0o755)
            summary = root / "summary.md"
            env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))
            done = subprocess.run(
                ["sh", str(SCRIPT), "--host-cpuinfo", str(root / "host"),
                 "g-1", str(guest)],
                capture_output=True, text=True, env=env, check=False)
            text = summary.read_text() if summary.exists() else ""
        return done.returncode, done.stdout + done.stderr, text

    def test_an_amd_guest_on_an_intel_host_passes_and_says_which_host(self):
        rc, out, summary = self._check(self.AMD_CPUINFO, self.DMESG)
        self.assertEqual(rc, 0, out)
        self.assertIn("::notice::host CPU for g-1: GenuineIntel, INTEL(R) "
                      "XEON(R) PLATINUM 8573C", out)
        self.assertIn("vendor_id=AuthenticAMD cpu family=23", out)
        self.assertNotIn("::error::", out)
        self.assertNotIn("::warning::", out)
        for row in ("| host vendor | GenuineIntel |",
                    "| guest vendor | AuthenticAMD |",
                    "| guest cpu family | 23 |",
                    "| P2PDMA refusals in dmesg | 0 |",
                    "| amdgpu 'AIS: P2P DMA not accessible' lines in dmesg "
                    "| 0 |",
                    "| unhandled-MSR lines in dmesg | 0 |"):
            self.assertIn(row, summary)
        # The host is not AMD, so this run is the case the check is for.
        shows = ("the stated vendor reached the guest: the host is "
                 "GenuineIntel and the guest reads AuthenticAMD family 23")
        self.assertIn(f"::notice::g-1: this run shows {shows}", out)
        self.assertIn(f"| what this run shows | {shows}", summary)

    AMD_HOST = ("processor\t: 0\nvendor_id\t: AuthenticAMD\n"
                "cpu family\t: 25\nmodel name\t: AMD EPYC 7763 64-Core "
                "Processor\n")

    def test_a_pass_on_an_amd_host_says_it_shows_nothing_of_the_change(self):
        # On an AMD host the guest reads AuthenticAMD with or without the
        # stated vendor. The run passes, and says in three places that it
        # is not a pass of the Intel-host case.
        rc, out, summary = self._check(self.AMD_CPUINFO, self.DMESG,
                                       host=self.AMD_HOST)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("::error::", out)
        self.assertNotIn("::warning::", out)
        shows = ("nothing about the stated vendor: the host is AuthenticAMD, "
                 "where a guest reads AuthenticAMD with or without it. NOT a "
                 "pass of the Intel-host case")
        self.assertIn(f"::notice::g-1: this run shows {shows}", out)
        self.assertIn(f"| what this run shows | {shows} |", summary)
        self.assertEqual(
            out.rstrip().splitlines()[-1],
            "g-1: the guest is an AMD machine on a AuthenticAMD host; this "
            f"run shows {shows}")
        self.assertNotIn("the stated vendor reached the guest", out + summary)

    def test_a_host_whose_vendor_cannot_be_read_is_not_counted_as_intel(self):
        rc, out, summary = self._check(self.AMD_CPUINFO, self.DMESG,
                                       host="processor\t: 0\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("the host's vendor could not be read", out)
        self.assertIn("the host's vendor could not be read", summary)
        self.assertNotIn("the stated vendor reached the guest", out + summary)

    def test_a_failed_guest_is_not_said_to_show_the_vendor_arrived(self):
        cpuinfo = self.AMD_CPUINFO.replace("AuthenticAMD", "GenuineIntel")
        for host in (self.INTEL_HOST, self.AMD_HOST):
            rc, out, summary = self._check(cpuinfo, self.DMESG, host=host)
            self.assertEqual(rc, 1, out)
            self.assertIn("the guest is NOT an AMD machine of family 23", out)
            self.assertIn("| what this run shows | the guest is NOT an AMD "
                          "machine of family 23", summary)
            self.assertNotIn("reached the guest", out + summary)
            self.assertNotIn("nothing about the stated vendor", out + summary)

    def test_a_guest_with_the_hosts_vendor_fails(self):
        cpuinfo = self.AMD_CPUINFO.replace("AuthenticAMD", "GenuineIntel")
        rc, out, summary = self._check(cpuinfo, self.DMESG)
        self.assertEqual(rc, 1, out)
        self.assertIn("::error::g-1: the guest's vendor_id is 'GenuineIntel'",
                      out)
        self.assertIn("| guest vendor | GenuineIntel |", summary)

    def test_another_family_fails(self):
        rc, out, _ = self._check(
            self.AMD_CPUINFO.replace(": 23", ": 6"), self.DMESG)
        self.assertEqual(rc, 1, out)
        self.assertIn("cpu family is '6', not 23", out)

    def test_the_kernels_refusal_fails_and_is_printed(self):
        rc, out, summary = self._check(
            self.AMD_CPUINFO, self.DMESG + self.REFUSAL)
        self.assertEqual(rc, 1, out)
        self.assertIn("whitelisted host bridge", out)
        self.assertIn("::error::g-1: the guest kernel refused P2PDMA 1 time",
                      out)
        self.assertIn("| P2PDMA refusals in dmesg | 1 |", summary)

    def test_other_p2p_lines_are_printed_and_do_not_fail(self):
        rc, out, _ = self._check(
            self.AMD_CPUINFO,
            self.DMESG + "[   2.0] nvme 0000:00:07.0: added peer-to-peer "
                         "DMA memory 0x4000000000-0x7fffffffff\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("added peer-to-peer DMA memory", out)

    def test_the_not_a_pci_device_message_is_not_the_cpus_refusal(self):
        # drivers/pci/p2pdma.c has two messages that begin "cannot be used
        # for peer-to-peer DMA"; this one is about the client, not the CPU.
        line = ("[   9.0] nullb0: cannot be used for peer-to-peer DMA as it "
                "is not a PCI device\n")
        rc, out, summary = self._check(self.AMD_CPUINFO, self.DMESG + line)
        self.assertEqual(rc, 0, out)
        self.assertIn("as it is not a PCI device", out)
        self.assertNotIn("::error::", out)
        self.assertIn("| P2PDMA refusals in dmesg | 0 |", summary)

    def test_amdgpus_ais_line_warns_and_does_not_fail(self):
        # What a degraded hipFile path logs: amdgpu asks the kernel quietly,
        # so the kernel's own refusal is not printed for it.
        line = ("[  60.0] amdgpu 0000:02:00.0: AIS: P2P DMA not accessible\n")
        rc, out, summary = self._check(self.AMD_CPUINFO,
                                       self.DMESG + line * 3)
        self.assertEqual(rc, 0, out)
        self.assertIn("::warning::g-1: amdgpu logged 'AIS: P2P DMA not "
                      "accessible' 3 time(s)", out)
        self.assertNotIn("::error::", out)
        self.assertIn("| amdgpu 'AIS: P2P DMA not accessible' lines in "
                      "dmesg | 3 |", summary)
        self.assertIn("| P2PDMA refusals in dmesg | 0 |", summary)

    def test_unhandled_msrs_warn_and_do_not_fail(self):
        rc, out, summary = self._check(
            self.AMD_CPUINFO,
            self.DMESG + "[   0.3] unchecked MSR access error: RDMSR from "
                         "0xc0011029 at rIP: 0xffffffff8108c2a7\n"
                         "[   0.4] kvm: unhandled wrmsr: 0xc0010015\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("::warning::g-1: 2 unhandled-MSR line(s)", out)
        self.assertIn("0xc0011029", out)
        self.assertIn("| unhandled-MSR lines in dmesg | 2 |", summary)

    def test_unhandled_alone_is_not_an_msr_line(self):
        # The filter was 'unhandled|...': an ACPI line was counted, warned
        # about and written to the summary as an unhandled MSR.
        rc, out, summary = self._check(
            self.AMD_CPUINFO,
            self.DMESG + "[   0.2] ACPI: unhandled thing\n"
                         "[   0.3] usb 1-1: Unhandled interrupt\n")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("unhandled-MSR line(s)", out)
        self.assertNotIn("ACPI: unhandled thing", out)
        self.assertIn("| unhandled-MSR lines in dmesg | 0 |", summary)

    def test_a_kernel_oops_warns_and_does_not_fail(self):
        # What a KVM hypercall from an AMD-vendor guest is read to leave on
        # an Intel host: a page fault at the VMMCALL.
        rc, out, summary = self._check(
            self.AMD_CPUINFO,
            self.DMESG + "[   9.1] BUG: unable to handle page fault for "
                         "address: ffffffffc0a01234\n"
                         "[   9.1] Oops: Oops: 0003 [#1] SMP NOPTI\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("::warning::g-1: 2 kernel oops line(s)", out)
        self.assertIn("ffffffffc0a01234", out)
        self.assertIn("| kernel oops lines in dmesg | 2 |", summary)
        rc, out, summary = self._check(self.AMD_CPUINFO, self.DMESG)
        self.assertNotIn("kernel oops line(s)", out)
        self.assertIn("| kernel oops lines in dmesg | 0 |", summary)

    def test_a_dmesg_that_cannot_be_read_fails(self):
        rc, out, _ = self._check(self.AMD_CPUINFO, None)
        self.assertEqual(rc, 1, out)
        self.assertIn("could not read the guest's dmesg", out)
        rc, out, _ = self._check(self.AMD_CPUINFO, "")
        self.assertEqual(rc, 1, out)
        self.assertIn("dmesg is empty", out)

    def test_both_guest_booting_lanes_run_it(self):
        for name, count in (("qemu-minimal-quickstart-boot.yml", 1),
                            ("qemu-minimal-report-for-vm-rocm-golden.yml", 1)):
            text = (GITHUB / "workflows" / name).read_text()
            self.assertEqual(
                len(re.findall(r"^\s+scripts/check-guest-cpu\.sh ", text,
                               re.MULTILINE)), count, name)
            # And a change to the script runs the lane.
            self.assertIn("- 'scripts/check-guest-cpu.sh'", text, name)
        boot = (GITHUB / "workflows" / "qemu-minimal-quickstart-boot.yml"
                ).read_text()
        step = boot[boot.index("The guest is an AMD machine whatever"):]
        step = step[:step.index("    - name:")]
        # The lane's verdict: no condition, and not allowed to fail quietly.
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("if:", step)
        self.assertNotIn("|| true", step)

    def test_the_shell_check_lane_covers_the_script(self):
        text = (GITHUB / "workflows" / "qemu-minimal-shell-check.yml"
                ).read_text()
        self.assertIn("scripts/*.sh", text)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
