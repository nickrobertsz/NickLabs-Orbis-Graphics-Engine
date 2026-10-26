"""Individual hardware controls (outside profiles), each validated, logged and reversible.

Only interfaces the running kernel really offers are written; a missing one returns
{"applied": false, "why": ...}. `reset()` restores every value to what it was before the first
change made through this module."""
from __future__ import annotations

import json
from pathlib import Path

from orbis_gfx import sysfs

from . import memory
from .util import append_jsonl, atomic_write, hwmon_by_name, kv, now_iso, p, read, write_sysfs

STATE = "var/lib/orbis-graphics/controls-state.json"
LOG = "var/log/orbis/hw-controls.jsonl"


def _state() -> dict:
    try:
        return json.loads(p(STATE).read_text())
    except (OSError, ValueError):
        return {"original": {}}


def _save(st: dict) -> None:
    atomic_write(p(STATE), json.dumps(st, indent=1) + "\n")


def _record(knob: str, path: Path, prev, new, dry_run: bool) -> dict:
    rec = {"time": now_iso(), "knob": knob, "path": str(path), "previous": prev, "value": new, "dry_run": dry_run}
    if not dry_run:
        st = _state()
        st["original"].setdefault(str(path), {"knob": knob, "value": prev})
        _save(st)
        append_jsonl(p(LOG), rec)
    rec["applied"] = not dry_run
    return rec


def _gpu_file(name: str) -> Path | None:
    g = sysfs.primary_gpu()
    f = g.sysdir / name if g else None
    return f if f and f.exists() else None


def set_sclk(state: str, dry_run: bool = False) -> dict:
    if state not in ("max", "startup"):
        raise ValueError("SCLK state must be 'max' (full SPLL output) or 'startup' (fused startup divider)")
    f = _gpu_file("ps4_sclk")
    if not f:
        return {"applied": False, "knob": "gpu_sclk", "why": "kernel without the Graphics Engine clock patch"}
    prev = kv(read(f)).get("state")
    if not dry_run:
        write_sysfs(f, state)
    return _record("gpu_sclk", f, prev if prev in ("max", "startup") else None, state, dry_run)


def set_fan_threshold(celsius: int, dry_run: bool = False) -> dict:
    if not 45 <= int(celsius) <= 85:
        raise ValueError("fan threshold must be 45..85 C (the driver accepts 20..85; Orbis keeps a safe floor)")
    h = hwmon_by_name("ps4_fan")
    if not h or not (h / "temp1_crit").exists():
        return {"applied": False, "knob": "fan_threshold_c", "why": "kernel without the Graphics Engine fan driver"}
    f = h / "temp1_crit"
    prev = read(f)
    if not dry_run:
        write_sysfs(f, str(int(celsius) * 1000))
    return _record("fan_threshold_c", f, prev, str(int(celsius) * 1000), dry_run)


def set_governor(gov: str, dry_run: bool = False) -> dict:
    files = sorted(p("sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"))
    if not files:
        return {"applied": False, "knob": "cpu_governor", "why": "no cpufreq driver"}
    avail = (read(files[0].parent / "scaling_available_governors") or "").split()
    if gov not in avail:
        raise ValueError(f"governor {gov!r} not offered (available: {' '.join(avail)})")
    recs = []
    for f in files:
        prev = read(f)
        if not dry_run:
            write_sysfs(f, gov)
        recs.append(_record("cpu_governor", f, prev, gov, dry_run))
    return {"applied": not dry_run, "knob": "cpu_governor", "cpus": len(recs), "value": gov}


def set_gtt_budget_mb(mb: int, dry_run: bool = False) -> dict:
    f = _gpu_file("orbis_gtt_budget")
    if not f:
        return {"applied": False, "knob": "gtt_budget", "why": "kernel without ge-6.15/0008"}
    prev = kv(read(f)).get("budget")
    rec = memory.set_gtt_budget(int(mb) << 20, "manual (orbis-hw)", dry_run)
    if not dry_run:
        st = _state()
        st["original"].setdefault(str(f), {"knob": "gtt_budget", "value": str(prev) if prev else None})
        _save(st)
    return rec


def reset(dry_run: bool = False) -> dict:
    st = _state()
    restored = []
    for path, info in (st.get("original") or {}).items():
        val = info.get("value")
        if val is None:
            restored.append({"path": path, "skipped": "no restorable previous value"})
            continue
        try:
            if not dry_run:
                write_sysfs(Path(path), str(val))
            restored.append({"path": path, "restored": val})
        except OSError as e:
            restored.append({"path": path, "error": str(e)})
    if not dry_run:
        _save({"original": {}})
        append_jsonl(p(LOG), {"time": now_iso(), "knob": "reset", "restored": restored})
    return {"reset": True, "restored": restored, "dry_run": dry_run}


def apply_request(req: dict, dry_run: bool = False) -> list[dict]:
    """Apply a {"gpu_sclk": .., "fan_threshold_c": .., "cpu_governor": .., "gtt_budget_mb": ..} request."""
    out = []
    known = {"gpu_sclk": set_sclk, "fan_threshold_c": set_fan_threshold, "cpu_governor": set_governor,
             "gtt_budget_mb": set_gtt_budget_mb}
    unknown = set(req) - set(known)
    if unknown:
        raise ValueError(f"unknown controls: {', '.join(sorted(unknown))}")
    for k, fn in known.items():
        if k in req:
            out.append(fn(req[k], dry_run=dry_run))
    return out
