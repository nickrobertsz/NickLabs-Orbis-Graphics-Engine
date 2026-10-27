#!/usr/bin/env python3
"""Prove the Orbis 0.2.0 checkout is not modified by a Graphics Engine build (GE-A05).

    orbis_src_guard.py snapshot <orbis-src> <state.json>
    orbis_src_guard.py verify   <orbis-src> <state.json>     exit 0 unchanged, 9 changed

What is recorded: the git HEAD and `git status --porcelain` (every tracked or untracked change
outside ignored paths) plus, for the paths the build reads (platform/, kernel/, out/repo/,
out/keys/, out/rootfs/packages.txt, Makefile, packaging/), every file's size, mode, mtime and
inode, and the SHA-256 of every file up to 64 MiB (larger files - packages in out/repo - are
covered by size+mtime+inode; a rewrite in place would change mtime). The build never writes
there: it copies what it needs (see build-pack.sh); this check is the evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

READ_PATHS = ("platform", "kernel", "packaging", "Makefile", "out/repo", "out/keys", "out/rootfs/packages.txt")
HASH_LIMIT = 64 << 20


def _git(src: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", src, *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def snapshot(src: str) -> dict:
    files: dict[str, list] = {}
    for rel in READ_PATHS:
        top = os.path.join(src, rel)
        if os.path.isfile(top) or os.path.islink(top):
            walk = [(os.path.dirname(top), [], [os.path.basename(top)])]
        elif os.path.isdir(top):
            walk = os.walk(top)
        else:
            files[rel] = ["missing"]
            continue
        for d, dirs, fs in walk:
            dirs[:] = sorted(x for x in dirs if x not in ("__pycache__",))
            for name in sorted(fs):
                p = os.path.join(d, name)
                key = os.path.relpath(p, src)
                try:
                    st = os.lstat(p)
                except OSError as e:
                    files[key] = ["unreadable", str(e)]
                    continue
                ent = [st.st_size, st.st_mode, st.st_mtime_ns, st.st_ino]
                if os.path.isfile(p) and not os.path.islink(p) and st.st_size <= HASH_LIMIT:
                    try:
                        ent.append(_hash(p))
                    except OSError as e:
                        ent.append("unreadable:" + str(e))
                files[key] = ent
    return {"schema": "orbis-src-guard/1", "src": os.path.realpath(src), "head": (_git(src, "rev-parse", "HEAD") or "").strip() or None,
            "status": _git(src, "status", "--porcelain", "--untracked-files=normal"), "files": files}


def diff(a: dict, b: dict) -> list[str]:
    out = []
    if a.get("head") != b.get("head"):
        out.append(f"git HEAD {a.get('head')} -> {b.get('head')}")
    if a.get("status") != b.get("status"):
        out.append("git status changed:\n" + (b.get("status") or "")[:2000])
    fa, fb = a.get("files", {}), b.get("files", {})
    for k in sorted(set(fa) | set(fb)):
        if k not in fb:
            out.append(f"removed: {k}")
        elif k not in fa:
            out.append(f"added: {k}")
        elif fa[k] != fb[k]:
            out.append(f"changed: {k}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("snapshot", "verify"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, src, state = argv[1:]
    now = snapshot(src)
    if cmd == "snapshot":
        tmp = state + ".tmp"
        with open(tmp, "w") as f:
            json.dump(now, f)
        os.replace(tmp, state)
        print(f"orbis-src guard: {len(now['files'])} entries recorded for {src}")
        return 0
    with open(state) as f:
        before = json.load(f)
    d = diff(before, now)
    if d:
        print(f"!!! the Orbis 0.2.0 checkout {src} CHANGED during the build ({len(d)} differences):", file=sys.stderr)
        for line in d[:50]:
            print("    " + line, file=sys.stderr)
        return 9
    print(f"orbis-src guard: {src} unchanged ({len(now['files'])} entries, HEAD {now['head'] or 'n/a'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
