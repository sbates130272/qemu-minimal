"""`qemu-tool down-all` -- tear down every qemu-tool stack you own.

Two questions have to be answered soundly before anything is destroyed, and
compose records the evidence for both on every container it starts:

* **Is this a qemu-tool stack?** `com.docker.compose.project.config_files`
  names the compose file. A generated stack carries the `gen-compose`
  provenance header; a committed one lives in a directory whose name is one
  of the six this tool ships. Neither is a guess, and the alternative --
  matching service names like `qemu-1` -- would happily tear down somebody's
  unrelated project that happened to have a service called `qemu-1`.

* **Is it yours?** `.Config.User` is the uid the container actually runs as,
  which is exactly what the CONTAINER_USER work put there. A project is
  yours if any of its containers runs as your uid.

The second has a limitation worth stating rather than discovering: a stack
started BEFORE the containers ran as the caller has every container as root,
so to a non-root caller it is root's and is skipped. That is the correct
answer -- you cannot `docker stop` your way out of not owning it -- but it is
not an obvious one, so skipped stacks are listed with the reason.

Destroying things is confirmed first, every time, unless --yes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .compose import _STACKS, resolve_identity
from .gen_compose import recorded_digest


@dataclass
class Stack:
    project: str
    config_file: Path | None
    working_dir: Path | None
    env_files: list[Path]
    containers: list[str] = field(default_factory=list)
    uids: set[str] = field(default_factory=set)

    def owned_by(self, uid: int) -> bool:
        """Does any container run as this uid?

        Any, not all: a stack legitimately mixes uids -- the stats exporters
        are pinned to 65534 and tapsetup/dockerapi stay root -- so requiring
        all of them would find nothing.
        """
        return any(u.split(":")[0] == str(uid) for u in self.uids if u)


def _inspect_running() -> list[dict]:
    """Every running container, with the labels and the user it runs as."""
    ids = subprocess.run(["docker", "ps", "-q"],
                         capture_output=True, text=True)
    if ids.returncode != 0 or not ids.stdout.split():
        return []
    out = subprocess.run(
        ["docker", "inspect", *ids.stdout.split(),
         "--format", "{{json .}}"],
        capture_output=True, text=True,
    )
    records = []
    for line in out.stdout.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _is_qemu_tool_stack(config_file: Path | None) -> bool:
    """A generated stack by its provenance header, a committed one by name.

    Deliberately NOT a service-name match. `qemu-1` is a name anyone might
    use, and this command removes containers.
    """
    if config_file is None:
        return False
    try:
        if recorded_digest(config_file) is not None:
            return True
    except OSError:
        pass
    # The committed stacks have no provenance header -- they are hand-written
    # -- so they are recognised by the directory this repo ships them in.
    return config_file.parent.name in _STACKS


def discover() -> list[Stack]:
    """Every running qemu-tool stack on this host, owned by anyone."""
    by_project: dict[str, Stack] = {}
    for rec in _inspect_running():
        labels = (rec.get("Config") or {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        if not project:
            continue
        if project not in by_project:
            raw_cfg = (labels.get("com.docker.compose.project.config_files")
                       or "").split(",")[0]
            raw_wd = labels.get("com.docker.compose.project.working_dir") or ""
            raw_env = labels.get(
                "com.docker.compose.project.environment_file") or ""
            by_project[project] = Stack(
                project=project,
                config_file=Path(raw_cfg) if raw_cfg else None,
                working_dir=Path(raw_wd) if raw_wd else None,
                env_files=[Path(p) for p in raw_env.split(",") if p],
            )
        stack = by_project[project]
        stack.containers.append((rec.get("Name") or "").lstrip("/"))
        stack.uids.add((rec.get("Config") or {}).get("User") or "")

    return sorted(
        (s for s in by_project.values() if _is_qemu_tool_stack(s.config_file)),
        key=lambda s: s.project,
    )


def _teardown_cmd(stack: Stack) -> list[str]:
    """The exact invocation that tears this project down.

    Rebuilt from what compose recorded, and pinned with an explicit `-p`.
    That last part matters: the project name is otherwise derived from the
    working directory, and a teardown run from the wrong one resolves to an
    empty project and reports success -- which is the leading theory for the
    containers that survived a `down` by 41 hours.
    """
    cmd = ["docker", "compose", "-p", stack.project]
    if stack.working_dir:
        cmd += ["--project-directory", str(stack.working_dir)]
    if stack.config_file:
        cmd += ["-f", str(stack.config_file)]
    for env_file in stack.env_files:
        # Skip one that has moved: compose refuses outright on a missing
        # --env-file, and a stack whose settings were tidied away is exactly
        # the kind this command exists to clean up.
        if env_file.is_file():
            cmd += ["--env-file", str(env_file)]
    # Every profile, or the metrics and lmcache services are filtered out of
    # the teardown on a compose version that does that.
    cmd += ["--profile", "*", "down", "--remove-orphans"]
    return cmd


def _describe(stack: Stack, uid: int) -> str:
    owner = "you" if stack.owned_by(uid) else \
        "uid " + ", ".join(sorted(u.split(":")[0] or "0" for u in stack.uids))
    where = stack.working_dir or "?"
    return (f"  {stack.project}  ({len(stack.containers)} containers, "
            f"{owner})\n      {where}")


def run(assume_yes: bool = False, dry_run: bool = False,
        all_users: bool = False) -> None:
    uid = os.getuid()
    stacks = discover()
    if not stacks:
        print("No running qemu-tool stacks found.")
        return

    if all_users and uid != 0:
        sys.exit(
            "Error: --all-users needs root -- another user's containers "
            "cannot be stopped\n"
            "       without it. Re-run under sudo."
        )
    # Root owns the machine, so "mine" is everything; that is what makes
    # `sudo qemu-tool down-all` the system-wide form the way it reads.
    take_all = all_users or uid == 0
    mine = [s for s in stacks if take_all or s.owned_by(uid)]
    skipped = [s for s in stacks if s not in mine]

    if mine:
        print(f"Will tear down {len(mine)} stack(s):")
        for stack in mine:
            print(_describe(stack, uid))
    if skipped:
        print(f"\nSkipping {len(skipped)} stack(s) you do not own:")
        for stack in skipped:
            print(_describe(stack, uid))
        print("\n  A stack started before the containers ran as the caller "
              "has every\n"
              "  container as root, so it reads as root's. Re-run under sudo "
              "to include it.")
    if not mine:
        return

    if dry_run:
        print("\n--dry-run: nothing was stopped. The commands would be:")
        for stack in mine:
            print(f"  {' '.join(_teardown_cmd(stack))}")
        return

    if not assume_yes:
        # Always, and never defaulting to yes. This removes containers; a
        # bare Enter must not be the thing that does it.
        total = sum(len(s.containers) for s in mine)
        try:
            reply = input(
                f"\nStop and remove {total} containers "
                f"across {len(mine)} stack(s)? [y/N]: ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Nothing was stopped.")
            return

    failed = 0
    for stack in mine:
        print(f"\n==> {stack.project}")
        env = os.environ.copy()
        # The uid the containers actually run as, taken from the containers
        # themselves rather than from this caller: the compose file
        # interpolates `user: ${CONTAINER_USER:?...}` even on `down`, and a
        # root-era stack being torn down by root needs 0:0, not the ids of
        # whoever is running the command.
        observed = next((u for u in sorted(stack.uids) if u), "")
        if observed:
            env.setdefault("CONTAINER_USER", observed)
        else:
            ident, _ = resolve_identity([])
            env.setdefault("CONTAINER_USER", ident["CONTAINER_USER"])
        env.setdefault("CONTAINER_KVM_GID", "0")
        result = subprocess.run(_teardown_cmd(stack), env=env)
        if result.returncode != 0:
            failed += 1
            print(f"    failed (exit {result.returncode}); "
                  f"remove by name with: docker rm -f "
                  f"{' '.join(stack.containers)}", file=sys.stderr)

    if failed:
        sys.exit(f"\n{failed} of {len(mine)} stack(s) did not tear down.")
    print(f"\n{len(mine)} stack(s) torn down.")
