"""Locate data files that ship inside the installed package.

qemu-tool has three install shapes and each puts its data somewhere different:
the .deb writes /usr/share/qemu-tool, a source checkout has qemu/ alongside
this file, and `pipx install qemu-tool` has neither -- only the wheel. The
wheel carries the compose stacks, the package manifests and env.example as
package data (see [tool.setuptools.package-dir] in pyproject.toml, which maps
them in place rather than duplicating them under the package directory).

Callers use this as the *last* resort, after the system path and the checkout,
so an admin editing /usr/share/qemu-tool still wins over the frozen copy.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

# Named to avoid qemu_tool.compose, which would shadow -- and be shadowed by
# -- the compose module. importlib.resources resolves the module first and
# hands back its parent directory, which fails silently rather than loudly.
SHARE = "qemu_tool.share"
COMPOSE = "qemu_tool.share.compose"
PACKAGES_D = "qemu_tool.share.packages_d"


def packaged_path(package: str, *parts: str) -> Path | None:
    """Return a real filesystem path inside `package`, or None.

    Returns None rather than raising: a missing resource means "fall through
    to the next lookup", and every caller already has a better error to give.
    importlib.resources can hand back a non-filesystem Traversable (a zip
    import), which is no use to us -- compose needs a directory to run docker
    in, and gen-vm reads its manifest by path -- so anything that is not a
    real path is treated as absent too. Neither pipx nor dpkg zip-imports, so
    this is a guard, not a code path we expect to take.
    """
    try:
        target = files(package)
    except (ImportError, ModuleNotFoundError, TypeError):
        return None
    for part in parts:
        target = target / part
    try:
        path = Path(str(target))
    except TypeError:
        return None
    return path if path.exists() else None
