"""orbis-gpu — command line for the Orbis Graphics Pack (also the GUI's machine interface).

  orbis-gpu health [--json]
  orbis-gpu qualify [--json-out F] [--text-out F] [--ring-test] [--sustained S] [--transitions]
                    [--recovery --i-accept-gpu-reset] [--video-sample FILE] [--level 1|2] [--frames N]
  orbis-gpu profile list|show [--json]
  orbis-gpu profile set NAME [--dry-run] [--reason TEXT]
  orbis-gpu profile reset [--dry-run]
  orbis-gpu profile personality MODE      (apply the default profile of pc|gaming|ai; used by systemd)
  orbis-gpu capabilities [--json]
  orbis-gpu version

Exit codes for qualify: 0 PASS, 1 DEGRADED/NOT_RUN, 2 FAIL/BLOCKED.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import API_VERSION, __version__, health, profile, qualify, sysfs

RESULTS_DIR = "var/lib/orbis-graphics"


def _save(name: str, text: str) -> None:
    try:
        d = sysfs.p(RESULTS_DIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    except OSError:
        pass


def capabilities() -> dict:
    last = {}
    try:
        last = json.loads(sysfs.p(RESULTS_DIR, "last-qualify.json").read_text())
    except (OSError, ValueError):
        pass
    by_id = {c["id"]: c for c in last.get("checks", [])}

    def st(cid):
        c = by_id.get(cid)
        return {"state": c["state"], "detail": c.get("detail"), "blocker": c.get("blocker")} if c else \
            {"state": "NOT_RUN", "detail": "run: sudo orbis-gpu qualify"}
    return {"schema": "orbis-gfx-capabilities/1", "version": __version__, "api": API_VERSION,
            "qualified_at": last.get("time"), "overall": last.get("overall", "NOT_RUN"),
            "capabilities": {
                "opengl.hardware": st("gl.renderer"), "opengl.correctness": st("gl.shader"),
                "vulkan.device": st("vk.device"), "vulkan.raster": st("vk.raster"), "vulkan.compute": st("vk.compute"),
                "vulkan.present": st("vk.present"), "opencl": st("cl.rusticl"), "video.decode": st("video.decode"),
                "display.hdmi": st("display.connector"), "display.edid": st("display.edid"),
                "power.clocks": st("pm.clocks"), "sensors.gpu": st("sensors.gpu"),
                "stability.sustained": st("perf.sustained"), "stability.recovery": st("gpu.recovery"),
                "modes.transitions": st("modes.transitions")},
            "profiles": profile.capabilities()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-gpu", description="NickLabs Orbis Graphics Pack")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health"); h.add_argument("--json", action="store_true")
    q = sub.add_parser("qualify")
    q.add_argument("--json-out"); q.add_argument("--text-out"); q.add_argument("--ring-test", action="store_true")
    q.add_argument("--sustained", type=int); q.add_argument("--transitions", action="store_true")
    q.add_argument("--recovery", action="store_true"); q.add_argument("--i-accept-gpu-reset", action="store_true")
    q.add_argument("--video-sample"); q.add_argument("--level", type=int, default=1); q.add_argument("--frames", type=int, default=120)
    q.add_argument("--timeout", type=float, default=120); q.add_argument("--probe")
    q.add_argument("--stage", default="full-system"); q.add_argument("--quiet", action="store_true")
    pr = sub.add_parser("profile")
    pr.add_argument("action", choices=("list", "show", "set", "reset", "personality"))
    pr.add_argument("name", nargs="?"); pr.add_argument("--json", action="store_true")
    pr.add_argument("--dry-run", action="store_true"); pr.add_argument("--reason", default="manual")
    c = sub.add_parser("capabilities"); c.add_argument("--json", action="store_true")
    sub.add_parser("version")
    a = ap.parse_args(argv)

    if a.cmd == "version":
        print(json.dumps({"orbis-gpu": __version__, "api": API_VERSION}))
        return 0
    if a.cmd == "health":
        s = health.snapshot()
        print(json.dumps(s, indent=1) if a.json else health.render_text(s), end="" if not a.json else "\n")
        return 0 if s["status"] == "OK" else 1
    if a.cmd == "capabilities":
        d = capabilities()
        if a.json:
            print(json.dumps(d, indent=1))
        else:
            for k, v in d["capabilities"].items():
                print(f"{v['state']:<11} {k:<22} {v.get('detail') or v.get('blocker') or ''}")
        return 0
    if a.cmd == "profile":
        if a.action in ("list", "show"):
            d = profile.listing()
            if a.json:
                print(json.dumps(d, indent=1))
            else:
                print(f"active: {d['active'] or 'none (kernel defaults)'}")
                for n, p in d["profiles"].items():
                    print(f"  {n:<12} {p['description']}")
                for k, v in d["capabilities"].items():
                    print(f"  knob {k:<15} {'available' if v['available'] else 'unavailable: ' + (v.get('reason') or '')}")
            return 0
        if a.action == "reset":
            print(json.dumps(profile.reset(dry_run=a.dry_run), indent=1))
            return 0
        name = a.name
        if a.action == "personality":
            if a.name not in ("pc", "gaming", "ai"):
                print("personality needs pc|gaming|ai", file=sys.stderr)
                return 2
            name = profile.personality_default(a.name)
            a.reason = f"personality {a.name}"
        if not name:
            print("profile set needs a NAME", file=sys.stderr)
            return 2
        try:
            print(json.dumps(profile.apply(name, reason=a.reason, dry_run=a.dry_run), indent=1))
        except (KeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    # qualify
    if os.geteuid() != 0 and not a.quiet:
        print("note: not root; debugfs/dmesg checks will report NOT_RUN (use sudo)", file=sys.stderr)
    opts = {"ring_test": a.ring_test, "sustained": a.sustained, "transitions": a.transitions,
            "recovery_accept": a.recovery and a.i_accept_gpu_reset, "video_sample": a.video_sample,
            "level": a.level, "frames": a.frames, "timeout": a.timeout, "stage": a.stage}
    if a.probe:
        opts["probe"] = a.probe
    rep = qualify.qualify(opts)
    js, txt = rep.to_json(), rep.render_text()
    if a.json_out:
        Path(a.json_out).write_text(js + "\n")
    if a.text_out:
        Path(a.text_out).write_text(txt)
    _save("last-qualify.json", js + "\n")
    if not a.quiet:
        print(txt, end="")
    return {"PASS": 0, "DEGRADED": 1, "NOT_RUN": 1}.get(rep.overall, 2)


if __name__ == "__main__":
    raise SystemExit(main())
