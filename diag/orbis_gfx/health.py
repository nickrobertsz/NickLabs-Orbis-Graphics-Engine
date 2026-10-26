"""orbis-gpu health: a fast, read-only snapshot for dashboards (no GPU work is submitted)."""
from __future__ import annotations

import time

from . import __version__, sysfs


def snapshot() -> dict:
    g = sysfs.primary_gpu()
    out = {"schema": "orbis-gfx-health/1", "version": __version__, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "gpu": None, "status": "UNKNOWN", "reasons": []}
    if not g:
        out["status"] = "NO_GPU"
        out["reasons"].append("no DRM card")
        return out
    mem = sysfs.memory(g)
    pm = sysfs.power(g)
    hits = sysfs.scan_log(sysfs.kernel_log())
    faults = {k: len(hits[k]) for k in ("vm_fault", "ring_timeout", "gpu_reset", "fw_fail")}
    cons = sysfs.connectors(g)
    out["gpu"] = {
        "card": g.card, "pci": f"{g.vendor}:{g.device}", "revision": g.revision, "name": g.name, "driver": g.driver,
        "memory_bytes": mem, "busy_percent": pm.get("gpu_busy_percent"),
        "perf_level": pm.get("power_dpm_force_performance_level"), "sclk": pm.get("pp_dpm_sclk"),
        "hwmon": sysfs.hwmon(g), "cpu_temps": sysfs.cpu_temps(),
        "connectors": [{k: c[k] for k in ("name", "status", "enabled", "dpms")} | {"modes": len(c["modes"])} for c in cons],
        "faults_since_boot": faults, "rings": sysfs.rings(g),
    }
    if g.driver != "amdgpu":
        out["status"] = "DRIVER_NOT_BOUND"
        out["reasons"].append(f"driver {g.driver}")
    elif any(faults[k] for k in ("vm_fault", "ring_timeout", "gpu_reset")):
        out["status"] = "FAULTED"
        out["reasons"] += [f"{k}={v}" for k, v in faults.items() if v]
    elif faults["fw_fail"]:
        out["status"] = "FIRMWARE_PROBLEM"
        out["reasons"].append("firmware load failure logged")
    else:
        out["status"] = "OK"
    return out


def render_text(s: dict) -> str:
    g = s.get("gpu") or {}
    lines = [f"Orbis GPU health: {s['status']}" + (f" ({', '.join(s['reasons'])})" if s["reasons"] else "")]
    if g:
        m = g["memory_bytes"]
        lines += [f"  {g['name']} {g['pci']} rev {g['revision']} driver {g['driver']} ({g['card']})",
                  f"  VRAM used {(m.get('mem_info_vram_used') or 0) >> 20}/{(m.get('mem_info_vram_total') or 0) >> 20} MiB, "
                  f"GTT used {(m.get('mem_info_gtt_used') or 0) >> 20}/{(m.get('mem_info_gtt_total') or 0) >> 20} MiB",
                  f"  busy {g['busy_percent'] or 'n/a'}%, perf level {g['perf_level'] or 'n/a (no DPM)'}",
                  "  temps: " + (", ".join(f"{k}={v}" for k, v in {**g['hwmon'], **g['cpu_temps']}.items() if 'temp' in k) or "n/a"),
                  "  displays: " + ", ".join(f"{c['name']}={c['status']}" for c in g["connectors"]),
                  "  faults since boot: " + ", ".join(f"{k}={v}" for k, v in g["faults_since_boot"].items())]
    return "\n".join(lines) + "\n"
