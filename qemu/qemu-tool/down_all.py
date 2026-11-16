"""`qemu-tool down-all` -- tear down every qemu-tool stack you own.

Two questions have to be answered soundly before anything is destroyed, and
compose records the evidence for both on every container it starts:

* **Is this a qemu-tool stack?** `com.docker.compose.project.config_files`
  names the compose file, and every stack this tool runs is generated, so
  that file carries the `gen-compose` provenance header. That is not a
  guess, and the alternative -- matching service names like `qemu-1` --
  would happily tear down somebody's unrelated project that happened to
  have a service called `qemu-1`.

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

from .compose import resolve_identity
from .gen_compose import recorded_digest


@dataclass
class Stack:
    project: str
    config_file: Path | None
    working_dir: Path | None
    env_files: list[Path]
    containers: list[str] = field(default_factory=list)
    uids: set[str] = field(default_factory=set)
    # Another compose project on this host uses the same project name.
    shared_name: bool = False

    def owned_by(self, uid: int) -> bool:
        """Does any container run as this uid?

        Any, not all: a stack legitimately mixes uids -- the stats exporters
        are pinned to 65534 and tapsetup/alloy stay root -- so requiring
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
    """By the gen-compose provenance header in its compose file.

    Deliberately NOT a service-name match, nor a directory-name one. `qemu-1`
    and a stack directory's basename are names anyone might use, and this command removes
    containers.
    """
    return config_file is not None and recorded_digest(config_file) is not None


def discover() -> list[Stack]:
    """Every running qemu-tool stack on this host, owned by anyone."""
    # Keyed on the compose file as well as the project name: the name is the
    # directory basename, which any unrelated project can share.
    by_project: dict[tuple[str, str], Stack] = {}
    for rec in _inspect_running():
        labels = (rec.get("Config") or {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        if not project:
            continue
        raw_cfg = (labels.get("com.docker.compose.project.config_files")
                   or "").split(",")[0]
        key = (project, raw_cfg)
        if key not in by_project:
            raw_wd = labels.get("com.docker.compose.project.working_dir") or ""
            raw_env = labels.get(
                "com.docker.compose.project.environment_file") or ""
            by_project[key] = Stack(
                project=project,
                config_file=Path(raw_cfg) if raw_cfg else None,
                working_dir=Path(raw_wd) if raw_wd else None,
                env_files=[Path(p) for p in raw_env.split(",") if p],
            )
        stack = by_project[key]
        stack.containers.append((rec.get("Name") or "").lstrip("/"))
        stack.uids.add((rec.get("Config") or {}).get("User") or "")

    names = [project for project, _ in by_project]
    for (project, _), stack in by_project.items():
        stack.shared_name = names.count(project) > 1
    return sorted(
        (s for s in by_project.values() if _is_qemu_tool_stack(s.config_file)),
        key=lambda s: (s.project, str(s.config_file)),
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
    # Only a stack started by an older qemu-tool records any; this version
    # passes its settings through the environment and so leaves no label.
    for env_file in stack.env_files:
        # Skip one that has moved: compose refuses outright on a missing
        # --env-file, and a stack whose settings were tidied away is exactly
        # the kind this command exists to clean up.
        if env_file.is_file():
            cmd += ["--env-file", str(env_file)]
    # Every profile, or the metrics services are filtered out of
    # the teardown on a compose version that does that.
    cmd += ["--profile", "*", "down"]
    # To compose, another project's containers under the same name ARE this
    # project's orphans, so the flag would remove them.
    if not stack.shared_name:
        cmd.append("--remove-orphans")
    return cmd


def _teardown_env(stack: Stack) -> dict[str, str]:
    """The environment `down` needs just to parse the compose file.

    compose interpolates on `down` exactly as on `up`, so every ${VAR:?} in
    the file has to resolve or nothing is torn down. Those are the two
    identity variables and nothing else; the rest carry a :- default, and a
    teardown does not care what a default resolves to.

    The uid is the one the containers actually run as, taken from the
    containers themselves rather than from this caller: a root-era stack
    being torn down by root needs 0:0, not the ids of whoever is running the
    command.
    """
    env = os.environ.copy()
    observed = next((u for u in sorted(stack.uids) if u), "")
    if observed:
        env.setdefault("CONTAINER_USER", observed)
    else:
        ident, _ = resolve_identity()
        env.setdefault("CONTAINER_USER", ident["CONTAINER_USER"])
    env.setdefault("CONTAINER_KVM_GID", "0")
    return env


def _describe(stack: Stack, uid: int) -> str:
    owner = "you" if stack.owned_by(uid) else \
        "uid " + ", ".join(sorted(u.split(":")[0] or "0" for u in stack.uids))
    where = stack.working_dir or "?"
    return (f"  {stack.project}  ({len(stack.containers)} containers, "
            f"{owner})\n      {where}")


def teardown_one(project: str, dry_run: bool = False,
                 assume_yes: bool = False) -> None:
    """Tear down one named stack, identified by its compose project name."""
    stacks = discover()
    match = next((s for s in stacks if s.project == project), None)
    if match is None:
        sys.exit(f"Error: no running qemu-tool stack named {project!r}.")
    uid = os.getuid()
    print(f"Will tear down stack:")
    print(_describe(match, uid))
    if dry_run:
        print(f"\n--dry-run: nothing was stopped. The command would be:")
        print(f"  {' '.join(_teardown_cmd(match))}")
        return
    if not assume_yes:
        total = len(match.containers)
        try:
            reply = input(
                f"\nStop and remove {total} container(s) in "
                f"{project!r}? [y/N]: ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Nothing was stopped.")
            return
    result = subprocess.run(_teardown_cmd(match), env=_teardown_env(match))
    if result.returncode != 0:
        sys.exit(f"Stack {project!r} did not tear down (exit {result.returncode}).")
    print(f"\nStack {project!r} torn down.")


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
        result = subprocess.run(_teardown_cmd(stack), env=_teardown_env(stack))
        if result.returncode != 0:
            failed += 1
            print(f"    failed (exit {result.returncode}); "
                  f"remove by name with: docker rm -f "
                  f"{' '.join(stack.containers)}", file=sys.stderr)

    if failed:
        sys.exit(f"\n{failed} of {len(mine)} stack(s) did not tear down.")
    print(f"\n{len(mine)} stack(s) torn down.")
