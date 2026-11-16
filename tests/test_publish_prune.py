"""publish-qcow-prune: what it deletes from the published image's repository.

Everything that decides is tested without a registry. plan() is a pure
function of the tag listing, and the command talks to Docker Hub through a
transport these tests replace, so no test opens a socket and none can delete
anything.

The listing fixtures have the shape the live API returns, read from
hub.docker.com/v2/namespaces/<ns>/repositories/<repo>/tags on a public
repository: {count, next, previous, results[]}, each result carrying name,
tag_last_pushed and digest among much else.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import datetime
import http.client
import http.server
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).parent.parent
PKG = REPO / "qemu" / "qemu-tool"

spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
sys.modules.setdefault("qemu_tool", importlib.util.module_from_spec(spec))
spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import cli, publish_prune  # noqa: E402
from qemu_tool.publish_prune import PruneError, Tag, plan  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FLEET = REPO / "qemu" / "stacks" / "rocm-golden.xml"
WORKFLOW = (REPO / ".github" / "workflows"
            / "qemu-minimal-publish-qcow-rocm-golden.yml")
REF = "docker.io/sbates130272/qemu-tool-rocm-golden"
CREDS = {"DOCKERHUB_USERNAME": "someone", "DOCKERHUB_TOKEN": "dckr_pat_SECRET"}


def digest(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def dated(n: int, age_days: float, sha: str | None = None) -> Tag:
    """A dated tag pushed *age_days* before NOW, pointing at manifest n."""
    when = NOW - datetime.timedelta(days=age_days)
    name = f"{when:%Y%m%d}.g{sha or f'{n:07x}'}"
    return Tag(name, when, digest(n))


def fallback(n: int) -> Tag:
    """The tag oras writes where there is no referrers API. Not on the Hub."""
    return Tag("sha256-" + f"{n:064x}", NOW, digest(1000 + n))


def names(pairs) -> list[str]:
    return [tag.name for tag, _ in pairs]


def decide(tags, keep=10, days=30):
    return plan(tags, keep=keep, days=days, now=NOW)


class Policy(unittest.TestCase):

    def test_the_ten_newest_are_kept_however_old(self):
        tags = [dated(n, 100 + n) for n in range(15)]
        result = decide(tags)
        self.assertEqual(names(result.delete),
                         [t.name for t in tags[10:]])
        self.assertEqual(set(names(result.keep)),
                         {t.name for t in tags[:10]})

    def test_anything_pushed_within_thirty_days_is_kept_beyond_the_ten(self):
        tags = [dated(n, n) for n in range(40)]
        result = decide(tags)
        # Ages 0..30 are within the window; 31..39 are neither new nor ten.
        self.assertEqual(names(result.delete), [t.name for t in tags[31:]])

    def test_the_window_edge_is_inclusive_to_the_second(self):
        tags = [dated(n, 0) for n in range(10)]
        edge = Tag("20261017.gaaaaaaa", NOW - datetime.timedelta(days=30),
                   digest(50))
        past = Tag("20261017.gbbbbbbb",
                   NOW - datetime.timedelta(days=30, seconds=1), digest(51))
        result = decide(tags + [edge, past])
        self.assertEqual(names(result.delete), [past.name])

    def test_newest_is_by_push_time_not_by_the_date_in_the_name(self):
        # Named for an old day, pushed today: a rerun of an old commit.
        late = Tag("20250101.g1111111", NOW, digest(1))
        others = [Tag(f"2026{n:04d}.g{n:07x}"[:8] + f".g{n:07x}",
                      NOW - datetime.timedelta(days=200 + n), digest(10 + n))
                  for n in range(1, 4)]
        result = decide([late] + others, keep=1, days=0)
        self.assertEqual(names(result.keep), [late.name])
        self.assertEqual(len(result.delete), 3)

    def test_a_tie_on_push_time_is_broken_the_same_way_every_time(self):
        when = NOW - datetime.timedelta(days=90)
        a = Tag("20260818.gaaaaaaa", when, digest(1))
        b = Tag("20260818.gbbbbbbb", when, digest(2))
        for order in ([a, b], [b, a]):
            result = decide(order, keep=1, days=0)
            self.assertEqual(names(result.keep), [b.name])
            self.assertEqual(names(result.delete), [a.name])

    def test_fewer_tags_than_the_ten_deletes_nothing(self):
        result = decide([dated(n, 400 + n) for n in range(4)])
        self.assertEqual(result.delete, ())
        self.assertEqual(len(result.keep), 4)

    def test_an_empty_repository_is_an_empty_plan(self):
        result = decide([])
        self.assertEqual((result.keep, result.delete), ((), ()))

    def test_a_dated_tag_with_no_push_time_is_kept_and_takes_no_slot(self):
        blind = Tag("20200101.g0000000", None, digest(99))
        tags = [dated(n, 100 + n) for n in range(11)]
        result = decide(tags + [blind])
        self.assertIn(blind.name, names(result.keep))
        self.assertEqual(names(result.delete), [tags[10].name])

    def test_the_policy_numbers_are_checked(self):
        with self.assertRaises(PruneError):
            decide([], keep=0)
        with self.assertRaises(PruneError):
            decide([], days=-1)

    def test_a_listing_that_names_a_tag_twice_is_refused(self):
        tag = dated(1, 400)
        with self.assertRaisesRegex(PruneError, "twice"):
            decide([tag, tag])


class NeverCandidates(unittest.TestCase):
    """Only <YYYYMMDD>.g<sha7>, exactly, is ever deleted."""

    OLD = NOW - datetime.timedelta(days=900)

    def _kept(self, name):
        filler = [dated(n, n) for n in range(12)]
        result = decide(filler + [Tag(name, self.OLD, digest(500))],
                        keep=1, days=0)
        self.assertIn(name, names(result.keep), name)
        self.assertNotIn(name, names(result.delete), name)

    def test_latest_and_every_rolling_tag_are_always_kept(self):
        for name in ("latest", "qm.096ddfe", "qm.0000000"):
            self._kept(name)

    def test_tags_that_only_resemble_a_dated_one_are_kept(self):
        for name in (
                "20260925.gb559b95-dirty",           # a suffix
                "20261112.gdca6903-vm.resolute-qcow2",
                "v20260925.gb559b95",                # a prefix
                "2026092.gb559b95",                  # seven digits
                "20260925.gb559b9",                  # six of sha
                "20260925.gb559b955",                # eight of sha
                "20260925.gB559B95",                 # not lower case
                "20260925-gb559b95", "20260925.b559b95",
                "20260925.gb559b95\n",               # fullmatch, not match
                "v1", "1.3.0", "stable", "qm.096ddfe-qcow2"):
            self._kept(name)

    def test_digits_of_another_script_are_not_a_date(self):
        name = "٢٠٢٥١٠٠١.gabcdef1"
        self.assertRegex(name, r"^\d{8}\.g")
        self._kept(name)
        self.assertIsNone(publish_prune.DATED.fullmatch(name))

    def test_a_fallback_referrers_tag_is_a_tag_like_any_other(self):
        """The Hub has none; one that turned up is never a candidate, even
        when the image it describes goes."""
        images = [dated(n, 100 + n) for n in range(11)]
        refs = [Tag("sha256-" + t.digest[len("sha256:"):], NOW, digest(900))
                for t in images]
        result = decide(images + refs)
        self.assertEqual(names(result.delete), [images[10].name])
        for ref in refs:
            self.assertIn(ref.name, names(result.keep))
        self.assertFalse(hasattr(publish_prune, "REFERRER"))

    def test_the_dated_form_is_the_one_the_workflow_pushes(self):
        run = next(s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"][
            "publish-qcow-rocm-golden"]["steps"]
            if s.get("name") == "Publish the qcow2")["run"]
        self.assertIn("tag=\"$(date -u +%Y%m%d).g$sha\"", run)
        self.assertIn("sha=$(printf '%s' \"$GITHUB_SHA\" | cut -c1-7)", run)
        self.assertTrue(publish_prune.DATED.fullmatch("20261116.g7c58998"))
        self.assertTrue(publish_prune.ROLLING.fullmatch("qm.7c58998"))


class Reference(unittest.TestCase):

    def test_a_docker_hub_repository_is_split(self):
        self.assertEqual(publish_prune.split_ref(REF),
                         ("sbates130272", "qemu-tool-rocm-golden"))
        self.assertEqual(
            publish_prune.split_ref("index.docker.io/me/my.repo_x"),
            ("me", "my.repo_x"))

    def test_any_other_registry_is_refused(self):
        for ref in ("ghcr.io/example/rocm-golden", "quay.io/a/b",
                    "localhost:5000/a/b", "registry.example.com/a/b",
                    "docker.io.evil.example/a/b"):
            with self.assertRaisesRegex(PruneError, "not a Docker Hub"):
                publish_prune.split_ref(ref)

    def test_the_registry_is_never_guessed(self):
        for ref in ("sbates130272/qemu-tool-rocm-golden", "ubuntu",
                    "docker.io/ubuntu", "docker.io/a/b/c"):
            with self.assertRaisesRegex(PruneError, "not a Docker Hub"):
                publish_prune.split_ref(ref)

    def test_a_tag_or_digest_on_the_reference_is_refused(self):
        for ref in (REF + ":latest", REF + "@sha256:" + "0" * 64):
            with self.assertRaisesRegex(PruneError, "tag or a digest"):
                publish_prune.split_ref(ref)

    def test_a_name_that_could_reach_another_path_is_refused(self):
        for ref in ("docker.io/../x", "docker.io/a/..", "docker.io/a/b?x=1",
                    "docker.io/a/b%2Fc", "docker.io/A/b", "docker.io//b"):
            with self.assertRaises(PruneError):
                publish_prune.split_ref(ref)

    def test_no_reference_at_all_says_how_to_give_one(self):
        with self.assertRaisesRegex(PruneError, "--ref, or --xml"):
            publish_prune.split_ref("")

    def test_the_shipped_fleet_names_a_repository_it_can_prune(self):
        args = _parse("--xml", str(FLEET))
        self.assertEqual(publish_prune._resolve_ref(args), REF)
        publish_prune.split_ref(REF)

    def test_the_flag_beats_the_xml_and_nothing_is_searched_for(self):
        args = _parse("--xml", str(FLEET), "--ref", "docker.io/me/other")
        self.assertEqual(publish_prune._resolve_ref(args),
                         "docker.io/me/other")
        with mock.patch.object(publish_prune.FleetXml, "find") as find:
            self.assertEqual(publish_prune._resolve_ref(_parse()), "")
        find.assert_not_called()


class Timestamps(unittest.TestCase):

    def test_the_fractions_the_hub_really_writes(self):
        for text, micro in (("2026-11-12T18:26:43.390844Z", 390844),
                            ("2026-11-12T18:26:41.60448Z", 604480),
                            ("2026-10-16T23:51:20.07045Z", 70450),
                            ("2026-11-14T19:17:43.931044168Z", 931044),
                            ("2026-11-12T18:26:43Z", 0)):
            got = publish_prune.parse_time(text)
            self.assertEqual((got.year, got.second, got.microsecond),
                             (2026, int(text[17:19]), micro), text)
            self.assertEqual(got.tzinfo, UTC)

    def test_anything_else_is_no_time_rather_than_a_guess(self):
        for text in (None, "", 0, "yesterday", "2026-11-12",
                     "2026-13-40T00:00:00Z", "2026-11-12T18:26:43+01:00",
                     "٢٠٢٦-11-12T18:26:43Z"):
            self.assertIsNone(publish_prune.parse_time(text), text)

    def test_a_time_from_before_the_hub_existed_is_no_time(self):
        """0001-01-01 is what a Go service writes for a time never set."""
        for text in ("0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z",
                     "2012-12-31T23:59:59.999999Z"):
            self.assertIsNone(publish_prune.parse_time(text), text)
        self.assertEqual(
            publish_prune.parse_time("2013-01-01T00:00:00Z").year, 2013)

    def test_a_tag_with_the_zero_time_is_kept_not_deleted_as_the_oldest(self):
        images = [dated(n, 100 + n) for n in range(11)]
        unset = publish_prune.tags_from_results([{
            "name": "20200105.g0000f04", "digest": digest(99),
            "tag_last_pushed": "0001-01-01T00:00:00Z"}])
        result = decide(images + unset)
        self.assertEqual(names(result.delete), [images[10].name])
        reason = dict((t.name, why) for t, why in result.keep)
        self.assertIn("no push time", reason["20200105.g0000f04"])


# ---------------------------------------------------------------------------
# The command, against a Hub that is a list in memory
# ---------------------------------------------------------------------------

def _parse(*argv):
    return cli._build_parser().parse_args(["publish-qcow-prune", *argv])


def _entry(tag: Tag) -> dict:
    """One result as the Hub returns it, less the fields nothing reads."""
    return {
        "name": tag.name, "digest": tag.digest or None,
        "tag_last_pushed": (tag.pushed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                            if tag.pushed else None),
        "last_updated": "2026-01-01T00:00:00Z", "tag_status": "active",
        "images": [], "media_type": "application/vnd.oci.image.manifest.v1+json",
    }


class Hub:
    """Docker Hub's three endpoints over a list of tags."""

    def __init__(self, tags, page_size=100):
        self.tags = list(tags)
        self.page_size = page_size
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict] = []
        self.bodies: list[bytes | None] = []
        self.fail: dict[tuple[str, str], tuple[int, object]] = {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        self.headers.append(headers)
        self.bodies.append(body)
        for (m, fragment), answer in self.fail.items():
            if m == method and fragment in url:
                return answer
        if method == "POST" and url.endswith("/v2/auth/token"):
            return 200, {"access_token": "bearer-token"}
        if method == "GET":
            page = int(url.split("&page=")[1])
            start = (page - 1) * self.page_size
            chunk = self.tags[start:start + self.page_size]
            more = start + self.page_size < len(self.tags)
            return 200, {"count": len(self.tags),
                         "next": "https://elsewhere.example/next" if more
                         else None,
                         "previous": None,
                         "results": [_entry(t) for t in chunk]}
        if method == "DELETE":
            name = url.rstrip("/").rsplit("/", 1)[1]
            self.tags = [t for t in self.tags if t.name != name]
            return 204, None
        raise AssertionError(f"unexpected {method} {url}")

    def did(self, method):
        return [url for m, url in self.calls if m == method]


def _run(hub, *argv, env=None):
    """(stdout, what it exited with or ""), with exactly *env*'s credentials."""
    out = io.StringIO()
    with mock.patch.dict(os.environ, env or {}), \
            contextlib.redirect_stdout(out):
        for name in CREDS:
            if name not in (env or {}):
                os.environ.pop(name, None)
        try:
            publish_prune.run(_parse("--ref", REF, *argv), transport=hub,
                              now=NOW)
        except SystemExit as stop:
            return out.getvalue(), str(stop.code)
    return out.getvalue(), ""


def _repository():
    images = [dated(n, 5 * n) for n in range(14)]       # ages 0..65 days
    refs = [fallback(n) for n in range(14)]
    newest = images[0]
    rolling = [Tag("latest", newest.pushed, newest.digest),
               Tag("qm.0000000", newest.pushed, newest.digest)]
    return images, refs, rolling


class Command(unittest.TestCase):

    def setUp(self):
        self.images, self.refs, self.rolling = _repository()
        self.hub = Hub(self.images + self.refs + self.rolling)
        # 10 newest are ages 0..45; 50..65 are past both rules.
        self.doomed = [t.name for t in self.images[10:]]

    def test_the_default_is_a_dry_run_that_needs_no_account(self):
        before = list(self.hub.tags)
        out, stop = _run(self.hub)
        self.assertEqual(stop, "")
        self.assertEqual(self.hub.tags, before)
        self.assertEqual(self.hub.did("DELETE"), [])
        self.assertEqual(self.hub.did("POST"), [])
        self.assertIn("Dry run: nothing was deleted", out)
        self.assertIn("26 kept, 4 to delete.", out)
        for name in self.doomed:
            self.assertRegex(out, rf"DELETE {name[:24]}")

    def test_dry_run_beats_yes(self):
        out, stop = _run(self.hub, "--yes", "--dry-run", env=CREDS)
        self.assertEqual(stop, "")
        self.assertEqual(self.hub.did("DELETE"), [])
        self.assertIn("Dry run", out)

    def test_yes_deletes_exactly_the_plan_in_order(self):
        out, stop = _run(self.hub, "--yes", env=CREDS)
        self.assertEqual(stop, "")
        self.assertEqual(
            [u.rstrip("/").rsplit("/", 1)[1] for u in self.hub.did("DELETE")],
            self.doomed)
        left = {t.name for t in self.hub.tags}
        self.assertEqual(
            left, {t.name for t in self.images[:10] + self.refs
                   + self.rolling})
        self.assertNotIn("Dry run", out)
        # A second run finds nothing to do.
        again, _ = _run(self.hub, "--yes", env=CREDS)
        self.assertIn("0 to delete.", again)

    def test_every_request_is_for_the_one_repository(self):
        _run(self.hub, "--yes", env=CREDS)
        for method, url in self.hub.calls:
            self.assertTrue(url.startswith("https://hub.docker.com/v2/"), url)
            if method != "POST":
                self.assertIn("sbates130272", url)
                self.assertIn("/qemu-tool-rocm-golden/tags", url)
        for url in self.hub.did("DELETE"):
            self.assertRegex(
                url, r"^https://hub\.docker\.com/v2/repositories/"
                     r"sbates130272/qemu-tool-rocm-golden/tags/[^/?#]+/$")

    def test_yes_without_credentials_stops_before_anything(self):
        for env in ({}, {"DOCKERHUB_USERNAME": "someone"},
                    {"DOCKERHUB_TOKEN": "x"}):
            hub = Hub(self.hub.tags)
            out, stop = _run(hub, "--yes", env=env)
            self.assertIn("DOCKERHUB_USERNAME and DOCKERHUB_TOKEN", stop)
            self.assertEqual(hub.calls, [])

    def test_the_credentials_come_from_the_environment_and_stay_there(self):
        out, stop = _run(self.hub, "--yes", env=CREDS)
        login = json.loads(self.hub.bodies[0])
        self.assertEqual(login, {"identifier": "someone",
                                 "secret": "dckr_pat_SECRET"})
        self.assertEqual(self.hub.calls[0],
                         ("POST", "https://hub.docker.com/v2/auth/token"))
        for text in (out, stop, *[u for _, u in self.hub.calls]):
            self.assertNotIn("dckr_pat_SECRET", text)
            self.assertNotIn("bearer-token", text)
        for headers in self.hub.headers[1:]:
            self.assertEqual(headers["Authorization"], "Bearer bearer-token")
        # No flag takes one.
        options = {o for a in cli._build_parser()._subparsers._group_actions[
            0].choices["publish-qcow-prune"]._actions
            for o in a.option_strings}
        self.assertEqual(options, {"-h", "--help", "--xml", "--ref", "--keep",
                                   "--days", "--yes", "--dry-run"})

    def test_a_refused_login_deletes_nothing_and_echoes_nothing(self):
        self.hub.fail[("POST", "/v2/auth/token")] = (
            401, {"detail": "Incorrect authentication credentials"})
        out, stop = _run(self.hub, "--yes", env=CREDS)
        self.assertIn("refused the credentials", stop)
        self.assertNotIn("dckr_pat_SECRET", stop)
        self.assertEqual(self.hub.did("GET"), [])
        self.assertEqual(self.hub.did("DELETE"), [])

    def test_the_listing_is_paged_and_next_is_never_followed(self):
        hub = Hub(self.hub.tags, page_size=5)
        with mock.patch.object(publish_prune, "PAGE_SIZE", 5):
            out, stop = _run(hub)
        self.assertEqual(stop, "")
        gets = hub.did("GET")
        self.assertEqual(len(gets), 6)                    # 30 tags, 5 a page
        self.assertEqual([u.split("&page=")[1] for u in gets],
                         ["1", "2", "3", "4", "5", "6"])
        for url in gets:
            self.assertNotIn("elsewhere.example", url)
        self.assertIn("26 kept, 4 to delete.", out)

    def test_a_page_that_fails_deletes_nothing(self):
        hub = Hub(self.hub.tags, page_size=5)
        hub.fail[("GET", "&page=3")] = (503, {"message": "try later"})
        with mock.patch.object(publish_prune, "PAGE_SIZE", 5):
            out, stop = _run(hub, "--yes", env=CREDS)
        self.assertIn("failed on page 3 (HTTP 503: try later)", stop)
        self.assertIn("Nothing was deleted", stop)
        self.assertEqual(hub.did("DELETE"), [])

    def test_a_short_listing_deletes_nothing(self):
        """The count says 30 and the pages hold fewer."""
        short = Hub(self.hub.tags)

        def lossy(method, url, headers, body):
            status, payload = Hub.__call__(short, method, url, headers, body)
            if method == "GET":
                payload["results"] = payload["results"][:-3]
            return status, payload

        out, stop = _run(lossy, "--yes", env=CREDS)
        self.assertIn("incomplete: 27 of 30", stop)
        self.assertEqual(short.did("DELETE"), [])

    def test_a_listing_of_the_wrong_shape_deletes_nothing(self):
        for payload in (None, [], {"results": []}, {"count": 1},
                        {"count": "1", "results": []},
                        {"count": 1, "results": [{"digest": "x"}]}):
            hub = Hub(self.hub.tags)
            hub.fail[("GET", "/tags?")] = (200, payload)
            out, stop = _run(hub, "--yes", env=CREDS)
            self.assertTrue(stop.startswith("Error:"), payload)
            self.assertEqual(hub.did("DELETE"), [], payload)

    def test_a_listing_that_never_ends_is_given_up_on(self):
        def endless(method, url, headers, body):
            if method == "POST":
                return 200, {"access_token": "t"}
            assert method == "GET"
            page = int(url.split("&page=")[1])
            return 200, {"count": 10**6, "next": "more",
                         "results": [_entry(dated(page, 400))]}
        with mock.patch.object(publish_prune, "MAX_PAGES", 7):
            out, stop = _run(endless, "--yes", env=CREDS)
        self.assertIn("did not end within 7 pages", stop)

    def test_a_repository_that_does_not_exist_is_said_plainly(self):
        self.hub.fail[("GET", "/tags?")] = (
            404, {"message": "object not found", "errinfo": {}})
        out, stop = _run(self.hub)
        self.assertIn("HTTP 404: object not found", stop)

    def test_a_delete_that_fails_stops_and_says_how_far_it_got(self):
        third = self.doomed[2]
        self.hub.fail[("DELETE", f"/tags/{third}/")] = (
            403, {"message": "insufficient scope"})
        out, stop = _run(self.hub, "--yes", env=CREDS)
        self.assertIn(f"deleting {third} failed (HTTP 403: insufficient "
                      "scope). 2 of 4 tags were deleted", stop)
        self.assertEqual(len(self.hub.did("DELETE")), 3)

    def test_another_registry_is_refused_before_any_request(self):
        hub = Hub([])
        args = _parse("--ref", "ghcr.io/example/rocm-golden", "--yes")
        with mock.patch.dict(os.environ, CREDS), \
                self.assertRaises(SystemExit) as stop:
            publish_prune.run(args, transport=hub, now=NOW)
        self.assertIn("not a Docker Hub repository", str(stop.exception))
        self.assertEqual(hub.calls, [])

    def test_yes_is_taken_only_spelled_in_full(self):
        for short in ("--y", "--ye", "--dry", "--k=3"):
            with self.subTest(short), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                _parse("--ref", REF, short)
        self.assertIs(_parse("--ref", REF, "--yes").yes, True)
        self.assertIs(_parse("--ref", REF).yes, False)


class Transport(unittest.TestCase):
    """The one piece that opens a socket, against 127.0.0.1 or a mock."""

    def test_a_dead_network_is_an_error_that_names_no_query(self):
        with mock.patch.object(
                publish_prune._OPENER, "open",
                side_effect=publish_prune.urllib.error.URLError("no route")):
            with self.assertRaisesRegex(PruneError, "no route") as caught:
                publish_prune.http_transport(
                    "GET", "https://hub.docker.com/v2/x?secret=1", {}, None)
        self.assertNotIn("secret", str(caught.exception))

    def test_a_garbled_or_truncated_response_is_an_error_not_a_traceback(self):
        for error in (http.client.IncompleteRead(b"half"),
                      http.client.BadStatusLine("junk"),
                      http.client.RemoteDisconnected("gone"),
                      TimeoutError("timed out")):
            with self.subTest(type(error).__name__), mock.patch.object(
                    publish_prune._OPENER, "open", side_effect=error):
                with self.assertRaisesRegex(PruneError, "DELETE /v2/x"):
                    publish_prune.http_transport(
                        "DELETE", "https://hub.docker.com/v2/x",
                        {"Authorization": "Bearer DUMMY"}, None)

    def test_a_delete_cut_off_mid_run_still_says_how_far_it_got(self):
        images, refs, rolling = _repository()
        hub = Hub(images + rolling)
        doomed = [t.name for t in images[10:]]

        def flaky(method, url, headers, body):
            if method == "DELETE" and f"/tags/{doomed[2]}/" in url:
                with mock.patch.object(
                        publish_prune._OPENER, "open",
                        side_effect=http.client.IncompleteRead(b"")):
                    return publish_prune.http_transport(
                        method, url, headers, body)
            return hub(method, url, headers, body)

        out, stop = _run(flaky, "--yes", env=CREDS)
        self.assertIn("2 of 4 tags were deleted", stop)
        self.assertIn("IncompleteRead", stop)
        self.assertNotIn(CREDS["DOCKERHUB_TOKEN"], out + stop)

    def _serve(self, handler):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_no_redirect_is_followed_so_the_token_goes_nowhere_else(self):
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        target = self._serve(Target)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(int(self.path.rsplit("/", 1)[1]))
                self.send_header("Location", target + "/landed")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        source = self._serve(Redirector)
        # A fresh opener: proxies are read from the environment when built.
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}), \
                mock.patch.object(
                    publish_prune, "_OPENER",
                    publish_prune.urllib.request.build_opener(
                        publish_prune._NoRedirect,
                        publish_prune.urllib.request.ProxyHandler({}))):
            for code in (301, 302, 303, 307, 308):
                status, payload = publish_prune.http_transport(
                    "GET", f"{source}/{code}",
                    {"Authorization": "Bearer DUMMY"}, None)
                self.assertEqual((status, payload), (code, None))
            # The target does answer, when asked directly.
            self.assertEqual(publish_prune.http_transport(
                "GET", target + "/direct", {}, None), (200, {}))
        self.assertEqual(seen, [None])

    def test_the_shipped_opener_is_the_one_that_refuses_redirects(self):
        self.assertTrue(any(isinstance(h, publish_prune._NoRedirect)
                            for h in publish_prune._OPENER.handlers))
        redirectors = [
            h for h in publish_prune._OPENER.handlers
            if isinstance(h, publish_prune.urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirectors), 1)

    def test_a_redirected_listing_deletes_nothing(self):
        images, refs, rolling = _repository()
        hub = Hub(images + rolling)
        hub.fail[("GET", "/tags?")] = (302, None)
        out, stop = _run(hub, "--yes", env=CREDS)
        self.assertIn("HTTP 302", stop)
        self.assertIn("Nothing was deleted", stop)
        self.assertEqual(hub.did("DELETE"), [])


class Workflow(unittest.TestCase):

    def setUp(self):
        self.steps = yaml.safe_load(WORKFLOW.read_text())["jobs"][
            "publish-qcow-rocm-golden"]["steps"]
        self.names = [s.get("name") for s in self.steps]

    def _step(self, name):
        return next(s for s in self.steps if s.get("name") == name)

    def test_it_prunes_after_a_real_publish_and_never_on_a_dry_run(self):
        step = self._step("Prune old tags from the registry")
        self.assertGreater(self.names.index(step["name"]),
                           self.names.index("Publish the qcow2"))
        self.assertEqual(step["if"], "${{ !inputs.dry-run }}")
        flat = " ".join(step["run"].replace("\\\n", " ").split())
        self.assertIn(
            "qemu-tool publish-qcow-prune --xml qemu/stacks/rocm-golden.xml "
            "--yes", flat)
        # The policy is the command's defaults, stated once.
        self.assertNotIn("--keep", step["run"])
        self.assertNotIn("--days", step["run"])

    def test_a_prune_that_fails_warns_and_leaves_the_publish_green(self):
        """The image is out; a red run would be re-run, and bake it again."""
        step = self._step("Prune old tags from the registry")
        self.assertNotIn("continue-on-error", step)
        self.assertIn("if ! qemu-tool publish-qcow-prune", step["run"])
        self.assertIn("::warning title=Registry not pruned::", step["run"])
        self.assertIn("GITHUB_STEP_SUMMARY", step["run"])
        self.assertEqual(self.names[-1], step["name"])

    def test_the_credentials_reach_it_as_environment_and_nothing_else(self):
        step = self._step("Prune old tags from the registry")
        self.assertEqual(step["env"], {
            "DOCKERHUB_USERNAME": "${{ secrets.DOCKERHUB_USERNAME }}",
            "DOCKERHUB_TOKEN": "${{ secrets.DOCKERHUB_TOKEN }}"})
        self.assertNotIn("secrets.", step["run"])
        self.assertNotIn("DOCKERHUB", step["run"])

    def test_three_tags_are_pushed_and_the_dated_one_is_first(self):
        """The referrer attaches to the first, and --digest-file reports it."""
        run = self._step("Publish the qcow2")["run"]
        flat = " ".join(run.replace("\\\n", " ").split())
        self.assertIn('--tag "$tag" --tag latest --tag "qm.$sha"', flat)
        self.assertEqual(flat.count("--tag "), 3)
        self.assertIn("sha=$(printf '%s' \"$GITHUB_SHA\" | cut -c1-7)", run)
        self.assertIn('tag="$(date -u +%Y%m%d).g$sha"', run)


if __name__ == "__main__":
    unittest.main()
