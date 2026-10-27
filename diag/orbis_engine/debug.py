"""orbis-debug: collect, inspect and export diagnostic evidence (Black Box front end).

  orbis-debug collect [--out DIR]          full bundle (JSON + text + raw evidence) in a directory
  orbis-debug export  [--out FILE]         collect + one .tar.gz with SHA-256 (also copied to the
                                           USB boot partition's orbis-logs/ when it is mounted)
  orbis-debug last-crash                   what happened at the end of the last unclean boot
  orbis-debug gpu | memory | video | boots live views
  orbis-debug stream enable --to <LAN-IP> [--port 6666] [--boot --local-ip <console IP>]
  orbis-debug stream status | disable
  orbis-debug kdump enable | disable | status | load      (experimental crash-capture kernel)
Every command takes --json for machine-readable output.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from orbis_gfx import runner, sysfs

from . import __version__, ai, blackbox, gpuregs, kdump, memory, telemetry, video
from .util import atomic_write, now_iso, p, read

DEBUGFS_FILES = ["amdgpu_firmware_info", "amdgpu_fence_info", "amdgpu_vm_info", "amdgpu_sa_info", "amdgpu_gem_info",
                 "amdgpu_liverpool_clk", "ps4_bridge_status", "amdgpu_vbios", "clients", "framebuffer", "state",
                 "amdgpu_pm_info"]
COMMANDS = [
    ("system/uname.txt", ["uname", "-a"]),
    ("system/cmdline.txt", ["cat", "/proc/cmdline"]),
    ("system/lspci.txt", ["lspci", "-nnk"]),
    ("system/lsusb.txt", ["lsusb"]),
    ("system/packages.txt", ["pacman", "-Q"]),
    ("system/df.txt", ["df", "-h"]),
    ("system/free.txt", ["free", "-m"]),
    ("logs/dmesg.txt", ["dmesg", "--time-format", "iso"]),
    ("logs/journal-boot.txt", ["journalctl", "-b", "0", "--no-pager", "-o", "short-iso", "-n", "20000"]),
    ("logs/journal-prev-boot.txt", ["journalctl", "-b", "-1", "--no-pager", "-o", "short-iso", "-n", "20000"]),
    ("graphics/vulkaninfo.txt", ["vulkaninfo", "--summary"]),
    ("graphics/glxinfo.txt", ["glxinfo", "-B"]),
    ("graphics/eglinfo.txt", ["eglinfo", "-B"]),
    ("graphics/vainfo.txt", ["vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"]),
    ("graphics/clinfo.txt", ["clinfo", "-l"]),
    ("graphics/drm_info.txt", ["drm_info"]),
    ("system/sensors.txt", ["sensors"]),
    # native components (orbis-native); skipped when not installed
    ("native/orbis-perf-status.json", ["orbis-perf", "status", "--json"]),
    ("native/orbis-perf-doctor.json", ["orbis-perf", "doctor", "--json"]),
    ("native/orbis-time-status.json", ["orbis-time", "status", "--json"]),
    ("native/orbis-games-runners.json", ["orbis-games", "runners", "--json"]),
]


def gpu_view() -> dict:
    g = sysfs.primary_gpu()
    out = {"telemetry": telemetry.gpu(include_busy=True), "registers": gpuregs.dump(), "faults": telemetry.faults()}
    if g and g.debugfs:
        out["fence_info"] = read(g.debugfs / "amdgpu_fence_info")
    return out


def memory_view() -> dict:
    return {"ownership": memory.ownership(), "plan_for_current_mode": memory.plan(memory.current_mode())}


def collect(out: Path | None = None, run=runner.run) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out or Path(p("var/lib/orbis-blackbox/exports")) / f"orbis-debug-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    errors = []

    def section(name, fn):
        try:
            return fn()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            return {"error": str(e)}

    report = {"schema": "orbis-debug-bundle/1", "time": now_iso(), "tool": f"orbis-debug {__version__}",
              "boot_id": blackbox.boot_id(),
              "telemetry": section("telemetry", lambda: telemetry.snapshot(include_busy=True, cpu_sample_s=0.5)),
              "gpu_registers": section("registers", gpuregs.dump),
              "memory": section("memory", memory_view),
              "video": section("video", video.status),
              "ai": section("ai", ai.status),
              "blackbox": section("blackbox", lambda: {"boots": blackbox.list_boots(), "last_crash": blackbox.last_crash()}),
              "kdump": section("kdump", kdump.status)}
    g = sysfs.primary_gpu()
    if g and g.debugfs:
        for name in DEBUGFS_FILES:
            f = g.debugfs / name
            try:
                data = f.read_bytes()[:4 << 20]
            except OSError:
                continue
            atomic_write(out / "debugfs" / name, data)
        for ring in g.debugfs.glob("amdgpu_ring_*"):
            try:
                atomic_write(out / "debugfs" / "rings" / (ring.name + ".bin"), ring.read_bytes()[:256 << 10])
            except OSError:
                pass
    for rel, argv in COMMANDS:
        r = run(argv, timeout=60)
        if r.missing:
            continue
        atomic_write(out / rel, (r.stdout or "") + (("\n--- stderr ---\n" + r.stderr) if r.stderr else ""))
    for src, dst in (("var/lib/orbis-graphics/last-qualify.json", "graphics/last-qualify.json"),
                     ("var/lib/orbis-graphics/integration.json", "graphics/integration.json"),
                     ("var/lib/orbis-graphics/video-test.json", "graphics/video-test.json"),
                     ("var/lib/orbis-graphics/ai-vulkan-tuning.json", "graphics/ai-vulkan-tuning.json"),
                     ("var/lib/orbis/gpu-gate.json", "graphics/gpu-gate.json"),
                     ("var/lib/orbis-graphics/membench.json", "graphics/membench.json"),
                     ("run/orbis/time-source.json", "native/time-source.json"),
                     ("run/orbis/perf/state.json", "native/perf-state.json"),
                     ("run/orbis/perf/scx-failures.json", "native/scx-failures.json"),
                     ("var/lib/orbis/perf/restore-mode.json", "native/perf-restore-mode.json"),
                     ("var/lib/orbis/perf/restore-session.json", "native/perf-restore-session.json"),
                     ("var/lib/orbis/games/vulkan-caps.json", "native/vulkan-caps.json")):
        f = p(src)
        if f.exists():
            (out / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out / dst)
    bb = blackbox.base() / "boots"
    if bb.is_dir():
        for b in sorted(bb.iterdir())[-3:]:
            shutil.copytree(b, out / "blackbox" / b.name, dirs_exist_ok=True)
    kd = p("var/lib/orbis-blackbox/kdump")
    if kd.is_dir():
        shutil.copytree(kd, out / "kdump", dirs_exist_ok=True)
    report["collection_errors"] = errors
    atomic_write(out / "report.json", json.dumps(report, indent=1, default=str) + "\n")
    atomic_write(out / "report.txt", render(report))
    return out


def export(out_file: Path | None = None, run=runner.run) -> dict:
    d = collect(run=run)
    out_file = out_file or d.with_suffix(".tar.gz")
    with tarfile.open(out_file, "w:gz") as t:
        t.add(d, arcname=d.name)
    h = hashlib.sha256(out_file.read_bytes()).hexdigest()
    Path(str(out_file) + ".sha256").write_text(f"{h}  {out_file.name}\n")
    res = {"bundle": str(out_file), "sha256": h, "directory": str(d)}
    usb = memory.boot_dir() / "orbis-logs"
    if memory.boot_dir().is_dir():
        try:
            usb.mkdir(exist_ok=True)
            shutil.copy2(out_file, usb / out_file.name)
            res["copied_to_usb"] = str(usb / out_file.name)
        except OSError as e:
            res["usb_copy_error"] = str(e)
    return res


def render(obj, indent: int = 0) -> str:
    """Readable text for any JSON-like structure."""
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(render(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(render(v, indent + 1))
            else:
                lines.append(f"{pad}- {v}")
    else:
        lines.append(f"{pad}{obj}")
    return "\n".join(l for l in lines if l != "") + ("\n" if indent == 0 else "")


def stream_enable(target: str, port: int = 6666, boot: bool = False, local_ip: str | None = None,
                  dry_run: bool = False) -> dict:
    res = {"netconsole_runtime": blackbox.netconsole_enable(target, port, dry_run)}
    cfg = blackbox.config()
    cfg["stream"] = f"udp://{target}:{port + 1}"
    cfg["netconsole"] = {"target": target, "port": port}
    if not dry_run:
        atomic_write(p("etc/orbis/blackbox.json"), json.dumps(cfg, indent=1) + "\n")
    res["events_stream"] = cfg["stream"]
    if boot:
        if not local_ip:
            raise ValueError("--boot needs --local-ip (the console's fixed LAN address)")
        f = memory.boot_dir() / "bootargs.txt"
        cur = (read(f) or "").splitlines()[0] if read(f) else ""
        toks = [t for t in cur.split() if not t.startswith("netconsole=")]
        toks.append(blackbox.netconsole_bootarg(target, local_ip, port))
        new = " ".join(toks)
        if not dry_run and new != cur:
            atomic_write(f.with_name("bootargs.txt.orbis-prev"), cur + "\n")
            atomic_write(f, new + "\n")
        res["bootargs"] = {"old": cur, "new": new}
    res["receiver"] = f"on the LAN machine: orbis-blackbox-receiver --netconsole-port {port} --events-port {port + 1}"
    return res


def stream_disable(boot: bool = False, dry_run: bool = False) -> dict:
    res = {"disabled": True}
    if boot:
        f = memory.boot_dir() / "bootargs.txt"
        cur = (read(f) or "").splitlines()[0] if read(f) else ""
        new = " ".join(t for t in cur.split() if not t.startswith("netconsole="))
        if not dry_run and new != cur:
            atomic_write(f.with_name("bootargs.txt.orbis-prev"), cur + "\n")
            atomic_write(f, new + "\n")
        res["bootargs"] = {"old": cur, "new": new}
    t = p("sys/kernel/config/netconsole/orbis")
    if t.is_dir() and not dry_run:
        try:
            (t / "enabled").write_text("0")
            t.rmdir()
        except OSError:
            pass
    cfg = blackbox.config()
    cfg.pop("stream", None)
    cfg.pop("netconsole", None)
    if not dry_run:
        atomic_write(p("etc/orbis/blackbox.json"), json.dumps(cfg, indent=1) + "\n")
    return res
