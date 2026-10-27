#!/usr/bin/env python3
"""Per-target release receipt + the release manifest (called by `orbis-ge verify`).

    receipt.py --status <release-status.json> --run-id ID --pack P --image I --stage0-image I0
               --base B --base0 B0 --toolchain JSON --orbis-src S --target T --target-dir D --selected-by X

r3.1 (fail-closed):
  * The receipt's "result" is COMPLETE only when the release gate (provenance.py release-status)
    verified every required and requested artifact; otherwise it is FAILED, names the failures,
    lists no image as released, and this program exits 7 so the run cannot report success.
  * An optional stage-0 image that was not requested is "omitted", never a failure.
  * The base-image target decisions (build/target_identity.py) are recorded with their evidence.
  * out/RELEASE-MANIFEST.json separates this run (current_run, one run id) from earlier runs
    (history): an output of an earlier run is never listed as a current-run output.
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

HISTORY_KEEP = 50


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


def atomic_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def update_manifest(path, run_id, t, entry, pack):
    """current_run holds only this run id; a different run id moves the old current_run to history."""
    man = j(path)
    if not isinstance(man, dict) or man.get("schema") != "orbis-ge-release-manifest/2":
        old = man
        man = {"schema": "orbis-ge-release-manifest/2", "current_run": None, "history": []}
        if isinstance(old, dict) and old.get("targets"):     # r3 format: keep it, labelled
            man["history"].append({"run_id": None, "format": "orbis-ge-release-manifest/1 (r3)", "record": old})
    cur = man.get("current_run")
    if not cur or cur.get("run_id") != run_id:
        if cur:
            man["history"].insert(0, cur)
            man["history"] = man["history"][:HISTORY_KEEP]
        cur = {"run_id": run_id, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "targets": {}}
    cur["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    cur["pack"] = pack
    cur["targets"][t] = entry
    cur["result"] = "COMPLETE" if all(e.get("result") == "COMPLETE" for e in cur["targets"].values()) else "FAILED"
    man["current_run"] = cur
    man["note"] = ("current_run lists only artifacts generated or verified by run current_run.run_id; history keeps "
                   "earlier runs for reproducibility (their files may since have been rebuilt or removed)")
    atomic_json(path, man)
    return man


def main(argv):
    ap = argparse.ArgumentParser()
    for k in ("status", "run-id", "pack", "image", "stage0-image", "base", "base0", "toolchain", "orbis-src",
              "target", "target-dir", "selected-by"):
        ap.add_argument("--" + k, default="")
    a = ap.parse_args(argv[1:])
    st = j(a.status) or {"complete": False, "failures": [f"release status {a.status} missing or unreadable"], "artifacts": {}}
    arts = st.get("artifacts") or {}
    complete = bool(st.get("complete")) and arts.get("image", {}).get("status") == "verified"
    failures = list(st.get("failures") or [])
    if not complete and not failures:
        failures.append("the main image did not verify")
    with open("kernel/patches/series") as f:
        series = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    reg = j("targets/generated/registry.json") or {}
    teff = (reg.get("targets") or {}).get(a.target) or {}
    sel = j("out/artifact-selection.json") or {}
    kernels = {}
    for name in ("kernel-ge", "kernel-ge-bore"):
        e = arts.get(name) or {}
        m = j(f"out/{name}/build-manifest.json") or {}
        if e.get("status") == "verified":
            kernels[name] = {"release": m.get("kernel_release"), "bzImage_sha256": e.get("sha256"), "packed": True,
                             "boot_tested": False, "orbis": m.get("orbis_graphics_engine"), "provenance": e.get("provenance")}
        elif e:
            kernels[name] = {"status": e.get("status"), "detail": e.get("detail")}
    not_selected = {n: e.get("detail") for n, e in (sel.get("artifacts") or {}).items() if not e.get("selected") and e.get("present")}

    def image_entry(name, path, base):
        e = arts.get(name) or {}
        if e.get("status") == "verified":
            return {"status": "verified", "file": path, "sha256": e.get("sha256"), "base_image": base or None,
                    "base_sha256": h(base) if base else None, "receipt": (j(path + ".integration.json") or {}).get("result"),
                    "provenance": e.get("provenance")}
        return {"status": e.get("status", "failed"), "file": path if e.get("status") != "omitted" else None,
                "detail": e.get("detail"), "released": False}

    with open("manifest/release.json") as f:
        release = json.load(f)
    rec = {"schema": "orbis-ge-release-receipt/4", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "run_id": a.run_id,
           "result": "COMPLETE" if complete else "FAILED", "failures": failures,
           "target": {"id": a.target, "selected_by": a.selected_by, "name": teff.get("name"), "status": teff.get("status"),
                      "kernel": teff.get("kernel"), "registry_file": teff.get("file")},
           "base_target_check": {"image": j(os.path.join(a.target_dir, "base-target-check.json")),
                                 "stage0": j(os.path.join(a.target_dir, "base0-target-check.json")) if a.base0 else None},
           "release_gate": {n: {k: v for k, v in e.items() if k != "provenance"} for n, e in arts.items()},
           "artifact_selection": {"ok": sel.get("ok"), "errors": sel.get("errors"), "not_selected": not_selected},
           "release": release,
           "source": {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
                      "orbis_0_2_0_commit": git("rev-parse", "HEAD", cwd=a.orbis_src) if a.orbis_src else None},
           "inputs": {"fingerprints": {s: fp(s) for s in ("kernel", "kernel-bore", "loader", "pack", f"image@{a.target}", f"stage0@{a.target}")},
                      "kernel_patch_series": [{"patch": p, "sha256": h("kernel/patches/" + p)} for p in series],
                      "kernel_config_fragments": {f: h(f) for f in sorted(glob.glob("kernel/config/*.fragment"))},
                      "loader_patches": {f: h(f) for f in sorted(glob.glob("loader/*.patch"))},
                      "locks": {f: h(f) for f in ("rust/Cargo.lock", "gaming/runners.lock.json", "gaming/runners.json",
                                                  "manifest/sources.json", "manifest/release.json", "targets/generated/registry.json")}},
           "toolchain": json.loads(a.toolchain or "{}"),
           "kernels": kernels,
           "loader": (arts.get("loader-ge") or {}).get("status"),
           "pack": {"file": a.pack, "status": (arts.get("pack") or {}).get("status"), "sha256": (arts.get("pack") or {}).get("sha256"),
                    "verify": (j("out/pack-verify.json") or {}).get("result")},
           "image": image_entry("image", a.image, a.base),
           "stage0_image": image_entry("stage0-image", a.stage0_image, a.base0),
           "rollback": {"base_image_unchanged": "compare image.base_sha256 with the hash you recorded before",
                        "console": "sudo /usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4",
                        "kernel_only": "sudo orbis boot rollback-kernel",
                        "image": "./orbis-graphics rollback --image <ge image> --output <restored image>"},
           "next": (["write the image to the USB drive (Fedora Media Writer, or dd to the WHOLE USB device, never a PS4 drive)",
                     f"boot it; on the console: sudo orbis-hw identify && sudo orbis-hw qualify --target {a.target} && "
                     "sudo orbis-gpu qualify && sudo orbis-debug collect --hardware"] if complete else
                    ["nothing from this receipt may be flashed: fix the failures above and run ./orbis-ge verify again"])}
    atomic_json(os.path.join(a.target_dir, "ORBIS-GE-RELEASE-RECEIPT.json"), rec)
    entry = {"result": rec["result"], "receipt": os.path.join(a.target_dir, "ORBIS-GE-RELEASE-RECEIPT.json"),
             "status": teff.get("status"), "failures": failures,
             "image": rec["image"].get("file") if complete else None, "image_sha256": rec["image"].get("sha256") if complete else None,
             "stage0_image": rec["stage0_image"].get("file") if complete and rec["stage0_image"].get("status") == "verified" else None,
             "stage0_sha256": rec["stage0_image"].get("sha256") if complete else None,
             "stage0": rec["stage0_image"].get("status")}
    update_manifest("out/RELEASE-MANIFEST.json", a.run_id, a.target, entry, {"file": a.pack, "sha256": rec["pack"]["sha256"]})
    print(json.dumps({k: rec[k] for k in ("result", "failures", "target", "image", "stage0_image")}, indent=1, default=str)[:4000])
    if not complete:
        print(f"!!! release of {a.target} FAILED: " + "; ".join(failures), file=sys.stderr)
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
