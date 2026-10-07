#!/usr/bin/env python3
"""Raise LMCache's C++ standard from 17 to 20, and quieten a noisy warning.

Run against an unpacked LMCache source tree before building it.

WHY THIS IS NEEDED. LMCache 0.5.5 hardcodes `-std=c++17` for its C++ (host)
sources in two places, while handing its `.hip` sources `-std=c++20`. Modern
libtorch headers require C++20, so the host half fails against a torch new
enough to say so:

    torch/include/torch/csrc/api/include/torch/all.h:5:2: error:
        C++20 or later compatible compiler is required to use PyTorch.
    c10/util/intrusive_ptr.h:775:27: error:
        no type named 'strong_ordering' in namespace 'std'
    c10/util/TypeIndex.h:56:25: error:
        no member named 'starts_with' in 'std::basic_string_view<char>'

`std::strong_ordering` and `string_view::starts_with` are C++20; the
diagnostics are all the same cause. Upstream does not hit this because their
Dockerfile installs whatever torch the vLLM ROCm wheels pin, which is older.

WHY A PATCH AND NOT A FLAG. The flag cannot be won from outside: setuptools
appends `extra_compile_args` LAST on the command line, and for `-std=` the
last one wins, so `CXXFLAGS=-std=c++20` is overridden by the hardcoded
`-std=c++17` that follows it. The source is the only place to change it.

Removing this: when LMCache stops hardcoding c++17, or raises it to c++20,
`--check` here starts reporting no sites and the Dockerfile step can go.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Both sites, with the file they live in at v0.5.5. Named explicitly rather
# than globbed so that a tree which has moved them fails loudly instead of
# being silently left at c++17 -- which would reproduce the original error a
# long way from here.
_TARGETS = (
    "setup_extensions/common_cpp.py",
    "setup_extensions/build_profiles/rocm.py",
)

_OLD = '"-std=c++17"'
# -Wno-unused-value silences the [-Wunused-value] nodiscard warnings LMCache's
# own HIP sources generate (event_recorder_hip.cpp, phase_timing_recorder.hip
# ignore hipError_t returns). They are upstream's to fix; here they are pure
# noise across every translation unit.
_NEW = '"-std=c++20", "-Wno-unused-value"'


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(f"usage: {sys.argv[0]} <lmcache-source-dir> [--check]")
    root = Path(argv[0])
    check = "--check" in argv[1:]

    patched = 0
    for rel in _TARGETS:
        path = root / rel
        if not path.is_file():
            sys.exit(
                f"Error: {path} does not exist. LMCache moved its build "
                "profiles; re-find the -std=c++17 sites before building, or "
                "the host sources compile at C++17 against a C++20 libtorch "
                "and fail with 'C++20 or later compatible compiler is "
                "required'."
            )
        text = path.read_text()
        if _OLD not in text:
            # Not fatal on its own: the point is that at least one site
            # matched. A version that already uses c++20 needs no patch.
            print(f"  {rel}: no {_OLD} found, skipping")
            continue
        if not check:
            path.write_text(text.replace(_OLD, _NEW))
        print(f"  {rel}: {_OLD} -> {_NEW}")
        patched += 1

    if patched == 0:
        sys.exit(
            "Error: no -std=c++17 site found anywhere in " + str(root) + ".\n"
            "Either LMCache fixed this upstream -- in which case delete this "
            "script and its Dockerfile step -- or it moved the flag and the "
            "build is about to fail on libtorch's C++20 requirement."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
