#!/bin/sh
# Which rdma-core a build with these configure arguments compiles against,
# links against and loads, and a refusal unless it is one.
#
#   perftest-rdma-probe.sh [CONFIGURE-ARG...]
#
# The arguments are the ones ./configure is about to be given. CPPFLAGS=,
# CFLAGS= and LDFLAGS= among them are used here the way configure uses them,
# the last of each winning; the rest are ignored.
#
# A build wants one rdma-core: one set of headers, and one libibverbs that
# is both linked and loaded. On a golden guest there is one to find. The
# archive's rdma-core is there as a runtime only, with no header and no
# libibverbs.so for a linker, and rocm-ernic's, built from source under
# /usr/local, is the only one a compiler or a linker can see and the one
# the loader gives every tool. The bake removes the archive's development
# packages to make that so.
#
# This is still asked, because a guest can have two to build against: one
# baked before those packages were dropped, or one where somebody installed
# libibverbs-dev again. The toolchain does not then pick one. gcc searches
# /usr/local/include ahead of /usr/include, ld searches /usr/lib/<multiarch>
# ahead of /usr/local/lib, and the loader's ld.so.conf has /usr/local/lib
# first. perftest's configure finds libibverbs with AC_CHECK_LIB and no -L,
# so with nothing said the tools are compiled against the newer headers,
# linked against the older library and run against the newer one. Where a
# newer header declares a function the older library lacks, the link fails
# with an undefined reference; where it does not, a feature the loaded
# library has is compiled out and nothing says so.
#
# So one small program is compiled, linked and resolved exactly as the tools
# will be, and the three answers are compared before anything is built:
#
#   headers   the infiniband/verbs.h the compiler reads
#   link      the libibverbs.so the linker takes
#   load      the libibverbs.so.1 the loader gives the result
#
# Prints those three lines and then one verdict: "ok: ..." and exit 0 when
# link and load are one file and the headers come from the same prefix,
# "FAIL: ..." and exit 1 otherwise. Needs no RDMA device: the program is
# never run.
#
# PERFTEST_RDMA_LIB_DIR, when it is set and not empty, is the directory the
# caller said its rdma-core's libraries are in (perftest_setup_rdma_core
# "local"). One rdma-core is then not enough: the library linked has to be
# in that directory, or the build is against some other rdma-core than the
# one asked for.
set -u

fail() {
    printf 'FAIL: %s\n' "$1"
    exit 1
}

cppflags='' cflags='' ldflags=''
for arg in "$@"; do
    case "$arg" in
        CPPFLAGS=*) cppflags=${arg#CPPFLAGS=} ;;
        CFLAGS=*) cflags=${arg#CFLAGS=} ;;
        LDFLAGS=*) ldflags=${arg#LDFLAGS=} ;;
    esac
done

cc=${CC:-gcc}
work=$(mktemp -d) || fail "could not make a scratch directory"
trap 'rm -rf "$work"' EXIT

cat > "$work/probe.c" <<'EOF'
#include <infiniband/verbs.h>
int main(void) { return ibv_get_device_list(0) == 0; }
EOF

# The flags are lists, split on spaces as make splits them.
# shellcheck disable=SC2086
pre=$($cc $cppflags $cflags -E "$work/probe.c" 2> "$work/cpp.err") ||
    fail "the compiler cannot find infiniband/verbs.h: $(tr '\n' ' ' < "$work/cpp.err")"
headers=$(printf '%s\n' "$pre" |
    sed -n 's|^# [0-9][0-9]* "\(.*/infiniband/verbs\.h\)".*|\1|p' | sed -n 1p)
[ -n "$headers" ] || fail "could not tell which infiniband/verbs.h the compiler read"
headers=$(readlink -f "$headers")

# --trace has the linker name every file it takes.
# shellcheck disable=SC2086
trace=$($cc $cppflags $cflags -o "$work/probe" "$work/probe.c" \
        $ldflags -libverbs -Wl,--trace 2>&1) ||
    fail "a program that calls libibverbs does not link: $(printf '%s' "$trace" | tail -3 | tr '\n' ' ')"
# shellcheck disable=SC2020
link=$(printf '%s\n' "$trace" | tr ' ()' '\n\n\n' |
    sed -n '/\/libibverbs\.so/p' | sed -n 1p)
[ -n "$link" ] || fail "the linker did not say which libibverbs.so it took"
link=$(readlink -f "$link")

load=$(ldd "$work/probe" 2> /dev/null |
    awk '$1 ~ /^libibverbs\.so/ { print $3 }' | sed -n 1p)
case "$load" in
    /*) load=$(readlink -f "$load") ;;
    *) fail "the loader cannot find the libibverbs.so.1 that $link was linked as" ;;
esac

printf 'headers=%s\nlink=%s\nload=%s\n' "$headers" "$link" "$load"

if [ "$link" != "$load" ]; then
    fail "the tools would be linked against $link and run against $load, with headers from $headers. The guest has two rdma-cores to build against, and the linker has to be given the directory of the library the loader uses: perftest_setup_rdma_core \"local\" with perftest_setup_rdma_lib_dir, which becomes LDFLAGS=-L<dir> on the configure line."
fi
prefix=${headers%/include/infiniband/verbs.h}
case "$link" in
    "$prefix"/lib/libibverbs.so* | "$prefix"/lib64/libibverbs.so* | \
    "$prefix"/lib/*/libibverbs.so*) ;;
    *)
        fail "the tools would be compiled against $headers and linked against $link, which is another rdma-core. Give configure the headers of the library it links, with -I<dir> in perftest_setup_cppflags, which the role puts in CPPFLAGS=."
        ;;
esac
want=${PERFTEST_RDMA_LIB_DIR:-}
if [ -n "$want" ]; then
    case "$link" in
        "$(readlink -f "$want")"/libibverbs.so*) ;;
        *)
            fail "the tools would be linked against $link, which is not in $want, where perftest_setup_rdma_lib_dir says the rdma-core to build against is. With perftest_setup_rdma_core \"local\" that one is the only one the role builds against; on a guest with only the archive's rdma-core, set it to \"archive\"."
            ;;
    esac
fi
printf 'ok: one rdma-core: headers %s, library %s\n' "$headers" "$link"
