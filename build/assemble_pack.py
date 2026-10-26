#!/usr/bin/env python3
"""Assemble ORBIS-GRAPHICS-PACK-v<ver>.tar.zst from a staging directory (last step of build-pack.sh).

  build/assemble_pack.py --staging out/pack --out out/release [--experimental-state BUILT|BLOCKED|NOT_BUILT]
                         [--experimental-reason TEXT] [--base-packages FILE] [--git-commit SHA]

Staging layout (filled by build/in-container-build.sh):
  repo/                 *.pkg.tar.zst (+ .sig), orbis-graphics.db.tar.zst, orbis-graphics.files...
  miniroot/             orbis-gfxdiag-miniroot.tar.zst + .manifest.json
  keys/                 orbis-repo-key.asc + .fingerprint (only when signed)
The assembler adds: stage0/ (overlay cpio), integrator/, docs/, source archive, LICENSES/,
pack-manifest.json and SHA256SUMS, then writes the deterministic archive and its .sha256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

R = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(R / "build"))
import make_overlay_cpio  # noqa: E402

NAME = "NickLabs Orbis Graphics Pack"
VERSION = "1.0.0"
TOP = f"ORBIS-GRAPHICS-PACK-v{VERSION}"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def pkg_meta(f: Path) -> dict:
    stem = f.name[: -len(".pkg.tar.zst")]
    name, ver, rel, arch = stem.rsplit("-", 3)
    return {"name": name, "version": f"{ver}-{rel}", "arch": arch, "file": f"repo/{f.name}", "sha256": sha256(f),
            "signed": (f.parent / (f.name + ".sig")).exists()}


ORBIS_BUILT = {"mesa-orbis-ps4", "opencl-mesa-orbis-ps4", "lib32-mesa-orbis-ps4", "mesa-orbis-ps4-experimental",
               "orbis-graphics", "orbis-graphics-pack", "linux-orbis-ps4-gfx"}


def git_source_archive(dest: Path, commit: str | None) -> Path:
    out = dest / f"NICKLABS-ORBIS-GRAPHICS-SOURCE-v{VERSION}.tar.gz"
    rev = commit if commit and commit != "worktree" else "HEAD"
    if (R / ".git").exists():
        r = subprocess.run(["git", "-C", str(R), "archive", "--format=tar.gz", f"--prefix=nicklabs-orbis-graphics-{VERSION}/",
                            "-o", str(out), rev], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"git archive failed: {r.stderr}")
        return out
    # unpacked source archive (no .git): archive the tree itself, deterministically
    r = subprocess.run(["tar", "--sort=name", "--numeric-owner", "--owner=0", "--group=0", "--mtime=@0",
                        "--exclude=./out", "--exclude=__pycache__", "--transform", f"s,^\\.,nicklabs-orbis-graphics-{VERSION},",
                        "-czf", str(out), "-C", str(R), "."], capture_output=True, text=True, env={"GZIP": "-n", "PATH": "/usr/bin:/bin"})
    if r.returncode != 0:
        raise SystemExit(f"tar of the source tree failed: {r.stderr}")
    return out


def assemble(staging: Path, outdir: Path, exp_state: str, exp_reason: str | None, base_packages: Path | None,
             commit: str | None, now: str | None = None) -> Path:
    top = outdir / TOP
    if top.exists():
        shutil.rmtree(top)
    top.mkdir(parents=True)
    for sub in ("repo", "miniroot", "keys"):
        if (staging / sub).is_dir():
            shutil.copytree(staging / sub, top / sub)
    if not (top / "repo/orbis-graphics.db.tar.zst").exists():
        raise SystemExit("staging/repo has no orbis-graphics.db.tar.zst")
    (top / "stage0").mkdir()
    ov = make_overlay_cpio.build(R / "diag/stage0/overlay")
    (top / "stage0/orbis-gfx-overlay.cpio.gz").write_bytes(ov)
    shutil.copytree(R / "diag/stage0/overlay", top / "stage0/overlay-src")
    shutil.copytree(R / "integrator", top / "integrator", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(R / "orbis-graphics", top / "orbis-graphics")
    (top / "docs").mkdir()
    for d in sorted((R / "docs").glob("*.md")):
        shutil.copy2(d, top / "docs" / d.name)
    (top / "source").mkdir()
    src = git_source_archive(top / "source", commit)
    (top / "LICENSES").mkdir()
    shutil.copy2(R / "LICENSE", top / "LICENSES/LICENSE.orbis-graphics")
    shutil.copy2(R / "mesa/stable/LICENSE.ps4-mesa", top / "LICENSES/LICENSE.ps4-mesa")
    shutil.copy2(R / "manifest/licenses.json", top / "LICENSES/licenses.json")

    pkgs = [pkg_meta(f) for f in sorted((top / "repo").glob("*.pkg.tar.zst"))]
    base = set()
    if base_packages and base_packages.exists():
        base = {l.split()[0] for l in base_packages.read_text().splitlines() if l.strip()}
    mr_man = top / "miniroot/orbis-gfxdiag-miniroot.manifest.json"
    sources = json.loads((R / "manifest/sources.json").read_text())
    manifest = {
        "schema": "orbis-graphics-pack/1", "name": NAME, "version": VERSION,
        "build_id": f"{now or time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{(commit or 'worktree')[:12]}",
        "base": {"orbis": sources["orbis_base"]["version"], "git_tag": sources["orbis_base"]["git_tag"],
                 "kernel_release": sources["orbis_base"]["kernel_release"],
                 "kernel_bzimage_sha256": sources["orbis_base"]["kernel_bzimage_sha256"],
                 "profile": sources["orbis_base"]["profile"], "arch_snapshot": sources["arch_snapshot"]["date"]},
        "kernel": {"replacement": any(p["name"] == "linux-orbis-ps4-gfx" for p in pkgs),
                   "note": "1.0.0 ships no replacement kernel: the Orbis 0.2.0 kernel already has amdgpu, debugfs, "
                           "hwmon/k10temp and every interface the diagnostics read; forward-port candidates are in "
                           "docs/GRAPHICS-KERNEL-MESA-COMPATIBILITY.md and kernel/ (wiring for a -gfx kernel)."},
        "packages": pkgs,
        "orbis_built": sorted(p["name"] for p in pkgs if p["name"] in ORBIS_BUILT),
        "dependency_closure": sorted(p["name"] for p in pkgs if p["name"] not in ORBIS_BUILT),
        "base_packages_excluded": len(base),
        "mesa": {"stable": sources["mesa"]["stable"],
                 "experimental": sources["mesa"]["experimental"]},
        "experimental": {"state": exp_state, "reason": exp_reason},
        "miniroot": json.loads(mr_man.read_text()) if mr_man.exists() else None,
        "stage0_overlay": {"file": "stage0/orbis-gfx-overlay.cpio.gz", "sha256": hashlib.sha256(ov).hexdigest(),
                           "files": ["bin/orbis-gfx-stage0", "hooks/pre_newroot_mount/4-orbis-gfx-stage0"]},
        "source_archive": {"file": f"source/{src.name}", "sha256": sha256(src)},
        "signed": (top / "keys/orbis-repo-key.asc").exists(),
        "firmware": "none: the pack contains no PS4 firmware; images keep the firmware of the Orbis initramfs they were built with",
        "integrator": {"entry": "orbis-graphics", "version": VERSION},
    }
    if manifest["miniroot"]:
        manifest["miniroot"].pop("packages", None)
    (top / "pack-manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return finalize(top)


def finalize(top: Path) -> Path:
    """Write SHA256SUMS over every file of the pack directory, then the deterministic archive + .sha256."""
    outdir = top.parent
    lines = []
    for f in sorted(p for p in top.rglob("*") if p.is_file()):
        rel = f.relative_to(top).as_posix()
        if rel != "SHA256SUMS":
            lines.append(f"{sha256(f)}  {rel}\n")
    (top / "SHA256SUMS").write_text("".join(lines))
    archive = outdir / f"{TOP}.tar.zst"
    r = subprocess.run(["tar", "--sort=name", "--numeric-owner", "--owner=0", "--group=0", "--mtime=@0", "--zstd",
                        "-cf", str(archive), "-C", str(outdir), TOP], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"tar failed: {r.stderr}")
    Path(str(archive) + ".sha256").write_text(f"{sha256(archive)}  {archive.name}\n")
    return archive


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--experimental-state", default="NOT_BUILT", choices=("BUILT", "BLOCKED", "NOT_BUILT"))
    ap.add_argument("--experimental-reason")
    ap.add_argument("--base-packages")
    ap.add_argument("--git-commit")
    a = ap.parse_args(argv)
    arc = assemble(Path(a.staging), Path(a.out), a.experimental_state, a.experimental_reason,
                   Path(a.base_packages) if a.base_packages else None, a.git_commit)
    print(arc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
