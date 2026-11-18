#!/usr/bin/python3
"""Write the SDXI device's counters for node_exporter's textfile collector.

Run by sdxi-textfile-metrics.timer, as root: it reads the 14 counters of
each context through sdxi_char's STATS ioctl and replaces sdxi.prom in the
collector directory.

    sdxi_up                          1 when every context was read, else 0
    sdxi_<stat>_total{context="N"}   one series per counter and context

Context 0 is the driver's admin ring and context 1 carries every client
operation. The counters live in the SDXI server and start again from zero
when it restarts.

A guest with no SDXI device is a normal configuration: sdxi_up 0, exit 0,
nothing on stderr. Any other reason the counters could not be read is one
line on stderr and the same file. The exit status is non-zero only when the
file could not be written, or for an argument that is not a context id.

Each run takes the driver's one lock for 28 register accesses per context;
see "SDXI metrics" in AGENTS.md before sampling faster.
"""
import argparse
import os
import sys

DEVICE = "/dev/sdxi0"
OUTPUT = "/var/lib/prometheus/node-exporter/sdxi.prom"
CONTEXTS = (0, 1)
# sdxi_char masks the id to seven bits, so 128 would read context 0 under
# another label.
MAX_CONTEXT = 127

# What each counter counts, read from the server at the pinned commit
# (src/sdxi_engine.c). A counter the installed sdxi_cli names and this table
# does not is still written, with a generic line.
HELP = {
    "bytes_moved": "Bytes copied by COPY operations.",
    "num_copy": "COPY operations completed.",
    "num_errors": "Operations that completed with an error.",
    "num_intr": "Interrupt descriptors processed.",
    "num_cxt_start": "Context start commands this context's ring carried.",
    "num_cxt_stop": "Context stop commands this context's ring carried.",
    "num_nop": "NOP descriptors processed.",
    "num_sync": "SYNC descriptors processed.",
    "bytes_compressed": "Input bytes given to COMPRESS operations.",
    "bytes_decompressed": "Output bytes produced by DECOMPRESS operations.",
    "num_compress": "COMPRESS operations completed.",
    "num_decompress": "DECOMPRESS operations completed.",
    "num_elemwise": "Elementwise operations completed.",
    "bytes_elemwise": "Input bytes given to elementwise operations.",
}


def context_id(text):
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= MAX_CONTEXT:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a context id (0 to {MAX_CONTEXT})")
    return value


def read_counters(device, contexts):
    """(names, {context: values}, None), or (None, None, why not).

    "why not" is the empty string when there is simply no device.
    """
    try:
        fd = os.open(device, os.O_RDWR)
    except FileNotFoundError:
        return None, None, ""
    except OSError as exc:
        return None, None, f"{device}: {exc.strerror}"
    cid = None
    try:
        from sdxi_cli.ioctl import STAT_NAMES, sdxi_stats
        names = list(STAT_NAMES)
        rows = {}
        for cid in contexts:
            values = list(sdxi_stats(fd, cid))
            if len(values) != len(names):
                return None, None, (
                    f"{device}: context {cid} returned {len(values)} "
                    f"counters and sdxi_cli names {len(names)}")
            rows[cid] = [int(value) for value in values]
    except ImportError as exc:
        return None, None, f"sdxi_cli is not importable: {exc}"
    except Exception as exc:  # pylint: disable=broad-except
        return None, None, f"{device}: reading context {cid}: {exc}"
    finally:
        os.close(fd)
    return names, rows, None


def exposition(names, rows):
    lines = [
        "# HELP sdxi_up Whether the SDXI device's counters could be read "
        "(1=yes, 0=no).",
        "# TYPE sdxi_up gauge",
        f"sdxi_up {0 if rows is None else 1}",
    ]
    if rows is not None:
        for at, stat in enumerate(names):
            text = HELP.get(stat, f"SDXI device counter {stat}.")
            lines.append(f"# HELP sdxi_{stat}_total {text}")
            lines.append(f"# TYPE sdxi_{stat}_total counter")
            for cid in sorted(rows):
                lines.append(
                    f'sdxi_{stat}_total{{context="{cid}"}} {rows[cid][at]}')
    return "\n".join(lines) + "\n"


def replace(path, text):
    """All of the file or none of it: node_exporter may read at any time."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="ascii") as out:
            out.write(text)
            # node_exporter runs as its own user; do not depend on a umask.
            os.fchmod(out.fileno(), 0o644)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default=DEVICE,
                        help="the sdxi_char node (default: %(default)s)")
    parser.add_argument("--output", default=OUTPUT,
                        help="the file to replace (default: %(default)s)")
    parser.add_argument("--context", type=context_id, nargs="+",
                        metavar="ID", help="the contexts to read "
                        f"(default: {' '.join(map(str, CONTEXTS))})")
    args = parser.parse_args(argv)
    contexts = sorted(set(args.context or CONTEXTS))

    names, rows, why = read_counters(args.device, contexts)
    if why:
        print(f"sdxi-textfile-metrics: {why}", file=sys.stderr)
    try:
        replace(args.output, exposition(names, rows))
    except OSError as exc:
        print(f"sdxi-textfile-metrics: {args.output}: {exc.strerror}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
