"""The command line's shape: what `compose` claims, and what --help says.

`qemu-tool compose` shares one line with docker compose, and the pre-split in
cli._split_compose_argv is what decides whose each token is. It was measured
against a plain parse_known_args() and kept, because two cases differ and
both forward the wrong thing to docker:

- a short cluster that starts with h (`exec svc ls -hl`) is read by argparse
  as this tool's -h, and prints help instead of running the command;
- a "--" after the docker subcommand is left in place by argparse, where the
  splitter treats the first one as "all docker from here" and drops it.

The table below pins the splitter's answers so a later swap has to match them.
"""

# pylint: disable=protected-access

import argparse
import importlib.util
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
from qemu_tool import cli  # noqa: E402


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = cli._build_parser()
    return dict(next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ).choices)


class ComposeArgv(unittest.TestCase):
    # typed after "compose" -> (our flags, docker's arguments)
    CASES = [
        ("--stack rocm-golden --profile metrics up -d",
         ["--stack", "rocm-golden"], ["--profile", "metrics", "up", "-d"]),
        ("up -d --stack rocm-golden",
         ["--stack", "rocm-golden"], ["up", "-d"]),
        ("--xml a.xml --xml b.xml logs --tail 200",
         ["--xml", "a.xml", "--xml", "b.xml"], ["logs", "--tail", "200"]),
        ("--stack=rocm-golden ps", ["--stack=rocm-golden"], ["ps"]),
        ("-- --profile x up", [], ["--profile", "x", "up"]),
        ("--stack-dir /d up -- -d", ["--stack-dir", "/d"], ["up", "-d"]),
        ("run qemu-1 -- ls --stack rocm-golden",
         [], ["run", "qemu-1", "ls", "--stack", "rocm-golden"]),
        ("logs -h", ["-h"], ["logs"]),
        ("up --help", ["--help"], ["up"]),
        ("exec qemu-1 ls -hl", [], ["exec", "qemu-1", "ls", "-hl"]),
        ("exec qemu-1 ls -lh", [], ["exec", "qemu-1", "ls", "-lh"]),
        ("--sta rocm-golden ps", [], ["--sta", "rocm-golden", "ps"]),
        ("--image x ps", [], ["--image", "x", "ps"]),
        ("-p proj -f other.yml config -q",
         [], ["-p", "proj", "-f", "other.yml", "config", "-q"]),
        ("exec qemu-1 echo --xml=v", ["--xml=v"], ["exec", "qemu-1", "echo"]),
        ("ps", [], ["ps"]),
        ("", [], []),
    ]

    def test_each_token_goes_to_its_owner(self):
        for typed, ours, theirs in self.CASES:
            with self.subTest(typed):
                expected = ["compose", *ours, *(["--", *theirs] if theirs else [])]
                self.assertEqual(
                    cli._split_compose_argv(["compose", *typed.split()]),
                    expected,
                )

    def test_dockers_arguments_arrive_in_the_order_typed(self):
        for typed, _, theirs in self.CASES:
            if "-h" in typed.split() or "--help" in typed.split():
                continue
            with self.subTest(typed):
                args = cli._build_parser().parse_args(
                    cli._split_compose_argv(["compose", *typed.split()]))
                got = list(args.compose_args)
                # argparse keeps the separator on some versions; compose.run
                # drops one either way.
                if got[:1] == ["--"]:
                    got = got[1:]
                self.assertEqual(got, theirs)

    def test_other_subcommands_are_left_alone(self):
        argv = ["run-vm", "--vm-name", "x", "--", "--stack", "rocm-golden"]
        self.assertEqual(cli._split_compose_argv(argv), argv)

    def test_the_claimed_flags_are_the_compose_parsers_own(self):
        own = {
            o for a in _subparsers()["compose"]._actions
            for o in a.option_strings if o.startswith("--")
        } - {"--help"}
        self.assertEqual(own, set(cli._COMPOSE_OURS))


class Help(unittest.TestCase):
    def test_every_argument_has_help_text(self):
        missing = {
            name: [a.option_strings or a.dest for a in parser._actions if not a.help]
            for name, parser in _subparsers().items()
        }
        self.assertEqual({k: v for k, v in missing.items() if v}, {})

    def test_every_subcommand_has_a_summary_and_a_description(self):
        parser = cli._build_parser()
        action = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        summaries = {c.dest: c.help for c in action._choices_actions}
        for name, sub in action.choices.items():
            with self.subTest(name):
                self.assertTrue(summaries.get(name))
                self.assertTrue(sub.description)

    def test_the_command_list_is_not_printed_as_a_brace_list(self):
        text = cli._build_parser().format_help()
        self.assertIn("COMMAND ...", text)
        self.assertNotIn("{", text)

    def test_xml_has_one_dest_everywhere(self):
        dests = {
            name: a.dest
            for name, parser in _subparsers().items()
            for a in parser._actions if "--xml" in a.option_strings
        }
        self.assertEqual(set(dests.values()), {"xml"}, dests)

    def test_no_help_names_an_env_format_setting(self):
        # Fleet XML is the only settings format; a help string that says
        # --env-file or names a VM_* key describes one that is gone.
        # VM_IMAGES_DIR is the exception: docker compose interpolates it.
        for name, parser in _subparsers().items():
            text = parser.format_help()
            with self.subTest(name):
                self.assertNotIn("env-file", text)
                self.assertNotIn("env file", text)
                self.assertNotRegex(
                    text.replace("VM_IMAGES_DIR", ""), r"\bVM_[A-Z_]+\b")

    def test_examples_use_the_shipped_stack_names(self):
        for name in ("gen-compose", "compose", "publish-qcow", "quick-start",
                     "gen-vm"):
            text = _subparsers()[name].format_help()
            with self.subTest(name):
                self.assertIn("examples:", text)
                self.assertNotIn("vfio-user-", text)
                self.assertNotIn("qemu/xml/", text)
        self.assertIn("--bake --xml qemu/stacks/rocm-golden.xml",
                      _subparsers()["gen-vm"].format_help())


if __name__ == "__main__":
    unittest.main()
