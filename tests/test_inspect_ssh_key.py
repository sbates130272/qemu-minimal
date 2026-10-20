"""Tests for the SSH key `qemu-tool inspect` offers.

The bug these exist for: inspect printed the configured ssh_key_file as if it
were a fact about the guest, so a fleet VM whose authorized_keys holds only the
injected fleet key was advertised as reachable with ~/.ssh/id_rsa. Following
that line got three password prompts and a permission denial. A guess in the
shape of a fact is worse than no answer, so each of the three outcomes --
matched, unmatched, unaskable -- has to look different.

Synthetic agent payloads and synthetic key files under a tmp dir throughout;
nothing here reads the real ~/.ssh or talks to a running VM.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
import threading
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
from qemu_tool.config import VMConfig  # noqa: E402


def make_key(seed: bytes, comment: str = "") -> tuple[str, str]:
    """(public key line, fingerprint) for a synthetic ed25519-shaped key.

    Only the wire format matters here -- a length-prefixed algorithm name
    followed by the key body -- because that is all inspect parses and
    fingerprints. No real key material is generated or read.
    """
    algo = b"ssh-ed25519"
    blob = len(algo).to_bytes(4, "big") + algo + (4).to_bytes(4, "big") + seed
    b64 = base64.b64encode(blob).decode()
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    line = f"ssh-ed25519 {b64}" + (f" {comment}" if comment else "")
    return line, fp


FLEET_LINE, FLEET_FP = make_key(b"flet", "qemu-minimal-mi100-fleet")
OTHER_LINE, OTHER_FP = make_key(b"othr", "someone@elsewhere")
DESK_LINE, DESK_FP = make_key(b"desk", "user@desktop")


def ga(authorized_keys: str | None, path: str = "/home/ubuntu/.ssh/authorized_keys",
       **kw):
    """A guest-agent payload, with or without a readable authorized_keys."""
    payload: dict = {"hostname": {"host-name": "vm1"}, "files": {}}
    if authorized_keys is not None:
        payload["authorized_keys"] = path
        payload["files"][path] = authorized_keys
    payload.update(kw)
    return payload


def vm(**kw):
    base = {
        "name": "qt-vm1", "pid": 4242, "arch": "amd64", "vcpus": 4,
        "memory_mib": 8192, "ssh_port_host": 2253, "image": None,
        "container": None, "uptime_seconds": 60, "vfio_user_sockets": [],
        "qmp_socket": None, "qmp_available": False,
        "ga_socket": "/tmp/qga.sock", "ga_available": True, "nvme": [],
    }
    base.update(kw)
    return base


class KeyFixture(unittest.TestCase):
    """A tmp dir standing in for the images dir, its keys/ and a fake ~/.ssh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qt-ssh-"))
        self.addCleanup(self._rm, self.tmp)
        self.images = self.tmp / "images"
        (self.images / "keys").mkdir(parents=True)
        self.home = self.tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)
        # Path.home() must never reach the real one from a test.
        p = mock.patch.object(inspect_mod.Path, "home", staticmethod(lambda: self.home))
        p.start()
        self.addCleanup(p.stop)

    def _rm(self, root: Path):
        for p in sorted(root.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
        root.rmdir()

    def write_key(self, path: Path, line: str, private: bool = True) -> Path:
        path.write_text(line + "\n")
        if private:
            # Only its existence is checked; inspect never reads a private key.
            path.with_name(path.name[:-4]).write_text("not a real private key\n")
        return path

    def cfg(self) -> VMConfig:
        return VMConfig(ssh_key_file=self.home / ".ssh" / "id_rsa.pub")

    def identity(self, ga_info, image=None, info=None):
        return inspect_mod._ssh_identity(
            image, info or {}, self.cfg(), ga_info)


class GuestKeyMatches(KeyFixture):
    def test_the_ssh_command_names_the_matching_key(self):
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga(FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["source"], "guest")
        self.assertEqual(ident["key_file"], str(key.with_name("fleet_ed25519")))
        self.assertIsNone(ident["note"])
        self.assertEqual(ident["keys"][0]["fingerprint"], FLEET_FP)
        self.assertEqual(ident["keys"][0]["path"], str(key))

    def test_the_configured_key_is_not_offered_when_the_guest_rejects_it(self):
        """The whole bug: ~/.ssh/id_rsa must not win over the guest's answer."""
        self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga(FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertNotIn("id_rsa", ident["key_file"])

    def test_options_and_comments_still_match_the_bare_pub(self):
        key = self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        line = ('command="/usr/bin/true",no-pty,from="10.0.0.0/8" '
                + DESK_LINE + " extra comment here")
        ident = self.identity(ga(line + "\n"))
        self.assertEqual(ident["key_file"], str(key.with_name("id_rsa")))
        self.assertEqual(ident["keys"][0]["fingerprint"], DESK_FP)

    def test_one_of_several_authorized_keys_matches(self):
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        ident = self.identity(
            ga("\n".join([OTHER_LINE, FLEET_LINE, DESK_LINE]) + "\n"),
            image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["key_file"], str(key.with_name("fleet_ed25519")))
        fps = [k["fingerprint"] for k in ident["keys"]]
        self.assertEqual(fps, [OTHER_FP, FLEET_FP, DESK_FP])
        self.assertEqual([k["path"] for k in ident["keys"]],
                         [None, str(key), None])

    def test_a_public_half_with_no_private_half_offers_no_identity(self):
        self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE,
                       private=False)
        ident = self.identity(ga(FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertIsNone(ident["key_file"])
        self.assertIn("private half", ident["note"])


class GuestKeysWithNoLocalMatch(KeyFixture):
    def test_fingerprints_are_shown_and_no_command_is_invented(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga(FLEET_LINE + "\n"))
        self.assertEqual(ident["source"], "guest")
        self.assertIsNone(ident["key_file"])
        self.assertIn("no key found locally", ident["note"])
        self.assertEqual(ident["keys"],
                         [{"fingerprint": FLEET_FP,
                           "comment": "qemu-minimal-mi100-fleet",
                           "path": None, "accepted": True,
                           "cert_authority": False, "restrictions": []}])

    def test_an_empty_authorized_keys_is_not_a_match(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga("\n# only a comment\n"))
        self.assertEqual(ident["keys"], [])
        self.assertIsNone(ident["key_file"])
        self.assertIn("no keys at all", ident["note"])


class CertAuthorityIsNotALoginKey(KeyFixture):
    """F1. A CA key signs certificates; it does not authenticate anything.

    The options field was skipped rather than parsed -- `cert-authority` is
    not valid base64, so the scan walked past it and matched the key field
    behind it. inspect then printed `ssh -i <the CA's private key>` in its
    maximum-confidence "accepted by guest" form, for a key sshd will refuse.
    """

    def test_no_identity_is_offered_for_a_cert_authority_line(self):
        self.write_key(self.images / "keys" / "ca_ed25519.pub", FLEET_LINE)
        ident = self.identity(ga("cert-authority " + FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertIsNone(ident["key_file"])
        self.assertIn("cert-authority", ident["note"])

    def test_a_cert_authority_key_is_labelled_rather_than_claimed(self):
        self.write_key(self.images / "keys" / "ca_ed25519.pub", FLEET_LINE)
        ident = self.identity(ga("cert-authority " + FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertTrue(ident["keys"][0]["cert_authority"])
        self.assertNotIn("accepted by guest",
                         inspect_mod._key_line(ident["keys"][0]))

    def test_a_real_login_key_beside_a_cert_authority_line_still_wins(self):
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        self.write_key(self.images / "keys" / "ca_ed25519.pub", OTHER_LINE)
        ident = self.identity(
            ga("cert-authority " + OTHER_LINE + "\n" + FLEET_LINE + "\n"),
            image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["key_file"], str(key.with_name("fleet_ed25519")))

    def test_the_options_field_is_case_insensitive(self):
        self.write_key(self.images / "keys" / "ca_ed25519.pub", FLEET_LINE)
        ident = self.identity(ga("Cert-Authority " + FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertIsNone(ident["key_file"])


class RestrictedKeysAreQualified(KeyFixture):
    """F3. from=, command=, expiry-time= and restrict all change the answer.

    Such a line is a login key, unlike a cert-authority one, but a pasteable
    `ssh -i` for it is still a claim inspect cannot make: the key may
    authenticate from nowhere this host is, may have expired, or may yield a
    forced command instead of a shell.
    """

    def test_a_forced_command_is_called_out(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(
            ga('command="/usr/bin/true",no-pty ' + DESK_LINE + "\n"))
        self.assertIn("restricted", ident["note"])
        self.assertIn("command", ident["note"])

    def test_a_from_restriction_is_called_out(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga('from="10.0.0.0/8" ' + DESK_LINE + "\n"))
        self.assertIn("from", ident["note"])

    def test_expiry_and_restrict_are_called_out(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(
            ga('restrict,expiry-time="20200101" ' + DESK_LINE + "\n"))
        self.assertIn("restrict", ident["note"])
        self.assertIn("expiry-time", ident["note"])

    def test_a_comma_inside_a_quoted_command_is_not_an_option_separator(self):
        opts = inspect_mod._split_options('command="a,b",no-pty')
        self.assertEqual(opts, ['command="a,b"', "no-pty"])

    def test_an_unrestricted_key_is_preferred_over_a_restricted_one(self):
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(
            ga('command="/bin/false" ' + DESK_LINE + "\n" + FLEET_LINE + "\n"),
            image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["key_file"], str(key.with_name("fleet_ed25519")))
        self.assertIsNone(ident["note"])

    def test_an_unrestricted_key_carries_no_restriction_note(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga("no-agent-forwarding " + DESK_LINE + "\n"))
        self.assertIsNone(ident["note"])


class NonRegularFilesDoNotBlock(KeyFixture):
    """F2. One `mkfifo stale.pub` in the images dir hung inspect for ever.

    The *.pub globs take whatever is in the directory and the size guard reads
    0 for a FIFO, so read_text() blocked on a writer that never came, with no
    wall-clock bound anywhere above it.
    """

    def test_a_fifo_in_the_images_dir_does_not_hang(self):
        os.mkfifo(self.images / "stale.pub")
        self.addCleanup(lambda: (self.images / "stale.pub").unlink(missing_ok=True))
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        out = {}

        def work():
            out["index"] = inspect_mod._local_key_index(
                str(self.images / "qt-vm1.qcow2"), {}, self.cfg())

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(20)
        self.assertFalse(t.is_alive(), "_local_key_index blocked on a FIFO")
        self.assertEqual(out["index"][FLEET_FP]["pub"], key)

    def test_a_directory_named_like_a_key_is_skipped(self):
        (self.images / "notakey.pub").mkdir()
        self.assertEqual(
            inspect_mod._local_key_index(
                str(self.images / "qt-vm1.qcow2"), {}, self.cfg()), {})


class PriorityBeforeTruncation(KeyFixture):
    """F4. The candidate cap was applied after an unbounded images-dir glob.

    A directory with more .pub files than the cap pushed keys/, the configured
    key and ~/.ssh off the end of the list entirely, so the one key the fleet
    guest accepts was never even read.
    """

    def noise(self, directory: Path, n: int, tag: bytes = b"n") -> None:
        for i in range(n):
            line, _ = make_key(tag + b"%03d" % i)
            self.write_key(directory / f"noise{i:03d}.pub", line, private=False)

    def test_the_keys_dir_survives_a_noisy_images_dir(self):
        self.noise(self.images, inspect_mod._GLOB_KEY_LIMIT + 32)
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        ident = self.identity(ga(FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["key_file"], str(key.with_name("fleet_ed25519")))

    def test_the_configured_key_survives_a_noisy_images_dir(self):
        self.noise(self.images, inspect_mod._GLOB_KEY_LIMIT + 32)
        key = self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity(ga(DESK_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["key_file"], str(key.with_name("id_rsa")))

    def test_the_ssh_dir_survives_two_noisy_directories_ahead_of_it(self):
        """G4. The cap was still applied across the concatenated glob results.

        keys/ and the images dir are both globbed before ~/.ssh, so a full cap
        from either -- 64 .pub files is nothing in a directory of baked
        images -- consumed the budget and ~/.ssh was never read at all. The
        key here is not the configured one, so only the glob can find it.
        """
        self.noise(self.images / "keys", inspect_mod._GLOB_KEY_LIMIT, b"k")
        self.noise(self.images, inspect_mod._GLOB_KEY_LIMIT, b"i")
        self.write_key(self.home / ".ssh" / "id_rsa.pub", OTHER_LINE)
        key = self.write_key(self.home / ".ssh" / "fleet_ed25519.pub", FLEET_LINE)
        ident = self.identity(ga(FLEET_LINE + "\n"),
                              image=str(self.images / "qt-vm1.qcow2"))
        self.assertEqual(ident["key_file"], str(key.with_name("fleet_ed25519")))

    def test_each_source_is_capped_on_its_own(self):
        self.noise(self.images / "keys", inspect_mod._GLOB_KEY_LIMIT + 16, b"k")
        self.noise(self.images, inspect_mod._GLOB_KEY_LIMIT + 16, b"i")
        self.noise(self.home / ".ssh", inspect_mod._GLOB_KEY_LIMIT + 16, b"h")
        candidates = inspect_mod._local_key_candidates(
            str(self.images / "qt-vm1.qcow2"), {}, self.cfg())
        for directory in (self.images / "keys", self.images, self.home / ".ssh"):
            found = [p for p in candidates
                     if p.parent == directory and p.name.startswith("noise")]
            self.assertEqual(len(found), inspect_mod._GLOB_KEY_LIMIT,
                             f"{directory} contributed {len(found)}")


class GuestCommentsAreSanitised(KeyFixture):
    """F7. The comment is guest-controlled text on an operator's terminal.

    str.split() does not split on ESC or BEL, so the comment field carries
    whatever an unprivileged guest user put in authorized_keys straight to the
    tty -- enough to overwrite the VM name and SSH line inspect printed for a
    different VM.
    """

    def test_a_control_sequence_in_the_comment_is_escaped(self):
        line, fp = make_key(b"evil", "ok\x1b[2K\rVM: qt-vm9")
        key = inspect_mod._parse_pubkey_line(line)
        self.assertNotIn("\x1b", key["comment"])
        self.assertNotIn("\r", key["comment"])

    def test_a_very_long_comment_is_capped(self):
        line, _ = make_key(b"long", "c" * 4000)
        self.assertLess(len(inspect_mod._parse_pubkey_line(line)["comment"]), 200)

    def test_the_printed_key_line_carries_no_control_characters(self):
        line, _ = make_key(b"evil", "x\x1b[31m\x07y")
        text = self._print_keys(ga(line + "\n"))
        self.assertFalse(any(ord(c) < 32 for c in text.replace("\n", "")),
                         repr(text))

    def _print_keys(self, ga_info):
        ident = self.identity(ga_info)
        return "\n".join(inspect_mod._key_line(k) for k in ident["keys"])


class GuestCannotBeAsked(KeyFixture):
    def test_no_key_is_asserted_without_an_agent_answer(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        ident = self.identity({})
        self.assertEqual(ident["source"], "configured")
        self.assertIn("not verified", ident["note"])
        self.assertTrue(all(k["accepted"] is None for k in ident["keys"]))

    def test_nothing_at_all_when_there_is_no_local_key_either(self):
        ident = self.identity({})
        self.assertEqual(ident["keys"], [])
        self.assertIsNone(ident["key_file"])
        self.assertIsNone(ident["source"])

    def test_an_unreadable_authorized_keys_is_not_an_empty_one(self):
        """The agent answered, but the file read failed: still unknown."""
        self.assertIsNone(inspect_mod._guest_accepted_keys(
            {"authorized_keys": "/home/ubuntu/.ssh/authorized_keys", "files": {}}))


class ParsingIsTotal(unittest.TestCase):
    def test_a_malformed_line_does_not_raise(self):
        for line in ["", "   ", "# comment", "ssh-ed25519", "ssh-ed25519 !!!!",
                     "ssh-ed25519 AAAA", "ssh-rsa " + base64.b64encode(
                         b"\x00\x00\x00\x0bssh-ed25519xx").decode(),
                     "\x00\x01\x02", "a" * 4096]:
            self.assertIsNone(inspect_mod._parse_pubkey_line(line), line)

    def test_a_truncated_blob_is_not_a_key(self):
        blob = (11).to_bytes(4, "big") + b"ssh-ed2"
        line = "ssh-ed25519 " + base64.b64encode(blob).decode()
        self.assertIsNone(inspect_mod._parse_pubkey_line(line))

    def test_the_fingerprint_matches_ssh_keygen(self):
        # ssh-keygen -lf on this exact key prints this fingerprint; the format
        # is SHA256 of the blob, base64, padding stripped.
        line, fp = make_key(b"abcd")
        self.assertEqual(inspect_mod._parse_pubkey_line(line)["fingerprint"], fp)
        self.assertFalse(fp.endswith("="))

    def test_duplicate_authorized_lines_are_reported_once(self):
        keys = inspect_mod._guest_accepted_keys(ga(FLEET_LINE + "\n" + FLEET_LINE + "\n"))
        self.assertEqual(len(keys), 1)


class GuestHomeDirectory(unittest.TestCase):
    """The agent script resolves the home from the guest's own passwd entry.

    A hardcoded /home/<user> is wrong for root and for a relocated home, and
    reading the wrong file is reported as "cannot say" rather than "no keys".
    The script runs in the guest, so what is checked here is the path it was
    told to read, as the payload records it.
    """

    def test_a_normal_user_home(self):
        info = ga(DESK_LINE + "\n", path="/home/ubuntu/.ssh/authorized_keys")
        self.assertEqual([k["fingerprint"] for k in
                          inspect_mod._guest_accepted_keys(info)], [DESK_FP])

    def test_root_home_is_not_under_slash_home(self):
        info = ga(DESK_LINE + "\n", path="/root/.ssh/authorized_keys")
        self.assertEqual([k["fingerprint"] for k in
                          inspect_mod._guest_accepted_keys(info)], [DESK_FP])
        self.assertNotIn("/home/root", str(info["files"]))

    def test_a_relocated_home(self):
        info = ga(DESK_LINE + "\n", path="/srv/ubuntu/.ssh/authorized_keys")
        self.assertEqual(len(inspect_mod._guest_accepted_keys(info)), 1)

    def test_the_script_takes_the_username_and_reads_passwd(self):
        script = inspect_mod._GA_SCRIPT.replace("__SOCKET__", "'/tmp/s'")
        self.assertIn("__USERNAME__", script)
        self.assertIn("/etc/passwd", script)
        self.assertNotIn("guest-exec", inspect_mod._GA_SCRIPT)

    def test_the_username_reaches_the_script(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["script"] = cmd[-1]
            raise OSError("not actually run")

        with mock.patch.object(inspect_mod.subprocess, "run", fake_run):
            inspect_mod._ga_query("/tmp/qga.sock", None, "ubuntu")
        self.assertIn("user = 'ubuntu'", seen["script"])
        self.assertNotIn("__USERNAME__", seen["script"])


class Rendering(KeyFixture):
    def _print(self, record) -> str:
        out = StringIO()
        with mock.patch("sys.stdout", out):
            inspect_mod._print_human([record])
        return out.getvalue()

    def _record(self, ga_info, image=None):
        with mock.patch.object(inspect_mod, "_ga_query", lambda *a: ga_info), \
                mock.patch.object(inspect_mod, "_read_vm_info", lambda i: {}), \
                mock.patch.object(inspect_mod, "_vm_argv", lambda pid: []):
            return inspect_mod._record(vm(image=image), self.cfg())

    def test_a_match_prints_a_pasteable_command(self):
        key = self.write_key(self.images / "keys" / "fleet_ed25519.pub", FLEET_LINE)
        text = self._print(self._record(ga(FLEET_LINE + "\n"),
                                        image=str(self.images / "qt-vm1.qcow2")))
        self.assertIn(f"SSH:       ssh -i {key.with_name('fleet_ed25519')} "
                      "-p 2253 ubuntu@localhost", text)
        self.assertIn("accepted by guest", text)

    def test_no_match_prints_the_fingerprint_and_says_so(self):
        text = self._print(self._record(ga(FLEET_LINE + "\n")))
        self.assertIn("guest accepts no key found locally", text)
        self.assertIn(FLEET_FP, text)
        self.assertNotIn(" -i ", text)

    def test_an_unaskable_guest_is_labelled_unverified(self):
        self.write_key(self.home / ".ssh" / "id_rsa.pub", DESK_LINE)
        text = self._print(self._record({}))
        self.assertIn("not verified against the guest", text)
        self.assertIn("configured default, unverified", text)

    def test_the_whole_authorized_keys_is_never_printed(self):
        text = self._print(self._record(ga(FLEET_LINE + "\n")))
        self.assertNotIn(FLEET_LINE.split()[1], text)

    def test_the_record_is_json_serialisable(self):
        import json
        json.dumps(self._record(ga(FLEET_LINE + "\n")))

    def test_the_raw_authorized_keys_text_is_not_carried_into_json(self):
        r = self._record(ga(FLEET_LINE + "\n"))
        self.assertNotIn("files", r.get("guest", {}))


if __name__ == "__main__":
    unittest.main()
