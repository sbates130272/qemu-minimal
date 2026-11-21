"""Tests for the Python API pages of the site (docs/api/).

The pages are built by mkdocstrings when the site is built: each is a few
lines of Markdown and one `::: qemu_tool.<module>` line per module, and the
text comes from the docstrings. Nothing that mkdocs writes is committed, so
what can go wrong without a build is the list: a module added to the package
and put on no page is missing from the site, and nothing says so. These
tests hold the list to the package.

They read files and need neither mkdocs nor griffe. The code under test,
`escape()` and `references()` in .mkdocs/griffe_qemu_tool.py, imports griffe
only when it is there.

What only a build can see is built on every pull request: Gen Compose Check
runs the publish workflow's own `mkdocs build --strict` and publishes
nothing.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "qemu" / "qemu-tool"
API = REPO / "docs" / "api"
CONFIG = REPO / ".mkdocs.yml"
PATHS = REPO / ".mkdocs"
EXTENSION = PATHS / "griffe_qemu_tool.py"
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "qemu-minimal-publish-pages.yml"
PR_WORKFLOW = WORKFLOWS / "qemu-minimal-gen-compose-check.yml"
BUILD = "mkdocs build --config-file .mkdocs.yml --strict"

# Modules that are on no page, each with the reason docs/api/index.md gives.
# A module goes here only when there is nothing in it to show.
EXCLUDED = {
    "qemu_tool": "sets __version__ and nothing else",
    "qemu_tool.__main__": "calls cli.main() and nothing else",
}

# What mkdocstrings takes for a directive: `:::` at the start of a line, with
# or without a space after it, and with or without a heading's `#` in front.
# Its own pattern is `^(?P<heading>#{1,6} *|)::: ?(?P<name>.+?) *...$`
# (mkdocstrings 1.0.8, _internal/extension.py).
DIRECTIVE = re.compile(r"^(?:#{1,6} *)?::: ?(\S+)[ \t]*$", re.M)
# The one form these pages use.
PLAIN = re.compile(r"^::: qemu_tool(?:\.\w+)+$")
# A line that may be a directive, in any spelling: `:::` after nothing but
# the marks that open a heading, a blockquote or a list item, or `:::` and
# the package's name anywhere in the line. Python-Markdown hands the inside
# of a blockquote or a list item to the block processors again, so
# `> ::: qemu_tool.ports` and `- ::: qemu_tool.ports` are rendered (seen in
# a build with mkdocstrings 1.0.8) and DIRECTIVE counts neither. This is
# wider than what is rendered on purpose: a line it matches is either the
# plain form on a page under docs/api/ or a failure.
SUSPECT = re.compile(r"^[#>\s*+\-\d.)]*:::|:::\s*qemu_tool")

# The suffixes mkdocs renders as a page (`markdown_extensions` in
# mkdocs/utils/__init__.py, 1.6.1, matched with str.endswith, so in this
# case only). A file under docs/ with any other suffix is copied as it is.
MARKDOWN = (".markdown", ".mdown", ".mkdn", ".mkd", ".md")


def rendered(directory: Path, below: bool = False) -> list[Path]:
    """The files mkdocs would render as pages in a directory, or below it."""
    found = directory.rglob("*") if below else directory.iterdir()
    return sorted(path for path in found
                  if path.is_file() and path.name.endswith(MARKDOWN))


def modules() -> set[str]:
    """Every module of the package, by the name it is imported under."""
    found = set()
    for path in PACKAGE.rglob("*.py"):
        parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found.add(".".join(["qemu_tool", *parts]))
    return found


def pages() -> dict[str, list[str]]:
    """Each API page and the identifiers it asks mkdocstrings for."""
    return {path.name: DIRECTIVE.findall(path.read_text())
            for path in rendered(API)}


def defined(tree: ast.Module) -> list[str]:
    """The public names a module defines: classes, functions and assignments.

    At module level, which includes the body of a `try` or an `if` there and
    not the body of a function or a class.
    """
    names: list[str] = []

    def target(node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                target(item)

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef,
                                 ast.AsyncFunctionDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                for item in node.targets:
                    target(item)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                target(node.target)
            else:
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []))
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)

    walk(tree.body)
    return [name for name in names if not name.startswith("_")]


def docstrings():
    """Every docstring of the package, with where it is."""
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node)
            if doc:
                where = f"{path.name}:{getattr(node, 'lineno', 1)}"
                yield where, getattr(node, "name", "the module"), doc


def load_extension():
    spec = importlib.util.spec_from_file_location("griffe_qemu_tool",
                                                  EXTENSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Modules(unittest.TestCase):

    def test_the_package_was_found(self) -> None:
        found = modules()
        self.assertIn("qemu_tool", found)
        self.assertIn("qemu_tool.fleet_xml", found)
        self.assertGreater(len(found), 20)

    def test_every_identifier_is_a_module_of_the_package(self) -> None:
        found = modules()
        for page, names in pages().items():
            for name in names:
                self.assertIn(
                    name, found,
                    f"docs/api/{page} asks for {name}, which is not a "
                    "module under qemu/qemu-tool/")

    def test_every_module_is_on_one_page_or_excluded(self) -> None:
        where: dict[str, list[str]] = {}
        for page, names in pages().items():
            for name in names:
                where.setdefault(name, []).append(page)
        for name in sorted(modules()):
            on = where.get(name, [])
            if name in EXCLUDED:
                self.assertEqual(
                    on, [], f"{name} is in EXCLUDED and on {on}; pick one")
                continue
            self.assertEqual(
                len(on), 1,
                f"{name} is on {on or 'no page'} under docs/api/. Add "
                f"`::: {name}` to exactly one page there and to the table "
                "in docs/api/index.md, or, if there is nothing in it to "
                "show, add it to EXCLUDED in this file and to \"Modules "
                "left out\" on that page, with the reason")

    def test_a_directive_is_written_one_way(self) -> None:
        # Any other spelling mkdocstrings renders (`:::name`, `## ::: name`)
        # is counted by pages() above. This keeps a spelling that is rendered
        # and not counted off the pages, one inside a blockquote or a list
        # item included: every line SUSPECT matches is the plain form.
        for path in rendered(API):
            for line in path.read_text().splitlines():
                if SUSPECT.search(line):
                    self.assertRegex(
                        line, PLAIN,
                        f"docs/api/{path.name}: write `::: qemu_tool.<module>` "
                        "on a line of its own, from the first column")

    def test_no_page_outside_docs_api_has_a_directive(self) -> None:
        # pages() reads the pages directly under docs/api/ and nothing
        # else, and mkdocs renders every page under docs/, in the nav or
        # not. A `:::` line on any other page, or in a directory under
        # docs/api/, would put a module on a second page that the count
        # above never sees. So would one inside a blockquote or a list
        # item, which is why the pattern is SUSPECT and not `^:::`. The
        # cost is that no page there can quote a directive in its prose.
        counted = set(rendered(API))
        for path in rendered(REPO / "docs", below=True):
            if path in counted:
                continue
            for line in path.read_text().splitlines():
                self.assertNotRegex(
                    line, SUSPECT,
                    f"{path.relative_to(REPO)}: a mkdocstrings directive "
                    "belongs on a page directly under docs/api/, where "
                    "this test counts it")

    def test_the_pages_are_where_the_test_reads_them(self) -> None:
        self.assertEqual([path for path in API.iterdir() if path.is_dir()],
                         [], "docs/api/ has no directory under it")
        self.assertGreater(len(pages()), 5)

    def test_a_page_is_any_file_mkdocs_renders(self) -> None:
        # Not only *.md: `::: qemu_tool.ports` in docs/more.markdown, or in
        # docs/api/more.mkd, is rendered, and a glob for *.md saw neither.
        with tempfile.TemporaryDirectory() as scratch:
            top = Path(scratch)
            (top / "sub").mkdir()
            names = ["a.md", "b.markdown", "c.mdown", "d.mkdn", "e.mkd"]
            for name in [*names, "f.txt", "g.css", "h.MD", "sub/i.mkd"]:
                (top / name).write_text("::: qemu_tool.ports\n")
            self.assertEqual([path.name for path in rendered(top)], names)
            self.assertEqual(
                [str(path.relative_to(top))
                 for path in rendered(top, below=True)],
                [*names, "sub/i.mkd"])

    def test_the_wider_forms_are_counted(self) -> None:
        text = ("::: qemu_tool.a\n## ::: qemu_tool.b\n:::qemu_tool.c\n"
                "###:::qemu_tool.d  \n ::: qemu_tool.e\ntext ::: qemu_tool.f\n")
        self.assertEqual(DIRECTIVE.findall(text),
                         ["qemu_tool.a", "qemu_tool.b", "qemu_tool.c",
                          "qemu_tool.d"])

    def test_a_directive_inside_a_block_is_suspect(self) -> None:
        # Each of these is rendered by mkdocstrings or close enough to one
        # that is; none is the plain form.
        for line in ("> ::: qemu_tool.ports", "- ::: qemu_tool.ports",
                     "* ::: qemu_tool.ports", "+ :::qemu_tool.ports",
                     "1. ::: qemu_tool.ports", "2) ::: qemu_tool.ports",
                     "> > ## ::: qemu_tool.ports", "    ::: qemu_tool.ports",
                     "> ::: other.module", "## ::: qemu_tool.ports",
                     "see ::: qemu_tool.ports", ":::qemu_tool.ports"):
            self.assertRegex(line, SUSPECT)
            self.assertNotRegex(line, PLAIN)
        self.assertRegex("::: qemu_tool.ports", SUSPECT)
        for line in ("a colon: and another", "| :--- | :---: |",
                     "the `qemu_tool.ports` module", "C++ has std::string"):
            self.assertNotRegex(line, SUSPECT)

    def test_nothing_excluded_has_left_the_package(self) -> None:
        self.assertEqual(set(EXCLUDED) - modules(), set())

    def test_an_excluded_module_has_nothing_to_show(self) -> None:
        # The reason on the index page is that there is no docstring and
        # nothing public is defined: no class, function or constant whose
        # name does not start with an underscore. Imported names do not
        # count, since a page does not list them. A module that gains one
        # belongs on a page.
        for name in EXCLUDED:
            tail = name.split(".")[1:] or ["__init__"]
            path = PACKAGE.joinpath(*tail).with_suffix(".py")
            tree = ast.parse(path.read_text())
            self.assertIsNone(ast.get_docstring(tree), name)
            self.assertEqual(defined(tree), [], name)

    def test_the_counts_in_agents_md_are_the_trees(self) -> None:
        # Four numbers AGENTS.md gives in the present tense. Two of them
        # were left behind by changes that added a function and a setting.
        agents = " ".join((REPO / "AGENTS.md").read_text().split())
        public = bare = 0
        fields = None
        for name in sorted(modules() - set(EXCLUDED)):
            path = PACKAGE.joinpath(*name.split(".")[1:]).with_suffix(".py")
            for node in ast.parse(path.read_text()).body:
                if not isinstance(node, (ast.FunctionDef, ast.ClassDef,
                                         ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                public += 1
                bare += ast.get_docstring(node) is None
                if node.name == "FleetConfig":
                    fields = sum(isinstance(item, ast.AnnAssign)
                                 for item in node.body)
        self.assertIn(f"{bare} of the {public} public top-level functions "
                      "and classes have none", agents)
        self.assertIsNotNone(fields)
        self.assertIn(f"`FleetConfig` {fields} entries long", agents)
        words = "zero one two three four five six seven eight nine ten".split()
        others = len(pages()) - 1
        self.assertIn("index.md", pages())
        self.assertLess(others + 1, len(words),
                        "more API files than this test has number words "
                        "for: add words here, then update AGENTS.md")
        self.assertIn(
            f"holds {words[others + 1]} short Markdown files: an index, and "
            f"{words[others]} pages", agents)

    def test_what_counts_as_defined(self) -> None:
        tree = ast.parse(
            "from a import shown_nowhere\n"
            "__version__ = 1\n_private = 2\n"
            "try:\n    FOUND = 1\nexcept E:\n    FOUND = 2\n"
            "if x:\n    typed: int = 3\n"
            "a, (b, _c) = 1, (2, 3)\n"
            "def f():\n    inner = 1\n"
            "class K:\n    field = 1\n")
        self.assertEqual(defined(tree),
                         ["FOUND", "FOUND", "typed", "a", "b", "f", "K"])


class IndexPage(unittest.TestCase):

    def setUp(self) -> None:
        self.text = (API / "index.md").read_text()

    def test_it_asks_mkdocstrings_for_nothing(self) -> None:
        self.assertEqual(pages()["index.md"], [])

    def test_the_table_names_each_page_and_its_modules(self) -> None:
        for page, names in pages().items():
            if page == "index.md":
                continue
            rows = [line for line in self.text.splitlines()
                    if f"]({page})" in line and line.startswith("|")]
            self.assertEqual(len(rows), 1, f"one table row for {page}")
            listed = re.findall(r"`(\w+)`", rows[0].split("|")[2])
            self.assertEqual(
                [f"qemu_tool.{name}" for name in listed], names,
                f"the row for {page} lists the modules the page shows, "
                "in the page's order")

    def test_every_excluded_module_is_named_with_a_reason(self) -> None:
        section = self.text.split("## Modules left out", 1)[1]
        for name in EXCLUDED:
            rows = [line for line in section.splitlines()
                    if line.startswith(f"| `{name}`")]
            self.assertEqual(len(rows), 1, f"a row for {name}")
            self.assertGreater(len(rows[0].split("|")[2].strip()), 20)
        named = re.findall(r"(?m)^\| `(qemu_tool[\w.]*)`", section)
        self.assertEqual(sorted(named), sorted(EXCLUDED))

    def test_it_says_this_is_no_library_interface(self) -> None:
        flat = " ".join(self.text.split())
        self.assertIn("is a command", flat)
        self.assertIn("nothing here is a library interface", flat)


class Config(unittest.TestCase):

    def setUp(self) -> None:
        self.text = CONFIG.read_text()

    def test_every_page_is_in_the_nav_once(self) -> None:
        for page in pages():
            self.assertEqual(
                len(re.findall(rf"(?m)^      - (?:.*: )?api/{page}$",
                               self.text)), 1,
                f"docs/api/{page} is under \"Python API\" in the nav once")

    def test_the_nav_names_no_page_that_is_not_there(self) -> None:
        for page in re.findall(r"api/([\w-]+\.md)", self.text):
            self.assertTrue((API / page).is_file(), page)

    def test_the_package_is_found_through_the_symlink(self) -> None:
        # griffe finds a package by its directory's name, and the
        # directory is qemu-tool. No install: the build reads the checkout.
        self.assertIn("paths: [.mkdocs]", self.text)
        link = PATHS / "qemu_tool"
        self.assertTrue(link.is_symlink(), ".mkdocs/qemu_tool is a symlink")
        target = link.readlink()
        self.assertFalse(target.is_absolute(),
                         "a relative link, so it works in any checkout")
        self.assertEqual(link.resolve(), PACKAGE.resolve())

    def test_the_options(self) -> None:
        for line in ("docstring_style: null",
                     "- .mkdocs/griffe_qemu_tool.py",
                     "show_source: true",
                     "members_order: source",
                     'filters: ["!^_"]',
                     # Without it a module whose public functions have no
                     # docstring shows no member at all.
                     "show_if_no_docstring: true",
                     "show_signature_annotations: true",
                     "separate_signature: true"):
            self.assertRegex(self.text, rf"(?m)^ +{re.escape(line)}$")

    def test_the_source_of_a_member_is_kept_out_of_the_search(self) -> None:
        # With show_source on, the folded source of every function and
        # class is page text, and the search plugin of Material indexed
        # it: three quarters of the index was source. Two templates wrap
        # mkdocstrings' own source block in an element that plugin skips.
        self.assertRegex(self.text,
                         r"(?m)^      custom_templates: \.mkdocs/templates$")
        templates = PATHS / "templates" / "python" / "material"
        self.assertEqual(sorted(path.name for path in templates.iterdir()),
                         ["class.html.jinja", "function.html.jinja"])
        for kind in ("class", "function"):
            text = (templates / f"{kind}.html.jinja").read_text()
            body = text.split("-#}", 1)[1]
            self.assertIn(f'{{% extends "_base/{kind}.html.jinja" %}}', body)
            block = body.split("{% block source scoped %}", 1)[1]
            block = block.split("{% endblock source %}", 1)[0]
            # Not a <div>: the plugin compares elements by tag name, so
            # the first <div> closing inside the block ends the exclusion.
            self.assertEqual(block.split(),
                             ["<section", "data-search-exclude>",
                              "{{", "super()", "}}", "</section>"])

    def test_the_edit_link_names_the_branch_that_exists(self) -> None:
        # With no edit_uri, MkDocs derives edit/master/docs/ from a GitHub
        # repo_url (EditURI in mkdocs/config/config_options.py, 1.6.1), and
        # the repository's branch is main.
        self.assertRegex(self.text, r"(?m)^edit_uri: edit/main/docs/$")


class Workflow(unittest.TestCase):

    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()

    def test_it_installs_mkdocstrings_and_not_qemu_tool(self) -> None:
        installs = re.findall(r"(?m)^\s+run: (pip install .*)$", self.text)
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0].split()[2:],
                         ["mkdocs-material", "'mkdocstrings[python]'",
                          "ruff"])
        self.assertNotIn("./qemu", installs[0])
        self.assertNotIn("==", installs[0],
                         "the workflow pins no version of these")

    def test_a_change_to_the_package_builds_the_site(self) -> None:
        paths = self.text.split("    paths:\n", 1)[1].split("workflow_dispatch", 1)[0]
        filters = re.findall(r"(?m)^\s+- '([^']+)'$", paths)
        self.assertIn("qemu/qemu-tool/**.py", filters)
        self.assertIn(".mkdocs/**", filters)
        self.assertIn("docs/**", filters)

    def test_the_build_is_strict(self) -> None:
        self.assertRegex(self.text, rf"(?m)^\s+run: {re.escape(BUILD)}$")

    def test_the_published_branch_has_a_nojekyll(self) -> None:
        # GitHub Pages runs Jekyll over the gh-pages branch unless its root
        # has a .nojekyll, and Jekyll leaves out every path that starts
        # with an underscore: assets/_mkdocstrings.css, the stylesheet of
        # the API pages. `mkdocs build` writes no .nojekyll, so the publish
        # step does. It has to come after the rsync, whose --delete removes
        # whatever site/ does not have, and before the commit, inside the
        # function every publish attempt runs.
        body = self.text.split("          publish() {\n", 1)[1]
        body = body.split("\n          }\n", 1)[0]
        lines = [line.strip() for line in body.splitlines()]
        sync = [n for n, line in enumerate(lines)
                if line.startswith("rsync ") and "--delete" in line
                and '"../site/" ./' in line]
        touch = [n for n, line in enumerate(lines)
                 if line == "touch .nojekyll"]
        add = [n for n, line in enumerate(lines) if line == "git add -A"]
        self.assertEqual((len(sync), len(touch), len(add)), (1, 1, 1))
        self.assertLess(sync[0], touch[0])
        self.assertLess(touch[0], add[0])
        # Nothing after it removes the file again.
        for line in lines[touch[0] + 1:]:
            self.assertNotRegex(line, r"^(rsync|rm|git (rm|clean|checkout)) ")

    def test_nothing_says_the_build_writes_the_nojekyll(self) -> None:
        # It was said in the workflow and in AGENTS.md, and it is true of
        # `mkdocs gh-deploy` only, which nothing here runs.
        for path in (WORKFLOW, REPO / "AGENTS.md"):
            flat = " ".join(path.read_text().split())
            self.assertIsNone(
                re.search(r"MkDocs adds `?\.nojekyll", flat),
                f"{path.relative_to(REPO)} says MkDocs adds .nojekyll")


class PullRequest(unittest.TestCase):
    """The site is built, strictly, before a change reaches main.

    The publish workflow runs on main only, and a docstring is page text:
    one the build warns about passes every unit test here and then fails
    the job that also publishes the performance page. Gen Compose Check
    runs on every pull request, so the same build is a step of it.
    """

    def setUp(self) -> None:
        self.text = PR_WORKFLOW.read_text()

    def test_it_runs_on_every_pull_request(self) -> None:
        triggers = self.text.split("\non:\n", 1)[1].split("\njobs:", 1)[0]
        self.assertIn("  pull_request:", triggers)
        self.assertNotRegex(triggers, r"(?m)^\s+paths(-ignore)?:")

    def test_it_installs_what_the_publish_workflow_installs(self) -> None:
        publish = re.findall(r"(?m)^\s+run: pip install (.*)$",
                             WORKFLOW.read_text())
        here = re.findall(
            r"(?m)^\s+python3 -m pip install --user --quiet (mkdocs-.*)$",
            self.text)
        self.assertEqual(len(publish), 1)
        self.assertEqual(here, publish)

    def test_it_runs_the_same_build_and_publishes_nothing(self) -> None:
        builds = re.findall(r"(?m)^\s+python3 -m (mkdocs build .*)$",
                            self.text)
        self.assertEqual(builds, [BUILD + ' --site-dir "$RUNNER_TEMP/site"'])
        self.assertIn(BUILD, WORKFLOW.read_text())
        self.assertNotIn("gh-pages", self.text)
        self.assertIn("permissions:\n  contents: read\n", self.text)


class Escape(unittest.TestCase):
    """`<` in a docstring reaches the page as a character, not as a tag."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.escape = staticmethod(load_extension().escape)

    def test_an_element_name_in_prose(self) -> None:
        self.assertEqual(self.escape("derived from <vm-count> and"),
                         "derived from &lt;vm-count> and")

    def test_an_arrow(self) -> None:
        self.assertEqual(self.escape("XML <-> VMConfig"),
                         "XML &lt;-> VMConfig")

    def test_a_code_span_is_left_alone(self) -> None:
        text = "(`<eth-queues>4</eth-queues>` is ``--x <y>``) and <z>"
        self.assertEqual(
            self.escape(text),
            "(`<eth-queues>4</eth-queues>` is ``--x <y>``) and &lt;z>")

    def test_a_fenced_block_is_left_alone(self) -> None:
        text = "before <a>\n```\n<fleet>\n```\nafter <b>"
        self.assertEqual(self.escape(text),
                         "before &lt;a>\n```\n<fleet>\n```\nafter &lt;b>")

    def test_an_indented_block_is_left_alone(self) -> None:
        text = ("Flags:\n\n    --state <vm>    show\n\n    --clean <vm>\n"
                "then <x>")
        self.assertEqual(
            self.escape(text),
            "Flags:\n\n    --state <vm>    show\n\n    --clean <vm>\n"
            "then &lt;x>")

    def test_an_indented_line_that_continues_a_list_item_is_prose(self) -> None:
        text = "* the account\n    (x). <vm-username>ubuntu\n\n    and <y>"
        self.assertEqual(
            self.escape(text),
            "* the account\n    (x). &lt;vm-username>ubuntu\n\n"
            "    and &lt;y>")

    def test_an_indented_line_inside_a_paragraph_is_prose(self) -> None:
        self.assertEqual(self.escape("a line\n    and <b> under it"),
                         "a line\n    and &lt;b> under it")

    def test_text_with_no_angle_bracket_is_unchanged(self) -> None:
        text = "Plain *text* with `code`.\n\n    block\n"
        self.assertEqual(self.escape(text), text)

    def test_every_docstring_of_the_package_survives(self) -> None:
        # Not a property of escape() alone: a docstring that names an
        # element only inside a block Markdown takes for code keeps its
        # `<`, and one in prose loses every one of them.
        seen = 0
        for where, _name, doc in docstrings():
            seen += 1
            out = self.escape(doc)
            self.assertEqual(out.replace("&lt;", "<"), doc)
            for before, after in zip(doc.split("\n"), out.split("\n")):
                if after != before:
                    self.assertNotRegex(
                        re.sub(r"(`+)(?:(?!\1).)+?\1", "", after),
                        r"<", f"{where}: {before!r}")
        self.assertGreater(seen, 200)


class References(unittest.TestCase):
    """`[a][b]` in a docstring is a link to the build, and a broken one.

    mkdocs-autorefs reads `[title][identifier]` and `[identifier][]` in
    rendered Markdown as a cross-reference, warns when the identifier names
    nothing, and the build is strict. Prose such as x[i][j] is one. The
    pull request build would stop it too; this says which docstring.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.references = staticmethod(load_extension().references)

    def test_what_is_one(self) -> None:
        self.assertEqual(
            self.references("See [the table][settings-table], x[i][j] "
                            "and [k][]."),
            ["[the table][settings-table]", "[i][j]", "[k][]"])

    def test_what_is_not(self) -> None:
        text = ("a [word], a [link](https://example.com), `x[i][j]` and\n"
                "\n    table[row][column]\n\n```\ny[0][1]\n```\n"
                "and argv[1:] then [2]")
        self.assertEqual(self.references(text), [])

    def test_one_with_a_code_span_as_its_text(self) -> None:
        self.assertEqual(len(self.references("see [`escape()`][escape]")), 1)

    def test_no_docstring_of_the_package_has_one(self) -> None:
        for where, name, doc in docstrings():
            self.assertEqual(
                self.references(doc), [],
                f"qemu/qemu-tool/{where}, the docstring of {name}: the "
                "site build reads this as a cross-reference, finds no "
                "target and fails. Put it in backticks")


if __name__ == "__main__":
    unittest.main()
