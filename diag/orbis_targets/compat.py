"""Target-aware compatibility and qualification (orbis-hw compatibility / qualify).

compatibility(): what the registry says for a target, side by side with what this console shows
right now (read-only observations). It never changes a capability state.

qualify(): runs only read-only checks plus the results of the existing qualification tools
(orbis-gpu qualify, orbis-mem bench), and writes a qualification record. A capability becomes
"verified" only through a reviewed registry change that cites such a record: qualify() proposes
that change (registry_patch) and marks it as requiring NickLabs review; it never applies it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import match as matcher
from . import probe as prober
from .registry import Registry


def _root() -> Path:
    return Path(os.environ.get("ORBIS_GFX_ROOT") or os.environ.get("ORBIS_ROOT") or "/")


def _rd(rel: str) -> str | None:
    try:
        return (_root() / rel).read_text().strip()
    except OSError:
        return None


def _json(rel: str) -> dict | None:
    t = _rd(rel)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


def observe(doc: dict, eff: dict) -> dict:
    """Read-only observations per capability: PRESENT / ABSENT / NOT_CHECKED (+ detail)."""
    pci = doc.get("pci") or []
    sbpci = {v.lower() for k, v in (eff["southbridge_info"].get("pci") or {}).items() if k not in ("vendor", "note")}
    sb_devs = [d for d in pci if d.get("vendor") == "0x104d"]
    gpu = [d for d in pci if d.get("vendor") == "0x1002" and (d.get("class") or "").startswith("0x03")]
    out = {}
    if sb_devs:
        unbound = [f"{d['slot']} {d.get('device')}" for d in sb_devs if not d.get("driver")]
        foreign = [d.get("device") for d in sb_devs if sbpci and d.get("device") not in sbpci]
        out["southbridge-io"] = {"observed": "PRESENT" if not unbound else "ABSENT",
                                 "detail": f"{len(sb_devs)} Sony functions" + (f", unbound: {unbound}" if unbound else ", all bound") +
                                           (f", not in this target's ID list: {foreign}" if foreign else "")}
    else:
        out["southbridge-io"] = {"observed": "NOT_CHECKED", "detail": "no Sony PCI functions visible (not on a PS4, or probe from a log only)"}
    if gpu:
        out["gpu-3d"] = {"observed": "PRESENT" if gpu[0].get("driver") == "amdgpu" else "ABSENT",
                         "detail": f"GPU {gpu[0].get('device')} driver {gpu[0].get('driver')}"}
    g = doc.get("gpu") or {}
    if "orbis_gtt_cgroup" in g:
        out["ge-unified-memory"] = {"observed": "PRESENT" if g["orbis_gtt_cgroup"].startswith("registered") else "ABSENT",
                                    "detail": f"orbis_gtt_cgroup: {g['orbis_gtt_cgroup']}"}
    if "ps4_sclk" in g:
        out["gpu-clocks"] = {"observed": "PRESENT", "detail": f"ps4_sclk: {g['ps4_sclk']}"}
    drv = _rd("sys/devices/system/cpu/cpufreq/policy0/scaling_driver")
    out["cpufreq"] = {"observed": "PRESENT" if drv else "NOT_CHECKED", "detail": f"scaling_driver {drv}" if drv else "no cpufreq policy0"}
    return out


def compatibility(reg: Registry, tid: str, doc: dict | None = None) -> dict:
    eff = reg.resolve(tid)
    rep = {"schema": "orbis-target-compatibility/1", "target": tid, "name": eff["name"], "status": eff["status"],
           "kernel": eff["kernel"], "capabilities": eff["capabilities"], "unified_memory": eff.get("unified_memory")}
    if doc is not None:
        m = matcher.match(reg, prober.facts(doc))
        rep["identity"] = m
        rep["this_console_is_target"] = m.get("result") == "confirmed" and m.get("target") == tid
        obs = observe(doc, eff)
        for cap, o in obs.items():
            if cap in rep["capabilities"]:
                rep["capabilities"][cap] = {**rep["capabilities"][cap], "observed_now": o}
    return rep


def qualify(reg: Registry, tid: str, doc: dict) -> dict:
    eff = reg.resolve(tid)
    m = matcher.match(reg, prober.facts(doc))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rec = {"schema": "orbis-target-qualification/1", "id": f"qual-{tid}-{stamp}", "target": tid, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "identity": m, "checks": {}, "registry_patch": {}, "review_required": True}
    if not (m.get("result") == "confirmed" and m.get("target") == tid):
        rec["result"] = "BLOCKED"
        rec["why"] = f"this console is not confirmed as {tid} (match: {m.get('result')} {m.get('target') or m.get('candidates')})"
        return rec
    obs = observe(doc, eff)
    q = _json("var/lib/orbis-graphics/last-qualify.json") or {}
    mb = _json("var/lib/orbis-graphics/membench.json") or {}
    for cap, c in eff["capabilities"].items():
        if c.get("state") == "unsupported":
            rec["checks"][cap] = {"result": "UNSUPPORTED", "reason": c.get("reason")}
            continue
        o = obs.get(cap)
        r = {"result": "NOT_RUN", "how": c.get("needs")}
        if cap == "gpu-3d" and q:
            r = {"result": "PASS" if q.get("result") == "PASS" else "FAIL", "evidence": "var/lib/orbis-graphics/last-qualify.json",
                 "detail": q.get("result")}
        elif cap == "ge-unified-memory" and mb:
            r = {"result": "PASS" if mb.get("result") == "PASS" and o and o["observed"] == "PRESENT" else
                 ("NOT_RUN" if mb.get("result") == "NOT_RUN" else "FAIL"),
                 "evidence": "var/lib/orbis-graphics/membench.json", "detail": f"membench {mb.get('result')}, {o and o['detail']}"}
        elif o and o["observed"] in ("PRESENT", "ABSENT") and cap in ("southbridge-io", "cpufreq", "gpu-clocks"):
            r = {"result": "PASS" if o["observed"] == "PRESENT" else "FAIL", "evidence": "orbis-hw probe", "detail": o["detail"]}
        rec["checks"][cap] = r
        if r["result"] == "PASS" and c.get("state") != "verified":
            rec["registry_patch"][cap] = {"state": "verified", "evidence": f"{rec['id']}: {r.get('evidence')} ({r.get('detail')})"}
    res = [c["result"] for c in rec["checks"].values()]
    rec["result"] = "FAIL" if "FAIL" in res else ("PASS" if res and all(x in ("PASS", "UNSUPPORTED") for x in res) else "INCOMPLETE")
    rec["note"] = ("registry_patch lists capabilities this run showed working; it is a proposal for NickLabs review "
                   f"(edit {eff['file']} with the evidence), never applied automatically")
    return rec
