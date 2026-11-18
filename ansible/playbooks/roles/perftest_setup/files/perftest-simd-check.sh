#!/bin/sh
# Which SIMD level a configured perftest tree will compile its host-memory
# validator for, whether the tree needs the _mm_pause workaround, and a
# refusal unless both are what the caller asked for.
#
#   perftest-simd-check.sh DIR LEVEL WORKAROUND
#
# DIR is the tree, after ./configure. LEVEL is avx2, sse4.2 or none, or
# empty when the caller left the choice to configure. WORKAROUND is true or
# false: whether the caller put "-include xmmintrin.h" on the configure
# line.
#
# The level. src/host_validation.c picks its validator at compile time:
# HAVE_AVX512, else HAVE_AVX2, else HAVE_SSE42, else HAVE_NEON, else none,
# and with none the tools refuse --data_validation on host memory. configure
# decides the x86 ones by compiling a program and RUNNING it on the machine
# that builds, so the answer is that machine's CPU: AVX2 on a guest given
# QEMU's EPYC model, nothing at all on the default model a bake boots with
# (qemu64, which has neither AVX2 nor SSE4.2). An image is built on one
# machine and run on others, so the role names the level instead:
# cross_compiling=yes on the configure line stops configure running
# anything, which leaves both macros undefined in config.h, and the level
# goes in as -DHAVE_<level>=1 in CPPFLAGS with its compiler flag in
# SIMD_CFLAGS, which Makefile.am applies to host_validation.o alone. None of
# that is an interface the tree promises, so it is read back here from what
# the compiler will be given: the macros a file that includes config.h sees
# with every flag make puts on the compile line of that one object (the
# Makefile's DEFS, CPPFLAGS and CFLAGS, then SIMD_CFLAGS), and the Makefile's
# SIMD_CFLAGS. A configure that stopped honouring any of the three shows up
# as another level, and so does a macro that came in another way, in a
# CFLAGS= of the caller's for one; the build stops before it compiles.
#
# The workaround. At e68c74e the same file defines cpu_relax() as
# _mm_pause() for every x86 build and includes a header that declares it
# only when a SIMD level is compiled in. With none, gcc 14 and later stop on
# the implicit declaration; gcc 13 and earlier warn, compile a call to a
# function nothing defines, and stop in the linker. That is asked of the
# tree itself: the file is compiled with no SIMD macro and no forced
# include, with the implicit declaration made an error whatever the
# compiler's own default is, and the tree has the bug when the compiler
# names _mm_pause. A tree that has it with the workaround off is refused
# here, in words, and not left to make.
#
# Prints
#
#   simd=<avx512|avx2|sse4.2|neon|none>   what the validator is compiled for
#   simd_cflags=<flags>                   what host_validation.o is given
#   mm_pause_bug=<true|false|unknown>     whether the tree needs the include
#
# and one verdict: "ok: ..." and exit 0, or "FAIL: ..." and exit 1. Compiles
# nothing that is kept and runs nothing it compiles, so the answer does not
# depend on the CPU it is asked on.
set -u

fail() {
    printf 'FAIL: %s\n' "$1"
    exit 1
}

[ $# -eq 3 ] || fail "usage: $0 DIR LEVEL WORKAROUND"
dir=$1 want=$2 workaround=$3

case "$want" in
    avx2) want_cflags=-mavx2 ;;
    sse4.2) want_cflags=-msse4.2 ;;
    none | '') want_cflags='' ;;
    *) fail "'$want' is not a SIMD level: avx2, sse4.2, none, or empty to leave it to configure" ;;
esac
case "$workaround" in
    true | false) ;;
    *) fail "'$workaround' is not true or false" ;;
esac
for file in config.h Makefile src/host_validation.c; do
    [ -f "$dir/$file" ] || fail "no $file in $dir: the tree has not been configured"
done

# What make will run and hand it. An automake Makefile has each on one line,
# as configure substituted it. The variable is empty when the Makefile has
# no such line, and when it has the name and nothing after it.
setting() {
    sed -n "s/^$1 =//p" "$dir/Makefile" | sed -n 1p | sed 's/^ *//; s/ *$//'
}
cc=$(setting CC)
defs=$(setting DEFS)
cppflags=$(setting CPPFLAGS)
cflags=$(setting CFLAGS)
simd_cflags=$(setting SIMD_CFLAGS)
[ -n "$cc" ] || fail "$dir/Makefile names no compiler"

work=$(mktemp -d) || fail "could not make a scratch directory"
trap 'rm -rf "$work"' EXIT

# The macros src/host_validation.c sees: config.h, as every file includes
# it, with the flags of its compile line in the order automake gives them,
# $(DEFS) -I. $(CPPFLAGS) $(CFLAGS), and SIMD_CFLAGS, which Makefile.am adds
# to the CFLAGS of that object. All of them and not CPPFLAGS alone: a -D or
# a -U is a preprocessor flag wherever it is written, and a level named in
# a CFLAGS= is the level the compiler builds. The flags are lists, split on
# spaces as make splits them.
printf '#include "config.h"\n' > "$work/level.c"
# shellcheck disable=SC2086
macros=$($cc $defs -I"$dir" $cppflags $cflags $simd_cflags -E -dM \
    "$work/level.c" 2> "$work/cpp.err") ||
    fail "the compiler cannot preprocess config.h with the build's flags: $(tr '\n' ' ' < "$work/cpp.err")"
defined() {
    printf '%s\n' "$macros" | grep -Eq "^#define $1( |\$)"
}
# The order host_validation.c takes them in.
if defined HAVE_AVX512; then
    simd=avx512
elif defined HAVE_AVX2; then
    simd=avx2
elif defined HAVE_SSE42; then
    simd=sse4.2
elif defined HAVE_NEON; then
    simd=neon
else
    simd=none
fi

# The bug, asked of the tree and of nothing the caller added: no config.h,
# so no SIMD macro, and no forced include. _GNU_SOURCE is what configure
# puts in CFLAGS, and without it the file stops on CPU_SET first. The
# implicit declaration is made an error by name: gcc 14 and later do that
# unasked, and an earlier one only warns here and fails the build later, in
# the linker, so left to the compiler's default the answer would be "no bug"
# on exactly the compilers that cannot say what is wrong. A compile that
# fails without naming _mm_pause says nothing either way.
if (cd "$dir" && LC_ALL=C $cc -fsyntax-only -D_GNU_SOURCE -I. -Isrc \
        -Werror=implicit-function-declaration \
        src/host_validation.c) > "$work/bug.out" 2>&1; then
    bug=false
elif grep -q '_mm_pause' "$work/bug.out"; then
    bug=true
else
    bug=unknown
fi

printf 'simd=%s\nsimd_cflags=%s\nmm_pause_bug=%s\n' "$simd" "$simd_cflags" "$bug"

if [ -n "$want" ]; then
    if [ "$simd" != "$want" ]; then
        fail "the validator would be compiled for $simd, and perftest_setup_simd asks for $want. The role names the level with cross_compiling=yes, SIMD_CFLAGS= and -DHAVE_<level>=1 in CPPFLAGS on the configure line; config.h and the Makefile say the compiler would be given another: either this configure did something else with those three, and the level is the one the CPU of this machine has, or a HAVE_<level> macro came in another way, such as a CFLAGS= in perftest_setup_configure_extra. Nothing was compiled."
    fi
    if [ "$simd_cflags" != "$want_cflags" ]; then
        fail "the validator would be compiled for $simd with '$simd_cflags', and that level is built with '$want_cflags'. configure did not keep the SIMD_CFLAGS it was given. Nothing was compiled."
    fi
fi
if [ "$bug" = true ] && [ "$workaround" = false ]; then
    fail "src/host_validation.c of this tree calls _mm_pause() and includes a header that declares it only when a SIMD level is compiled in, which gcc 14 and later refuse and earlier ones cannot link, and perftest_setup_mm_pause_workaround is off. Turn it back on, or pin a commit that has the fix. Nothing was compiled."
fi

note=''
if [ "$bug" = false ] && [ "$workaround" = true ]; then
    note='; this tree declares _mm_pause by itself, so perftest_setup_mm_pause_workaround can be turned off'
fi
if [ -n "$want" ]; then
    printf 'ok: the validator is compiled for %s, as asked%s\n' "$simd" "$note"
else
    printf 'ok: the validator is compiled for %s, which configure chose from the CPU of this machine%s\n' "$simd" "$note"
fi
