#!/usr/bin/env python3
"""Per-target release receipt + the run's release manifest (called by `orbis-ge verify`).

    receipt.py <pack> <image|""> <stage0-image|""> <base> <base0> <toolchain-json> <orbis-src> <target> <target-dir> <selected-by>

Writes <target-dir>/ORBIS-GE-RELEASE-RECEIPT.json and merges the target into out/RELEASE-MANIFEST.json.
Only artifacts selected for this run (out/artifact-selection.json) and images that passed their
provenance check (passed in by orbis-ge; empty string = not current) are described as released.
"""
import glob
import hashlib
import json
import os
import subprocess
import sys
import time


def h(p):
    if not p or not os.path.isfile(p):
        return None
    d = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            d.update(c)
    return d.hexdigest()


def j(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def git(*a, cwd="."):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def fp(step):
    try:
        with open(f"out/.fp/{step}") as f:
            return f.read().strip()
    except OSError:
        return None


def main(argv):
    pack, img, img0, base, base0, toolchain, orbis_src, target, tdir, target_by = argv[1:11]
    with open("kernel/patches/series") as f:
        series = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    reg = j("targets/generated/registry.json") or {}
    teff = (reg.get("targets") or {}).get(target) or {}
    sel = j("out/artifact-selection.json") or {}
    kernels, not_selected = {}, {}
    for name in ("kernel-ge", "kernel-ge-bore", "kernel-ge-uvd", "kernel-ge-sg"):
        m = j(f"out/{name}/build-manifest.json")
        a = (sel.get("artifacts") or {}).get(name) or {}
        if m and a.get("selected"):
            kernels[name] = {"release": m.get("kernel_release"), "bzImage_sha256": h(f"out/{name}/bzImage"),
                             "packed": True, "boot_tested": False, "orbis": m.get("orbis_graphics_engine"),
                             "provenance": a.get("provenance")}
        elif m:
            not_selected[name] = a.get("detail") or "present, not selected"
    repo_pkgs = [{"file": os.path.basename(f), "sha256": h(f)} for f in sorted(glob.glob("out/pack/repo/*.pkg.tar.zst"))]
    with open("manifest/release.json") as f:
        release = json.load(f)
    rec = {"schema": "orbis-ge-release-receipt/3", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "target": {"id": target, "selected_by": target_by, "name": teff.get("name"), "status": teff.get("status"),
                      "kernel": teff.get("kernel"), "registry_file": teff.get("file")},
           "artifact_selection": {"ok": sel.get("ok"), "errors": sel.get("errors"), "not_selected": not_selected},
           "release": release,
           "source": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
                      "orbis_0_2_0_commit": git("rev-parse", "HEAD", cwd=orbis_src) if orbis_src else None},
           "inputs": {"fingerprints": {s: fp(s) for s in ("kernel", "kernel-bore", "loader", "pack", f"image@{target}", f"stage0@{target}")},
                      "kernel_patch_series": [{"patch": p, "sha256": h("kernel/patches/" + p)} for p in series],
                      "kernel_config_fragments": {f: h(f) for f in sorted(glob.glob("kernel/config/*.fragment"))},
                      "loader_patches": {f: h(f) for f in sorted(glob.glob("loader/*.patch"))},
                      "locks": {f: h(f) for f in ("rust/Cargo.lock", "gaming/runners.lock.json", "gaming/runners.json",
                                                  "manifest/sources.json", "manifest/release.json", "targets/generated/registry.json")}},
           "toolchain": json.loads(toolchain),
           "kernels": kernels,
           "loader": j("out/loader-ge/build.json"),
           "pack": {"file": pack, "sha256": h(pack), "packages": repo_pkgs, "verify": (j("out/pack-verify.json") or {}).get("result"),
                    "provenance": j(pack + ".provenance.json")},
           "image": ({"file": img, "sha256": h(img), "base_image": base or None, "base_sha256": h(base) if base else None,
                      "receipt": (j(img + ".integration.json") or {}).get("result"), "provenance": j(img + ".provenance.json")}
                     if img else {"skipped": "no current image for this run's inputs"}),
           "stage0_image": ({"file": img0, "sha256": h(img0), "base_image": base0, "base_sha256": h(base0),
                             "receipt": (j(img0 + ".integration.json") or {}).get("result"), "provenance": j(img0 + ".provenance.json")}
                            if img0 else {"skipped": "no --base-stage0-image given" if not base0 else "stage-0 image not current"}),
           "rollback": {"base_image_unchanged": "compare image.base_sha256 with the hash you recorded before",
                        "console": "sudo /usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4",
                        "kernel_only": "sudo orbis boot rollback-kernel",
                        "image": "./orbis-graphics rollback --image <ge image> --output <restored image>"},
           "next": ["write the image to the USB drive (Fedora Media Writer, or dd to the WHOLE USB device, never a PS4 drive)",
                    f"boot it; on the console: sudo orbis-hw identify && sudo orbis-hw qualify --target {target} && "
                    "sudo orbis-gpu qualify && sudo orbis-debug collect --hardware"]}
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "ORBIS-GE-RELEASE-RECEIPT.json"), "w") as f:
        json.dump(rec, f, indent=1)
    man = j("out/RELEASE-MANIFEST.json") or {"schema": "orbis-ge-release-manifest/1", "targets": {}}
    man["time"] = rec["time"]
    man["pack"] = {"file": pack, "sha256": rec["pack"]["sha256"]}
    man["targets"][target] = {"receipt": os.path.join(tdir, "ORBIS-GE-RELEASE-RECEIPT.json"), "status": teff.get("status"),
                              "image": rec["image"].get("file"), "image_sha256": rec["image"].get("sha256"),
                              "stage0_image": rec["stage0_image"].get("file"), "stage0_sha256": rec["stage0_image"].get("sha256")}
    with open("out/RELEASE-MANIFEST.json", "w") as f:
        json.dump(man, f, indent=1)
    print(json.dumps({k: rec[k] for k in ("target", "source", "kernels", "pack", "image", "stage0_image")}, indent=1, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
