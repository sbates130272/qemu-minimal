#!/usr/bin/python3
"""Record what a quick-start test measured, for node_exporter's textfile collector.

Run in a guest by vm-quickstart.yml, as root, after an optional test has
passed. Neither test leaves anything Prometheus can see once it has ended:
the NIC's server counts no bytes for an fp8 reduction, and one 4 MiB SDXI
copy is a blip in a counter. So the play hands the result to this, which
replaces one file in the collector directory, and the home dashboard reads
"the last pass" from it. A test that failed never gets here, so the file is
always a passing run's.

    p4-fp8     quickstart_p4_fp8.prom, from p4_fp8_bench's result line
        quickstart_p4_fp8_bytes_per_second{device}   fp8 result bytes a second
        quickstart_p4_fp8_operation_seconds{device}  average time of one reduction
        quickstart_p4_fp8_elements{device}           elements in each reduction
        quickstart_p4_fp8_iterations{device}         reductions timed
        quickstart_p4_fp8_timestamp_seconds{device}  when this was recorded

    sdxi-copy  quickstart_sdxi_copy.prom, from the SDXI test's result line
        quickstart_sdxi_copy_bytes                   bytes copied and compared
        quickstart_sdxi_copy_timestamp_seconds       when this was recorded

The timestamp is this guest's clock. The file stays until the guest's disk
is recreated, so what it holds can be older than the boot it is read in;
the timestamp is how a reader tells.

Both take the line the test printed, as --line, and write nothing for a
line that is not one: exit 2, and so for a device name that is not one. A
guest with no collector directory has no node_exporter to serve the file:
one line on stdout, exit 0, nothing written. Exit 1 is a file that could
not be replaced.
"""
import argparse
import os
import re
import sys
import tempfile
import time

DIRECTORY = "/var/lib/prometheus/node-exporter"
P4_FILE = "quickstart_p4_fp8.prom"
SDXI_FILE = "quickstart_sdxi_copy.prom"

# p4_fp8_bench.c's one result line. MB is 1e6 bytes there.
BENCH = re.compile(
    r"p4 fp8 bench: (\d+) elements x (\d+) iters, "
    r"avg (\d+(?:\.\d+)?) ms, (\d+(?:\.\d+)?) MB/s")
DEVICE = re.compile(r"[A-Za-z0-9_.:-]+")
# What vm-quickstart.yml's SDXI play prints once cmp has passed.
COPY = re.compile(
    r"sdxi copy: (\d+) bytes through \S+, source and destination identical")


def p4_fp8(line, device, now):
    """The exposition text for one bench line, or None when it is not one."""
    found = BENCH.fullmatch(line.strip())
    if not found:
        return None
    elements, iterations = int(found.group(1)), int(found.group(2))
    seconds, rate = float(found.group(3)) / 1e3, float(found.group(4)) * 1e6
    if not (elements and iterations and rate > 0):
        return None
    label = f'{{device="{device}"}}'
    series = [
        ("bytes_per_second",
         "fp8 result bytes per second in the last p4_fp8_bench run "
         "quick-start test recorded, one byte an element.",
         f"{rate:.0f}"),
        ("operation_seconds",
         "Average seconds one fp8 reduction took in that run.",
         f"{seconds:.9g}"),
        ("elements", "Elements in each reduction of that run.",
         str(elements)),
        ("iterations", "Reductions that run timed.", str(iterations)),
        ("timestamp_seconds",
         "Unix time, by the guest's clock, at which that run was recorded.",
         str(now)),
    ]
    lines = []
    for name, text, value in series:
        lines += [f"# HELP quickstart_p4_fp8_{name} {text}",
                  f"# TYPE quickstart_p4_fp8_{name} gauge",
                  f"quickstart_p4_fp8_{name}{label} {value}"]
    return "\n".join(lines) + "\n"


def sdxi_copy(line, now):
    """The exposition text for one verified copy, or None."""
    found = COPY.fullmatch(line.strip())
    if not found or not int(found.group(1)):
        return None
    count = int(found.group(1))
    return "\n".join([
        "# HELP quickstart_sdxi_copy_bytes Bytes the last quick-start SDXI "
        "test copied through the device and found identical to their source.",
        "# TYPE quickstart_sdxi_copy_bytes gauge",
        f"quickstart_sdxi_copy_bytes {count}",
        "# HELP quickstart_sdxi_copy_timestamp_seconds Unix time, by the "
        "guest's clock, at which that copy was recorded.",
        "# TYPE quickstart_sdxi_copy_timestamp_seconds gauge",
        f"quickstart_sdxi_copy_timestamp_seconds {now}",
    ]) + "\n"


def replace(path, text):
    """All of the file or none of it: node_exporter may read at any time.

    The scratch file is a new one of its own name (O_EXCL), beside the file
    it becomes: a fixed name would be written through a symlink left at it,
    and shared by two runs. Its name does not end in .prom, so node_exporter
    never reads it.
    """
    handle, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=os.path.basename(path) + ".",
        suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="ascii") as out:
            out.write(text)
            # node_exporter runs as its own user; mkstemp makes it 0600.
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
    parser.add_argument("--dir", default=DIRECTORY,
                        help="the collector directory (default: %(default)s)")
    tests = parser.add_subparsers(dest="test", required=True)
    p4 = tests.add_parser("p4-fp8", help="record a p4_fp8_bench result")
    p4.add_argument("--device", required=True,
                    help="the IB device the bench ran on")
    p4.add_argument("--line", required=True,
                    help="the bench's 'p4 fp8 bench: ...' line")
    sdxi = tests.add_parser("sdxi-copy", help="record a verified SDXI copy")
    sdxi.add_argument("--line", required=True,
                      help="the test's 'sdxi copy: ...' line")
    args = parser.parse_args(argv)

    now = int(time.time())
    if args.test == "p4-fp8":
        if not DEVICE.fullmatch(args.device):
            parser.error(f"{args.device!r} is not an IB device name")
        text = p4_fp8(args.line, args.device, now)
        if text is None:
            parser.error(f"not a p4_fp8_bench result with a rate above "
                         f"zero: {args.line!r}")
        name = P4_FILE
    else:
        text = sdxi_copy(args.line, now)
        if text is None:
            parser.error(f"not a verified SDXI copy: {args.line!r}")
        name = SDXI_FILE

    if not os.path.isdir(args.dir):
        print(f"quickstart-test-metrics: not recorded: {args.dir} is not "
              "there, so this guest has no node_exporter textfile collector "
              "to serve it")
        return 0
    path = os.path.join(args.dir, name)
    try:
        replace(path, text)
    except OSError as exc:
        print(f"quickstart-test-metrics: {path}: {exc.strerror}",
              file=sys.stderr)
        return 1
    print(f"quickstart-test-metrics: recorded {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
