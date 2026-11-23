"""The griffe extension the API pages of the site are built with.

mkdocstrings builds docs/api/ from the docstrings of qemu_tool, and griffe is
what reads them. This extension changes two things about what griffe hands
on, and no source file:

`<` in a docstring. mkdocstrings renders a docstring as Markdown. The
docstrings here are prose that names fleet XML elements as they are written,
<vm-count>, and Markdown passes an unknown tag straight through to the page,
where a browser drops it. Every `<` is rewritten as `&lt;`, except in what
Markdown takes for code and already keeps the character in: a code span, a
fenced code block, and a block indented by four spaces after a blank line.

The path under "Source code in". griffe finds the package through the
symlink .mkdocs/qemu_tool, so it would name every file as
.mkdocs/qemu_tool/<module>.py. Each module's path is resolved, so the page
names the file a reader can open: qemu/qemu-tool/<module>.py.

Loaded from .mkdocs.yml (plugins > mkdocstrings > handlers > python >
options > extensions). tests/test_api_docs.py runs `escape()` and
`references()` with no griffe installed, so the import below is allowed to
fail there.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import griffe
except ImportError:  # the unit tests need escape() and references() only
    griffe = None

_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def rewrite(text: str, prose, code=None) -> str:
    """Return *text* with `prose` applied to every stretch that is not code.

    Code is what Markdown takes for code: a span in backticks, a fenced
    block, and a block indented by four spaces or more that follows a blank
    line. An indented line that continues a list item is prose, so a list
    lasts until the next line that starts in the first column and is not an
    item of it. `prose` is called with each stretch of a line outside code
    and returns what replaces it. `code`, when given, is called the same
    way with each code span and each line of a code block.
    """
    if code is None:
        code = str
    out = []
    fenced = False
    in_list = False
    block = False       # inside an indented code block
    blank = True        # the line before this one was blank
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(code(line))
            continue
        if fenced:
            out.append(code(line))
            continue
        if not line.strip():
            blank = True
            out.append(line)
            continue
        indented = line.startswith("    ")
        if _LIST_ITEM.match(line):
            in_list, block = True, False
        elif not indented and line[0] != " ":
            in_list, block = False, False
        elif indented and not in_list and (blank or block):
            block = True
        elif not indented:
            block = False
        blank = False
        if block:
            out.append(code(line))
            continue
        parts = []
        last = 0
        for span in _CODE_SPAN.finditer(line):
            parts.append(prose(line[last:span.start()]))
            parts.append(code(span.group(0)))
            last = span.end()
        parts.append(prose(line[last:]))
        out.append("".join(parts))
    return "\n".join(out)


def escape(text: str) -> str:
    """Return *text* with every `<` outside code written as `&lt;`."""
    return rewrite(text, lambda part: part.replace("<", "&lt;"))


# What mkdocs-autorefs takes for a cross-reference: `[title][identifier]`
# and `[identifier][]`. The site build is strict and a reference to nothing
# is a warning, so one of these in a docstring fails the build.
_REFERENCE = re.compile(r"\[[^\]\[]*\]\[[^\]\[]*\]")


def references(text: str) -> list[str]:
    """Every `[a][b]` in *text* outside code, which the build reads as a link.

    Prose such as x[i][j] is one. Put it in backticks.
    """
    return _REFERENCE.findall(rewrite(text, str, lambda part: "`"))


if griffe is not None:

    class QemuTool(griffe.Extension):
        """Escape `<` in docstrings and name source files by their real path."""

        def on_instance(self, *, obj: griffe.Object, **kwargs: object) -> None:
            if obj.docstring is not None:
                obj.docstring.value = escape(obj.docstring.value)

        def on_package(
            self, *, pkg: griffe.Module, loader: griffe.GriffeLoader,
            **kwargs: object,
        ) -> None:
            modules = [pkg]
            while modules:
                module = modules.pop()
                if module.is_alias:
                    continue
                modules.extend(module.modules.values())
                # griffe has no public setter for a module's path. Without
                # the attribute the page shows the symlink's path, which is
                # wrong only in what it prints.
                path = getattr(module, "_filepath", None)
                if not isinstance(path, Path) or path not in loader.lines_collection:
                    continue
                real = path.resolve()
                # The source shown under each member is looked up by path.
                loader.lines_collection[real] = loader.lines_collection[path]
                module._filepath = real
