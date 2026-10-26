"""Graphics performance profiles: selectable, reversible, attributable.

A profile is a set of *actions*. Each action names exactly one knob, is applied only when the
knob exists on this console, records the previous value, and is undone by `reset`. Nothing
overclocks: the GPU performance level can only be set to the kernel's own auto/low/high, and
only when the kernel exposes it (the Orbis 6.15.4 kernel does not, see pm.clocks).

Knobs:
  gpu_perf_level   /sys/class/drm/cardN/device/power_dpm_force_performance_level
  cpu_governor     /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
  session_env      Mesa/RADV environment for graphical sessions, written to
                   /etc/environment.d/90-orbis-graphics.conf (takes effect at next login)
  vram_mb          a recommendation only; changing it needs `orbis boot set-vram` + payload relaunch

State: /var/lib/orbis-graphics/profile-state.json; log: /var/log/orbis/graphics-profile.jsonl
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import __version__, sysfs

PROFILES = {
    "balanced": {"description": "kernel defaults; no hardware knob is changed (default for PC)",
                 "gpu_perf_level": None, "gpu_sclk": None, "fan_threshold_c": None, "cpu_governor": None,
                 "session_env": {}, "vram_mb": None},
    "performance": {"description": "full SPLL SCLK, fan reacts earlier, performance governor, large shader cache "
                                   "(default for GAMING)",
                    "gpu_perf_level": "high", "gpu_sclk": "max", "fan_threshold_c": 70, "cpu_governor": "performance",
                    "session_env": {"MESA_SHADER_CACHE_MAX_SIZE": "4G"}, "vram_mb": 2048},
    "quiet": {"description": "fused startup SCLK divider (lower clock, less heat), powersave governor",
              "gpu_perf_level": "low", "gpu_sclk": "startup", "fan_threshold_c": None, "cpu_governor": "powersave",
              "session_env": {}, "vram_mb": None},
    "compute": {"description": "AI/compute: full SCLK, fan reacts earlier for long runs, no session changes "
                               "(default for AI)",
                "gpu_perf_level": "auto", "gpu_sclk": "max", "fan_threshold_c": 72, "cpu_governor": "performance",
                "session_env": {}, "vram_mb": 1024},
    "diagnostic": {"description": "no tuning; shader cache disabled so measurements are cold and repeatable",
                   "gpu_perf_level": None, "gpu_sclk": None, "fan_threshold_c": None, "cpu_governor": None,
                   "session_env": {"MESA_SHADER_CACHE_DISABLE": "true"}, "vram_mb": None},
}
PERSONALITY_DEFAULTS = {"pc": "balanced", "gaming": "performance", "ai": "compute"}
ALLOWED_PERF = {"auto", "low", "high"}


def _state_file() -> Path:
    return sysfs.p("var/lib/orbis-graphics/profile-state.json")


def _log_file() -> Path:
    return sysfs.p("var/log/orbis/graphics-profile.jsonl")


def _env_file() -> Path:
    return sysfs.p("etc/environment.d/90-orbis-graphics.conf")


def _gov_files() -> list[Path]:
    return sorted(sysfs.p("sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"))


def _fan_crit() -> Path | None:
    base = sysfs.p("sys/class/hwmon")
    for h in sorted(base.glob("hwmon*")) if base.is_dir() else []:
        if sysfs.read(h / "name") == "ps4_fan" and (h / "temp1_crit").exists():
            return h / "temp1_crit"
    return None


def capabilities() -> dict:
    g = sysfs.primary_gpu()
    perf = g.sysdir / "power_dpm_force_performance_level" if g else None
    sclk = g.sysdir / "ps4_sclk" if g else None
    fan = _fan_crit()
    gov = _gov_files()
    avail = sysfs.read(gov[0].parent / "scaling_available_governors") if gov else None
    return {
        "gpu_perf_level": {"available": bool(perf and perf.exists()),
                           "reason": None if perf and perf.exists() else "no DPM on this kernel (pm.clocks UNSUPPORTED)"},
        "cpu_governor": {"available": bool(gov), "governors": (avail or "").split(),
                         "reason": None if gov else "no cpufreq driver for the PS4 Jaguar CPU"},
        "gpu_sclk": {"available": bool(sclk and sclk.exists()),
                     "reason": None if sclk and sclk.exists() else "kernel without the Graphics Engine clock patch"},
        "fan_threshold_c": {"available": fan is not None,
                            "reason": None if fan else "kernel without the Graphics Engine fan driver"},
        "session_env": {"available": True},
        "vram_mb": {"available": True, "note": "applied at the next payload launch (orbis boot set-vram)"},
    }


def plan(name: str) -> list[dict]:
    if name not in PROFILES:
        raise KeyError(f"unknown profile {name!r}; known: {', '.join(PROFILES)}")
    prof = PROFILES[name]
    caps = capabilities()
    acts = []
    lvl = prof["gpu_perf_level"]
    if lvl:
        if lvl not in ALLOWED_PERF:
            raise ValueError("refusing a non-kernel performance level")
        g = sysfs.primary_gpu()
        if caps["gpu_perf_level"]["available"]:
            acts.append({"knob": "gpu_perf_level", "path": str(g.sysdir / "power_dpm_force_performance_level"), "value": lvl})
        else:
            acts.append({"knob": "gpu_perf_level", "skipped": caps["gpu_perf_level"]["reason"], "value": lvl})
    gov = prof["cpu_governor"]
    if gov:
        if caps["cpu_governor"]["available"] and gov in caps["cpu_governor"]["governors"]:
            for f in _gov_files():
                acts.append({"knob": "cpu_governor", "path": str(f), "value": gov})
        else:
            acts.append({"knob": "cpu_governor", "skipped": caps["cpu_governor"]["reason"] or f"governor {gov} not offered",
                         "value": gov})
    sc = prof.get("gpu_sclk")
    if sc:
        if sc not in ("max", "startup"):
            raise ValueError("refusing a SCLK state other than the two hardware-defined ones")
        g = sysfs.primary_gpu()
        if caps["gpu_sclk"]["available"]:
            acts.append({"knob": "gpu_sclk", "path": str(g.sysdir / "ps4_sclk"), "value": sc})
        else:
            acts.append({"knob": "gpu_sclk", "skipped": caps["gpu_sclk"]["reason"], "value": sc})
    fan = prof.get("fan_threshold_c")
    if fan:
        if not 45 <= int(fan) <= 85:
            raise ValueError("fan threshold outside 45..85 C")
        if caps["fan_threshold_c"]["available"]:
            acts.append({"knob": "fan_threshold_c", "path": str(_fan_crit()), "value": str(int(fan) * 1000)})
        else:
            acts.append({"knob": "fan_threshold_c", "skipped": caps["fan_threshold_c"]["reason"], "value": fan})
    acts.append({"knob": "session_env", "path": str(_env_file()), "value": prof["session_env"]})
    if prof["vram_mb"]:
        acts.append({"knob": "vram_mb", "recommendation": prof["vram_mb"],
                     "how": f"orbis boot set-vram {prof['vram_mb']} (takes effect at the next payload launch)"})
    return acts


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def apply(name: str, reason: str = "manual", dry_run: bool = False) -> dict:
    acts = plan(name)
    state = load_state()
    original = state.get("original") or {}
    done = []
    for a in acts:
        if "path" not in a:
            done.append(a)
            continue
        path = Path(a["path"])
        if a["knob"] == "session_env":
            prev = path.read_text() if path.exists() else None
            text = ("# NickLabs Orbis Graphics profile '%s' — managed by orbis-gpu profile; do not edit\n" % name +
                    "".join(f"{k}={v}\n" for k, v in sorted(a["value"].items())))
            new = text if a["value"] else None
        else:
            prev = sysfs.read(path)
            if a["knob"] == "gpu_perf_level" and prev:
                prev = prev.split()[0]
            if a["knob"] == "gpu_sclk":
                state = dict(t.split("=", 1) for t in (prev or "").split() if "=" in t).get("state")
                prev = state if state in ("max", "startup") else None
            new = a["value"]
        original.setdefault(a["path"], prev)
        if not dry_run:
            if new is None:
                if path.exists():
                    path.unlink()
            else:
                _write(path, new)
        done.append(dict(a, previous=prev))
    rec = {"schema": "orbis-gfx-profile-state/1", "profile": name, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "reason": reason, "actions": done, "original": original, "version": __version__}
    if not dry_run:
        _write(_state_file(), json.dumps(rec, indent=1) + "\n")
        _log(rec)
    return rec


def reset(dry_run: bool = False) -> dict:
    state = load_state()
    restored = []
    for path, prev in (state.get("original") or {}).items():
        pth = Path(path)
        if not dry_run:
            if prev is None:
                if pth.exists() and pth.name.endswith(".conf"):
                    pth.unlink()
            else:
                _write(pth, prev)
        restored.append({"path": path, "restored": prev})
    rec = {"schema": "orbis-gfx-profile-state/1", "profile": None, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "reason": "reset", "restored": restored, "version": __version__}
    if not dry_run:
        _write(_state_file(), json.dumps({**rec, "original": {}}, indent=1) + "\n")
        _log(rec)
    return rec


def load_state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def _log(rec: dict) -> None:
    try:
        f = _log_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def listing() -> dict:
    return {"schema": "orbis-gfx-profiles/1", "active": load_state().get("profile"), "profiles": PROFILES,
            "personality_defaults": PERSONALITY_DEFAULTS, "capabilities": capabilities()}


def personality_default(mode: str) -> str:
    cfg = sysfs.p("etc/orbis/graphics.json")
    try:
        over = json.loads(cfg.read_text()).get("personality_profiles", {})
    except (OSError, ValueError):
        over = {}
    return over.get(mode) or PERSONALITY_DEFAULTS.get(mode, "balanced")
