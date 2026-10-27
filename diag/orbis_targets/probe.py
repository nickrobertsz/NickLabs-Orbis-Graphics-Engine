"""orbis-hw probe: a safe, non-destructive hardware inventory (Porting Kit).

What it reads, and nothing else:
  * /sys/bus/pci/devices/*/{vendor,device,subsystem_vendor,subsystem_device,class,revision} and
    the bound driver name (a symlink) - PCI configuration as already parsed by the kernel;
  * /proc/cpuinfo (model name, family/model/stepping, flags), /proc/meminfo, /proc/cmdline;
  * /sys/class/drm/card*/device/* attribute files the GE kernel publishes (no register access);
  * the kernel log only when given as a file (--dmesg FILE) or readable at /dev/kmsg is NOT used:
    logs are imported explicitly (bootloader/serial/dmesg text files).
It never opens /dev/mem, PCI resource files, MMIO, I/O ports, ICC or SMU mailboxes, and never
writes anything. Serial numbers, MAC addresses and console IDs are redacted from imported logs.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path


def _root() -> Path:
    return Path(os.environ.get("ORBIS_GFX_ROOT") or os.environ.get("ORBIS_ROOT") or "/")


def _rd(p: Path) -> str | None:
    try:
        return p.read_text(errors="replace").strip()
    except OSError:
        return None


REDACTIONS = [
    (re.compile(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
    (re.compile(r"(?i)\b(serial(?:\s*(?:no|number))?|s/n|sn|psid|idps|openpsid|console[_ ]?id)\s*[:=]\s*\S+"), r"\1: <redacted>"),
    (re.compile(r"\b[A-Z]{2}\d{9,}\b"), "<serial>"),           # console/board serial style
    (re.compile(r"\b[0-9A-Fa-f]{32}\b"), "<id128>"),            # IDPS/PSID-like 128-bit ids
]


def redact(text: str) -> str:
    for rx, rep in REDACTIONS:
        text = rx.sub(rep, text)
    return text


def pci_devices() -> list[dict]:
    base = _root() / "sys/bus/pci/devices"
    out = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for d in entries:
        rec = {"slot": d.name}
        for k in ("vendor", "device", "subsystem_vendor", "subsystem_device", "class", "revision"):
            v = _rd(d / k)
            if v is not None:
                rec[k] = v.lower()
        try:
            rec["driver"] = os.path.basename(os.readlink(d / "driver"))
        except OSError:
            rec["driver"] = None
        out.append(rec)
    return out


def cpu_info() -> dict:
    txt = _rd(_root() / "proc/cpuinfo") or ""
    first = txt.split("\n\n")[0]
    info = {}
    for line in first.splitlines():
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
    return {"model_name": info.get("model name"), "family": info.get("cpu family"), "model": info.get("model"),
            "stepping": info.get("stepping"), "cpus": txt.count("processor\t:") or None,
            "flags": sorted((info.get("flags") or "").split())}


def mem_info() -> dict:
    txt = _rd(_root() / "proc/meminfo") or ""
    m = re.search(r"MemTotal:\s+(\d+) kB", txt)
    return {"mem_total": int(m.group(1)) * 1024 if m else None}


def gpu_sysfs() -> dict:
    out = {}
    drm = _root() / "sys/class/drm"
    try:
        cards = sorted(x for x in drm.iterdir() if re.fullmatch(r"card\d+", x.name))
    except OSError:
        return out
    for c in cards:
        dev = c / "device"
        if _rd(dev / "vendor") != "0x1002":
            continue
        for k in ("device", "mem_info_vram_total", "mem_info_gtt_total", "orbis_gtt_cgroup", "ps4_sclk"):
            v = _rd(dev / k)
            if v is not None:
                out[k] = v
        out["card"] = c.name
        break
    return out


# ------------------------------------------------------------------ log import
LOG_PATTERNS = {
    "model": re.compile(r"\b(CUH-\d{4}[A-Z]?)\b"),
    "motherboard": re.compile(r"\b((?:SA[A-F]|NV[ABG]|HAC|CVN)-[0-9K]{3})\b"),
    "subsys_subid": re.compile(r"(?i)(?:sce_subsys_subid|subsys[_ ]?sub[_ ]?id)\D{0,8}(0x[0-9a-f]{5,6})"),
    "southbridge_word": re.compile(r"(?i)\b(aeolia|belize\s*2|belize2|belize|baikal)\b"),
    "pci_id": re.compile(r"\b(104d|1002):([0-9a-f]{4})\b", re.I),
    "kernel_release": re.compile(r"Linux version (\S+)"),
}


def import_log(text: str, source: str) -> dict:
    """Facts from a bootloader, serial console or dmesg log (text, already captured by the user)."""
    t = redact(text)
    facts: dict = {"source": source, "models": [], "motherboards": [], "subsys_subids": [], "southbridge_words": [],
                   "pci_ids": [], "kernel_releases": []}
    for key, rx in LOG_PATTERNS.items():
        vals = []
        for m in rx.finditer(t):
            v = ":".join(x.lower() for x in m.groups()) if key == "pci_id" else m.group(1)
            if key == "southbridge_word":
                v = v.lower().replace(" ", "")
            if key == "model":
                v = v.upper()
            if v not in vals:
                vals.append(v)
        facts[{"model": "models", "motherboard": "motherboards", "subsys_subid": "subsys_subids",
               "southbridge_word": "southbridge_words", "pci_id": "pci_ids", "kernel_release": "kernel_releases"}[key]] = vals
    facts["excerpt_lines"] = [l for l in t.splitlines() if re.search(r"(?i)aeolia|belize|baikal|CUH-|subsys|1002:99|104d:90", l)][:40]
    return facts


def probe(logs: list[tuple[str, str]] | None = None, model_hint: str | None = None) -> dict:
    pci = pci_devices()
    doc = {"schema": "orbis-hw-probe/1", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "safety": "read-only: sysfs/procfs attributes and user-supplied logs; no MMIO, no register access, no writes",
           "pci": pci, "cpu": cpu_info(), "memory": mem_info(), "gpu": gpu_sysfs(),
           "kernel": {"release": _rd(_root() / "proc/sys/kernel/osrelease"), "cmdline": redact(_rd(_root() / "proc/cmdline") or "")},
           "logs": [import_log(text, src) for src, text in (logs or [])],
           "hints": {"model": model_hint.upper() if model_hint else None}}
    return doc


def facts(doc: dict) -> dict:
    """Normalize a probe document into matchable facts (used by match.py)."""
    f = {"gpu_pci_device": None, "southbridge_pci": set(), "subsys_subids": set(), "models": set(), "motherboards": set(),
         "southbridge_words": set(), "cpu_jaguar": None}
    for d in doc.get("pci") or []:
        if d.get("vendor") == "0x1002" and (d.get("class") or "").startswith("0x03"):
            f["gpu_pci_device"] = d.get("device")
        if d.get("vendor") == "0x104d":
            f["southbridge_pci"].add(d.get("device"))
    g = (doc.get("gpu") or {}).get("device")
    if g and not f["gpu_pci_device"]:
        f["gpu_pci_device"] = g.lower()
    for lg in doc.get("logs") or []:
        f["models"].update(lg.get("models") or [])
        f["motherboards"].update(lg.get("motherboards") or [])
        f["subsys_subids"].update(x.lower() for x in lg.get("subsys_subids") or [])
        f["southbridge_words"].update(lg.get("southbridge_words") or [])
        for pid in lg.get("pci_ids") or []:
            ven, dev = pid.split(":")
            if ven == "104d":
                f["southbridge_pci"].add("0x" + dev)
            elif ven == "1002" and dev.startswith("99") and not f["gpu_pci_device"]:
                f["gpu_pci_device"] = "0x" + dev
    if (doc.get("hints") or {}).get("model"):
        f["models"].add(doc["hints"]["model"])
    cpu = doc.get("cpu") or {}
    if cpu.get("family"):
        f["cpu_jaguar"] = cpu.get("family") == "22"     # AMD family 16h (Jaguar)
    return f
