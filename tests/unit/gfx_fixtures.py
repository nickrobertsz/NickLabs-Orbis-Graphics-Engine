"""Fixture trees and canned tool outputs for the Orbis Graphics tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

EDID = bytes.fromhex("00ffffffffffff00") + bytes(120)


def ps4_root(root: Path, *, driver="amdgpu", device="0x9920", connected=True, debugfs=True, rings=None,
             pm=False, uvd=False, kmsg="", hwmon_temp=False, governor=False) -> Path:
    pci = root / "sys/devices/pci0000:00/0000:00:01.0"
    pci.mkdir(parents=True)
    for k, v in {"vendor": "0x1002", "device": device, "revision": "0x00", "subsystem_vendor": "0x1002",
                 "subsystem_device": "0x0000", "mem_info_vram_total": str(1024 << 20), "mem_info_vram_used": str(64 << 20),
                 "mem_info_vis_vram_total": str(256 << 20), "mem_info_gtt_total": str(3 << 30),
                 "mem_info_gtt_used": str(10 << 20)}.items():
        (pci / k).write_text(v + "\n")
    (root / "sys/bus/pci/devices").mkdir(parents=True)
    os.symlink(pci, root / "sys/bus/pci/devices/0000:00:01.0")
    (pci / "class").write_text("0x030000\n")
    (pci / "config").write_bytes(bytes.fromhex("0210209907000000") + bytes(56))
    drv = root / "sys/bus/pci/drivers" / driver
    drv.mkdir(parents=True)
    os.symlink(drv, pci / "driver")
    if pm:
        (pci / "power_dpm_force_performance_level").write_text("auto\n")
        (pci / "pp_dpm_sclk").write_text("0: 300Mhz\n1: 800Mhz *\n")
    if hwmon_temp:
        h = pci / "hwmon/hwmon1"
        h.mkdir(parents=True)
        (h / "temp1_input").write_text("55000\n")
    drm = root / "sys/class/drm"
    drm.mkdir(parents=True)
    card = drm / "card0"
    card.mkdir()
    os.symlink(pci, card / "device")
    con = drm / "card0-HDMI-A-1"
    con.mkdir()
    (con / "status").write_text("connected\n" if connected else "disconnected\n")
    (con / "enabled").write_text("enabled\n" if connected else "disabled\n")
    (con / "dpms").write_text("On\n")
    (con / "modes").write_text("1920x1080\n1280x720\n" if connected else "")
    (con / "edid").write_bytes(EDID if connected else b"")
    if debugfs:
        dbg = root / "sys/kernel/debug/dri/0"
        dbg.mkdir(parents=True)
        (dbg / "amdgpu_firmware_info").write_text("\n".join(
            f"{n} feature version: 1, firmware version: 0x000000{i + 10:02x}"
            for i, n in enumerate(["CE", "PFP", "ME", "MEC", "MEC2", "RLC", "SDMA0", "SDMA1"])) + "\n")
        for r in rings if rings is not None else ["gfx", "comp_1.0.0", "comp_1.1.0", "sdma0", "sdma1"] + (["uvd"] if uvd else []):
            (dbg / f"amdgpu_ring_{r}").write_text("")
    if governor:
        c = root / "sys/devices/system/cpu/cpu0/cpufreq"
        c.mkdir(parents=True)
        (c / "scaling_governor").write_text("schedutil\n")
        (c / "scaling_available_governors").write_text("performance powersave schedutil\n")
    params = root / "sys/module/amdgpu/parameters"
    params.mkdir(parents=True)
    (params / "gpu_recovery").write_text("-1\n")
    (root / "proc").mkdir(exist_ok=True)
    (root / "proc/cmdline").write_text("root=LABEL=ORBIS-ROOT orbis.mode=pc\n")
    (root / "proc/meminfo").write_text("MemTotal: 7000000 kB\nMemAvailable: 6000000 kB\n")
    kf = root / "kmsg.txt"
    kf.write_text(kmsg)
    return kf


def probe_json(*, renderer="AMD Liverpool (radeonsi, LLVM 21.1.8, DRM 3.61, 6.15.4-orbis-ps4)", draw_ok=True,
               shader_ok=True, devices=None, compute_ok=True, raster_ok=True, fatal=False, cl=None) -> dict:
    devs = devices if devices is not None else [
        {"index": 0, "name": "AMD Liverpool (RADV GFX7)", "device_type": 1, "vendor_id": 4098, "device_id": 39200,
         "api_version": "1.3.320", "driver_id": 3, "driver_name": "radv", "driver_info": "Mesa 26.2.5-orbis"}]
    return {
        "probe": "orbis-gfx-probe", "probe_version": "1.0.0", "raster_shaders": True,
        "gl": {"renderer": renderer, "vendor": "AMD", "version": "OpenGL ES 3.2 Mesa 26.2.5", "draw_ok": draw_ok,
               "bench": {"shader_ok": shader_ok, "shader_wrong_px": 0 if shader_ok else 77, "shader_max_channel_diff": 0,
                         "frame_ms_1080p": {"frames": 120, "min": 1.0, "median": 2.0, "p95": 2.5, "max": 3.0},
                         "draw_calls": {"count": 5000, "total_ms": 10.0, "us_per_draw": 2.0}}},
        "vulkan": {"devices": devs},
        "raster": {"device_index": 0, "tests": [{"test": "raster_gradient", "ok": raster_ok, "seconds": 0.01, "detail": "x"},
                                                {"test": "raster_depth", "ok": raster_ok, "seconds": 0.01, "detail": "y"}],
                   "fatal": False},
        "compute": {"tests": [{"test": "fill_4096", "ok": compute_ok, "seconds": 0.01},
                              {"test": "saxpy_u32_1048576", "ok": True, "seconds": 0.02},
                              {"test": "pipeline_cache", "ok": True, "seconds": 0, "detail": "first_ms=3"}],
                    "passed_count": 3 if compute_ok else 2, "failed_count": 0 if compute_ok else 1, "fatal": fatal,
                    **({"fatal_reason": "VK_ERROR_DEVICE_LOST"} if fatal else {})},
        "opencl": cl if cl is not None else {"platform_count": 1, "platforms": [
            {"name": "rusticl", "version": "OpenCL 3.0", "devices": [
                {"name": "AMD Liverpool (radeonsi)", "version": "OpenCL 1.2", "type": 4, "ok": True, "cl_error": 0,
                 "wrong": 0, "run_s": 0.01}]}]},
        "exit_code": 0}


class FakeRunner:
    """Answers argv by program name; records calls."""

    def __init__(self, probe: dict | None = None, vainfo: str = "", missing=(), vkcube_rc=0):
        self.probe, self.vainfo, self.missing, self.vkcube_rc = probe, vainfo, set(missing), vkcube_rc
        self.calls = []

    def __call__(self, argv, timeout=60.0, env=None):
        from orbis_gfx.runner import Run
        self.calls.append((list(argv), env))
        name = os.path.basename(argv[0])
        if name in self.missing:
            return Run(argv, None, "", "missing", False, 0.0, missing=True)
        if "probe" in name:
            return Run(argv, 0, json.dumps(self.probe or {}), "", False, 0.1)
        if name == "vainfo":
            return Run(argv, 0, self.vainfo, "", False, 0.1)
        if name == "vkcube":
            return Run(argv, self.vkcube_rc, "", "", False, 1.0)
        return Run(argv, 0, "", "", False, 0.0)
