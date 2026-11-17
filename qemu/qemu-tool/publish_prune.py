"""Retention for the published guest image's repository on Docker Hub.

publish-qcow pushes each image under a dated tag, "<YYYYMMDD>.g<sha7>", and
moves two rolling tags onto it. Nothing ever removed a dated tag, so the
repository grew by a multi-gigabyte image per publish. This lists the tags,
decides which dated ones the policy no longer keeps, prints that plan, and
deletes them only when told to.

A tag is a name, and names are all this removes. The manifest a deleted tag
pointed at, its blobs, and its referrer (vm-info.json and the image's
keypair) are the registry's to keep or collect. Docker Hub serves the OCI
referrers API, so oras attaches the referrer to the manifest and there is no
"sha256-<hex>" fallback tag there to delete; one that did turn up is not a
dated tag and is kept like any other.

The plan is a pure function of the listing (plan()), so that everything that
decides what is deleted can be tested without a registry.

Docker Hub only: tags are deleted through the Hub API, which other
registries do not have. The endpoints, read from Docker's published OpenAPI
description and from the live API:

  POST   /v2/auth/token                         {identifier, secret}
                                                -> {access_token}, a bearer
  GET    /v2/namespaces/<ns>/repositories/<repo>/tags?page=N&page_size=100
                                                -> {count, next, results[]}
  DELETE /v2/repositories/<ns>/<repo>/tags/<tag>/  -> 204

The DELETE is the one the Hub's own web interface sends. It is not in the
published description, which documents only GET and HEAD for a tag.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable

from .fleet_xml import FleetXml
from .vm_artifact import VM_COMMIT_TAG, VM_DATED_TAG

HUB = "https://hub.docker.com"
HUB_REGISTRIES = ("docker.io", "index.docker.io", "registry-1.docker.io")
USER_ENV = "DOCKERHUB_USERNAME"
TOKEN_ENV = "DOCKERHUB_TOKEN"
PAGE_SIZE = 100
# A repository this tool publishes holds a few dozen tags. A listing that is
# still going after this many pages is not one to act on.
MAX_PAGES = 200

# The only shape of tag that is ever a candidate for deletion: what the
# publish workflow pushes for each image. "latest", "qm.<sha7>", and anything
# somebody pushed by hand are not of it, and so are never deleted.
# [0-9], not \d, which also matches every other script's decimal digits.
DATED = VM_DATED_TAG
ROLLING = VM_COMMIT_TAG
# Docker Hub opened in 2013. An earlier push time is a placeholder, such as
# the 0001-01-01 a Go service writes for a time it never set.
EARLIEST_YEAR = 2013


class PruneError(Exception):
    """Nothing was deleted, or deletion stopped; the message says which."""


@dataclasses.dataclass(frozen=True)
class Tag:
    name: str
    # None when the listing gave no usable push time.
    pushed: datetime.datetime | None
    # The manifest the tag points at, "sha256:<hex>"; "" when not listed.
    digest: str


@dataclasses.dataclass(frozen=True)
class Plan:
    # (tag, why). delete is in the order the deletions are made.
    keep: tuple[tuple[Tag, str], ...]
    delete: tuple[tuple[Tag, str], ...]


def split_ref(ref: str) -> tuple[str, str]:
    """(namespace, repository) of a Docker Hub *ref*; PruneError otherwise.

    The registry must be named. "me/repo" does mean Docker Hub to docker and
    to oras, but a command that deletes should not be the one to guess.
    """
    if not ref:
        raise PruneError(
            "no repository named: give --ref, or --xml with a <publish-ref>.")
    if "@" in ref or ":" in ref.rsplit("/", 1)[-1]:
        raise PruneError(
            f"{ref!r} carries a tag or a digest; name the repository alone, "
            "as <publish-ref> does.")
    parts = ref.split("/")
    if len(parts) != 3 or parts[0] not in HUB_REGISTRIES:
        raise PruneError(
            f"{ref!r} is not a Docker Hub repository. Tags are deleted "
            "through the Docker Hub API, which no other registry has, so "
            "the reference must read docker.io/<namespace>/<repository>.")
    namespace, repository = parts[1], parts[2]
    for part in (namespace, repository):
        if not re.fullmatch(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*", part):
            raise PruneError(f"{ref!r} is not a valid repository name.")
    return namespace, repository


def parse_time(text: object) -> datetime.datetime | None:
    """An API timestamp as an aware datetime; None for anything else.

    The Hub writes between zero and nine fractional digits, which
    fromisoformat does not accept on every supported Python. A time from
    before the Hub existed is not a push time, and is None as well.
    """
    if not isinstance(text, str):
        return None
    found = re.fullmatch(
        r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z", text,
        re.ASCII)
    if not found:
        return None
    *whole, fraction = found.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        when = datetime.datetime(*map(int, whole), micro,
                                 tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return when if when.year >= EARLIEST_YEAR else None


def tags_from_results(results: Iterable[dict]) -> list[Tag]:
    tags = []
    for entry in results:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise PruneError("the tag listing holds an entry with no name.")
        digest = entry.get("digest")
        tags.append(Tag(
            name=name,
            pushed=parse_time(entry.get("tag_last_pushed")),
            digest=digest if isinstance(digest, str) else ""))
    return tags


def plan(tags: Iterable[Tag], *, keep: int, days: int,
         now: datetime.datetime) -> Plan:
    """Which tags the policy keeps and which it deletes.

    Kept: the *keep* newest dated tags, every dated tag pushed within *days*
    days of *now*, and every tag that is not a dated one. A dated tag whose
    push time the listing did not give is kept, and is not one of the
    *keep*: its age is unknown.
    """
    if keep < 1:
        raise PruneError("--keep must be at least 1: the newest published "
                         "image is never deleted.")
    if days < 0:
        raise PruneError("--days must not be negative.")
    tags = list(tags)
    names = [tag.name for tag in tags]
    if len(set(names)) != len(names):
        raise PruneError("the tag listing names a tag twice; not acting on "
                         "a listing that changed while it was read.")

    cutoff = now - datetime.timedelta(days=days)
    dated = [tag for tag in tags if DATED.fullmatch(tag.name)]
    timed = sorted((tag for tag in dated if tag.pushed is not None),
                   key=lambda tag: (tag.pushed, tag.name), reverse=True)
    kept: list[tuple[Tag, str]] = []
    doomed: list[tuple[Tag, str]] = []
    for rank, tag in enumerate(timed, start=1):
        age = (now - tag.pushed).days
        if rank <= keep:
            kept.append((tag, f"dated, newest {rank} of {keep} kept"))
        elif tag.pushed >= cutoff:
            kept.append((tag, f"dated, pushed {age} days ago, within {days}"))
        else:
            doomed.append((tag, f"dated, pushed {age} days ago, "
                                f"not among the {keep} newest"))
    for tag in dated:
        if tag.pushed is None:
            kept.append((tag, "dated, but the listing gives no push time"))

    for tag in tags:
        if DATED.fullmatch(tag.name):
            continue
        if tag.name == "latest":
            kept.append((tag, "latest, always kept"))
        elif ROLLING.fullmatch(tag.name):
            kept.append((tag, "qm.<sha7>, always kept"))
        else:
            kept.append((tag, "not a dated tag, never a candidate"))
    return Plan(keep=tuple(kept), delete=tuple(doomed))


# ---------------------------------------------------------------------------
# The Hub API
# ---------------------------------------------------------------------------

# (method, url, headers, body) -> (status, parsed JSON or None). Injected so
# the tests never open a socket.
Transport = Callable[[str, str, dict, bytes | None], tuple[int, object]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows a 30x and sends the Authorization header along, to
    whatever host the response names. Refused, so a redirect is a status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_transport(method: str, url: str, headers: dict,
                   body: bytes | None) -> tuple[int, object]:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers)
    try:
        with _OPENER.open(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            try:
                status, raw = error.code, error.read()
            except (OSError, http.client.HTTPException):
                status, raw = error.code, b""
    # HTTPException is not an OSError: a truncated or garbled response.
    except (urllib.error.URLError, OSError,
            http.client.HTTPException) as error:
        raise PruneError(
            f"{method} {_path(url)}: {type(error).__name__}: {error}"
        ) from None
    try:
        return status, json.loads(raw) if raw.strip() else None
    except ValueError:
        return status, None


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path


def _detail(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("message", "detail"):
            if isinstance(payload.get(key), str):
                return f": {payload[key]}"
    return ""


def login(transport: Transport, user: str, secret: str) -> str:
    """A short-lived bearer token for *user*. The secret is never echoed."""
    status, payload = transport(
        "POST", f"{HUB}/v2/auth/token",
        {"Content-Type": "application/json"},
        json.dumps({"identifier": user, "secret": secret}).encode())
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(token, str) or not token:
        raise PruneError(
            f"Docker Hub refused the credentials in {USER_ENV} and "
            f"{TOKEN_ENV} (HTTP {status}).")
    return token


def list_tags(transport: Transport, namespace: str, repository: str,
              token: str | None) -> list[Tag]:
    """Every tag of the repository, or PruneError: never part of them."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = (f"{HUB}/v2/namespaces/{namespace}/repositories/{repository}"
            f"/tags?page_size={PAGE_SIZE}")
    results: list[dict] = []
    count = None
    for page in range(1, MAX_PAGES + 1):
        # Built here, not taken from "next", and the transport follows no
        # redirect: the token goes nowhere a response names.
        url = f"{base}&page={page}"
        status, payload = transport("GET", url, headers, None)
        if status != 200:
            raise PruneError(
                f"listing {namespace}/{repository} failed on page {page} "
                f"(HTTP {status}{_detail(payload)}). Nothing was deleted.")
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("results"), list)
                or not isinstance(payload.get("count"), int)):
            raise PruneError(
                f"page {page} of the tag listing is not the shape the Hub "
                "API documents. Nothing was deleted.")
        if count is None:
            count = payload["count"]
        elif payload["count"] != count:
            raise PruneError(
                "the repository's tag count changed while it was listed "
                f"({count}, then {payload['count']}). Nothing was deleted; "
                "run it again.")
        results += payload["results"]
        if not payload.get("next"):
            break
        if not payload["results"]:
            raise PruneError(
                f"page {page} of the tag listing is empty but names a next "
                "page. Nothing was deleted.")
    else:
        raise PruneError(
            f"the tag listing did not end within {MAX_PAGES} pages. "
            "Nothing was deleted.")
    if len(results) != count:
        raise PruneError(
            f"the tag listing is incomplete: {len(results)} of {count} "
            "tags. Nothing was deleted.")
    return tags_from_results(results)


def delete_tag(transport: Transport, namespace: str, repository: str,
               token: str, name: str) -> None:
    quoted = urllib.parse.quote(name, safe="")
    status, payload = transport(
        "DELETE",
        f"{HUB}/v2/repositories/{namespace}/{repository}/tags/{quoted}/",
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        None)
    if status not in (200, 202, 204):
        raise PruneError(
            f"deleting {name} failed (HTTP {status}{_detail(payload)})")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def _resolve_ref(args: argparse.Namespace) -> str:
    if args.ref:
        return args.ref
    if args.xml:
        # Only an explicit --xml, as publish-qcow: an ambient fleet.xml must
        # not decide which repository loses tags.
        return FleetXml.parse_layers(FleetXml.find(args.xml)).publish_ref
    return ""


def _when(tag: Tag) -> str:
    return tag.pushed.strftime("%Y-%m-%d") if tag.pushed else "unknown   "


def render(ref: str, result: Plan, *, keep: int, days: int) -> str:
    lines = [f"{ref}: keep the {keep} newest dated tags and any pushed "
             f"within {days} days; latest and qm.* always."]
    width = max((len(tag.name) for tag, _ in result.keep + result.delete),
                default=0)
    # A tag may be 128 characters; do not let one set the column.
    width = min(width, 24)
    for verb, group in (("keep  ", result.keep), ("DELETE", result.delete)):
        for tag, why in group:
            lines.append(f"  {verb} {tag.name:<{width}}  {_when(tag)}  {why}")
    lines.append(f"{len(result.keep)} kept, {len(result.delete)} to delete.")
    return "\n".join(lines)


def run(args: argparse.Namespace, transport: Transport = http_transport,
        now: datetime.datetime | None = None) -> None:
    try:
        _run(args, transport,
             now or datetime.datetime.now(datetime.timezone.utc))
    except PruneError as error:
        sys.exit(f"Error: {error}")


def _run(args: argparse.Namespace, transport: Transport,
         now: datetime.datetime) -> None:
    ref = _resolve_ref(args)
    namespace, repository = split_ref(ref)
    deleting = args.yes and not args.dry_run
    user = os.environ.get(USER_ENV, "")
    secret = os.environ.get(TOKEN_ENV, "")
    if deleting and not (user and secret):
        raise PruneError(
            f"--yes deletes tags, which needs {USER_ENV} and {TOKEN_ENV} in "
            "the environment. Nothing was deleted.")
    # A dry run of a public repository needs no account; with one, the
    # listing is the account's view, which is the only view of a private one.
    token = login(transport, user, secret) if user and secret else None

    tags = list_tags(transport, namespace, repository, token)
    result = plan(tags, keep=args.keep, days=args.days, now=now)
    print(render(ref, result, keep=args.keep, days=args.days))
    if not result.delete:
        return
    if not deleting:
        print("Dry run: nothing was deleted. Pass --yes to delete.")
        return

    done = 0
    for tag, _ in result.delete:
        try:
            delete_tag(transport, namespace, repository, token, tag.name)
        except PruneError as error:
            raise PruneError(
                f"{error}. {done} of {len(result.delete)} tags were deleted "
                "before it; the rest are untouched.") from None
        done += 1
        print(f"Deleted {ref}:{tag.name}")
