#!/usr/bin/env python3
"""Artifact provenance and explicit artifact selection for orbis-ge (GE-A06).

    provenance.py write  <artifact> <dir> <main-file> <fingerprint> <target> <options-json>
    provenance.py check  <artifact> <dir> <main-file> <fingerprint>        exit 0 current, 1 not
    provenance.py select <out.json> <target> <options-json> <name>=<dir>:<main>:<fp>:<want> ...

Every build step writes <main-file>.provenance.json next to its output: artifact name, build target,
source revision (+dirty), patch-set hash (kernel/patches/series and every listed patch),
configuration hash (kernel config fragments, manifest/sources.json), toolchain (container image
ids), options, the step's input fingerprint and the SHA-256 of the main output file.

`select` decides what goes into a release: an artifact is selected only when it was asked for
(want=required|optional-requested) AND its provenance matches the current inputs (same
fingerprint, same checksum of the file on disk). Present but unrequested artifacts (an old BORE
kernel, an old stage-0 image) are listed as "present, not selected" and never slip in. A
requested artifact that is stale or missing makes `select` fail (exit 1) with the reason.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import time


def sha256(path: str) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _git(*a: str) -> str | None:
    try:
        r = subprocess.run(["git", *a], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def patch_set_hash(root: str = ".") -> str:
    h = hashlib.sha256()
    series = os.path.join(root, "kernel/patches/series")
    try:
        with open(series) as fh:
            lines = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    except OSError:
        lines = []
    h.update(b"series\0")
    for l in lines:
        h.update(l.encode() + b"\0" + (sha256(os.path.join(root, "kernel/patches", l)) or "missing").encode() + b"\0")
    return h.hexdigest()


def config_hash(root: str = ".") -> str:
    h = hashlib.sha256()
    for f in sorted(glob.glob(os.path.join(root, "kernel/config/*.fragment"))) + [os.path.join(root, "manifest/sources.json")]:
        h.update(os.path.relpath(f, root).encode() + b"\0" + (sha256(f) or "missing").encode() + b"\0")
    return h.hexdigest()


def toolchain() -> dict:
    out = {}
    eng = os.environ.get("ENGINE") or "podman"
    for img in ("orbis-build:0.2.0",):
        try:
            r = subprocess.run([eng, "image", "inspect", "--format", "{{.Id}}", img], capture_output=True, text=True, timeout=30)
            out[img] = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            out[img] = None
    return out


def write(artifact: str, d: str, main: str, fp: str, target: str, options: dict) -> dict:
    rec = {"schema": "orbis-ge-provenance/1", "artifact": artifact, "target": target,
           "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "source_revision": _git("rev-parse", "HEAD"), "source_dirty": bool(_git("status", "--porcelain")),
           "patch_set_sha256": patch_set_hash(), "config_sha256": config_hash(), "toolchain": toolchain(),
           "options": options, "fingerprint": fp, "main_file": os.path.relpath(main, d) if main.startswith(d) else main,
           "sha256": sha256(main)}
    pf = prov_file(main)
    os.makedirs(os.path.dirname(pf) or ".", exist_ok=True)
    tmp = pf + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1)
    os.replace(tmp, pf)
    return rec


def prov_file(main: str) -> str:
    return main + ".provenance.json"


def check(artifact: str, d: str, main: str, fp: str) -> tuple[bool, str]:
    pf = prov_file(main)
    if not os.path.isfile(main):
        return False, f"{artifact}: {main} missing"
    try:
        with open(pf) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return False, f"{artifact}: no {os.path.basename(pf)} (built before r3 or by hand): rebuild it"
    if rec.get("artifact") != artifact:
        return False, f"{artifact}: provenance names {rec.get('artifact')}"
    if rec.get("fingerprint") != fp:
        return False, f"{artifact}: built from other inputs (fingerprint {str(rec.get('fingerprint'))[:12]} != {fp[:12]})"
    have = sha256(main)
    if rec.get("sha256") != have:
        return False, f"{artifact}: {main} changed after it was built (sha256 {str(have)[:12]} != {str(rec.get('sha256'))[:12]})"
    return True, f"{artifact}: current ({have[:12]})"


def select(out: str, target: str, options: dict, specs: list[str]) -> int:
    sel = {"schema": "orbis-ge-artifact-selection/1", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "target": target, "options": options, "artifacts": {}}
    errors = []
    for spec in specs:
        name, _, rest = spec.partition("=")
        d, main, fp, want = rest.split(":", 3)
        present = os.path.exists(main)
        ok, why = check(name, d, main, fp) if present else (False, f"{name}: not built")
        entry = {"dir": d, "main": main, "present": present, "want": want, "current": ok, "detail": why}
        if want in ("required", "requested"):
            entry["selected"] = ok
            if not ok:
                errors.append(why)
            else:
                with open(prov_file(main)) as fh:
                    prov = json.load(fh)
                entry["provenance"] = prov
        else:   # not asked for this run: never selected, whatever is on disk
            entry["selected"] = False
            if present:
                entry["detail"] = f"{name}: present but not requested ({why}); left out"
        sel["artifacts"][name] = entry
    sel["ok"] = not errors
    sel["errors"] = errors
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sel, f, indent=1)
    os.replace(tmp, out)
    for n, e in sel["artifacts"].items():
        print(f"   {'SELECTED ' if e['selected'] else 'left out '} {n:16} {e['detail']}")
    for e in errors:
        print(f"!!! {e}", file=sys.stderr)
    return 0 if not errors else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "write" and len(argv) == 8:
        rec = write(argv[2], argv[3], argv[4], argv[5], argv[6], json.loads(argv[7] or "{}"))
        print(f"   provenance: {prov_file(argv[4])} ({rec['sha256'] and rec['sha256'][:12]})")
        return 0
    if cmd == "check" and len(argv) == 6:
        ok, why = check(argv[2], argv[3], argv[4], argv[5])
        print(why)
        return 0 if ok else 1
    if cmd == "select" and len(argv) >= 5:
        return select(argv[2], argv[3], json.loads(argv[4] or "{}"), argv[5:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
