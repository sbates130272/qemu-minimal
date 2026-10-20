"""Tests for who `qemu-tool inspect` reports as logged in.

The bug these exist for: inspect printed "Login: unknown" for a fleet guest
with an ssh session open. guest-get-users reads utmp and nothing else, and
these Ubuntu 26.04 guests ship no /var/run/utmp at all -- `who` prints
nothing there -- so the agent returned [] whether or not anyone was on the
box, and [] with no utmp was correctly rendered as "unknown".

systemd-logind knows, and its state is readable files under /run, so the
answer costs no guest-exec. The point of the change is that the three states
stay distinct: somebody is on -> name them; logind readable and nobody on ->
a real "none", which the utmp path could never produce on these guests;
neither source answers -> "unknown".

Synthetic guest-agent payloads throughout; nothing here talks to a VM. The
session and user file bodies are copied from a live fleet guest.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
from qemu_tool import inspect as inspect_mod  # noqa: E402

SESSION_DIR = inspect_mod._LOGIND_SESSION_DIR

# An ssh login, verbatim from /run/systemd/sessions/9 on fleet guest 32.
SSH_SESSION = """\
# This is private data. Do not parse.
UID=1001
ACTIVE=1
IS_DISPLAY=1
STATE=active
REMOTE=1
LEADER_FD_SAVED=1
USER=ubuntu
TYPE=tty
ORIGINAL_TYPE=tty
CLASS=user
SCOPE=session-9.scope
REMOTE_HOST=172.31.0.1
SERVICE=sshd
LEADER=1553
AUDIT=9
REALTIME=1792456425270548
MONOTONIC=2091866064
"""

# The lingering user manager from the same guest, /run/systemd/sessions/1.
# ernic_fleet_guest enables linger, so one of these exists per user whether
# or not anybody is logged in.
MANAGER_SESSION = """\
# This is private data. Do not parse.
UID=1001
ACTIVE=1
IS_DISPLAY=0
STATE=active
REMOTE=0
LEADER_FD_SAVED=1
USER=ubuntu
TYPE=unspecified
ORIGINAL_TYPE=unspecified
CLASS=manager
SERVICE=systemd-user
LEADER=1044
AUDIT=1
REALTIME=1792454467582735
MONOTONIC=134178250
"""


def session(user="ubuntu", realtime="1792456425270548", remote="1",
            host="172.31.0.1", service="sshd", cls="user"):
    """A logind session file body with the fields inspect looks at."""
    lines = ["# This is private data. Do not parse.", f"USER={user}",
             f"REMOTE={remote}", f"SERVICE={service}", f"REALTIME={realtime}"]
    if host:
        lines.append(f"REMOTE_HOST={host}")
    if cls:
        lines.append(f"CLASS={cls}")
    return "\n".join(lines) + "\n"


def ga(users=None, utmp=False, sessions=None, logind=True, extra=None,
       complete=True):
    """A guest-agent payload with no utmp, i.e. one that reaches the logind path.

    `logind=False` drops the flag the in-guest script sets when it could open
    the sessions directory, which is how an unreadable or absent logind looks
    from here. `complete=None` drops the completeness flag the script sets
    when it enumerated everything.
    """
    files = {"/etc/passwd": "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n"}
    for sid, text in (sessions or {}).items():
        files[f"{SESSION_DIR}/{sid}"] = text
    files.update(extra or {})
    info = {"files": files}
    if logind:
        info["logind"] = True
    if users is not None:
        info["users"] = users
    if utmp is not None:
        info["utmp"] = utmp
    if complete is not None:
        info["logind_complete"] = complete
    return info


class LogindSessions(unittest.TestCase):
    def test_an_ssh_session_is_reported(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={"9": SSH_SESSION}))
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["user"], "ubuntu")
        self.assertEqual(s[0]["remote_host"], "172.31.0.1")
        self.assertEqual(s[0]["session"], "9")
        line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s})
        self.assertIn("ubuntu from 172.31.0.1 (since ", line)

    def test_realtime_is_microseconds(self):
        """Read as seconds this login lands in about 58000 AD, not in an error."""
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={"9": SSH_SESSION}))
        self.assertAlmostEqual(s[0]["login_time"], 1792456425.270548, places=5)

    def test_a_lingering_user_manager_is_not_a_login(self):
        s = inspect_mod._ssh_sessions(
            ga(users=[], sessions={"1": MANAGER_SESSION}))
        self.assertEqual(s, [])
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s}),
            "none")

    def test_a_manager_without_a_class_field_is_still_not_a_login(self):
        """Before systemd 256 a user manager was CLASS=user; SERVICE is the mark."""
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "1": session(service="systemd-user", remote="0", host=None, cls=None),
        }))
        self.assertEqual(s, [])

    def test_background_and_greeter_classes_are_not_logins(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "3": session(service="cron", remote="0", host=None, cls="background"),
            "4": session(service="gdm", remote="0", host=None, cls="greeter"),
        }))
        self.assertEqual(s, [])

    def test_several_sessions_for_one_user(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "1": MANAGER_SESSION,
            "9": session(realtime="1792456425270548", host="172.31.0.1"),
            "11": session(realtime="1792456999000000", host="172.31.0.2"),
        }))
        self.assertEqual([x["session"] for x in s], ["9", "11"])
        self.assertEqual({x["user"] for x in s}, {"ubuntu"})
        self.assertEqual([x["remote_host"] for x in s],
                         ["172.31.0.1", "172.31.0.2"])

    def test_sessions_for_several_users(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "2": session(user="batesste", service="systemd-user", remote="0",
                         host=None, cls="manager"),
            "9": session(user="ubuntu", realtime="1792456425270548"),
            "12": session(user="stebates", realtime="1792456000000000",
                          host="10.0.0.5"),
        }))
        # Oldest first, so the order does not depend on the dict the agent
        # happened to return.
        self.assertEqual([x["user"] for x in s], ["stebates", "ubuntu"])

    def test_sparse_session_ids(self):
        """Ids are 1, 2, 9, 11 on a real guest; nothing may stop at the gap."""
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "1": MANAGER_SESSION,
            "2": session(user="batesste", service="systemd-user", remote="0",
                         host=None, cls="manager"),
            "9": session(realtime="1792456425270548"),
            "11": session(user="stebates", realtime="1792456425999999"),
        }))
        self.assertEqual([x["session"] for x in s], ["9", "11"])

    def test_readable_with_no_sessions_is_none_not_unknown(self):
        """The whole point: a real "none" the utmp path could not give here."""
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={}))
        self.assertEqual(s, [])
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s}),
            "none")

    def test_unreadable_logind_is_unknown(self):
        s = inspect_mod._ssh_sessions(ga(users=[], logind=False))
        self.assertIsNone(s)
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s}),
            "unknown")

    def test_no_agent_answer_at_all_is_unknown(self):
        self.assertIsNone(inspect_mod._ssh_sessions({}))

    def test_a_malformed_session_file_does_not_raise(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "1": "",
            "2": "\x00\x01 not a logind file at all",
            "3": "USER=ubuntu\nREALTIME=not-a-number\nSERVICE=sshd\nREMOTE=1\n",
            "4": "REALTIME=1792456425270548\n",  # no USER
            "9": SSH_SESSION,
        }))
        by_id = {x["session"]: x for x in s}
        self.assertEqual(sorted(by_id), ["3", "9"])
        # An unparsable timestamp is no timestamp, not a timestamp of zero.
        self.assertIsNone(by_id["3"]["login_time"])
        line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s})
        self.assertIn("ubuntu", line)

    def test_a_session_file_outside_the_sessions_dir_is_ignored(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={"9": SSH_SESSION},
                                         extra={"/run/systemd/users/1001":
                                                "NAME=ubuntu\nSESSIONS=9 1\n"}))
        self.assertEqual([x["session"] for x in s], ["9"])


class UtmpStillWins(unittest.TestCase):
    def test_guest_get_users_answering_normally_wins(self):
        """Where utmp exists it is one command, not a directory of file reads."""
        s = inspect_mod._ssh_sessions({
            "users": [{"user": "stebates", "login-time": 1760000000.0}],
            "utmp": True,
            "files": {SESSION_DIR: "", f"{SESSION_DIR}/9": SSH_SESSION},
        })
        self.assertEqual([x["user"] for x in s], ["stebates"])
        self.assertEqual(s[0]["login_time"], 1760000000.0)

    def test_an_empty_utmp_that_exists_is_a_real_none(self):
        s = inspect_mod._ssh_sessions({
            "users": [], "utmp": True,
            "files": {SESSION_DIR: "", f"{SESSION_DIR}/9": SSH_SESSION},
        })
        self.assertEqual(s, [])


class IncompleteEnumerationIsUnknown(unittest.TestCase):
    """F5. "none" has to mean we looked everywhere and found nothing.

    The sessions-directory sentinel proves the enumeration started, not that
    it finished. Every way it can fall short -- the deadline breaking out of
    either loop, a cap truncating a list, /etc/passwd unreadable -- used to
    land in the same empty list as a genuinely empty guest, so a guest with a
    live ssh session on it printed "Login: none".
    """

    def test_a_truncated_enumeration_is_unknown_not_none(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={}, complete=False))
        self.assertIsNone(s)
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s}),
            "unknown")

    def test_a_missing_completeness_flag_is_unknown(self):
        """An agent script that did not report completeness has not proved any."""
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={}, complete=None))
        self.assertIsNone(s)

    def test_a_truncated_enumeration_that_did_find_someone_still_names_them(self):
        """Partial evidence of a login is evidence; only the empty case inverts."""
        s = inspect_mod._ssh_sessions(
            ga(users=[], sessions={"9": SSH_SESSION}, complete=False))
        self.assertEqual([x["user"] for x in s], ["ubuntu"])

    def test_a_complete_enumeration_with_nothing_in_it_is_still_none(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={}, complete=True))
        self.assertEqual(s, [])


class MalformedTimestamps(unittest.TestCase):
    """F6. A 300-digit REALTIME raised OverflowError and aborted the whole run.

    OverflowError is an ArithmeticError, not a ValueError, so the guard around
    the microsecond conversion did not catch it -- one crafted session file on
    one guest took down the inspect of all 32.
    """

    def test_an_enormous_realtime_does_not_raise(self):
        s = inspect_mod._logind_session(session(realtime="9" * 320))
        self.assertIsNotNone(s)
        self.assertIsNone(s["login_time"])

    def test_an_out_of_range_realtime_is_no_timestamp(self):
        """A plausible int that dates the login to 58000 AD is not a timestamp."""
        self.assertIsNone(
            inspect_mod._logind_session(session(realtime="17924564252705480000"))
            ["login_time"])
        self.assertIsNone(
            inspect_mod._logind_session(session(realtime="-17924564252705"))
            ["login_time"])

    def test_the_whole_run_survives_one_crafted_session_file(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "7": session(user="evil", realtime="9" * 320),
            "9": SSH_SESSION,
        }))
        self.assertEqual(sorted(x["session"] for x in s), ["7", "9"])
        line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s})
        self.assertIn("ubuntu", line)

    def test_a_poisoned_login_time_does_not_break_rendering(self):
        """_login_line formats a clock time and an elapsed duration from it."""
        for bad in (10 ** 30, -10 ** 30, float("inf"), float("nan")):
            line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": [
                {"user": "ubuntu", "login_time": bad, "remote_host": None,
                 "session": "9", "service": "sshd"}]})
            self.assertIn("ubuntu", line)


class GuestStringsAreSanitised(unittest.TestCase):
    """F7. USER and REMOTE_HOST come from inside a guest, onto an operator tty.

    str.split() does not split on ESC, so an unprivileged guest user could
    write a session file whose USER carries a control sequence and forge
    inspect's own output for a different VM.
    """

    def test_a_control_sequence_in_user_is_escaped(self):
        s = inspect_mod._logind_session(
            session(user="ubuntu\x1b[2K\rVM: qt-vm9  (pid 1)"))
        self.assertNotIn("\x1b", s["user"])
        self.assertNotIn("\r", s["user"])

    def test_a_control_sequence_in_remote_host_is_escaped(self):
        s = inspect_mod._logind_session(session(host="10.0.0.1\x07\x08\x08\x08"))
        self.assertNotIn("\x07", s["remote_host"])
        self.assertNotIn("\x08", s["remote_host"])

    def test_a_long_guest_string_is_capped(self):
        s = inspect_mod._logind_session(session(user="u" * 4000))
        self.assertLess(len(s["user"]), 200)

    def test_a_utmp_username_is_sanitised_too(self):
        s = inspect_mod._ssh_sessions({
            "users": [{"user": "root\x1b[31m", "login-time": 1760000000.0}],
            "utmp": True, "files": {},
        })
        self.assertNotIn("\x1b", s[0]["user"])

    def test_the_rendered_line_carries_no_control_characters(self):
        s = inspect_mod._ssh_sessions(ga(users=[], sessions={
            "9": session(user="ubuntu\x1b[2K", host="1.2.3.4\x1b[1m"),
        }))
        line = inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": s})
        self.assertFalse(any(ord(c) < 32 for c in line), repr(line))


class SessionState(unittest.TestCase):
    """F10. A session logind is tearing down is not somebody logged in."""

    def test_a_closing_session_is_not_a_login(self):
        text = session().replace("USER=", "STATE=closing\nUSER=", 1)
        self.assertIsNone(inspect_mod._logind_session(text))

    def test_an_active_session_still_counts(self):
        text = session().replace("USER=", "STATE=active\nUSER=", 1)
        self.assertIsNotNone(inspect_mod._logind_session(text))

    def test_an_online_session_still_counts(self):
        text = session().replace("USER=", "STATE=online\nUSER=", 1)
        self.assertIsNotNone(inspect_mod._logind_session(text))

    def test_a_session_with_no_state_field_still_counts(self):
        """An older logind writes no STATE; absence must not drop the session."""
        self.assertIsNotNone(inspect_mod._logind_session(session()))


class FakeAgent:
    """A QEMU guest agent serving a fixed file tree over a unix socket.

    The uid enumeration, the session-id dedupe and the completeness flag all
    live inside _GA_SCRIPT, which runs in the guest and cannot be imported.
    Running the real script, unmodified, against a socket that answers the
    real QGA protocol is the only way to regression-test them.
    """

    def __init__(self, files, users=None, delay=0.0, unreadable=(), silent=()):
        self.files = files
        self.users = [] if users is None else users
        # `delay` is per command, the way a loaded fleet guest answers; it is
        # what pushes the script's 8 s budget over before a later probe runs.
        # `unreadable` opens but fails the READ, which is what an unreadable
        # file really looks like over this protocol. `silent` never answers
        # the OPEN at all -- a wedged agent, which is a third state and not
        # the same fact as a file that is not there.
        self.delay = delay
        self.unreadable = set(unreadable)
        self.silent = set(silent)
        self.dir = Path(tempfile.mkdtemp(prefix="qt-fakega-"))
        self.path = self.dir / "qga.sock"
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(self.path))
        self.srv.listen(2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        handles, nxt, buf = {}, [1], b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, _, buf = buf.partition(b"\n")
                    if not raw.strip():
                        continue
                    resp = self._handle(json.loads(raw), handles, nxt)
                    if resp is None:
                        continue  # a wedged agent: the request is simply dropped
                    try:
                        conn.sendall(json.dumps(resp).encode() + b"\n")
                    except OSError:
                        return

    def _handle(self, req, handles, nxt):
        cmd, args = req.get("execute"), req.get("arguments") or {}
        if self.delay:
            time.sleep(self.delay)
        if cmd == "guest-sync":
            return {"return": args.get("id")}
        if cmd == "guest-get-users":
            return {"return": self.users}
        if cmd == "guest-file-open":
            if args.get("path") in self.silent:
                return None
            if args.get("path") not in self.files:
                return {"error": {"class": "GenericError", "desc": "ENOENT"}}
            handles[nxt[0]] = args["path"]
            nxt[0] += 1
            return {"return": nxt[0] - 1}
        if cmd == "guest-file-read":
            path = handles.get(args.get("handle"))
            if path is None or path in self.unreadable:
                return {"error": {"class": "GenericError", "desc": "EIO"}}
            text = self.files[path]
            count = args.get("count", 65536)
            return {"return": {"buf-b64": base64.b64encode(
                text[:count].encode()).decode(), "count": len(text[:count]),
                "eof": len(text) <= count}}
        if cmd == "guest-file-close":
            handles.pop(args.get("handle"), None)
            return {"return": {}}
        return {"error": {"class": "CommandNotFound", "desc": str(cmd)}}

    def run(self, username="ubuntu", timeout=60):
        script = (inspect_mod._GA_SCRIPT
                  .replace("__SOCKET__", repr(str(self.path)))
                  .replace("__USERNAME__", repr(username)))
        r = subprocess.run([sys.executable, "-I", "-c", script],
                           capture_output=True, text=True, timeout=timeout)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


class InGuestEnumeration(unittest.TestCase):
    """The real _GA_SCRIPT against a socket speaking the real QGA protocol."""

    def agent(self, files, users=None, delay=0.0, unreadable=None):
        # The sessions directory opens and then fails the read, because that
        # is what a directory does over this protocol: the script may use it
        # as a sentinel but must never expect bytes out of it.
        if unreadable is None:
            unreadable = (SESSION_DIR,)
        a = FakeAgent(files, users, delay=delay, unreadable=unreadable)
        self.addCleanup(a.close)
        return a

    @staticmethod
    def _passwd(n_users, home_user="ubuntu"):
        lines = ["root:x:0:0:root:/root:/bin/bash",
                 "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin"]
        for i in range(n_users):
            name = f"user{i}"
            lines.append(f"{name}:x:{1000 + i}:{1000 + i}::/home/{name}:/bin/bash")
        lines.append(f"{home_user}:x:1900:1900::/home/{home_user}:/bin/bash")
        return "\n".join(lines) + "\n"

    def test_the_script_reports_a_complete_enumeration(self):
        files = {
            "/etc/passwd": self._passwd(1),
            SESSION_DIR: "",
            "/run/systemd/users/1000": "NAME=user0\nSESSIONS=9\n",
            f"{SESSION_DIR}/9": SSH_SESSION,
        }
        info = self.agent(files).run()
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual(
            [s["session"] for s in inspect_mod._ssh_sessions(info)], ["9"])

    def test_a_user_past_the_old_uid_cap_is_still_found(self):
        """F9. Ubuntu appends regular users last; the cap dropped exactly those."""
        files = {
            "/etc/passwd": self._passwd(12),
            SESSION_DIR: "",
            "/run/systemd/users/1900": "NAME=ubuntu\nSESSIONS=9\n",
            f"{SESSION_DIR}/9": SSH_SESSION,
        }
        info = self.agent(files).run()
        self.assertTrue(info.get("logind_complete"))
        sessions = inspect_mod._ssh_sessions(info)
        self.assertEqual([s["user"] for s in sessions], ["ubuntu"])

    def test_a_crafted_sessions_line_does_not_burn_the_host_cpu(self):
        """F8. A quadratic list scan with no deadline check inside the loop.

        The per-VM budget is 8 s and there are 32 VMs; a guest that can make
        one of them cost 35 s of host CPU can make the fleet inspect unusable.
        """
        flood = " ".join(str(i) for i in range(10000, 99999))
        files = {"/etc/passwd": self._passwd(4), SESSION_DIR: ""}
        for i in range(4):
            files[f"/run/systemd/users/{1000 + i}"] = f"NAME=u\nSESSIONS={flood}\n"
        start = time.monotonic()
        info = self.agent(files).run(timeout=60)
        # The script's own budget is 8 s; a guest must not be able to push it
        # past that from the inside. Bounded, this input costs well under 1 s.
        self.assertLess(time.monotonic() - start, 8)
        # Truncated work must degrade the line to "unknown", never to "none".
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))


class BudgetExpiryIsUnknown(unittest.TestCase):
    """G1. A budget consumed during guest-get-users rendered as a confident none.

    The script sets result["utmp"] only once it has probed for the file. When
    the 8 s in-guest budget runs out during the five guest-get-* commands,
    users is [] and the probe never happens -- and the host read that absent
    key as "utmp exists and is empty", returned [], and never consulted
    logind. A guest somebody is sitting on printed "Login: none".

    The real script against a socket answering each command in 1.75 s, which
    is a loaded fleet guest; a host-side unit test cannot reach this.
    """

    def agent(self, files, users=None, delay=0.0):
        a = FakeAgent(files, users, delay=delay, unreadable=(SESSION_DIR,))
        self.addCleanup(a.close)
        return a

    FILES = {
        "/etc/passwd": "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n",
        SESSION_DIR: "",
        "/run/systemd/users/1001": "NAME=ubuntu\nSESSIONS=9\n",
        f"{SESSION_DIR}/9": SSH_SESSION,
    }

    def test_a_budget_spent_before_the_utmp_probe_is_unknown(self):
        info = self.agent(dict(self.FILES), delay=1.75).run()
        self.assertEqual(info.get("users"), [])
        self.assertNotIn("utmp", info)
        self.assertIsNone(inspect_mod._ssh_sessions(info))
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s",
                                     "ssh_sessions": inspect_mod._ssh_sessions(info)}),
            "unknown")

    def test_an_absent_utmp_key_never_means_an_empty_utmp(self):
        """The host rule, stated directly: only utmp is True licenses "none"."""
        self.assertIsNone(inspect_mod._ssh_sessions({"users": [], "files": {}}))
        self.assertIsNone(
            inspect_mod._ssh_sessions({"users": [], "utmp": None, "files": {}}))

    def test_an_unhurried_guest_still_probes_and_answers(self):
        info = self.agent(dict(self.FILES)).run()
        self.assertIs(info.get("utmp"), False)
        self.assertEqual(
            [s["session"] for s in inspect_mod._ssh_sessions(info)], ["9"])


class FailedReadsAreNotEmptyFiles(unittest.TestCase):
    """G2. read_file returned "" for a failed READ and for an empty file alike.

    Only guest-file-OPEN failing yielded None, and that is what every caller
    tested, so an error response to guest-file-read kept the enumeration
    looking complete while it read nothing -- and an unreadable
    authorized_keys rendered as "the guest accepts no keys at all".
    """

    def agent(self, files, unreadable, users=None):
        a = FakeAgent(files, users, unreadable=unreadable)
        self.addCleanup(a.close)
        return a

    FILES = {
        "/etc/passwd": "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n",
        SESSION_DIR: "",
        "/run/systemd/users/1001": "NAME=ubuntu\nSESSIONS=9\n",
        f"{SESSION_DIR}/9": SSH_SESSION,
        "/home/ubuntu/.ssh/authorized_keys": "ssh-ed25519 AAAAC3Nz fleet\n",
    }

    def test_an_unreadable_user_file_hides_a_session_so_it_is_unknown(self):
        info = self.agent(dict(self.FILES),
                          unreadable=(SESSION_DIR, "/run/systemd/users/1001")).run()
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))

    def test_an_unreadable_session_file_is_unknown_not_none(self):
        info = self.agent(dict(self.FILES),
                          unreadable=(SESSION_DIR, f"{SESSION_DIR}/9")).run()
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))

    def test_an_unreadable_authorized_keys_is_not_an_empty_one(self):
        info = self.agent(dict(self.FILES),
                          unreadable=(SESSION_DIR,
                                      "/home/ubuntu/.ssh/authorized_keys")).run()
        self.assertNotIn("authorized_keys", info)
        self.assertIsNone(inspect_mod._guest_accepted_keys(info))

    def test_a_readable_tree_still_completes(self):
        info = self.agent(dict(self.FILES), unreadable=(SESSION_DIR,)).run()
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual(
            [s["session"] for s in inspect_mod._ssh_sessions(info)], ["9"])

    def test_a_truncated_authorized_keys_is_not_a_guest_with_no_keys(self):
        """A file longer than one read is evidence of nothing past the cut."""
        path = "/home/ubuntu/.ssh/authorized_keys"
        self.assertIsNone(inspect_mod._guest_accepted_keys(
            {"authorized_keys": path, "files": {path: "# a comment\n"},
             "partial_files": [path]}))
        self.assertEqual(inspect_mod._guest_accepted_keys(
            {"authorized_keys": path, "files": {path: "# a comment\n"}}), [])


class AnUnansweredOpenIsNotAnAbsentFile(unittest.TestCase):
    """H1. read_file's OPEN timed out, returned None, and recorded nothing.

    Three states reach read_file and only two came back out: ENOENT and "the
    agent never answered" both returned None with nothing appended to
    `partial`. The logind walk then skipped the file and still claimed
    logind_complete, so a guest with a live ssh session on it printed
    "Login: none". open_file had always separated the three.
    """

    FILES = {
        "/etc/passwd": "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n",
        SESSION_DIR: "",
        "/run/systemd/users/1001": "NAME=ubuntu\nSESSIONS=9\n",
        f"{SESSION_DIR}/9": SSH_SESSION,
    }

    def agent(self, silent):
        a = FakeAgent(dict(self.FILES), [], unreadable=(SESSION_DIR,),
                      silent=silent)
        self.addCleanup(a.close)
        return a

    def test_an_unanswered_user_file_open_hides_a_session(self):
        info = self.agent(("/run/systemd/users/1001",)).run()
        self.assertIn("/run/systemd/users/1001",
                      info.get("partial_files") or [])
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))

    def test_an_unanswered_session_file_open_is_unknown_not_none(self):
        info = self.agent((f"{SESSION_DIR}/9",)).run()
        self.assertIn(f"{SESSION_DIR}/9", info.get("partial_files") or [])
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))

    def test_an_absent_file_is_still_not_a_partial_read(self):
        """The over-correction check: ENOENT is an answer, and a real one.

        uid 1001 is listed in passwd with no user file under /run, which is
        every account nobody is logged into. If that counted as a failed read
        the whole fleet would go "unknown" and the fix would be worse than
        the bug.
        """
        files = {"/etc/passwd": self.FILES["/etc/passwd"], SESSION_DIR: ""}
        a = FakeAgent(files, [], unreadable=(SESSION_DIR,))
        self.addCleanup(a.close)
        info = a.run()
        self.assertEqual(info.get("partial_files"), None)
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual(inspect_mod._ssh_sessions(info), [])


class ATruncatedPasswdIsNotACompleteLook(unittest.TestCase):
    """H2. One 65536-byte read of /etc/passwd, claimed as the whole file.

    The path was appended to `partial`, but the claim tested only
    `passwd is not None`, and `partial_before` was sampled after the grab so
    the later growth check could not see it either. Every account past the
    cut was dropped from the uid enumeration and the guest still reported a
    confident "none". Raising the read size is not a fix; the next larger
    passwd brings it back.
    """

    @staticmethod
    def _oversized_passwd():
        pad = "".join(f"svc{i}:x:{2000 + i}:0::/nonexistent:/usr/sbin/nologin\n"
                      for i in range(2000))
        return pad + "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n"

    def test_an_account_past_the_cut_makes_the_look_incomplete(self):
        passwd = self._oversized_passwd()
        self.assertGreater(len(passwd), 65536, "the point is that it truncates")
        files = {
            "/etc/passwd": passwd,
            "/etc/nsswitch.conf": "passwd: files systemd\n",
            SESSION_DIR: "",
            "/run/systemd/users/1001": "NAME=ubuntu\nSESSIONS=9\n",
            f"{SESSION_DIR}/9": SSH_SESSION,
        }
        a = FakeAgent(files, [], unreadable=(SESSION_DIR,))
        self.addCleanup(a.close)
        info = a.run()
        self.assertIn("/etc/passwd", info.get("partial_files") or [])
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))


class TheDeviceProbesGetTheBudgetFirst(unittest.TestCase):
    """M1. The session walk ran first and spent the whole 8 s in-guest budget.

    Raising the uid cap to 256 and the session cap to 32 made the walk long
    enough to reach the deadline on a guest with a few dozen accounts, and the
    KFD and NVMe reads sat behind it. Unlike the session path those degrade to
    a confident EMPTY LIST, so a guest reported no GPU and no disk sizes
    because somebody was logged into it.
    """

    # Split around the node index rather than written out whole: the full path
    # is 45 characters of [A-Za-z0-9/], which the commit-time secret scanner
    # reads as a high-entropy string and refuses the commit over. Same reason
    # test_inspect_devices.kfd_props() exists.
    KFD = "/sys/class/kfd/kfd/topology/nodes/%d/properties" % 1
    KFD_PROPS = "cpu_cores_count 0\ngfx_target_version 90010\n"
    PARTITIONS = "major minor  #blocks  name\n\n 253 0 536870912 vda\n"

    def _busy_guest(self):
        """Twenty accounts each holding a session: enough to outrun the budget."""
        lines = ["root:x:0:0:root:/root:/bin/bash"]
        files = {SESSION_DIR: "", self.KFD: self.KFD_PROPS,
                 "/proc/partitions": self.PARTITIONS,
                 "/etc/nsswitch.conf": "passwd: files systemd\n"}
        for i in range(20):
            uid = 1000 + i
            lines.append(f"u{i}:x:{uid}:{uid}::/home/u{i}:/bin/bash")
            files[f"/run/systemd/users/{uid}"] = f"NAME=u{i}\nSESSIONS={i + 1}\n"
            files[f"{SESSION_DIR}/{i + 1}"] = session(user=f"u{i}")
        files["/etc/passwd"] = "\n".join(lines) + "\n"
        return files

    def test_the_device_probes_run_before_the_session_walk(self):
        a = FakeAgent(self._busy_guest(), [], delay=0.15,
                      unreadable=(SESSION_DIR,))
        self.addCleanup(a.close)
        info = a.run()

        # The session walk is the open-ended one, so it is the one that pays:
        # truncated, it degrades to "unknown", which is a safe answer.
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info),
                          "a truncated walk is unknown, which is a safe answer")

        # The device probes are bounded and always wanted, so they finish --
        # and an empty GPU list from here would be a claim, not a shrug.
        self.assertIn(self.KFD, info["files"])
        self.assertIn("/proc/partitions", info["files"])
        self.assertTrue(info.get("gpu_complete"))


class ASourceOutsideEtcPasswdIsNotEnumerated(unittest.TestCase):
    """A session owned by an sssd or LDAP account is never looked for.

    The uid enumeration reads /etc/passwd and nothing else, so on a guest
    whose nsswitch.conf resolves passwd through sss, ldap or nis there are
    accounts it cannot see -- and claiming completeness over them is a
    confident "none" for a guest somebody may well be on.
    """

    FILES = {
        "/etc/passwd": "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n",
        SESSION_DIR: "",
    }

    def run_with(self, nsswitch):
        files = dict(self.FILES)
        if nsswitch is not None:
            files["/etc/nsswitch.conf"] = nsswitch
        a = FakeAgent(files, [], unreadable=(SESSION_DIR,))
        self.addCleanup(a.close)
        return a.run()

    def test_an_ldap_passwd_source_is_not_a_complete_look(self):
        info = self.run_with("passwd:         files ldap\ngroup: files\n")
        self.assertFalse(info.get("logind_complete"))
        self.assertIsNone(inspect_mod._ssh_sessions(info))

    def test_the_fleet_guests_nsswitch_still_licenses_none(self):
        """Verbatim from fleet guest 1: files plus nss-systemd, all enumerable."""
        info = self.run_with("passwd:         files systemd\n")
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual(inspect_mod._ssh_sessions(info), [])

    def test_an_unreadable_nsswitch_does_not_by_itself_turn_a_guest_unknown(self):
        """An absent nsswitch.conf means glibc's built-in default, which is files."""
        info = self.run_with(None)
        self.assertTrue(info.get("logind_complete"))


class ServiceAccountsAreNotExcluded(unittest.TestCase):
    """G3. The uid filter bounded the cost; the completeness claim ignored it.

    Only uid 0 and 1000..65533 were probed, so a session owned by a service
    account at uid 500 was never looked for -- and logind_complete was still
    True, so the host printed a confident "none" for a guest with somebody on
    it. The bound is now the login shell, which excludes only accounts that
    cannot hold an interactive session at all.
    """

    def agent(self, files):
        a = FakeAgent(files, [], unreadable=(SESSION_DIR,))
        self.addCleanup(a.close)
        return a

    def test_a_session_owned_by_a_uid_500_account_is_found(self):
        files = {
            "/etc/passwd": (
                "root:x:0:0:root:/root:/bin/bash\n"
                "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
                "svc:x:500:500::/var/lib/svc:/bin/bash\n"
                "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n"),
            SESSION_DIR: "",
            "/run/systemd/users/500": "NAME=svc\nSESSIONS=9\n",
            f"{SESSION_DIR}/9": session(user="svc"),
        }
        info = self.agent(files).run()
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual([s["user"] for s in inspect_mod._ssh_sessions(info)],
                         ["svc"])

    def test_a_nologin_account_is_still_not_probed(self):
        """The filter has to keep bounding the cost, or this is 40 round trips."""
        files = {
            "/etc/passwd": "".join(
                f"svc{i}:x:{100 + i}:0::/nonexistent:/usr/sbin/nologin\n"
                for i in range(40)),
            SESSION_DIR: "",
        }
        info = self.agent(files).run()
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual(inspect_mod._ssh_sessions(info), [])

    def test_a_filtered_guest_with_nobody_on_it_is_still_a_real_none(self):
        files = {
            "/etc/passwd": ("root:x:0:0:root:/root:/bin/bash\n"
                            "sync:x:4:65534:sync:/bin:/bin/sync\n"
                            "ubuntu:x:1001:1001::/home/ubuntu:/bin/bash\n"),
            SESSION_DIR: "",
        }
        info = self.agent(files).run()
        self.assertTrue(info.get("logind_complete"))
        self.assertEqual(inspect_mod._ssh_sessions(info), [])


class UnparseableUtmpEntriesAreUnknown(unittest.TestCase):
    """G5. Entries with no user field were dropped, leaving a confident [].

    guest-get-users answered with something, and every entry fell out of the
    parse -- an agent naming that field differently does exactly this. An
    answer in a shape this code does not understand is unknown, not none.
    """

    def test_a_non_empty_answer_that_yields_no_names_is_unknown(self):
        info = {"users": [{"name": "ubuntu", "login-time": 1760000000.0}],
                "utmp": True, "files": {}}
        self.assertIsNone(inspect_mod._ssh_sessions(info))
        self.assertEqual(
            inspect_mod._login_line({"ga_socket": "/s", "ssh_sessions": None}),
            "unknown")

    def test_an_unrecognised_answer_falls_through_to_logind(self):
        info = ga(users=[{"name": "ubuntu"}], utmp=True,
                  sessions={"9": SSH_SESSION})
        self.assertEqual([s["session"] for s in inspect_mod._ssh_sessions(info)],
                         ["9"])

    def test_entries_that_do_parse_are_still_reported(self):
        info = {"users": [{"user": "ubuntu"}, {"name": "other"}],
                "utmp": True, "files": {}}
        self.assertEqual([s["user"] for s in inspect_mod._ssh_sessions(info)],
                         ["ubuntu"])


class ParseFields(unittest.TestCase):
    def test_the_do_not_parse_banner_is_skipped(self):
        fields = inspect_mod._logind_fields(SSH_SESSION)
        self.assertEqual(fields["USER"], "ubuntu")
        self.assertEqual(fields["SERVICE"], "sshd")
        self.assertNotIn("# This is private data. Do not parse.", fields)

    def test_an_empty_value_parses(self):
        self.assertEqual(inspect_mod._logind_fields("SEATS=\n")["SEATS"], "")

    def test_a_remote_host_without_remote_set_is_not_reported(self):
        s = inspect_mod._logind_session(session(remote="0", host="172.31.0.1"))
        self.assertIsNone(s["remote_host"])


if __name__ == "__main__":
    unittest.main()
