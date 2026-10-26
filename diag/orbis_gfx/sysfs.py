"""Read-only access to the kernel's view of the GPU: PCI, amdgpu sysfs, debugfs, DRM connectors,
hwmon and the kernel log. Every path is re-rooted under ORBIS_GFX_ROOT so tests run on fixture
trees. Nothing in this module writes to the hardware."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LIVERPOOL_IDS = {"0x9920", "0x9922", "0x9923"}
GLADIUS_IDS = {"0x9924"}
# names as printed by debugfs amdgpu_firmware_info for the 8 GFX7 microcode images the loader also extracts
FW_EXPECTED = ["CE", "PFP", "ME", "MEC", "MEC2", "RLC", "SDMA0", "SDMA1"]

FAULT_PATTERNS = {
    "vm_fault": re.compile(r"amdgpu.*(VM_CONTEXT1_PROTECTION_FAULT|GPU fault detected|page fault)", re.I),
    "ring_timeout": re.compile(r"(ring \S+ timeout|\*ERROR\* ring .* test failed|ring .* timeout)", re.I),
    "gpu_reset": re.compile(r"amdgpu.*(GPU reset|gpu recover|ASIC reset)", re.I),
    "fw_fail": re.compile(r"(amdgpu|mwifiex|radeon).*(failed to load firmware|Direct firmware load for .* failed|firmware .* not found)", re.I),
    "ib_test_fail": re.compile(r"(ib test failed|IB test failed)", re.I),
    "dp_link": re.compile(r"(clock recovery tried|link training failed|amdgpu_atombios_dp_link_train)", re.I),
    "bridge": re.compile(r"ps4[_-]bridge", re.I),
}


def root() -> Path:
    return Path(os.environ.get("ORBIS_GFX_ROOT", "/"))


def p(*parts: str) -> Path:
    r = root()
    out = r
    for part in parts:
        out = out / str(part).lstrip("/")
    return out


def read(path: Path, default: str | None = None) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return default


def read_int(path: Path) -> int | None:
    v = read(path)
    try:
        return int(v, 0) if v is not None else None
    except ValueError:
        return None


@dataclass
class Gpu:
    card: str
    pci_slot: str
    vendor: str | None
    device: str | None
    revision: str | None
    subsystem: str | None
    driver: str | None
    name: str
    sysdir: Path
    debugfs: Path | None
    attrs: dict = field(default_factory=dict)


def gpu_name(vendor: str | None, device: str | None) -> str:
    if vendor == "0x1002" and device in LIVERPOOL_IDS:
        return "Liverpool (PS4)"
    if vendor == "0x1002" and device in GLADIUS_IDS:
        return "Gladius (PS4 Pro)"
    if vendor == "0x1002":
        return "AMD (not a PS4 GPU)"
    return "unknown"


def find_gpus() -> list[Gpu]:
    out = []
    drm = p("sys/class/drm")
    for card in sorted(drm.glob("card[0-9]*")) if drm.is_dir() else []:
        if "-" in card.name:
            continue
        dev = card / "device"
        vendor, device = read(dev / "vendor"), read(dev / "device")
        drv = None
        try:
            drv = Path(os.readlink(dev / "driver")).name
        except OSError:
            pass
        try:
            slot = Path(os.path.realpath(dev)).name
        except OSError:
            slot = "?"
        idx = card.name[4:]
        dbg = p("sys/kernel/debug/dri", idx)
        sub = None
        sv, sd = read(dev / "subsystem_vendor"), read(dev / "subsystem_device")
        if sv and sd:
            sub = f"{sv}:{sd}"
        out.append(Gpu(card.name, slot, vendor, device, read(dev / "revision"), sub, drv,
                       gpu_name(vendor, device), dev, dbg if dbg.is_dir() else None))
    return out


def primary_gpu() -> Gpu | None:
    gpus = find_gpus()
    for g in gpus:
        if g.vendor == "0x1002" and g.device in LIVERPOOL_IDS | GLADIUS_IDS:
            return g
    return gpus[0] if gpus else None


MEM_ATTRS = ["mem_info_vram_total", "mem_info_vram_used", "mem_info_vis_vram_total", "mem_info_vis_vram_used",
             "mem_info_gtt_total", "mem_info_gtt_used"]
PM_ATTRS = ["power_dpm_state", "power_dpm_force_performance_level", "pp_dpm_sclk", "pp_dpm_mclk",
            "pp_power_profile_mode", "gpu_busy_percent", "mem_busy_percent", "pp_od_clk_voltage"]


def memory(g: Gpu) -> dict:
    return {a: read_int(g.sysdir / a) for a in MEM_ATTRS}


def power(g: Gpu) -> dict:
    return {a: read(g.sysdir / a) for a in PM_ATTRS}


def hwmon(g: Gpu) -> dict:
    out = {}
    for h in sorted((g.sysdir / "hwmon").glob("hwmon*")) if (g.sysdir / "hwmon").is_dir() else []:
        for f in sorted(h.iterdir()):
            if re.match(r"(temp\d+_(input|label|crit)|fan\d+_input|pwm\d+|power\d+_average|freq\d+_(input|label)|in\d+_input)$", f.name):
                out[f"{h.name}/{f.name}"] = read(f)
    return out


def cpu_temps() -> dict:
    out = {}
    base = p("sys/class/hwmon")
    for h in sorted(base.glob("hwmon*")) if base.is_dir() else []:
        name = read(h / "name")
        if name in ("k10temp", "fam15h_power"):
            for f in sorted(h.glob("temp*_input")):
                out[f"{name}/{f.name}"] = read_int(f)
    return out


def connectors(g: Gpu) -> list[dict]:
    out = []
    for c in sorted(p("sys/class/drm").glob(f"{g.card}-*")):
        edid = c / "edid"
        try:
            eb = edid.read_bytes()
        except OSError:
            eb = b""
        modes = (read(c / "modes") or "").split()
        out.append({"name": c.name[len(g.card) + 1:], "status": read(c / "status"), "enabled": read(c / "enabled"),
                    "dpms": read(c / "dpms"), "modes": modes, "edid_bytes": len(eb),
                    "edid_valid_header": eb[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00" if eb else False,
                    "edid_hex": eb[:256].hex() if eb else ""})
    return out


def firmware_info(g: Gpu) -> dict:
    """Parse debugfs amdgpu_firmware_info: {name: {"feature": .., "version": ..}}."""
    out = {}
    if not g.debugfs:
        return out
    txt = read(g.debugfs / "amdgpu_firmware_info") or ""
    for line in txt.splitlines():
        m = re.match(r"\s*(\S[^:]*?)\s+feature version:\s*(\d+),\s*firmware version:\s*(0x[0-9a-fA-F]+)", line)
        if m:
            out[m.group(1).strip()] = {"feature": int(m.group(2)), "version": m.group(3).lower()}
    return out


def rings(g: Gpu) -> list[str]:
    if not g.debugfs:
        return []
    return sorted(f.name[len("amdgpu_ring_"):] for f in g.debugfs.glob("amdgpu_ring_*"))


def module_params() -> dict:
    d = p("sys/module/amdgpu/parameters")
    out = {}
    for f in sorted(d.iterdir()) if d.is_dir() else []:
        if f.name in ("dpm", "dc", "gpu_recovery", "vm_size", "gttsize", "lockup_timeout", "ppfeaturemask",
                      "runpm", "audio", "msi", "sched_jobs", "vramlimit", "cik_support", "virtual_display",
                      "abmlevel", "aspm"):
            out[f.name] = read(f)
    return out


def kernel_log() -> str:
    f = os.environ.get("ORBIS_GFX_KMSG_FILE")
    if f:
        return Path(f).read_text(errors="replace")
    try:
        r = subprocess.run(["dmesg", "--notime"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return read(p("var/log/orbis/dmesg-boot.txt")) or ""


def scan_log(text: str) -> dict:
    hits = {k: [] for k in FAULT_PATTERNS}
    for line in text.splitlines():
        for k, rx in FAULT_PATTERNS.items():
            if rx.search(line) and len(hits[k]) < 20:
                hits[k].append(line.strip()[:240])
    return hits


def cmdline() -> str:
    return read(p("proc/cmdline")) or ""


def meminfo() -> dict:
    out = {}
    for line in (read(p("proc/meminfo")) or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "CmaTotal"):
            out[k] = int(v.split()[0]) if v.split() else None
    return out
