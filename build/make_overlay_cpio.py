#!/usr/bin/env python3
"""Build the graphics initramfs overlay (a gzip'd newc cpio) deterministically.

  build/make_overlay_cpio.py diag/stage0/overlay -o stage0/orbis-gfx-overlay.cpio.gz

Appended after the Orbis 0.2.0 initramfs (Linux unpacks concatenated archives in order). Contains
only new files (bin/orbis-gfx-stage0, hooks/pre_newroot_mount/4-orbis-gfx-stage0); the integrator
refuses if any would replace a file of the base initramfs.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import stat
import sys
from pathlib import Path


def newc(entries: list[tuple[str, int, bytes]]) -> bytes:
    buf = io.BytesIO()
    for ino, (name, mode, data) in enumerate(entries + [("TRAILER!!!", 0, b"")], 1):
        nb = name.encode() + b"\0"
        fields = [ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0, len(data), 0, 0, 0, 0, len(nb), 0]
        buf.write(b"070701" + b"".join(f"{x:08X}".encode() for x in fields))
        buf.write(nb + b"\0" * ((-(110 + len(nb))) % 4))
        buf.write(data + b"\0" * ((-len(data)) % 4))
    return buf.getvalue()


def collect(src: Path) -> list[tuple[str, int, bytes]]:
    out = {}
    for root, dirs, files in os.walk(src):
        dirs.sort()
        rel = Path(root).relative_to(src)
        for d in dirs:
            out[str(rel / d) if str(rel) != "." else d] = (stat.S_IFDIR | 0o755, b"")
        for f in sorted(files):
            p = Path(root) / f
            name = str(rel / f) if str(rel) != "." else f
            out[name] = (stat.S_IFREG | (0o755 if os.access(p, os.X_OK) else 0o644), p.read_bytes())
    return [(n, m, d) for n, (m, d) in sorted(out.items())]


def build(src: Path) -> bytes:
    raw = newc(collect(src))
    z = io.BytesIO()
    with gzip.GzipFile(fileobj=z, mode="wb", compresslevel=9, mtime=0, filename="") as g:
        g.write(raw)
    return z.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args(argv)
    data = build(Path(a.src))
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_bytes(data)
    print(f"{a.output}: {len(data)} bytes sha256 {hashlib.sha256(data).hexdigest()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
