#!/usr/bin/env python3
"""Refuse to build from anything but the pinned commits in manifest/sources.json.

  build/check_sources.py --src DIR [--variant stable|experimental]

DIR holds checkouts named meson, mako, libdrm and mesa-<dir_suffix>.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[1]


def head(d: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--variant", default="stable", choices=("stable", "experimental"))
    a = ap.parse_args(argv)
    s = json.loads((R / "manifest/sources.json").read_text())
    src = Path(a.src)
    m = s["mesa"][a.variant]
    checks = [("meson", s["meson"]["commit"]), ("mako", s["mako"]["commit"]),
              ("libdrm", s["libdrm"]["commit"]), (f"mesa-{m['dir_suffix']}", m["commit"])]
    bad = 0
    for name, want in checks:
        got = head(src / name)
        if want is None:
            print(f"UNPINNED  {name}: manifest has no commit; refusing", file=sys.stderr)
            bad += 1
        elif got != want:
            print(f"MISMATCH  {name}: {got} != {want}", file=sys.stderr)
            bad += 1
        else:
            print(f"ok        {name} {want[:12]}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
