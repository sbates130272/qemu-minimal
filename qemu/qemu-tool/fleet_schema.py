"""The fleet XML schema and its reference page, generated from the parser.

Nothing here is a second description of the format. render_rng(),
render_xsd() and render_markdown() read the tables in fleet_xml.py (the
settings and their sections, the server types, their options and backends,
the value forms and FleetConfig's defaults) and write a RELAX NG schema, an
XML Schema (XSD 1.0) and a Markdown page. The committed copies are
qemu/schema/fleet.rng, qemu/schema/fleet.xsd and docs/fleet-xml.md, and
tests/test_fleet_schema.py fails when one is not what these return.

The parser is the authority. The RELAX NG schema is held to one rule: it
never rejects a document the parser accepts. Where a grammar can say what
the parser enforces it does, and where it cannot it takes more, never less.
What it reads off the parser's CODE and not off a table, the lexical form of
each kind in _convert() and the checks in _read_devices(), is spelled once
below, and the differential test runs both over the same documents.

The XSD is held to the same rule one step further out: it never rejects a
document the RELAX NG schema accepts, and is otherwise as strict as XSD 1.0
can be. It uses the same patterns. xsd_looser() is the list of where it
takes more than the RELAX NG schema, and the test has a document for each.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import textwrap

from . import fleet_xml as fx
from .config import DEFAULT_ERNIC_MTU, MAX_ERNIC_MTU, NVME_BLOCK_SIZES

RNG_NS = "http://relaxng.org/ns/structure/1.0"
ANNOTATION_NS = "http://relaxng.org/ns/compatibility/annotations/1.0"
XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# Where the committed schema is, in a checkout and in the Debian package.
RNG_CHECKOUT_PATH = "qemu/schema/fleet.rng"
RNG_INSTALLED_PATH = "/usr/share/qemu-tool/schema/fleet.rng"
XSD_CHECKOUT_PATH = "qemu/schema/fleet.xsd"
XSD_INSTALLED_PATH = "/usr/share/qemu-tool/schema/fleet.xsd"
PAGE_CHECKOUT_PATH = "docs/fleet-xml.md"
_REPO_URL = "https://github.com/sbates130272/qemu-minimal"

# ---------------------------------------------------------------------------
# Lexical forms, as XSD regular expressions
# ---------------------------------------------------------------------------

# The characters str.strip() and int() treat as white space, of those an
# XML 1.0 document can hold. The parser strips with Python's rules, which
# take more than XML's four, so a pattern that allowed only those four
# would refuse text the parser accepts.
SPACE_RANGES: tuple[tuple[int, int], ...] = (
    (0x09, 0x0A), (0x0D, 0x0D), (0x20, 0x20), (0x85, 0x85), (0xA0, 0xA0),
    (0x1680, 0x1680), (0x2000, 0x200A), (0x2028, 0x2029), (0x202F, 0x202F),
    (0x205F, 0x205F), (0x3000, 0x3000),
)
# What _read_devices refuses inside an option's value once it is stripped:
# the control characters XML can carry, DEL and the Unicode line breaks.
_CONTROL_RANGES: tuple[tuple[int, int], ...] = (
    (0x09, 0x0A), (0x0D, 0x0D), (0x7F, 0x7F), (0x85, 0x85), (0x2028, 0x2029),
)


def _class_body(ranges: tuple[tuple[int, int], ...]) -> str:
    """The inside of a character class for these code point ranges."""
    names = {0x09: "\\t", 0x0A: "\\n", 0x0D: "\\r"}
    out = []
    for low, high in ranges:
        if high - low < 2:
            out += [names.get(cp, chr(cp)) for cp in range(low, high + 1)]
        else:
            out.append(f"{chr(low)}-{chr(high)}")
    return "".join(out)


_SP = _class_body(SPACE_RANGES)
_WS = f"[{_SP}]*"                        # what strip() removes
_NOT_SPACE = f"[^{_SP}]"
_WORD_END = f"[^{_SP}{chr(0x7F)}]"       # first and last character of a value
_WORD_MID = f"[^{_class_body(_CONTROL_RANGES)}]"


def _any_case(word: str) -> str:
    return "".join(
        f"[{c.upper()}{c.lower()}]" if c.isalpha() else c for c in word)


def _stripped(body: str) -> str:
    """A pattern for text the parser strips before it looks at it."""
    return f"{_WS}({body}){_WS}"


def _xsd_regex(python: str) -> str:
    """One of the parser's regular expressions as an XSD one.

    The parser's forms use a small common subset; the one difference is
    that XSD wants a literal hyphen in a class escaped.
    """
    body = python[:-2] if python.endswith(r"\Z") else python
    if not re.fullmatch(r"[A-Za-z0-9_.:,/=\-\[\]{}+*]*", body):
        raise ValueError(
            f"{python!r} uses regular expression syntax fleet_schema does "
            "not translate; teach _xsd_regex about it")
    return body.replace("-]", "\\-]")


_PORT_NUMBER = (
    "0*([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}"
    "|655[0-2][0-9]|6553[0-5])")
# An address is what Python's ipaddress module takes, which the parser
# hands the text to. IPv4: four decimal octets of 0 to 255 with no leading
# zero. IPv6: eight groups of one to four hexadecimal digits, the last two
# of which may be written as an IPv4 address, and at most one "::" standing
# for one or more groups of zeros.
_OCTET = "(0|[1-9][0-9]?|1[0-9][0-9]|2[0-4][0-9]|25[0-5])"
_IPV4 = "{0}\\.{0}\\.{0}\\.{0}".format(_OCTET)
_HEX = "[0-9A-Fa-f]"


def _repeat(body: str, low: int, high: int) -> str:
    """`body` from low to high times, written out.

    Not {low,high}: with [0-9A-Fa-f]{1,4} for a group in this pattern,
    libxml2 2.14.6 took a group of five hexadecimal digits. Written out
    there is nothing for a validator to count.
    """
    optional = ""
    for _ in range(high - low):
        optional = f"({body}{optional})?"
    return body * low + optional


_HEXTET = _repeat(_HEX, 1, 4)


def _ipv6() -> str:
    group = f"{_HEXTET}:"
    more = f":{_HEXTET}"
    forms = [_repeat(group, 7, 7) + _HEXTET, _repeat(group, 6, 6) + _IPV4]
    for left in range(8):
        # `left` groups, "::", then what is left of seven: groups, or
        # groups and an IPv4 address, which counts as two.
        head = _HEXTET + _repeat(more, left - 1, left - 1) if left else ""
        tails = []
        if left <= 6:
            tails.append(f"({_HEXTET}{_repeat(more, 0, 6 - left)})?")
        if left <= 5:
            tails.append(_repeat(group, 0, 5 - left) + _IPV4)
        forms.append(
            f"{head}::({'|'.join(tails)})" if tails else f"{head}::")
    return "(" + "|".join(forms) + ")"


_IPV6 = _ipv6()


def _decimal_range(low: int, high: int) -> str:
    """Every number from low to high, of one width, with no leading zero.

    Alternatives of fixed digits and digit classes and no counted
    repetition, so there is nothing for a validator to count.
    """
    lo, hi = str(low), str(high)
    if len(lo) != len(hi) or lo[0] == "0" or low > high:
        raise ValueError(f"_decimal_range({low}, {high}): one width, in order")

    def cls(a: int, b: int) -> str:
        return str(a) if a == b else f"[{a}-{b}]"

    def span(lo: str, hi: str) -> list[str]:
        if not lo:
            return [""]
        a, b = int(lo[0]), int(hi[0])
        rest = len(lo) - 1
        if a == b:
            return [lo[0] + tail for tail in span(lo[1:], hi[1:])]
        out = []
        if lo[1:] == "0" * rest:
            first = a
        else:
            out += [lo[0] + tail for tail in span(lo[1:], "9" * rest)]
            first = a + 1
        if hi[1:] == "9" * rest:
            last = b
        else:
            last = b - 1
        if first <= last:
            out.append(cls(first, last) + "[0-9]" * rest)
        if last < b:
            out += [hi[0] + tail for tail in span("0" * rest, hi[1:])]
        return out

    return "|".join(span(lo, hi))

# What Python's int() takes as a digit is Unicode's decimal digits, and
# which characters those are depends on the Unicode version: Python's, and
# the validator's for \d. libxml2 knows fewer than half of Python's. So a
# digit here is an ASCII digit or ANY character outside ASCII that is not
# white space: no validator's table can then refuse a digit the parser
# takes. A non-ASCII letter where a digit goes passes the schema.
# One character class and not "[0-9]|[^...]": java.util.regex, which jing
# uses, recurses once for each repetition of a group, and overflowed its
# stack on a 2000-digit integer that int() takes. The underscore group of
# the int pattern is still a repeated group, and no pattern without one says
# "single underscores between digits": jing overflows on 2000 underscores
# in one integer and takes 1000. The page says so; the pattern stays exact.
_DIGIT = "[^{}]".format(_class_body(
    ((0x09, 0x2F), (0x3A, 0x7F))
    + tuple(r for r in SPACE_RANGES if r[0] > 0x7F)))
# What split_args() refuses anywhere in a command line.
_ARGS_REFUSED = ((0x7F, 0x7F), (0x85, 0x85), (0x2028, 0x2029))

# One character of a CPU vendor: U+0020 to U+007E without the comma.
_VENDOR_CHAR = "[ -+.-~\\-]"

# kind -> the pattern _convert() accepts, or None where it takes any text.
# bool: lower() and a comparison, no strip. int: Python's int(), which
# strips, takes a sign and takes single underscores between digits. port:
# ports.parse_ssh_port. addr: ports.publish_address. args: split_args(),
# of which a grammar can say the characters and not the quoting.
_INT_BODY = f"[+\\-]?{_DIGIT}+(_{_DIGIT}+)*"

KIND_PATTERNS: dict[str, str | None] = {
    "str": None,
    "expand": None,
    "args": f"[^{_class_body(_ARGS_REFUSED)}]*",
    "bool": "|".join(
        _any_case(word) for word in ("true", "1", "yes", "false", "0", "no")),
    "int": _stripped(_INT_BODY),
    # _convert splits on commas and hands each part to int(), which strips
    # it: the int form, once or more, with a comma between.
    "ints": f"{_stripped(_INT_BODY)}(,{_stripped(_INT_BODY)})*",
    "port": _stripped(f"{_any_case('auto')}|{_PORT_NUMBER}"),
    "addr": _stripped(
        f"({_IPV4}|{_IPV6}|\\[({_IPV4}|{_IPV6})\\])?"),
    # identity.cpu_vendor_problem: nothing, or twelve characters of
    # printable ASCII less the comma. Written out, not {12}: see _repeat.
    "vendor": f"({_repeat(_VENDOR_CHAR, 12, 12)})?",
    # config.nvme_block_size: one of NVME_BLOCK_SIZES in ASCII digits,
    # stripped, and nothing looser (no sign, no underscore, no leading zero).
    "blocksize": _stripped("|".join(str(s) for s in NVME_BLOCK_SIZES)),
    # config.ernic_mtu: four ASCII digits from DEFAULT_ERNIC_MTU to
    # MAX_ERNIC_MTU, stripped. Written out digit by digit: see _repeat.
    "mtu": _stripped(_decimal_range(DEFAULT_ERNIC_MTU, MAX_ERNIC_MTU)),
}

# The same, for a reader: (name on the page, what the parser accepts, what
# the schema checks of it).
KIND_HELP: dict[str, tuple[str, str, str]] = {
    "str": (
        "string",
        "Any text, kept as written, surrounding white space included. An "
        "empty element is the empty string.",
        "Nothing: any text."),
    "expand": (
        "path",
        "Any text. `~` and `$VARS` are expanded when the file is parsed, so "
        "every command sees the same path.",
        "Nothing: any text."),
    "args": (
        "command-line words",
        "Words as a shell would split them: quotes group and are removed, "
        "nothing is expanded. An unclosed quote, a control character and a "
        "word that spans lines are errors.",
        "That it holds no control character (U+007F, U+0085, U+2028 or "
        "U+2029). Quoting is not something a grammar can check: an unclosed "
        "quote, a trailing backslash and a quoted word that spans lines pass "
        "the schema."),
    "bool": (
        "boolean",
        "`true`, `false`, `1`, `0`, `yes` or `no`, in any letter case, with "
        "no white space round it. An empty element is an error.",
        "The same six words in any case, exactly."),
    "int": (
        "integer",
        "What Python's `int()` takes in base 10: an optional sign, decimal "
        "digits, single underscores between digits, white space round the "
        "whole. An empty element is an error. No range is checked here; the "
        "command that reads the value may check one.",
        "The same form, with one difference: any character outside ASCII "
        "that is not white space passes as a digit. Which characters are "
        "decimal digits depends on a Unicode version, and a validator's is "
        "not Python's, so the schema does not ask. Nor does it count: an "
        "integer of more digits than `int()` converts (4300 unless Python "
        "is told otherwise) passes the schema and not the parser. An "
        "integer with thousands of underscores can stop a Java validator; "
        "see [below](#what-the-schema-cannot-check)."),
    "ints": (
        "integer for each guest",
        "One integer, which every guest gets, or integers separated by "
        "commas, one for each guest in order. Each is what Python's `int()` "
        "takes, as for an integer, white space round it included. An empty "
        "element and an empty item are errors. That a list has exactly "
        "`<vm-count>` items is the generator's to check, not the parser's.",
        "The integer form, once or more, with commas between. The count of "
        "items is not checked, and what the integer form lets through is "
        "let through here."),
    "port": (
        "port",
        "A port number from 1 to 65535 in ASCII digits, or the word `auto` "
        "in any case; white space round it is ignored.",
        "The same form, range included. A number of more digits than "
        "Python's `int()` converts, which takes thousands of leading zeros, "
        "passes the schema and not the parser."),
    "addr": (
        "address",
        "An IPv4 or IPv6 literal, with or without square brackets, with no "
        "zone and no host name. Empty, or white space alone, is the default.",
        "The same form: octets of 0 to 255 with no leading zero, groups of "
        "one to four hexadecimal digits, one `::` at most, brackets in "
        "pairs."),
    "vendor": (
        "CPU vendor",
        "Exactly 12 printable ASCII characters (space to `~`) with no comma, "
        "such as `AuthenticAMD` or `GenuineIntel`, kept as written with no "
        "white space stripped; or an empty element, which is a value of its "
        "own (state no vendor) and not the default.",
        "The same form, exactly."),
    "blocksize": (
        "block size",
        "`" + "` or `".join(str(s) for s in NVME_BLOCK_SIZES) + "`, a "
        "number of bytes in ASCII digits; white space round it is ignored. "
        "No sign, no underscore, no leading zero and no unit. An empty "
        "element is an error.",
        "The same form, exactly."),
    "mtu": (
        "MTU",
        f"A number of bytes from {DEFAULT_ERNIC_MTU} to {MAX_ERNIC_MTU} in "
        "four ASCII digits; white space round it is ignored. No sign, no "
        "underscore, no leading zero and no unit. An empty element is an "
        "error.",
        "The same form, range included."),
}

# ---------------------------------------------------------------------------
# Shared reads of the parser's tables
# ---------------------------------------------------------------------------

_DEFAULTS = {f.name: f.default for f in dataclasses.fields(fx.FleetConfig)}


def _default_text(setting: fx._Setting) -> str:
    value = _DEFAULTS[setting.field]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return ",".join(str(v) for v in value)
    return str(value)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _doc(help_text: str, form: str) -> str:
    return " ".join([_sentence(help_text), _sentence(form)])


def _server_children(kind: str) -> list[tuple[str, str]]:
    """(element, shape) for each child a <server> of this type may hold."""
    fixed = [(name, name) for name in fx._SERVER_SETTINGS[kind]]
    return fixed + list(fx.SERVER_OPTIONS[kind].items())


def _child_doc(kind: str, name: str) -> tuple[str, str]:
    if name in fx._SERVER_SETTINGS[kind]:
        return fx.DEVICES_HELP[name]
    return fx.SERVER_OPTION_HELP[kind][name]


def _value_pattern(kind: str, name: str) -> str:
    """The pattern the text of a value element has to match."""
    form = fx._VALUE_FORMS.get((kind, name))
    if form is not None:
        return _stripped(_xsd_regex(form))
    return _stripped(f"{_WORD_END}({_WORD_MID}*{_WORD_END})?")


# ---------------------------------------------------------------------------
# RELAX NG
# ---------------------------------------------------------------------------

def _xml(text: str) -> str:
    """Text as XML character data, with everything but printable ASCII as a
    character reference so the file reads the same in any editor."""
    out = []
    for char in text:
        if char == "&":
            out.append("&amp;")
        elif char == "<":
            out.append("&lt;")
        elif char == ">":
            out.append("&gt;")
        elif 0x20 <= ord(char) < 0x7F:
            out.append(char)
        else:
            out.append(f"&#x{ord(char):X};")
    return "".join(out)


class _Lines:
    """An indented XML document, one line at a time."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.depth = 0

    def line(self, text: str) -> None:
        self.lines.append("  " * self.depth + text)

    def open(self, tag: str) -> None:
        self.line(f"<{tag}>")
        self.depth += 1

    def close(self, tag: str) -> None:
        self.depth -= 1
        self.line(f"</{tag.split()[0]}>")

    def comment(self, lines: list[str]) -> None:
        self.line("<!--")
        for text in lines:
            self.line(f"  {text}".rstrip())
        self.line("-->")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


class _Rng(_Lines):
    """A RELAX NG document."""

    def doc(self, text: str) -> None:
        self.line(f"<a:documentation>{_xml(text)}</a:documentation>")

    def pattern(self, regex: str) -> None:
        self.open('data type="string"')
        self.line(f'<param name="pattern">{_xml(regex)}</param>')
        self.close("data")

    def value(self, text: str) -> None:
        # type="string": compared as written. The default, token, would
        # take " tcp " for tcp, which the parser does not.
        self.line(f'<value type="string">{_xml(text)}</value>')


def _rng_interleave(out: _Rng, parts: list) -> None:
    """Mixed content holding these parts in any order.

    <mixed>, because the parser allows any text between elements that
    Python's strip() calls white space, and a grammar that wanted element
    content only would allow XML's four characters and refuse the rest.
    """
    out.open("mixed")
    if len(parts) > 1:
        out.open("interleave")
    for part in parts:
        part()
    if len(parts) > 1:
        out.close("interleave")
    out.close("mixed")


def _rng_setting(out: _Rng, setting: fx._Setting) -> None:
    out.open("optional")
    out.open(f'element name="{setting.tag}"')
    out.doc(_doc(setting.help, setting.form))
    out.line(f'<ref name="kind.{setting.kind}"/>')
    out.close("element")
    out.close("optional")


def _rng_backend(out: _Rng) -> None:
    help_text, form = fx.DEVICES_HELP["backend"]
    out.open('element name="backend"')
    out.doc(_doc(help_text, form))
    out.open("choice")
    # tcp is the mesh the generator derives and takes no options; every
    # other type takes its options as one word.
    for types, pattern in (
            (("tcp",), _WS),
            (tuple(t for t in fx.BACKEND_TYPES if t != "tcp"),
             f"{_WS}{_NOT_SPACE}*{_WS}")):
        out.open("group")
        out.open('attribute name="type"')
        out.doc(_doc(*fx.DEVICES_HELP["backend@type"]))
        if len(types) > 1:
            out.open("choice")
        for name in types:
            out.value(name)
        if len(types) > 1:
            out.close("choice")
        out.close("attribute")
        out.pattern(pattern)
        out.close("group")
    out.close("choice")
    out.close("element")


def _rng_child(out: _Rng, kind: str, name: str, shape: str) -> None:
    wrapper = "zeroOrMore" if shape in ("values", "flags") else "optional"
    out.open(wrapper)
    if name == "backend":
        _rng_backend(out)
    else:
        out.open(f'element name="{name}"')
        out.doc(_doc(*_child_doc(kind, name)))
        out.pattern(_WS if shape == "flags" else _value_pattern(kind, name))
        out.close("element")
    out.close(wrapper)


def _rng_server(out: _Rng, kind: str) -> None:
    out.open("group")
    out.open('attribute name="type"')
    out.value(kind)
    out.close("attribute")
    out.line('<ref name="server.id"/>')
    out.line('<optional><ref name="server.vm"/></optional>')
    children = _server_children(kind)
    if children:
        _rng_interleave(out, [
            (lambda name=name, shape=shape: _rng_child(out, kind, name, shape))
            for name, shape in children])
    else:
        out.line("<text/>")
    out.close("group")


def render_rng() -> str:
    """The RELAX NG schema (XML syntax) for one fleet XML file."""
    out = _Rng()
    out.line('<?xml version="1.0" encoding="UTF-8"?>')
    out.comment([
        f"RELAX NG schema for qemu-tool fleet XML, namespace {fx.NS_FLEET}.",
        "",
        "GENERATED. Do not edit: this is what the fleet-schema subcommand",
        "of qemu-tool writes from the tables in qemu/qemu-tool/fleet_xml.py.",
        "Change the tables and write it again.",
        "",
        "The parser is the authority. This schema never rejects a file the",
        "parser accepts, and accepts some the parser refuses: it checks one",
        "file, not layers, and no rule that spans elements. The reference",
        f"page, {PAGE_CHECKOUT_PATH}, lists what it cannot check."])
    out.line(f'<grammar xmlns="{RNG_NS}"')
    out.line(f'         xmlns:a="{ANNOTATION_NS}"')
    out.line(f'         ns="{fx.NS_FLEET}"')
    out.line(f'         datatypeLibrary="{XSD_DATATYPES}">')
    out.depth = 1

    out.open("start")
    out.open('element name="fleet"')
    out.doc(
        "One fleet XML file: flat settings in any order, each at most once, "
        "and at most one devices block.")

    def devices() -> None:
        out.open("optional")
        out.line('<ref name="devices"/>')
        out.close("optional")

    _rng_interleave(out, [
        (lambda setting=setting: _rng_setting(out, setting))
        for setting in fx._SETTINGS] + [devices])
    out.close("element")
    out.close("start")

    for kind, pattern in KIND_PATTERNS.items():
        name, accepts, _checked = KIND_HELP[kind]
        out.open(f'define name="kind.{kind}"')
        out.doc(f"{name}: {accepts.replace('`', '')}")
        if pattern is None:
            out.line("<text/>")
        else:
            out.pattern(pattern)
        out.close("define")

    out.open('define name="devices"')
    out.open(f'element name="{fx._DEVICES}"')
    out.doc(_doc(*fx.DEVICES_HELP["devices"]))
    out.open("mixed")
    out.open("oneOrMore")
    out.line('<ref name="server"/>')
    out.close("oneOrMore")
    out.close("mixed")
    out.close("element")
    out.close("define")

    out.open('define name="server"')
    out.open('element name="server"')
    out.doc(_doc(*fx.DEVICES_HELP["server"]) + " "
            + _doc(*fx.DEVICES_HELP["server@type"]))
    out.open("choice")
    for kind in fx.SERVER_TYPES:
        _rng_server(out, kind)
    out.close("choice")
    out.close("element")
    out.close("define")

    out.open('define name="server.id"')
    out.open('attribute name="id"')
    out.doc(_doc(*fx.DEVICES_HELP["server@id"]))
    out.pattern(_xsd_regex(fx._SERVER_ID.pattern))
    out.close("attribute")
    out.close("define")

    out.open('define name="server.vm"')
    out.open('attribute name="vm"')
    out.doc(_doc(*fx.DEVICES_HELP["server@vm"]))
    out.pattern(fx.SERVER_VM_FORM)
    out.close("attribute")
    out.close("define")

    out.depth = 0
    out.line("</grammar>")
    return out.text()


# ---------------------------------------------------------------------------
# XML Schema (XSD 1.0)
# ---------------------------------------------------------------------------

# A <server>'s children come in any order, some at most once and some any
# number of times. XSD 1.0 can only say that by naming each state: a group
# for every set of once-only children already seen, 2**n of them for n such
# children. Past this many, a type's content is "any of its children, any
# number of times" instead, and xsd_looser() says so.
_XSD_ONCE_MAX = 5

# The text of a <backend>, whatever its type: XSD 1.0 cannot hold tcp, which
# takes none, to less than the others.
_BACKEND_TEXT = f"{_WS}{_NOT_SPACE}*{_WS}"


@dataclasses.dataclass(frozen=True)
class _Child:
    """One child element of a <server>, as the XSD declares it."""
    name: str
    backend: bool
    repeats: bool
    pattern: str
    doc: str


@dataclasses.dataclass(frozen=True)
class _Branch:
    """The children one <server> may hold together.

    One server type's, unless two types have a child of the same name: XSD
    wants one declaration for one name in one content model, so those types
    share a branch that takes the children of both.
    """
    types: tuple[str, ...]
    children: tuple[_Child, ...]

    @property
    def name(self) -> str:
        return ".or.".join(self.types)

    @property
    def once(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.children if not c.repeats)

    @property
    def repeat(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.children if c.repeats)

    @property
    def exact(self) -> bool:
        return len(self.once) <= _XSD_ONCE_MAX


def _xsd_child(types: tuple[str, ...], name: str) -> _Child:
    shapes, patterns, docs = [], [], []
    for kind in types:
        shape = dict(_server_children(kind)).get(name)
        if shape is None:
            continue
        shapes.append(shape)
        if shape == "backend":
            pattern = _BACKEND_TEXT
        elif shape == "flags":
            pattern = _WS
        else:
            pattern = _value_pattern(kind, name)
        if pattern not in patterns:
            patterns.append(pattern)
        doc = _doc(*_child_doc(kind, name))
        if doc not in docs:
            docs.append(doc)
    if "backend" in shapes and set(shapes) != {"backend"}:
        raise ValueError(
            f"<{name}> is a backend in one server type and an option in "
            "another; teach fleet_schema's XSD about an element that takes "
            "an attribute in one and not in the other")
    return _Child(
        name=name, backend="backend" in shapes,
        repeats=any(shape in ("values", "flags") for shape in shapes),
        # Where the types disagree on the form, either form passes.
        pattern=(patterns[0] if len(patterns) == 1
                 else "|".join(f"({pattern})" for pattern in patterns)),
        doc=" ".join(docs))


def _xsd_branches() -> list[_Branch]:
    groups: list[list[str]] = []
    for kind in fx.SERVER_TYPES:
        names = {name for name, _shape in _server_children(kind)}
        if not names:
            continue
        sharing = [group for group in groups if any(
            names & {name for name, _shape in _server_children(other)}
            for other in group)]
        if not sharing:
            groups.append([kind])
            continue
        index = groups.index(sharing[0])
        groups = [group for group in groups if group not in sharing]
        groups.insert(index, [k for group in sharing for k in group] + [kind])
    branches = []
    for group in groups:
        names: list[str] = []
        for kind in group:
            names += [name for name, _shape in _server_children(kind)
                      if name not in names]
        types = tuple(group)
        branches.append(_Branch(
            types, tuple(_xsd_child(types, name) for name in names)))
    return branches


def _elements(names) -> str:
    return ", ".join(f"`<{name}>`" for name in names)


def xsd_looser() -> list[tuple[str, str]]:
    """Where the XSD accepts a document the RELAX NG schema refuses.

    (key, sentence in Markdown). The whole list: the differential test has
    a document for each key and requires the two schemas to agree on every
    other document it holds. Built from the tables, so a new server type or
    option changes the list with the schema.
    """
    branches = _xsd_branches()
    items = [(
        "xsi",
        "An attribute XML Schema reserves for itself passes: "
        "`xsi:schemaLocation` and `xsi:noNamespaceSchemaLocation` on any "
        "element, with any value, and `xsi:type` on a flat setting when it "
        "names the type that setting already has. The RELAX NG schema "
        "refuses them, as the parser does: no element but `<server>` and "
        "`<backend>` takes an attribute.")]
    if branches and len(fx.SERVER_TYPES) > 1:
        example = ""
        if len(branches) > 1:
            example = (
                f" `<{branches[1].children[0].name}>` in a server of type "
                f"`{branches[0].types[0]}` passes.")
        items.append((
            "server-children",
            "A `<server>`'s children are held to the children of one type, "
            "which need not be the type its `type` attribute names: XSD 1.0 "
            "cannot make an element's content depend on an attribute's "
            f"value.{example} Children of two types in one server are still "
            "refused. The sets are "
            + "; ".join(f"{_elements(c.name for c in branch.children)} "
                        f"(`{'`, `'.join(branch.types)}`)"
                        for branch in branches) + "."))
    for branch in branches:
        if len(branch.types) > 1:
            items.append((
                f"merged.{branch.name}",
                f"The types `{'`, `'.join(branch.types)}` have a child "
                "element of the same name, and XSD allows one declaration "
                "for one name: a server of any of them may hold the children "
                "of all of them together, a child that repeats in one may "
                "repeat in each, and a value passes in the form any of them "
                "gives it."))
        if not branch.exact:
            items.append((
                f"repeat.{branch.name}",
                f"A child of `{'`, `'.join(branch.types)}` that may appear "
                f"once ({_elements(branch.once)}) may appear any number of "
                f"times: with more than {_XSD_ONCE_MAX} such children the "
                "any-order, at-most-once rule is not written out."))
    if any(child.backend for branch in branches for child in branch.children):
        items.append((
            "backend-tcp",
            "`<backend type='tcp'>` may hold option text, as the other "
            "backend types do. `tcp` takes none, and XSD 1.0 cannot make an "
            "element's text depend on an attribute's value."))
    return items


def _plain(markdown: str) -> str:
    return markdown.replace("`", "")


def _attr(text: str) -> str:
    return _xml(text).replace('"', "&quot;")


class _Xsd(_Lines):
    """An XML Schema document."""

    def doc(self, text: str) -> None:
        self.open("xs:annotation")
        self.line(f"<xs:documentation>{_xml(text)}</xs:documentation>")
        self.close("xs:annotation")

    def pattern(self, regex: str | None) -> None:
        # xs:string, whose white space is kept: the patterns spell out what
        # the parser strips, as they do in the RELAX NG schema.
        if regex is None:
            self.line('<xs:restriction base="xs:string"/>')
            return
        self.open('xs:restriction base="xs:string"')
        self.line(f'<xs:pattern value="{_attr(regex)}"/>')
        self.close("xs:restriction")

    def enumeration(self, values) -> None:
        self.open('xs:restriction base="xs:string"')
        for value in values:
            self.line(f'<xs:enumeration value="{_attr(value)}"/>')
        self.close("xs:restriction")

    def simple(self, regex: str | None) -> None:
        self.open("xs:simpleType")
        self.pattern(regex)
        self.close("xs:simpleType")


def _xsd_child_group(out: _Xsd, child: _Child) -> None:
    # A group of one element, so that the element is declared, and
    # documented, once however many content models name it.
    out.open(f'xs:group name="child.{child.name}"')
    out.open("xs:sequence")
    out.open(f'xs:element name="{child.name}"')
    out.doc(child.doc)
    if child.backend:
        out.open("xs:complexType")
        out.open("xs:simpleContent")
        out.open('xs:extension base="f:backend.options"')
        out.open('xs:attribute name="type" use="required"')
        out.doc(_doc(*fx.DEVICES_HELP["backend@type"]))
        out.open("xs:simpleType")
        out.enumeration(fx.BACKEND_TYPES)
        out.close("xs:simpleType")
        out.close("xs:attribute")
        out.close("xs:extension")
        out.close("xs:simpleContent")
        out.close("xs:complexType")
    else:
        out.simple(child.pattern)
    out.close("xs:element")
    out.close("xs:sequence")
    out.close("xs:group")


def _xsd_states(out: _Xsd, branch: _Branch) -> None:
    """The groups that say: these children, in any order, some only once."""
    once, repeat = branch.once, branch.repeat
    base = f"children.{branch.name}"

    def refs(names) -> None:
        for name in names:
            out.line(f'<xs:group ref="f:child.{name}"/>')

    if not branch.exact:
        out.open(f'xs:group name="{base}"')
        out.open("xs:sequence")
        out.open('xs:choice minOccurs="0" maxOccurs="unbounded"')
        refs(child.name for child in branch.children)
        out.close("xs:choice")
        out.close("xs:sequence")
        out.close("xs:group")
        return

    def name(seen: tuple[str, ...]) -> str:
        return f"{base}.after.{'.'.join(seen)}" if seen else base

    def nothing_left(seen: tuple[str, ...]) -> bool:
        return not repeat and len(seen) == len(once)

    for size in range(len(once) + 1):
        for seen in itertools.combinations(once, size):
            if nothing_left(seen):
                continue
            out.open(f'xs:group name="{name(seen)}"')
            out.open("xs:sequence")
            if repeat:
                out.open('xs:choice minOccurs="0" maxOccurs="unbounded"')
                refs(repeat)
                out.close("xs:choice")
            left = [n for n in once if n not in seen]
            if left:
                out.open('xs:choice minOccurs="0"')
                for following in left:
                    after = tuple(
                        n for n in once if n in seen or n == following)
                    if nothing_left(after):
                        refs([following])
                        continue
                    out.open("xs:sequence")
                    refs([following])
                    out.line(f'<xs:group ref="f:{name(after)}"/>')
                    out.close("xs:sequence")
                out.close("xs:choice")
            out.close("xs:sequence")
            out.close("xs:group")


def render_xsd() -> str:
    """The XML Schema (XSD 1.0) for one fleet XML file."""
    branches = _xsd_branches()
    out = _Xsd()
    out.line('<?xml version="1.0" encoding="UTF-8"?>')
    header = [
        f"XML Schema (XSD 1.0) for qemu-tool fleet XML, namespace "
        f"{fx.NS_FLEET}.",
        "",
        "GENERATED. Do not edit: this is what the fleet-schema subcommand",
        "of qemu-tool writes, with its format option set to xsd, from the",
        "tables in qemu/qemu-tool/fleet_xml.py. Change the tables and write",
        "it again.",
        "",
        "The parser is the authority, and fleet.rng, the RELAX NG schema",
        "generated from the same tables, is the precise schema. This one is",
        "for tools that take only XSD. It never rejects a file fleet.rng",
        "accepts, and so never one the parser accepts. It is looser than",
        "fleet.rng in these ways and no other:",
        ""]
    for _key, sentence in xsd_looser():
        header += textwrap.wrap(
            _plain(sentence), width=70, initial_indent="- ",
            subsequent_indent="  ", break_long_words=False,
            break_on_hyphens=False)
    header += [
        "",
        "Like fleet.rng it checks one file, not layers, and no rule that",
        f"spans elements. The reference page, {PAGE_CHECKOUT_PATH}, lists",
        "what neither can check."]
    if any("--" in text for text in header):
        raise ValueError("a comment cannot hold two hyphens in a row")
    out.comment(header)
    out.line(f'<xs:schema xmlns:xs="{XSD_NS}"')
    out.line(f'           xmlns:f="{fx.NS_FLEET}"')
    out.line(f'           targetNamespace="{fx.NS_FLEET}"')
    out.line('           elementFormDefault="qualified"')
    # No type may stand in for another through xsi:type.
    out.line('           blockDefault="#all">')
    out.depth = 1

    out.open('xs:element name="fleet"')
    out.doc(
        "One fleet XML file: flat settings in any order, each at most once, "
        "and at most one devices block.")
    # mixed, as the RELAX NG schema is: the parser allows any text between
    # elements that Python's strip() calls white space, and element-only
    # content would allow XML's four characters and refuse the rest.
    out.open('xs:complexType mixed="true"')
    out.open("xs:all")
    for setting in fx._SETTINGS:
        out.open(f'xs:element name="{setting.tag}" '
                 f'type="f:kind.{setting.kind}" minOccurs="0"')
        out.doc(_doc(setting.help, setting.form))
        out.close("xs:element")
    # <devices> and <server> have their types inline, with no name, so
    # that no xsi:type can name one.
    out.open(f'xs:element name="{fx._DEVICES}" minOccurs="0"')
    out.doc(_doc(*fx.DEVICES_HELP["devices"]))
    out.open('xs:complexType mixed="true"')
    out.open("xs:sequence")
    out.open('xs:element name="server" maxOccurs="unbounded"')
    out.doc(_doc(*fx.DEVICES_HELP["server"]))
    out.open('xs:complexType mixed="true"')
    if branches:
        out.open('xs:choice minOccurs="0"')
        for branch in branches:
            out.line(f'<xs:group ref="f:children.{branch.name}"/>')
        out.close("xs:choice")
    out.open('xs:attribute name="type" use="required"')
    out.doc(_doc(*fx.DEVICES_HELP["server@type"]))
    out.open("xs:simpleType")
    out.enumeration(fx.SERVER_TYPES)
    out.close("xs:simpleType")
    out.close("xs:attribute")
    out.open('xs:attribute name="id" use="required"')
    out.doc(_doc(*fx.DEVICES_HELP["server@id"]))
    out.simple(_xsd_regex(fx._SERVER_ID.pattern))
    out.close("xs:attribute")
    out.open('xs:attribute name="vm"')
    out.doc(_doc(*fx.DEVICES_HELP["server@vm"]))
    out.simple(fx.SERVER_VM_FORM)
    out.close("xs:attribute")
    out.close("xs:complexType")
    out.close("xs:element")
    out.close("xs:sequence")
    out.close("xs:complexType")
    out.close("xs:element")
    out.close("xs:all")
    out.close("xs:complexType")
    out.close("xs:element")

    for kind, pattern in KIND_PATTERNS.items():
        name, accepts, _checked = KIND_HELP[kind]
        out.open(f'xs:simpleType name="kind.{kind}"')
        out.doc(f"{name}: {_plain(accepts)}")
        out.pattern(pattern)
        out.close("xs:simpleType")

    for branch in branches:
        _xsd_states(out, branch)
    for branch in branches:
        for child in branch.children:
            _xsd_child_group(out, child)
    if any(child.backend for branch in branches for child in branch.children):
        out.open('xs:simpleType name="backend.options"')
        out.pattern(_BACKEND_TEXT)
        out.close("xs:simpleType")

    out.depth = 0
    out.line("</xs:schema>")
    return out.text()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _md(text: str) -> str:
    """Text for a Markdown table cell or paragraph, with nothing in it read
    as markup: a help string names elements in angle brackets."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", text)


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return out


def _renders_as(kind: str, name: str, shape: str) -> str:
    variable = fx.SERVER_ENV.get(kind, {}).get(name)
    if variable:
        return f"`{variable}=VALUE` in the container's environment"
    return f"`--{name}`" if shape == "flags" else f"`--{name} VALUE`"


_SHAPES = {
    "value": "one value, at most once",
    "values": "one value each, repeatable, order kept",
    "flags": "empty element, repeatable",
}


def render_markdown() -> str:
    """The reference page: every element, the <devices> grammar, the schema."""
    lines: list[str] = []
    add = lines.append

    def para(*text: str) -> None:
        lines.extend(text)
        add("")

    para("# Fleet XML reference")
    para(
        "<!-- GENERATED by `qemu-tool fleet-schema --format md` from the "
        "tables in",
        "     qemu/qemu-tool/fleet_xml.py. Do not edit this file: change the "
        "tables and",
        f"     write it again. tests/test_fleet_schema.py fails when it is "
        "out of date. -->")
    para(
        "Fleet XML is the only settings format `qemu-tool` reads. This page "
        "lists every",
        "element the parser knows. It is generated from the parser's own "
        "tables, as are",
        "the two [schemas](#the-schemas) that go with it, so none of them "
        "can describe",
        "an element the parser does not have.")
    para(
        f"[`qemu/stacks/rocm-golden.xml`]({_REPO_URL}/blob/main/qemu/stacks/"
        "rocm-golden.xml)",
        "is the annotated example to copy or to lay an overlay on. `qemu-tool "
        "check --xml",
        "FILE` reports everything wrong with a file, and is the check that "
        "counts.")

    para("## The format")
    para(
        f"A file is one `<fleet>` element in the namespace `{fx.NS_FLEET}`. "
        "Each setting",
        "is a child element whose text is the value, in any order:")
    para(
        "```xml",
        f'<fleet xmlns="{fx.NS_FLEET}">',
        "  <vm-count>2</vm-count>",
        "  <vm-name-prefix>rocm-golden</vm-name-prefix>",
        "  <vm-vcpus>4</vm-vcpus>",
        "</fleet>",
        "```")
    para(
        "An element that is absent keeps its default. The parser is strict: "
        "an element",
        "it does not know, an element given twice, an attribute, a nested "
        "element and",
        "text directly under `<fleet>` are each an error. Comments are "
        f"allowed. `<{fx._DEVICES}>`",
        "is the one element with attributes and children, and has [a "
        "section of its",
        "own](#the-devices-block).")
    para(
        "`--xml` is repeatable, and a later file overrides the elements it "
        "sets. The",
        "schemas and this page describe one file.")

    para("## Value kinds")
    para(
        "Every setting has a kind, which is how the parser reads its text. "
        "White space",
        "here means what Python's `str.strip()` removes, which is more than "
        "a space, a",
        "tab and a line break.")
    lines.extend(_table(
        ("Kind", "The parser accepts", "The schemas check"),
        [(f"{KIND_HELP[kind][0]} (`{kind}`)", KIND_HELP[kind][1],
          KIND_HELP[kind][2]) for kind in KIND_PATTERNS]))
    add("")

    para("## Elements")
    para(
        "**Default** is the value when the element is absent; `(empty)` is "
        "the empty",
        "string, and for many elements the command that reads it then "
        "applies a default",
        "of its own, which the description gives. **Sparse** elements are "
        "left out when",
        "`FleetXml.emit()` writes a configuration back out as XML and the "
        "value is an",
        "empty string, zero or false; no command calls it today, so the "
        "column matters",
        "to code that does.")
    for section in fx._SECTIONS:
        para(f"### {section.title}")
        para(_md(_sentence(section.intro)))
        rows = []
        for setting in section.settings:
            text = f"{_md(_sentence(setting.help))} {_md(_sentence(setting.form))}"
            default = _default_text(setting)
            rows.append((
                f"`<{setting.tag}>`",
                KIND_HELP[setting.kind][0],
                f"`{default}`" if default else "(empty)",
                "yes" if setting.sparse else "no",
                text))
        lines.extend(_table(
            ("Element", "Kind", "Default", "Sparse", "Description"), rows))
        add("")

    para("### Removed elements")
    para(
        "These were elements of the format and are not any more. Most were "
        "parsed and",
        "read by no command; the table says what to do about the ones that "
        "were read.",
        "A file that sets one is refused by the parser, which says so by "
        "name, and by",
        "both schemas, to which it is an unknown element like any other:")
    lines.extend(_table(
        ("Element", "Why it went"),
        [(f"`<{tag}>`", _md(_sentence(why[0].upper() + why[1:])))
         for tag, why in fx.REMOVED_ELEMENTS.items()]))
    add("")
    para(
        "```",
        f"my-fleet.xml: {fx.removed_element_problem('prom-port')}",
        "```")

    para("## The devices block")
    para(
        f"`<{fx._DEVICES}>` lists the vfio-user servers a guest gets, "
        "one `<server>`",
        "entry each. It exists so that two servers of one type can differ, "
        "so that",
        "a NIC can be given an RDMA backend, and, with the `vm` attribute, "
        "so that the",
        "guests of one fleet can differ.")
    para(
        "```xml",
        "<devices>",
        "  <server type='ernic' id='kv'>",
        "    <backend type='nvmeof'>kv=1,size=256M</backend>",
        "    <log-level>info</log-level>",
        "  </server>",
        "  <server type='ernic' id='mesh'/>",
        "  <server type='rocjitsu' id='gpu0'>",
        "    <cpu-thread-budget>2</cpu-thread-budget>",
        "  </server>",
        "  <server type='sdxi' id='sdxi0'/>",
        "</devices>",
        "```")
    lines.extend(_table(
        ("Part", "Description"),
        [(f"`{label}`", f"{_md(_sentence(fx.DEVICES_HELP[key][0]))} "
          f"{_md(_sentence(fx.DEVICES_HELP[key][1]))}")
         for label, key in (
             (f"<{fx._DEVICES}>", "devices"), ("<server>", "server"),
             ("<server type=...>", "server@type"),
             ("<server id=...>", "server@id"),
             ("<server vm=...>", "server@vm"))]))
    add("")
    para(
        "The children of a `<server>` depend on its type. They may come in "
        "any order.",
        "A value is stripped of surrounding white space, may not be empty "
        "and may not",
        "hold a line break, a tab or a control character.")
    for kind in fx.SERVER_TYPES:
        para(f"### Servers of type `{kind}`")
        rows = []
        for name, shape in _server_children(kind):
            help_text, form = _child_doc(kind, name)
            if name == "backend":
                shape_text = "attribute `type`, options as text, at most once"
                renders = "`-b TYPE` or `-b TYPE:OPTIONS`"
            elif name == "config":
                shape_text = _SHAPES["value"]
                renders = "the server's config file"
            else:
                shape_text = _SHAPES[shape]
                renders = _renders_as(kind, name, shape)
            rows.append((
                f"`<{name}>`", shape_text, renders,
                f"{_md(_sentence(help_text))} {_md(_sentence(form))}"))
        lines.extend(_table(
            ("Element", "Shape", "Becomes", "Description"), rows))
        add("")
    para("### Backend types")
    para(
        "The `type` attribute of `<backend>`. The element's text is the "
        "backend's own",
        "options as one word; the parser checks that it has no white space "
        "and that",
        "`tcp` has none, and nothing else about it.")
    lines.extend(_table(
        ("Type", "Description"),
        [(f"`{name}`", f"{_md(_sentence(fx.BACKEND_HELP[name][0]))} "
          f"{_md(_sentence(fx.BACKEND_HELP[name][1]))}")
         for name in fx.BACKEND_TYPES]))
    add("")

    para("## The schemas")
    para(
        "There are two, generated from the same tables and with the same "
        "patterns:")
    lines.extend(_table(
        ("Schema", "Language", "Use it"),
        [("`fleet.rng`",
          "[RELAX NG](https://relaxng.org/), XML syntax, with XML Schema "
          "datatypes for the patterns",
          "Wherever the tool takes RELAX NG. It is the precise one: it says "
          "everything about one file that a grammar can."),
         ("`fleet.xsd`",
          "XML Schema (XSD) 1.0",
          "With an editor or a tool that takes only XSD. It is "
          "[looser](#where-the-xsd-is-looser), and not only inside "
          "`<devices>`.")]))
    add("")
    para(
        "Neither rejects a file the parser accepts, and the XSD rejects no "
        "file the",
        "RELAX NG schema accepts. Each is one file:")
    para(
        f"- `{RNG_CHECKOUT_PATH}` and `{XSD_CHECKOUT_PATH}` in a checkout,",
        f"- `{RNG_INSTALLED_PATH}` and `{XSD_INSTALLED_PATH}`,",
        "  installed by the Debian package and by nothing else,",
        "- `qemu_tool/share/schema/` inside the wheel, which a `uv tool "
        "install` or",
        "  `pipx install` puts in an environment of its own, at no path "
        "worth typing,",
        "- or written on demand, which works from any install: `qemu-tool "
        "fleet-schema",
        "  --output fleet.rng` and `qemu-tool fleet-schema --format xsd "
        "--output",
        "  fleet.xsd`. The examples below use copies written that way.")
    para(
        "They are generated, and so is this page. After a change to the "
        "tables in",
        "`qemu/qemu-tool/fleet_xml.py`, from the root of a checkout:")
    para(
        "```bash",
        f"qemu-tool fleet-schema --format rng --output {RNG_CHECKOUT_PATH}",
        f"qemu-tool fleet-schema --format xsd --output {XSD_CHECKOUT_PATH}",
        f"qemu-tool fleet-schema --format md --output {PAGE_CHECKOUT_PATH}",
        "```")
    para(
        "The XSD is written by `qemu-tool` from the tables, not converted "
        "from the",
        "RELAX NG file. `trang`, the usual converter, does convert "
        "`fleet.rng`, with",
        "two warnings that it is approximating. What it writes is neither "
        "stricter nor",
        "looser than `fleet.xsd` throughout. It takes more: a flat setting, "
        "the",
        "`<devices>` block and a once-only child of a `<server>` may "
        "repeat, and every",
        "element is declared globally, so any of them passes as the root of "
        "a",
        "document. And it takes less in one place: it types a string or a "
        "path",
        "setting as `xs:string` and declares no `kind.str` or `kind.expand`, "
        "so an",
        "`xsi:type` naming either, which `fleet.xsd` accepts on such a "
        "setting, is",
        "refused by trang's.")

    para("### Where the XSD is looser")
    para(
        "The XSD accepts everything the RELAX NG schema accepts, and these "
        "besides.",
        "This is the whole list: the test suite has a document for each "
        "item that",
        "RELAX NG refuses and the XSD takes, and requires the two to agree "
        "on every",
        "other document it holds.")
    para(*(f"- {sentence}" for _key, sentence in xsd_looser()))
    para(
        "Everything else is the same in both: the flat settings, each at "
        "most once",
        "and in any order; the value kinds; the server types and ids; the "
        "backend",
        "types; the form of each option's value; and, in a `<server>`, "
        "that a child",
        "which may appear once does not appear twice.")

    para("### Validating a file")
    para(
        "`qemu-tool check --xml FILE` runs the parser itself and is the "
        "answer that",
        "counts. A schema is for tools that do not have `qemu-tool`: an "
        "editor, a",
        "CI step, another program that writes fleet files.")
    para(
        "With `xmllint`, from libxml2:")
    para(
        "```bash",
        "qemu-tool fleet-schema --output fleet.rng",
        "xmllint --noout --relaxng fleet.rng my-fleet.xml",
        "",
        "qemu-tool fleet-schema --format xsd --output fleet.xsd",
        "xmllint --noout --schema fleet.xsd my-fleet.xml",
        "```")
    para("With `lxml`, in Python:")
    para(
        "```python",
        "from lxml import etree",
        "",
        'document = etree.parse("my-fleet.xml")',
        "",
        'rng = etree.RelaxNG(etree.parse("fleet.rng"))',
        "if not rng.validate(document):",
        "    print(rng.error_log)",
        "",
        'xsd = etree.XMLSchema(etree.parse("fleet.xsd"))',
        "if not xsd.validate(document):",
        "    print(xsd.error_log)",
        "```")
    para(
        "With `jing`, a RELAX NG validator that is not libxml2. It takes "
        "the RELAX",
        "NG schema only:")
    para(
        "```bash",
        "jing fleet.rng my-fleet.xml",
        "```")
    para(
        "`xmllint` and `lxml` both use libxml2. Its messages name the "
        "element it",
        "stopped at and little else; the parser's say what is wrong and "
        "suggest the",
        "closest known name.")

    para("### Editors")
    para(
        "A fleet file cannot name its schema in an attribute. The usual way "
        "to point",
        "at an XSD is `xsi:schemaLocation` on the root element, and the "
        "parser",
        "refuses it, as it refuses any attribute on `<fleet>`:")
    para(
        "```",
        "my-fleet.xml: <fleet> takes no attributes, found "
        f"{{{XSI_NS}}}schemaLocation",
        "```")
    para(
        "The XSD itself would accept the attribute, which is the first "
        "item of the",
        "list above; the file would validate in the editor and not load. "
        "So the",
        "association is made outside the element, in one of two ways.")
    para(
        "**In the editor's settings**, which leaves the fleet file alone. "
        "For the XML",
        "extension of VS Code (`redhat.vscode-xml`), in `settings.json`, "
        "with the",
        "pattern and the path changed to suit:")
    para(
        "```json",
        "{",
        '  "xml.fileAssociations": [',
        "    {",
        '      "pattern": "**/fleet*.xml",',
        '      "systemId": "/path/to/fleet.xsd"',
        "    }",
        "  ]",
        "}",
        "```")
    para(
        "`systemId` is the path or the URL of the schema. "
        "IntelliJ-based",
        "editors map a namespace to a file under Settings, Languages & "
        "Frameworks,",
        f"Schemas and DTDs: the URI is `{fx.NS_FLEET}`. Emacs' nXML "
        "mode reads RELAX",
        "NG's compact syntax only, so it wants the RELAX NG schema "
        "converted first",
        "(`trang fleet.rng fleet.rnc`) and a `schemas.xml` entry for the "
        "namespace.")
    para(
        "**In a processing instruction** before the root element, which "
        "the parser",
        "ignores and editors that follow the `xml-model` convention read. "
        "The `href`",
        "is wherever the schema is: a copy beside the fleet file here, or "
        "the Debian",
        "package's path. For the XSD:")
    para(
        "```xml",
        '<?xml-model href="fleet.xsd"',
        '            type="application/xml"',
        f'            schematypens="{XSD_NS}"?>',
        f'<fleet xmlns="{fx.NS_FLEET}">',
        "  ...",
        "</fleet>",
        "```")
    para("And for the RELAX NG schema, in an editor that takes one:")
    para(
        "```xml",
        '<?xml-model href="fleet.rng"',
        '            type="application/xml"',
        f'            schematypens="{RNG_NS}"?>',
        "```")
    para(
        "None of the editor setups has been tried here. What has been run "
        "is the",
        "parser on a file that carries each processing instruction, which "
        "it accepts,",
        "and on one with `xsi:schemaLocation`, which it refuses.")

    para("## What the schema cannot check")
    para(
        "A file that validates can still be refused. Either schema "
        "describes one",
        "file and one element at a time; these rules are the parser's and "
        "the",
        "generator's alone. What is said of the schema here holds for both; "
        "the XSD",
        "also lets by [what is listed above](#where-the-xsd-is-looser).")
    para("In one file, refused by the parser:")
    para(
        "- two `<server>` entries with the same `id`;",
        "- a `<devices>` block beside an `<ernic-per-vm>`, "
        "`<rocjitsu-per-vm>` or",
        "  `<sdxi-per-vm>` that says another number than the block lists: "
        "the block",
        "  replaces the three counts, so such a count would never apply;",
        "- text directly under `<fleet>`, `<devices>` or a `<server>`. The "
        "schema allows",
        "  any text there, because the parser allows white space the "
        "grammar's own",
        "  rule for element content would refuse;",
        "- an unclosed quote, a trailing backslash or a quoted word that "
        "spans lines",
        "  in `<vm-qemu-args>`;",
        "- a character outside ASCII that is not a decimal digit, in an "
        "integer;",
        "- an integer or a port of more digits than Python's `int()` "
        "converts, which",
        "  is 4300 unless Python is told otherwise. The parser reports it "
        "as not an",
        "  integer; a grammar does not count that far.")
    para(
        "And a validator may answer differently from the parser for a "
        "reason that is",
        "the validator's own and not the grammar's:")
    para(
        "- a `<!DOCTYPE>` whose internal subset gives an attribute a "
        "default",
        "  (`<!ATTLIST server type CDATA \"ernic\">`), in either "
        "direction. The parser",
        "  applies the default, so a `<server>` with no `type` loads, and "
        "a default on",
        "  `<fleet>` is refused as an attribute. A validator sees the "
        "attribute only if",
        "  it applies such defaults: `lxml` does not unless its parser is "
        "made with",
        "  `attribute_defaults=True`, and then it agrees with the parser;",
        "- a `<!DOCTYPE>` whose internal subset declares an entity that "
        "holds an",
        "  element (`<!ENTITY e \"<vm-count>2</vm-count>\">`, then `&e;` "
        "under",
        "  `<fleet>`). The parser accepts it, and so do `jing` and Xerces. "
        "libxml2",
        "  (`xmllint`, `lxml`) leaves that element out of the fleet "
        "namespace and",
        "  both schemas then refuse it. An entity that holds text only is "
        "read the",
        "  same way by all of them;",
        "- an integer with thousands of single underscores between its "
        "digits. A",
        "  validator built on Java's regular expressions, `jing` for one, "
        "recurses",
        "  once for each underscore and ends in a `StackOverflowError`, "
        "which is no",
        "  answer at all: seen with 2000 underscores, not with 1000, at "
        "Java's",
        "  default stack size. The parser accepts such an integer up to "
        "`int()`'s",
        "  limit, and so does libxml2 with either schema;",
        "- a value of hundreds of thousands of characters in an element "
        "the schemas",
        "  give a pattern. Xerces, the XSD validator of Java and of many "
        "editors,",
        "  takes time that grows with the square of the length there: "
        "seconds for",
        "  200,000 characters. Its answer is the right one when it comes.")
    para("No shipped file has a `<!DOCTYPE>`.")
    para("Across files given as layers:")
    para(
        "- a later file overrides the flat elements it sets, whole;",
        "- `<devices>` entries merge by `id`: an entry for an id an earlier "
        "layer has",
        "  changes what it names and keeps the rest, a new id is appended, "
        "and no",
        "  layer can remove one;",
        "- an `id` cannot change `type` in a later layer;",
        "- a per-VM count set in a layer after a `<devices>` block, to "
        "another number,",
        "  is an error. `qemu-tool check` reports these as a `layers "
        "merge` line.")
    para("Per-VM lists, checked when a stack is generated:")
    para(
        "- `<vm-shm-size>`, `<rocjitsu-config>` and `<vm-filesystem>` take "
        "one value",
        "  for every guest, or a comma list of exactly `<vm-count>` "
        "values. Any other",
        "  length is an error;",
        "- `<vm-vcpus>`, `<vm-vmem>` and `<vm-nvme>` take one integer for "
        "every guest,",
        "  or exactly `<vm-count>` of them, the same way;",
        "- `<vm-extra-hostfwd>` is the exception: its commas are several "
        "rules for one",
        "  guest, and the whole value goes to every guest.")
    para(
        "Rules between elements, checked by `gen-compose` "
        "(`gen_compose.py`):")
    para(
        "- `<vm-count>` of at least 1, and `<ernic-per-vm>` and "
        "`<rocjitsu-per-vm>` of",
        "  at least 1, or a `<devices>` block with at least one `ernic` "
        "and one",
        "  `rocjitsu` server;",
        "- `<vm-roles>` with exactly `<vm-count>` roles, each lower-case "
        "letters and",
        "  digits and not the word `all`;",
        "- a `vm` attribute on a `<server>` that names no guest above "
        "`<vm-count>` and",
        "  no role `<vm-roles>` lacks, does not mix `all` with anything "
        "else,",
        "  leaves every guest at least one `ernic` server, and is not used "
        "together",
        "  with `<vm-pcie-switch-width>`;",
        "- `<vm-nvme-size>` a whole number and `K`, `M`, `G` or `T`, on a "
        "fleet where",
        "  a guest has an NVMe on a scratch file;",
        "- at most one `spdk` server a guest, and `<spdk-image>` set when "
        "there is one;",
        "- `<sdxi-image>` set when a guest has an SDXI device;",
        "- `<qemu-ref>`, `<ernic-ref>` and `<rocjitsu-ref>` not empty, on "
        "one build",
        "  stamp, with one libvfio-user revision between QEMU and the "
        "servers;",
        "- the tag `<backing-ref>` names, and how it relates to "
        "`<publish-ref>` and",
        "  `<image-allow-guest-lag>`;",
        "- the fleet's size: at most 64 NICs on the mesh and 244 NICs in "
        "all, with a",
        "  warning above 40 on the mesh;",
        "- `<vm-nvme-vram>` and `<vm-pci-mmio-bridge>` need `<vm-nvme>` of "
        "at least 1,",
        "  and `<vm-nvme-block-size>` of 4096 a `<vm-nvme>` other than 0;",
        "- `<ernic-mtu>` other than 1500 is refused when `<vm-qemu-args>` "
        "already",
        "  passes a credential of the name it uses, and noted when it is "
        "under 4192",
        "  or `<ernic-tap>` is false.")
    para(
        "And nothing checks a value the parser passes through as text: a "
        "size, a",
        "duration, an image reference, `<p4-guest-scope>`, a server "
        "option's value.",
        "Those reach docker, QEMU or the device server as written.")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


FORMATS = {"rng": render_rng, "xsd": render_xsd, "md": render_markdown}
