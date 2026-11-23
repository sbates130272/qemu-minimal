# Python API

`qemu-tool` is a command. It is installed and run, not imported, and nothing
here is a library interface: no module, function or signature on these pages
is promised to stay as it is from one release to the next. The promises are
the command line (`qemu-tool --help`, the man page) and the
[fleet XML format](../fleet-xml.md).

These pages are for people working on the code. They are built from the
docstrings of the `qemu_tool` package when the site is built, by
[mkdocstrings](https://mkdocstrings.github.io/), and nothing on them is
committed: what they say is what the source at that commit says.

What each page shows for a module:

- the module's own docstring, where it has one;
- every public class, function and constant, in the order the source has
  them, with its signature and annotations, its docstring where it has one,
  and its source folded under it;
- nothing whose name starts with an underscore. Much of the code is private
  helpers, and for those the source is the reference.

The package's source is the directory
[`qemu/qemu-tool/`](https://github.com/sbates130272/qemu-minimal/tree/main/qemu/qemu-tool),
with a hyphen. It is imported as `qemu_tool`, with an underscore, through
`package-dir` in `qemu/pyproject.toml`.

## Pages

| Page | Modules |
| --- | --- |
| [Settings and fleet XML](settings.md) | `settings`, `fleet_xml`, `fleet_schema` |
| [Stack generation](stacks.md) | `gen_compose`, `compose`, `ports`, `resources` |
| [Guests](guests.md) | `config`, `gen_vm`, `run_vm`, `caps`, `identity`, `qcow`, `libvirt_xml` |
| [Quick start](quick-start.md) | `quick_start` |
| [Inspection](inspection.md) | `list_vms`, `inspect`, `summary`, `check_system`, `ernic_stats` |
| [Teardown](teardown.md) | `shutdown`, `down_all` |
| [Publishing](publishing.md) | `vm_artifact`, `publish_qcow`, `publish_prune` |
| [Command line](cli.md) | `cli` |

## Modules left out

Two modules are on no page, because there is nothing in them to show:

| Module | Why |
| --- | --- |
| `qemu_tool` (`__init__.py`) | Six lines that set `__version__` from the installed package's metadata. No docstring, and it defines nothing public: `__version__` starts with an underscore, and a page does not list the names a module imports. |
| `qemu_tool.__main__` | Four lines that call `cli.main()`, so that `python3 -m qemu_tool` works. No docstring, and it defines nothing: `main` is imported from `cli`, where it is shown. |

`tests/test_api_docs.py` fails when a module of the package is on no page and
not in that table, or on more than one page, so a new module cannot be
forgotten. It also fails when one of these two gains a docstring or a public
class, function or constant of its own, which is when it belongs on a page.
