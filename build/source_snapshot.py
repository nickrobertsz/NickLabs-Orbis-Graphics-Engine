#!/usr/bin/env python3
"""Hash and archive the exact non-generated GE source worktree, including local edits.

This is intentionally not `git archive HEAD`: an overnight repair can build from
uncommitted PKGBUILDs and patches. The output contains a source file hash list.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIRS = {".git", "out", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                  "target", ".venv", "venv", ".tox", ".cache"}
GENERATED_MESA = {"mesa/stable", "mesa/lib32", "mesa/experimental"}


def sources() -> list[Path]:
    found = []
    for directory, dirs, files in os.walk(ROOT, followlinks=False):
        base = Path(directory)
        relative = base.relative_to(ROOT).as_posix()
        dirs[:] = sorted(d for d in dirs if d not in GENERATED_DIRS and
                         not (relative in GENERATED_MESA and d in {"src", "pkg"}))
        for filename in sorted(files):
            p = base / filename
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith("mesa/") and filename.endswith((".tar.xz", ".pkg.tar.zst")):
                continue
            if filename.endswith((".pyc", ".log", ".pkg.tar.zst")):
                continue
            if p.is_symlink():
                raise SystemExit(f"Source contains symlink; review rather than silently following: {rel}")
            if not p.is_file():
                continue
            if p.stat().st_size > 25 * 1024 * 1024:
                raise SystemExit(f"Unexpected source input exceeds 25 MiB: {rel}")
            found.append(p)
    return sorted(found, key=lambda p: p.relative_to(ROOT).as_posix())


def manifest(files: list[Path]) -> dict:
    entries = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        contents = p.read_bytes()
        entries.append({"path": rel, "sha256": hashlib.sha256(contents).hexdigest(),
                        "size": len(contents), "executable": bool(p.stat().st_mode & 0o111)})
    data = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": "orbis-build-source/1", "sha256": hashlib.sha256(data).hexdigest(), "files": entries}


def archive(destination: Path, files: list[Path], info: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = "nicklabs-orbis-graphics-build-source/"
    with destination.open("wb") as fp:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fp, mtime=0) as gz:
            with tarfile.open(mode="w|", fileobj=gz) as tf:
                for p in files:
                    rel = p.relative_to(ROOT).as_posix()
                    content = p.read_bytes()
                    header = tarfile.TarInfo(prefix + rel)
                    header.size, header.mtime, header.uid, header.gid = len(content), 0, 0, 0
                    header.mode = 0o755 if p.stat().st_mode & 0o111 else 0o644
                    tf.addfile(header, io.BytesIO(content))
                payload = json.dumps(info, indent=2, sort_keys=True).encode() + b"\n"
                header = tarfile.TarInfo(prefix + "BUILD-INPUT-MANIFEST.json")
                header.size, header.mtime, header.mode = len(payload), 0, 0o644
                tf.addfile(header, io.BytesIO(payload))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fingerprint", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    files = sources()
    info = manifest(files)
    if args.archive:
        archive(args.archive, files, info)
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
    if args.fingerprint or not (args.archive or args.manifest):
        print(info["sha256"])


if __name__ == "__main__":
    main()
