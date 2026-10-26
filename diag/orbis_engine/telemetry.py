"""Read-only hardware telemetry for the settings backend, the Black Box and the debug tools.

Every section says where its numbers come from ("source") and, when an interface does not exist
on the running kernel, why ("missing") — values are never estimated or filled in."""
from __future__ import annotations

import os
import time
from pathlib import Path

from orbis_gfx import sysfs

from . import __version__
from .util import colon_kv, cmdline_params, hwmon_by_name, kv, p, read, read_int

# Graphics Engine kernel interfaces (kernel/patches/series)
GE_SYSFS = {
    "sclk": "ps4_sclk",                 # ge-6.15/0002
    "engine_busy": "orbis_engine_busy",  # ge-6.15/0008
    "gtt_budget": "orbis_gtt_budget",    # ge-6.15/0008
    "smu_cmd": "liverpool_smu_cmd",      # ge-6.15/0009 (only with amdgpu.ps4_smu_mailbox=1)
}


def _missing(what: str, why: str) -> dict:
    return {"available": False, "missing": what, "why": why}


def gpu(include_busy: bool = True) -> dict:
    g = sysfs.primary_gpu()
    if not g:
        return {"present": False, "why": "no DRM card in /sys/class/drm"}
    out = {"present": True, "card": g.card, "pci_slot": g.pci_slot, "vendor": g.vendor, "device": g.device,
           "revision": g.revision, "name": g.name, "driver": g.driver}
    sclk = read(g.sysdir / GE_SYSFS["sclk"])
    out["sclk"] = ({"available": True, "source": "sysfs ps4_sclk", **kv(sclk)} if sclk is not None else
                   _missing("ps4_sclk", "kernel without the Graphics Engine clock patch (ge-6.15/0002)"))
    if g.debugfs:
        clk = read(g.debugfs / "amdgpu_liverpool_clk")
        if clk:
            out["sclk_registers"] = colon_kv(clk)
    if include_busy:
        busy = read(g.sysdir / GE_SYSFS["engine_busy"])
        out["engine_busy_percent"] = ({"available": True, "source": "GRBM/SRBM_STATUS sampling (orbis_engine_busy)",
                                       **kv(busy)} if busy is not None else
                                      _missing("orbis_engine_busy", "kernel without ge-6.15/0008"))
    mem = sysfs.memory(g)
    gtt = kv(read(g.sysdir / GE_SYSFS["gtt_budget"]))
    out["memory"] = {"source": "amdgpu mem_info_* sysfs", **mem,
                     "gtt_budget": gtt or None,
                     "gtt_budget_control": bool(gtt)}
    pm = {k: v for k, v in sysfs.power(g).items() if v is not None}
    out["dpm"] = pm or _missing("power_dpm_*", "Liverpool has no powerplay block in this kernel; clocks are "
                                              "controlled through ps4_sclk only")
    out["firmware"] = sysfs.firmware_info(g)
    out["rings"] = sysfs.rings(g)
    out["video_engines"] = {"uvd_ring": any(r.startswith("uvd") for r in out["rings"]),
                            "vce_ring": any(r.startswith("vce") for r in out["rings"])}
    if g.debugfs:
        bridge = read(g.debugfs / "ps4_bridge_status")
        out["display_bridge"] = colon_kv(bridge) if bridge else _missing("ps4_bridge_status", "kernel without ge-6.15/0006")
    out["connectors"] = [{k: c[k] for k in ("name", "status", "enabled", "dpms", "modes", "edid_valid_header")}
                         for c in sysfs.connectors(g)]
    out["hwmon"] = sysfs.hwmon(g)
    return out


def _cpu_times() -> list[tuple[int, int]]:
    out = []
    for line in (read(p("proc/stat")) or "").splitlines():
        if line.startswith("cpu") and line[3:4].isdigit():
            f = [int(x) for x in line.split()[1:]]
            idle = f[3] + (f[4] if len(f) > 4 else 0)
            out.append((sum(f), idle))
    return out


def cpu(sample_s: float = 0.0) -> dict:
    cpus = []
    base = p("sys/devices/system/cpu")
    for c in sorted(base.glob("cpu[0-9]*"), key=lambda x: int(x.name[3:])) if base.is_dir() else []:
        f = c / "cpufreq"
        if not f.is_dir():
            cpus.append({"cpu": c.name, "cpufreq": False})
            continue
        cpus.append({"cpu": c.name, "cpufreq": True, "driver": read(f / "scaling_driver"),
                     "cur_khz": read_int(f / "scaling_cur_freq"), "min_khz": read_int(f / "scaling_min_freq"),
                     "max_khz": read_int(f / "scaling_max_freq"), "governor": read(f / "scaling_governor"),
                     "available_governors": (read(f / "scaling_available_governors") or "").split(),
                     "available_khz": [int(x) for x in (read(f / "scaling_available_frequencies") or "").split() if x.isdigit()]})
    out = {"cpus": cpus, "cpufreq_driver": next((c.get("driver") for c in cpus if c.get("cpufreq")), None)}
    if not out["cpufreq_driver"]:
        out["cpufreq_missing_why"] = "no cpufreq driver (PS4 has no ACPI P-states; the GE kernel adds ps4-cpufreq)"
    load = read(p("proc/loadavg"))
    out["loadavg"] = [float(x) for x in load.split()[:3]] if load else None
    if sample_s > 0:
        a = _cpu_times()
        time.sleep(sample_s)
        b = _cpu_times()
        out["util_percent"] = [round(100.0 * (1 - (bi - ai) / max(1, bt - at)), 1)
                               for (at, ai), (bt, bi) in zip(a, b)]
    return out


def thermal() -> dict:
    out = {}
    fan = hwmon_by_name("ps4_fan")
    if fan:
        out["apu_temp_c"] = (read_int(fan / "temp1_input") or 0) / 1000 if read(fan / "temp1_input") else None
        crit = read_int(fan / "temp1_crit")
        out["fan_threshold_c"] = crit / 1000 if crit is not None else None
        out["fan_source"] = "EMC via ICC (ps4_fan hwmon, ge-6.15/0001)"
        out["fan_rpm"] = None
        out["fan_rpm_why"] = "not reported: the EMC RPM field was unreliable and upstream removed it"
    else:
        out["fan"] = _missing("ps4_fan hwmon", "kernel without ge-6.15/0001")
    out["cpu_k10temp"] = sysfs.cpu_temps()
    out["note"] = "The EMC keeps its own thermal protection independent of Linux."
    return out


def _psi(kind: str) -> dict | None:
    txt = read(p(f"proc/pressure/{kind}"))
    if not txt:
        return None
    out = {}
    for line in txt.splitlines():
        parts = line.split()
        if parts:
            out[parts[0]] = {k: float(v) if "." in v else int(v) for k, v in (x.split("=") for x in parts[1:])}
    return out


def meminfo_full() -> dict:
    out = {}
    for line in (read(p("proc/meminfo")) or "").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def memory() -> dict:
    mi = meminfo_full()
    cl = cmdline_params()
    g = sysfs.primary_gpu()
    vram_total = read_int(g.sysdir / "mem_info_vram_total") if g else None
    out = {"source": "/proc/meminfo, /proc/pressure, amdgpu sysfs, /proc/cmdline",
           "linux_ram_total": mi.get("MemTotal"), "linux_ram_available": mi.get("MemAvailable"),
           "swap_total": mi.get("SwapTotal"), "swap_free": mi.get("SwapFree"),
           "vram_carveout": vram_total,
           "physical_pool_estimate": (mi.get("MemTotal") or 0) + (vram_total or 0) or None,
           "psi_memory": _psi("memory"), "psi_cpu": _psi("cpu"), "psi_io": _psi("io"),
           "ttm_pages_limit": read_int(p("sys/module/ttm/parameters/pages_limit")),
           "boot_contract": {"loader": cl.get("orbis.loader"), "vram_mb": cl.get("orbis.vram_mb"),
                             "gtt_mb": cl.get("orbis.gtt_mb"), "ttm.pages_limit": cl.get("ttm.pages_limit"),
                             "amdgpu.gttsize": cl.get("amdgpu.gttsize")}}
    if out["psi_memory"] is None:
        out["psi_missing_why"] = "kernel without CONFIG_PSI (the GE kernel enables it)"
    return out


def versions() -> dict:
    out = {"kernel": read(p("proc/sys/kernel/osrelease")), "graphics_engine": __version__,
           "mesa_line": read(p("usr/share/orbis-graphics/mesa-line")),
           "mesa_version": read(p("usr/share/orbis/mesa-version")),
           "graphics_pack": None, "orbis_platform": None, "loader": cmdline_params().get("orbis.loader")}
    pk = read(p("usr/share/orbis/packages.txt")) or ""
    for line in pk.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "orbis-platform":
            out["orbis_platform"] = parts[1]
    local = p("var/lib/pacman/local")
    for name in ("orbis-graphics", "orbis-graphics-engine", "linux-orbis-ps4-ge", "linux-orbis-ps4",
                 "mesa-orbis-ps4", "orbis-platform"):
        hits = sorted(local.glob(f"{name}-[0-9]*")) if local.is_dir() else []
        if hits:
            out.setdefault("packages", {})[name] = hits[-1].name[len(name) + 1:]
    return out


def faults() -> dict:
    hits = sysfs.scan_log(sysfs.kernel_log())
    dumps = []
    base = p("sys/class/devcoredump")
    for d in sorted(base.glob("devcd*")) if base.is_dir() else []:
        dumps.append({"name": d.name, "failing_device": read(d / "failing_device")})
    return {"source": "kernel log since boot + /sys/class/devcoredump",
            "counts": {k: len(v) for k, v in hits.items()},
            "recent": {k: v[-3:] for k, v in hits.items() if v}, "devcoredumps": dumps}


def capabilities() -> dict:
    g = sysfs.primary_gpu()
    caps = {}

    def cap(name, ok, why_not=None, control=False, path=None):
        caps[name] = {"available": bool(ok), "control": control and bool(ok)}
        if path:
            caps[name]["path"] = str(path)
        if not ok and why_not:
            caps[name]["why"] = why_not

    if g:
        cap("gpu.sclk", (g.sysdir / "ps4_sclk").exists(), "kernel without ge-6.15/0002", True, g.sysdir / "ps4_sclk")
        cap("gpu.engine_busy", (g.sysdir / "orbis_engine_busy").exists(), "kernel without ge-6.15/0008")
        cap("gpu.gtt_budget", (g.sysdir / "orbis_gtt_budget").exists(), "kernel without ge-6.15/0008", True,
            g.sysdir / "orbis_gtt_budget")
        cap("gpu.dpm", (g.sysdir / "power_dpm_force_performance_level").exists(),
            "no powerplay for Liverpool in this kernel", True)
        cap("gpu.smu_mailbox", (g.sysdir / "liverpool_smu_cmd").exists(), "expert interface off (amdgpu.ps4_smu_mailbox=0)")
        rings = sysfs.rings(g)
        cap("video.uvd_decode", any(r.startswith("uvd") for r in rings),
            "no UVD ring: the default kernels do not enable UVD on Liverpool (see GRAPHICS-ENGINE-VIDEO.md)")
        cap("video.vce_encode", any(r.startswith("vce") for r in rings), "no VCE ring in any Orbis kernel")
    fan = hwmon_by_name("ps4_fan")
    cap("thermal.fan_threshold", fan is not None and (fan / "temp1_crit").exists(), "kernel without ge-6.15/0001",
        True, fan / "temp1_crit" if fan else None)
    gov = p("sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    cap("cpu.governor", gov.exists(), "no cpufreq driver (GE kernel adds ps4-cpufreq)", True, gov)
    cap("memory.psi", p("proc/pressure/memory").exists(), "kernel without CONFIG_PSI")
    cap("memory.ttm_pages_limit", p("sys/module/ttm/parameters/pages_limit").exists(), "ttm parameters not exposed", True)
    cap("memory.vram_carveout_runtime_resize", False,
        "the carve-out is fixed by the loader before Linux starts (E820, MC_VM_FB_LOCATION, MTRRs); "
        "change it with vram.txt + reboot; runtime flexibility comes from the GTT budget")
    return caps


def snapshot(include_busy: bool = True, cpu_sample_s: float = 0.0) -> dict:
    return {"schema": "orbis-engine-telemetry/1", "time": time.time(), "versions": versions(),
            "gpu": gpu(include_busy), "cpu": cpu(cpu_sample_s), "thermal": thermal(), "memory": memory(),
            "faults": faults(), "capabilities": capabilities()}
