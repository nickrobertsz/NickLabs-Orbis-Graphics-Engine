"""Command-line entry points of the Graphics Engine (one module, several programs):

  orbis-hw       hardware status/telemetry/capabilities and individual controls
  orbis-mem      unified-memory ownership, boot-time plan and runtime budget (+ the orbis-memd loop)
  orbis-debug    Black Box front end (see debug.py)
  orbis-video    video decode status, hardware-vs-software test, player configuration
  orbis-ai-gpu   Vulkan inference variant campaign, recommendation and application
  orbis-blackbox the recording daemon (systemd: orbis-blackbox.service)
  orbis-hwd      the settings/telemetry HTTP API (systemd: orbis-hwd.service)
Exit codes: 0 success, 1 the request ran but reported a problem, 2 usage/refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, ai, blackbox, controls, debug, hwd, kdump, memory, telemetry, video


def _out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
    else:
        sys.stdout.write(debug.render(obj))


def _common(ap):
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")


def main_hw(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-hw", description="Orbis hardware status and controls")
    _common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    t = sub.add_parser("telemetry")
    t.add_argument("--cpu-sample", type=float, default=0.5)
    sub.add_parser("capabilities")
    s = sub.add_parser("set", help="gpu-sclk max|startup, fan-threshold <C>, cpu-governor <name>, gtt-budget <MiB>")
    s.add_argument("knob", choices=("gpu-sclk", "fan-threshold", "cpu-governor", "gtt-budget"))
    s.add_argument("value")
    s.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("reset")
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            snap = telemetry.snapshot(include_busy=True)
            out = {"versions": snap["versions"], "gpu": {k: snap["gpu"].get(k) for k in
                   ("name", "driver", "sclk", "engine_busy_percent", "memory", "video_engines")},
                   "thermal": snap["thermal"], "memory": {k: snap["memory"].get(k) for k in
                   ("linux_ram_total", "linux_ram_available", "vram_carveout", "psi_memory", "boot_contract")},
                   "faults": snap["faults"]["counts"]}
        elif a.cmd == "telemetry":
            out = telemetry.snapshot(include_busy=True, cpu_sample_s=a.cpu_sample)
        elif a.cmd == "capabilities":
            out = telemetry.capabilities()
        elif a.cmd == "set":
            key = {"gpu-sclk": "gpu_sclk", "fan-threshold": "fan_threshold_c", "cpu-governor": "cpu_governor",
                   "gtt-budget": "gtt_budget_mb"}[a.knob]
            val = int(a.value) if key in ("fan_threshold_c", "gtt_budget_mb") else a.value
            out = controls.apply_request({key: val}, dry_run=a.dry_run)
        else:
            out = controls.reset(dry_run=a.dry_run)
    except (ValueError, KeyError) as e:
        print(f"orbis-hw: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"orbis-hw: the kernel refused: {e}", file=sys.stderr)
        return 1
    _out(out, a.json)
    if a.cmd == "set" and any(not r.get("applied", True) and not a.dry_run for r in out):
        return 1
    return 0


def main_mem(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-mem", description="Orbis unified CPU/GPU memory")
    _common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pl = sub.add_parser("plan")
    pl.add_argument("mode", nargs="?", choices=tuple(memory.MODE_POLICY))
    b = sub.add_parser("set-boot", help="write vram.txt / gtt.txt on the boot partition (reboot required)")
    b.add_argument("--mode", choices=tuple(memory.MODE_POLICY))
    b.add_argument("--vram-mb", type=int)
    b.add_argument("--gtt-mb", type=int)
    b.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("set-budget", help="runtime GTT budget in MiB")
    g.add_argument("mib", type=int)
    g.add_argument("--dry-run", action="store_true")
    rc = sub.add_parser("reclaim", help="GPU buffer reclaim (ge-0014): status | scan <pages> [--writeback] | enable | disable")
    rc.add_argument("action", nargs="?", default="status", choices=("status", "scan", "enable", "disable"))
    rc.add_argument("pages", nargs="?", type=int, default=65536)
    rc.add_argument("--writeback", action="store_true")
    rc.add_argument("--dry-run", action="store_true")
    mb = sub.add_parser("bench", help="bandwidth per memory kind + zero-copy check on this GPU (orbis-gfx-probe --membench)")
    mb.add_argument("--mb", type=int, default=128)
    d = sub.add_parser("daemon", help="pressure/mode-driven GTT budget loop (orbis-memd)")
    d.add_argument("--interval", type=float, default=5.0)
    d.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            out = memory.ownership()
        elif a.cmd == "plan":
            out = memory.plan(a.mode or memory.current_mode())
        elif a.cmd == "set-boot":
            out = memory.apply_boot(a.mode, a.vram_mb, a.gtt_mb, dry_run=a.dry_run)
        elif a.cmd == "set-budget":
            out = memory.set_gtt_budget(a.mib << 20, "manual (orbis-mem)", dry_run=a.dry_run)
        elif a.cmd == "bench":
            out = memory.membench(a.mb)
        elif a.cmd == "reclaim":
            if a.action == "scan":
                out = memory.reclaim_scan(a.pages, a.writeback, a.dry_run)
            elif a.action in ("enable", "disable"):
                out = memory.reclaim_bootarg(a.action == "enable", a.dry_run)
            else:
                g = memory.sysfs.primary_gpu()
                out = {"reclaim": memory.reclaim_status(g), "compressed": memory.compressed_status()}
        else:
            return memory.daemon(a.interval, a.once)
    except ValueError as e:
        print(f"orbis-mem: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"orbis-mem: {e}", file=sys.stderr)
        return 1
    _out(out, a.json)
    return 0


def main_debug(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-debug", description=debug.__doc__.split("\n")[0])
    _common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--out")
    e = sub.add_parser("export")
    e.add_argument("--out")
    for n in ("last-crash", "gpu", "memory", "video", "boots"):
        sub.add_parser(n)
    s = sub.add_parser("stream")
    s.add_argument("action", choices=("enable", "disable", "status"))
    s.add_argument("--to")
    s.add_argument("--port", type=int, default=6666)
    s.add_argument("--boot", action="store_true", help="also put netconsole= in bootargs.txt (captures early boot)")
    s.add_argument("--local-ip", help="console's fixed LAN address (with --boot)")
    s.add_argument("--dry-run", action="store_true")
    k = sub.add_parser("kdump")
    k.add_argument("action", choices=("enable", "disable", "status", "load", "build-capture"))
    k.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "collect":
            d = debug.collect(Path(a.out) if a.out else None)
            out = {"bundle_dir": str(d), "report": str(d / "report.txt")}
        elif a.cmd == "export":
            out = debug.export(Path(a.out) if a.out else None)
        elif a.cmd == "last-crash":
            out = blackbox.last_crash()
            out["kdump_captures"] = kdump.status().get("captures")
        elif a.cmd == "gpu":
            out = debug.gpu_view()
        elif a.cmd == "memory":
            out = debug.memory_view()
        elif a.cmd == "video":
            out = video.status()
        elif a.cmd == "boots":
            out = {"boots": blackbox.list_boots()}
        elif a.cmd == "stream":
            if a.action == "enable":
                if not a.to:
                    raise ValueError("--to <LAN address of the receiving machine> is required")
                out = debug.stream_enable(a.to, a.port, a.boot, a.local_ip, a.dry_run)
            elif a.action == "disable":
                out = debug.stream_disable(a.boot, a.dry_run)
            else:
                out = {"config": blackbox.config(),
                       "netconsole_runtime": os.path.isdir("/sys/kernel/config/netconsole/orbis")}
        else:
            if a.action == "enable":
                out = {"bootargs": kdump.set_bootarg(True, a.dry_run),
                       "capture": None if a.dry_run else str(kdump.build_capture()),
                       "next": "reboot; orbis-kdump.service then loads the capture kernel (systemctl enable orbis-kdump)"}
            elif a.action == "disable":
                out = {"bootargs": kdump.set_bootarg(False, a.dry_run)}
            elif a.action == "load":
                out = kdump.load(a.dry_run)
            elif a.action == "build-capture":
                out = {"capture": str(kdump.build_capture())}
            else:
                out = kdump.status()
    except (ValueError, FileNotFoundError) as e:
        print(f"orbis-debug: {e}", file=sys.stderr)
        return 2
    _out(out, a.json)
    return 0


def main_video(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-video", description="Orbis video decode status and test")
    _common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    t = sub.add_parser("test")
    t.add_argument("--seconds", type=int, default=4)
    t.add_argument("--size", default="1280x720")
    c = sub.add_parser("configure")
    c.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        out = video.status()
    elif a.cmd == "test":
        out = video.test(a.seconds, a.size)
    else:
        out = video.configure(a.dry_run)
    _out(out, a.json)
    return 0 if out.get("state", "PASS") in ("PASS", "UNSUPPORTED") else 1


def main_ai(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-ai-gpu", description="Vulkan inference tuning for Liverpool")
    _common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("variants")
    c = sub.add_parser("campaign")
    c.add_argument("--model", required=True)
    c.add_argument("--out", default="/var/lib/orbis-graphics/ai-campaign")
    c.add_argument("--variants", help="comma-separated (default: all, in order)")
    c.add_argument("--remote-log", help="udp://<LAN-IP>:47999 mirror (orbis-infer-harness receive)")
    c.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("recommend")
    r.add_argument("--out", default="/var/lib/orbis-graphics/ai-campaign")
    ap_ = sub.add_parser("apply")
    ap_.add_argument("--out", default="/var/lib/orbis-graphics/ai-campaign")
    ap_.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            out = ai.status()
        elif a.cmd == "variants":
            out = {"order": ai.DEFAULT_ORDER, "variants": ai.VARIANTS}
        elif a.cmd == "campaign":
            out = ai.run_campaign(a.model, Path(a.out), a.variants.split(",") if a.variants else None,
                                  a.remote_log, a.dry_run)
        elif a.cmd == "recommend":
            out = ai.recommend(Path(a.out))
        else:
            out = ai.apply(Path(a.out), a.dry_run)
    except (ValueError, FileNotFoundError) as e:
        print(f"orbis-ai-gpu: {e}", file=sys.stderr)
        return 2
    _out(out, a.json)
    return 0


def main_blackbox(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-blackbox", description="NickLabs Orbis Black Box recorder")
    ap.add_argument("action", choices=("run", "start-boot", "end-boot", "status"))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--reason", default="orderly shutdown")
    a = ap.parse_args(argv)
    if a.action == "run":
        return blackbox.run(a.interval)
    if a.action == "start-boot":
        print(blackbox.start_boot())
        return 0
    if a.action == "end-boot":
        blackbox.end_boot(a.reason)
        return 0
    print(json.dumps({"current": str(blackbox.current_dir()), "boots": blackbox.list_boots()[-5:]}, indent=1))
    return 0


def main_hwd(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orbis-hwd", description=hwd.__doc__.split("\n")[0])
    ap.add_argument("--bind")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)
    hwd.serve(a.bind, a.port)
    return 0


ENTRY = {"orbis-hw": main_hw, "orbis-mem": main_mem, "orbis-debug": main_debug, "orbis-video": main_video,
         "orbis-ai-gpu": main_ai, "orbis-blackbox": main_blackbox, "orbis-hwd": main_hwd}


def main(argv=None) -> int:
    """python -m orbis_engine <program> [args]  (the /usr/bin wrappers call ENTRY directly)"""
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ENTRY:
        print(f"usage: python -m orbis_engine {{{','.join(ENTRY)}}} ...", file=sys.stderr)
        return 2
    return ENTRY[argv[0]](argv[1:])
