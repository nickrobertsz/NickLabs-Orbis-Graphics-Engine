#!/usr/bin/env python3
"""Precise input sets for orbis-ge step fingerprints (r3.1, audit P2-8).

    fingerprint.py hash <set> [extra path ...]     sha256 over the set's files (names + contents)
    fingerprint.py list <set>                      the files a set covers (for review and tests)

Each set lists what the step's output is actually built from, nothing more: a change to
research/, tests/ or documentation never invalidates the kernel, the loader or a compiled package.
Documentation is an input of the pack only (orbis-graphics-docs); inside the pack the per-package
cache (build/in-container-build.sh, mkc) keys each package by its own inputs, so a documentation
change rebuilds the documentation package and nothing else.

Dynamic inputs (container image ids, the 0.2.0 checkout, the pack checksum, options, target) are
added by orbis-ge itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

SKIP_DIRS = {".git", "__pycache__", "target", "out"}


def _series_files(root: str = ".") -> list[str]:
    series = os.path.join(root, "kernel/patches/series")
    out = [series]
    try:
        with open(series) as f:
            out += [os.path.join(root, "kernel/patches", l.strip()) for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        pass
    return out


KERNEL_COMMON = ["kernel/build-ge-kernel.sh", "kernel/config", "@series", "@sources:orbis_base,kernel_series"]
SETS = {
    "kernel": KERNEL_COMMON,
    "kernel-bore": KERNEL_COMMON + ["kernel/patches/variants/bore"],
    "kernel-uvd": KERNEL_COMMON + ["kernel/patches/research/uvd-feernt-engine2-6.15.diff"],
    "kernel-sg": KERNEL_COMMON + ["kernel/patches/research/sg-scanout-liverpool-6.15.diff"],
    "loader": ["loader", "@sources:loader"],
    # everything the in-container build reads; research/ and tests/ are not inputs of any package
    "pack": ["build", "packages", "mesa", "diag", "tools", "targets", "rust", "gaming", "integrator/root",
             "integrator/vendor", "kernel/package", "kernel/package-bore", "manifest", "LICENSE", "docs",
             "README.md", "CHANGELOG.md"],
    "image": ["integrator", "orbis-graphics", "build/target_identity.py"],
    "stage0": ["integrator", "orbis-graphics", "build/target_identity.py", "diag/stage0", "diag/miniroot"],
}


def expand(spec: list[str], root: str = ".") -> tuple[list[str], list[tuple[str, bytes]]]:
    """-> (files, synthetic entries)"""
    files, synth = [], []
    for item in spec:
        if item == "@series":
            files += _series_files(root)
        elif item.startswith("@sources:"):
            keys = item.split(":", 1)[1].split(",")
            try:
                with open(os.path.join(root, "manifest/sources.json")) as f:
                    src = json.load(f)
                synth.append((item, json.dumps({k: src.get(k) for k in keys}, sort_keys=True).encode()))
            except (OSError, ValueError):
                synth.append((item, b"missing"))
        else:
            p = os.path.join(root, item)
            if os.path.isfile(p):
                files.append(p)
            elif os.path.isdir(p):
                for d, dirs, fs in os.walk(p):
                    dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
                    files += [os.path.join(d, f) for f in fs if not f.endswith((".pyc", ".log"))]
            else:
                synth.append((item, b"missing"))
    return sorted(set(files)), synth


def digest(spec: list[str], root: str = ".") -> str:
    files, synth = expand(spec, root)
    h = hashlib.sha256()
    for name, data in synth:
        h.update(name.encode() + b"\0" + data + b"\0")
    for f in files:
        h.update(os.path.relpath(f, root).encode() + b"\0")
        try:
            with open(f, "rb") as fh:
                for c in iter(lambda: fh.read(1 << 20), b""):
                    h.update(c)
        except OSError as e:
            h.update(b"unreadable:" + str(e).encode())
    return h.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("hash", "list") or argv[2] not in SETS:
        print(__doc__ + "\nsets: " + ", ".join(SETS), file=sys.stderr)
        return 2
    spec = SETS[argv[2]] + argv[3:]
    if argv[1] == "list":
        files, synth = expand(spec)
        print("\n".join([n for n, _ in synth] + files))
        return 0
    print(digest(spec))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
