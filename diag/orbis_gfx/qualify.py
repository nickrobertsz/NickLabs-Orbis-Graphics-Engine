"""orbis-gpu qualify: graphics qualification of a running Orbis system.

Kernel-side checks read sysfs/debugfs/dmesg. Userspace checks run orbis-gfx-probe and the
standard tools (vulkaninfo, vainfo, vkcube) in bounded child processes. Heavy or risky
tests are opt-in and otherwise reported as NOT_RUN with the flag that enables them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from . import __version__, runner, sysfs
from .results import Check, Report

SOFTWARE_RENDERERS = re.compile(r"llvmpipe|softpipe|swrast|lavapipe|software", re.I)
VK_DRIVER_RADV = 3
VK_TYPE_CPU = 4
KERNEL_PM_BLOCKER = ("no power-management (SMU/DPM) IP block is registered for CHIP_LIVERPOOL in the Orbis "
                     "6.15.4 kernel (cik.c: pp_smu commented out), so amdgpu exposes no clock or DPM controls")
KERNEL_VIDEO_BLOCKER = ("the UVD 4.2 and VCE 2.0 IP blocks are commented out for CHIP_LIVERPOOL in the Orbis "
                        "6.15.4 kernel (cik.c), so there is no hardware video decode/encode ring")


class Ctx:
    def __init__(self, opts: dict, run=runner.run):
        self.opts = opts
        self.run = run
        self.probe = opts.get("probe") or runner.probe_path()
        self.timeout = float(opts.get("timeout", 120))
        self.gpu = None


def _ck(rep: Report, **kw) -> Check:
    return rep.add(Check(**kw))


# ------------------------------------------------------------------ kernel
def kernel_checks(rep: Report, cx: Ctx) -> None:
    gpus = sysfs.find_gpus()
    g = sysfs.primary_gpu()
    cx.gpu = g
    if not g:
        _ck(rep, id="gpu.identity", title="GPU present on the PCI bus", state="FAIL", category="kernel",
            required=True, detail="no DRM card found under /sys/class/drm")
        return
    is_ps4 = g.vendor == "0x1002" and g.device in sysfs.LIVERPOOL_IDS
    _ck(rep, id="gpu.identity", title="Liverpool GPU identified", category="kernel", required=True,
        state="PASS" if is_ps4 else "FAIL",
        detail=f"{g.name}: {g.vendor}:{g.device} rev {g.revision} subsys {g.subsystem} at {g.pci_slot}",
        evidence={"gpus": [{"card": x.card, "vendor": x.vendor, "device": x.device, "revision": x.revision,
                            "subsystem": x.subsystem, "slot": x.pci_slot, "driver": x.driver} for x in gpus]})
    _ck(rep, id="gpu.driver", title="amdgpu kernel driver bound", category="kernel", required=True,
        state="PASS" if g.driver == "amdgpu" else "FAIL", detail=f"driver: {g.driver}",
        evidence={"module_params": sysfs.module_params(), "cmdline": sysfs.cmdline()})
    log = sysfs.kernel_log()
    hits = sysfs.scan_log(log)
    fw = sysfs.firmware_info(g)
    present = sorted(k for k, v in fw.items() if v["version"] not in ("0x00000000",))
    if hits["fw_fail"]:
        st, det = "FAIL", f"firmware load failures in the kernel log: {len(hits['fw_fail'])}"
    elif fw:
        missing = [n for n in sysfs.FW_EXPECTED if n not in {k.split()[0].upper() for k in present}]
        st = "PASS" if not missing else "DEGRADED"
        det = f"loaded: {', '.join(present)}" + (f"; not reported: {', '.join(missing)}" if missing else "")
    elif log:
        st, det = "PASS", "no firmware failures logged (debugfs amdgpu_firmware_info not readable; run as root)"
    else:
        st, det = "NOT_RUN", "kernel log not readable (run as root)"
    _ck(rep, id="gpu.firmware", title="GPU firmware loaded", category="kernel", required=True, state=st, detail=det,
        evidence={"firmware_info": fw, "failures": hits["fw_fail"]})
    rings = sysfs.rings(g)
    if rings:
        want = {"gfx", "sdma0", "sdma1"}
        have = {r.split("_")[0] if r.startswith("comp") else r for r in rings}
        miss = sorted(w for w in want if not any(r.startswith(w) for r in rings))
        _ck(rep, id="gpu.rings", title="GFX/compute/SDMA rings registered", category="kernel",
            state="PASS" if not miss else "FAIL", detail=f"rings: {', '.join(rings)}" + (f"; missing {miss}" if miss else ""),
            evidence={"rings": rings, "have": sorted(have)})
    else:
        _ck(rep, id="gpu.rings", title="GFX/compute/SDMA rings registered", category="kernel", state="NOT_RUN",
            detail="debugfs dri/<n> not readable (mount debugfs, run as root)")
    if cx.opts.get("ring_test"):
        if g.debugfs and (g.debugfs / "amdgpu_test_ib").exists():
            t0 = time.monotonic()
            out = sysfs.read(g.debugfs / "amdgpu_test_ib")
            after = sysfs.scan_log(sysfs.kernel_log())
            bad = len(after["ib_test_fail"]) > len(hits["ib_test_fail"]) or (out and "fail" in out.lower())
            _ck(rep, id="gpu.ib_test", title="kernel IB test on every ring", category="kernel",
                state="FAIL" if bad else "PASS", duration_s=time.monotonic() - t0, detail=(out or "")[:300],
                evidence={"output": out})
        else:
            _ck(rep, id="gpu.ib_test", title="kernel IB test on every ring", category="kernel", state="NOT_RUN",
                detail="debugfs amdgpu_test_ib not available")
    else:
        _ck(rep, id="gpu.ib_test", title="kernel IB test on every ring", category="kernel", state="NOT_RUN",
            detail="opt-in: --ring-test (briefly parks the GPU schedulers)")
    mem = sysfs.memory(g)
    vram = mem.get("mem_info_vram_total")
    _ck(rep, id="gpu.memory", title="VRAM carve-out and GTT reported", category="kernel",
        state="PASS" if vram and mem.get("mem_info_gtt_total") else "FAIL" if mem.get("mem_info_vram_total") is None and g.driver == "amdgpu" else "NOT_RUN",
        detail=f"VRAM {(vram or 0) >> 20} MiB, GTT {(mem.get('mem_info_gtt_total') or 0) >> 20} MiB, "
               f"visible VRAM {(mem.get('mem_info_vis_vram_total') or 0) >> 20} MiB",
        evidence={"memory": mem, "meminfo_kib": sysfs.meminfo(), "vram_txt_hint": "set by vram.txt at payload launch"})
    cons = sysfs.connectors(g)
    hdmi = [c for c in cons if c["status"] == "connected"]
    if not cons:
        st, det = "FAIL", "no DRM connectors"
    elif not hdmi:
        st, det = "DEGRADED", "no connector reports 'connected' (headless, or bridge/HPD problem)"
    else:
        c = hdmi[0]
        has1080 = "1920x1080" in c["modes"]
        st = "PASS" if has1080 else "DEGRADED"
        det = f"{c['name']} connected, enabled={c['enabled']}, {len(c['modes'])} modes" + ("" if has1080 else ", no 1920x1080")
    _ck(rep, id="display.connector", title="HDMI connector and modes", category="display", state=st, detail=det,
        evidence={"connectors": [{k: v for k, v in c.items() if k != "edid_hex"} for c in cons]})
    if hdmi:
        c = hdmi[0]
        _ck(rep, id="display.edid", title="EDID read through the PS4 bridge", category="display",
            state="PASS" if c["edid_valid_header"] else "DEGRADED",
            detail=f"{c['edid_bytes']} bytes" + ("" if c["edid_valid_header"] else
                                                  " (empty/invalid: modes come from the ps4_bridge table)"),
            evidence={"edid_hex": c["edid_hex"]})
    else:
        _ck(rep, id="display.edid", title="EDID read through the PS4 bridge", category="display", state="NOT_RUN",
            detail="no connected display")
    _ck(rep, id="display.link", title="no DP/HDMI link-training or bridge errors logged", category="display",
        state="PASS" if not hits["dp_link"] else "DEGRADED", detail=f"{len(hits['dp_link'])} link messages",
        evidence={"dp_link": hits["dp_link"], "bridge": hits["bridge"]})
    pm = sysfs.power(g)
    if any(pm.get(k) for k in ("power_dpm_force_performance_level", "pp_dpm_sclk")):
        _ck(rep, id="pm.clocks", title="clock / DPM interfaces", category="power", state="PASS",
            detail=f"perf level {pm.get('power_dpm_force_performance_level')}, sclk {pm.get('pp_dpm_sclk')}",
            evidence={"pm": pm})
    else:
        _ck(rep, id="pm.clocks", title="clock / DPM interfaces", category="power", state="UNSUPPORTED",
            blocker=KERNEL_PM_BLOCKER, evidence={"pm": pm})
    hw = sysfs.hwmon(g)
    ct = sysfs.cpu_temps()
    temps = {k: v for k, v in hw.items() if "temp" in k and k.endswith("_input")}
    if temps:
        _ck(rep, id="sensors.gpu", title="GPU temperature sensor", category="power", state="PASS",
            detail=", ".join(f"{k}={int(v) / 1000:.1f}°C" for k, v in temps.items() if v and v.lstrip('-').isdigit()),
            evidence={"hwmon": hw})
    else:
        _ck(rep, id="sensors.gpu", title="GPU temperature sensor", category="power", state="UNSUPPORTED",
            blocker="amdgpu registers no hwmon temperature for Liverpool (no SMU/thermal block on this kernel); "
                    "APU temperature is approximated by k10temp", evidence={"hwmon": hw, "cpu_temps": ct})
    faults = {k: len(hits[k]) for k in ("vm_fault", "ring_timeout", "gpu_reset")}
    _ck(rep, id="health.since_boot", title="no VM faults, ring timeouts or resets since boot", category="kernel",
        state="PASS" if not any(faults.values()) else "FAIL", detail=", ".join(f"{k}={v}" for k, v in faults.items()),
        evidence={k: hits[k] for k in faults})


# ------------------------------------------------------------------ userspace
def _probe(cx: Ctx, args: list, env: dict | None = None, timeout: float | None = None):
    r = cx.run([cx.probe, "--json", "--timeout-ms", str(int(cx.opts.get("gpu_timeout_ms", 5000)))] + args,
               timeout=timeout or cx.timeout, env=env)
    return r, r.json()


def _blocked(rep, ids_titles, why):
    for cid, title, cat in ids_titles:
        _ck(rep, id=cid, title=title, category=cat, state="BLOCKED", blocker=why)


def userspace_checks(rep: Report, cx: Ctx) -> None:
    driver_ok = rep.passed("gpu.driver")
    if not driver_ok and not cx.opts.get("force_userspace"):
        _blocked(rep, [("gl.renderer", "OpenGL ES renderer is the GPU", "opengl"),
                       ("vk.device", "Vulkan device is RADV on the GPU", "vulkan"),
                       ("vk.compute", "Vulkan compute correctness", "vulkan"),
                       ("vk.raster", "Vulkan rasterisation and depth", "vulkan"),
                       ("cl.rusticl", "OpenCL (RustiCL) compute correctness", "compute")],
                 "amdgpu is not bound to the GPU (gpu.driver)")
        return
    # OpenGL
    r, j = _probe(cx, ["--gl", "--gl-bench", "--frames", str(cx.opts.get("frames", 120))])
    gl = (j or {}).get("gl") or {}
    if r.missing:
        _ck(rep, id="gl.renderer", title="OpenGL ES renderer is the GPU", category="opengl", state="NOT_RUN",
            required=True, detail=f"orbis-gfx-probe missing at {cx.probe}")
    elif r.timed_out or j is None:
        _ck(rep, id="gl.renderer", title="OpenGL ES renderer is the GPU", category="opengl", state="FAIL", required=True,
            detail="probe timed out (possible GPU hang)" if r.timed_out else "probe output unreadable",
            evidence={"stderr": r.stderr[-2000:]})
    else:
        ren = gl.get("renderer") or ""
        sw = bool(SOFTWARE_RENDERERS.search(ren))
        _ck(rep, id="gl.renderer", title="OpenGL ES renderer is the GPU", category="opengl", required=True,
            state="FAIL" if (sw or not ren) else "PASS",
            detail=(f"SOFTWARE RENDERING: {ren}" if sw else ren or gl.get("error", "no renderer")),
            evidence={k: gl.get(k) for k in ("vendor", "renderer", "version", "glsl", "egl_platform", "error")})
        _ck(rep, id="gl.draw", title="GL draw lands exact pixels", category="opengl",
            state="PASS" if gl.get("draw_ok") else "FAIL", detail=gl.get("draw_error", ""),
            evidence={k: gl.get(k) for k in ("draw_red_px", "draw_blue_px", "draw_wrong_px")})
        b = gl.get("bench") or {}
        _ck(rep, id="gl.shader", title="GLSL arithmetic matches the CPU reference", category="opengl",
            state="PASS" if b.get("shader_ok") else ("FAIL" if b else "NOT_RUN"),
            detail=f"wrong pixels {b.get('shader_wrong_px')}, max channel diff {b.get('shader_max_channel_diff')}")
        ft = b.get("frame_ms_1080p")
        if ft:
            stutter = ft["p95"] > 2.0 * ft["median"]
            _ck(rep, id="gl.frame_timing", title="1080p frame timing (offscreen, glFinish per frame)", category="performance",
                state="DEGRADED" if stutter else "PASS",
                detail=f"median {ft['median']:.2f} ms, p95 {ft['p95']:.2f} ms over {ft['frames']} frames"
                       + (" (p95 > 2x median: unstable)" if stutter else ""), evidence={"frame_ms": ft, "draw_calls": b.get("draw_calls")})
        else:
            _ck(rep, id="gl.frame_timing", title="1080p frame timing", category="performance", state="NOT_RUN",
                detail=b.get("frame_error", "bench not run"))
    # Vulkan device + raster + compute + pipeline cache
    r, j = _probe(cx, ["--vk", "--vk-raster", "--compute", "--pipeline-cache", "--level", str(cx.opts.get("level", 1))])
    vk = (j or {}).get("vulkan") or {}
    devs = vk.get("devices") or []
    hw = [d for d in devs if d.get("device_type") != VK_TYPE_CPU]
    if r.missing:
        _ck(rep, id="vk.device", title="Vulkan device is RADV on the GPU", category="vulkan", state="NOT_RUN", required=True,
            detail="orbis-gfx-probe missing")
        return
    if r.timed_out and not devs:
        _ck(rep, id="vk.device", title="Vulkan device is RADV on the GPU", category="vulkan", state="FAIL", required=True,
            detail="probe timed out before listing devices")
        return
    radv = [d for d in hw if d.get("driver_id") == VK_DRIVER_RADV]
    if radv:
        d = radv[0]
        st, det = "PASS", f"{d.get('name')} ({d.get('driver_name')} {d.get('driver_info')}), Vulkan {d.get('api_version')}"
    elif hw:
        st, det = "DEGRADED", f"non-RADV hardware driver: {hw[0].get('driver_name')}"
    else:
        st = "FAIL"
        det = ("only a CPU Vulkan device (lavapipe/swiftshader): " + ", ".join(d.get("name", "?") for d in devs)) if devs \
            else (vk.get("error") or "no Vulkan devices")
    _ck(rep, id="vk.device", title="Vulkan device is RADV on the GPU", category="vulkan", required=True, state=st,
        detail=det, evidence={"devices": devs})
    if not radv:
        _blocked(rep, [("vk.raster", "Vulkan rasterisation and depth", "vulkan"),
                       ("vk.compute", "Vulkan compute correctness", "vulkan"),
                       ("vk.pipeline_cache", "pipeline creation with/without cache", "performance")],
                 "no RADV hardware device (vk.device)")
    else:
        ra = (j or {}).get("raster") or {}
        if "not_run" in ra:
            _ck(rep, id="vk.raster", title="Vulkan rasterisation and depth", category="vulkan", state="NOT_RUN",
                detail=ra["not_run"])
        else:
            recs = ra.get("tests") or []
            oks = [x.get("ok") for x in recs]
            if ra.get("fatal"):
                st = "FAIL"
            elif recs and all(oks):
                st = "PASS"
            elif recs:
                st = "FAIL"
            else:
                st = "FAIL" if ra.get("error") else "NOT_RUN"
            _ck(rep, id="vk.raster", title="Vulkan rasterisation and depth", category="vulkan", state=st,
                detail=ra.get("error") or ra.get("fatal_reason") or "; ".join(f"{x.get('test')}: {x.get('detail')}" for x in recs),
                evidence={"raster": ra})
        co = (j or {}).get("compute") or {}
        tests = co.get("tests") or []
        pc = [t for t in tests if t.get("test") == "pipeline_cache"]
        core = [t for t in tests if t.get("test") != "pipeline_cache"]
        if co.get("fatal"):
            st, det = "FAIL", f"GPU {co.get('fatal_reason')} after {len(core)} tests"
        elif core:
            nbad = sum(1 for t in core if not t.get("ok"))
            st, det = ("PASS" if not nbad else "FAIL"), f"{len(core) - nbad}/{len(core)} correct"
        else:
            st, det = "FAIL", co.get("error") or "compute suite did not run"
        _ck(rep, id="vk.compute", title="Vulkan compute correctness (numeric oracle)", category="vulkan", state=st,
            detail=det, evidence={"compute": co})
        if pc:
            _ck(rep, id="vk.pipeline_cache", title="pipeline creation with/without cache", category="performance",
                state="PASS" if pc[0].get("ok") else "FAIL", detail=pc[0].get("detail", ""))
    # presentation
    present_checks(rep, cx, radv)
    opencl_checks(rep, cx)
    video_checks(rep, cx)


def present_checks(rep, cx, radv):
    if not radv:
        _ck(rep, id="vk.present", title="Vulkan presentation to the display", category="vulkan", state="BLOCKED",
            blocker="no RADV hardware device")
        return
    disp = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
    if not disp:
        _ck(rep, id="vk.present", title="Vulkan presentation to the display", category="vulkan", state="NOT_RUN",
            detail="no graphical session (run from the PC desktop or GAMING session; DISPLAY/WAYLAND_DISPLAY unset)")
        return
    r = cx.run(["vkcube", "--c", "300"], timeout=60)
    if r.missing:
        _ck(rep, id="vk.present", title="Vulkan presentation to the display", category="vulkan", state="NOT_RUN",
            detail="vkcube not installed (vulkan-tools)")
    else:
        _ck(rep, id="vk.present", title="Vulkan presentation to the display", category="vulkan",
            state="PASS" if r.rc == 0 else "FAIL", duration_s=r.seconds,
            detail=("300 frames presented" if r.rc == 0 else f"vkcube rc={r.rc}{' timed out' if r.timed_out else ''}"),
            evidence={"stderr": r.stderr[-1500:]})


def opencl_checks(rep, cx):
    r, j = _probe(cx, ["--opencl"], env={"RUSTICL_ENABLE": cx.opts.get("rusticl_enable", "radeonsi")})
    cl = (j or {}).get("opencl") or {}
    if r.missing:
        _ck(rep, id="cl.rusticl", title="OpenCL (RustiCL) compute correctness", category="compute", state="NOT_RUN",
            detail="orbis-gfx-probe missing")
        return
    plats = cl.get("platforms") or []
    devs = [d for p in plats for d in p.get("devices", [])]
    gpu = [d for d in devs if not SOFTWARE_RENDERERS.search(d.get("name", "")) and d.get("type", 0) & 4]
    good = [d for d in gpu if d.get("ok")]
    if good:
        st, det = "PASS", f"{good[0]['name']} ({good[0].get('version')}): 1M-element integer kernel exact, run {good[0].get('run_s', 0):.3f}s"
    elif gpu:
        st, det = "FAIL", f"{gpu[0]['name']}: cl_error {gpu[0].get('cl_error')} wrong {gpu[0].get('wrong')}"
    elif devs:
        st, det = "DEGRADED", "only CPU OpenCL devices: " + ", ".join(d.get("name", "?") for d in devs)
    else:
        _ck(rep, id="cl.rusticl", title="OpenCL (RustiCL) compute correctness", category="compute", state="UNSUPPORTED",
            blocker=cl.get("error") or "no OpenCL platform; mesa-orbis-ps4 must be built with -D gallium-rusticl=true "
                                       "and ocl-icd installed", evidence={"opencl": cl})
        return
    _ck(rep, id="cl.rusticl", title="OpenCL (RustiCL) compute correctness", category="compute", state=st, detail=det,
        evidence={"opencl": cl, "env": "RUSTICL_ENABLE=" + cx.opts.get("rusticl_enable", "radeonsi")})


def video_checks(rep, cx):
    g = cx.gpu
    rings = sysfs.rings(g) if g else []
    has_uvd = any(r.startswith("uvd") for r in rings)
    r = cx.run(["vainfo"], timeout=30, env={"LIBVA_DRIVER_NAME": "radeonsi"})
    ent = sorted(set(re.findall(r"(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)", r.stdout or ""))) if not r.missing else []
    dec = [e for e in ent if e[1] == "VAEntrypointVLD"]
    if rings and not has_uvd:
        _ck(rep, id="video.decode", title="hardware video decode (VA-API/UVD)", category="media", state="UNSUPPORTED",
            blocker=KERNEL_VIDEO_BLOCKER, evidence={"rings": rings, "vainfo": ent})
    elif r.missing:
        _ck(rep, id="video.decode", title="hardware video decode (VA-API/UVD)", category="media", state="NOT_RUN",
            detail="vainfo not installed (libva-utils)")
    elif not dec:
        _ck(rep, id="video.decode", title="hardware video decode (VA-API/UVD)", category="media", state="UNSUPPORTED",
            blocker="VA-API reports no decode entrypoints on this GPU", evidence={"vainfo": ent})
    else:
        sample = cx.opts.get("video_sample")
        if not sample:
            _ck(rep, id="video.decode", title="hardware video decode (VA-API/UVD)", category="media", state="NOT_RUN",
                detail=f"{len(dec)} decode profiles advertised; give --video-sample FILE to decode and checksum it",
                evidence={"vainfo": ent})
        else:
            hw = cx.run(["ffmpeg", "-v", "error", "-hwaccel", "vaapi", "-i", sample, "-f", "md5", "-"], timeout=300)
            sw = cx.run(["ffmpeg", "-v", "error", "-i", sample, "-f", "md5", "-"], timeout=300)
            same = hw.rc == 0 and sw.rc == 0 and hw.stdout.strip() == sw.stdout.strip()
            _ck(rep, id="video.decode", title="hardware video decode (VA-API/UVD)", category="media",
                state="PASS" if same else "FAIL", duration_s=hw.seconds,
                detail=f"hw md5 {hw.stdout.strip()} vs sw md5 {sw.stdout.strip()}")


# ------------------------------------------------------------------ opt-in
def sustained_check(rep, cx, seconds: int):
    g = cx.gpu
    samples, perf = [], []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        r, j = _probe(cx, ["--compute"], timeout=120)
        co = (j or {}).get("compute") or {}
        sax = [t for t in co.get("tests", []) if t.get("test", "").startswith("saxpy")]
        perf.append(sax[0]["seconds"] if sax else None)
        snap = {"t": round(time.monotonic(), 1), "ok": bool(co) and not co.get("fatal") and not co.get("failed_count"),
                "cpu_temps": sysfs.cpu_temps()}
        if g:
            snap["hwmon"] = sysfs.hwmon(g)
            snap["busy"] = sysfs.power(g).get("gpu_busy_percent")
        samples.append(snap)
        if co.get("fatal") or r.timed_out:
            break
    ok_all = all(s["ok"] for s in samples) and samples
    good = [x for x in perf if x]
    drop = (max(good[-3:]) / min(good[:3]) - 1.0) if len(good) >= 6 else 0.0
    st = "FAIL" if not ok_all else ("DEGRADED" if drop > 0.15 else "PASS")
    _ck(rep, id="perf.sustained", title=f"sustained compute load ({seconds}s)", category="performance", state=st,
        detail=f"{len(samples)} rounds, all correct={bool(ok_all)}, slowdown first→last {drop * 100:.1f}%",
        evidence={"samples": samples, "saxpy_seconds": perf})


def recovery_check(rep, cx):
    g = cx.gpu
    params = sysfs.module_params()
    if not cx.opts.get("recovery_accept"):
        _ck(rep, id="gpu.recovery", title="GPU reset and recovery", category="kernel", state="NOT_RUN",
            detail=f"opt-in: --recovery --i-accept-gpu-reset (may hang the console). amdgpu.gpu_recovery={params.get('gpu_recovery')}")
        return
    if not (g and g.debugfs and (g.debugfs / "amdgpu_gpu_recover").exists()):
        _ck(rep, id="gpu.recovery", title="GPU reset and recovery", category="kernel", state="NOT_RUN",
            detail="debugfs amdgpu_gpu_recover not available")
        return
    before = sysfs.scan_log(sysfs.kernel_log())
    t0 = time.monotonic()
    sysfs.read(g.debugfs / "amdgpu_gpu_recover")
    time.sleep(5)
    r, j = _probe(cx, ["--compute"], timeout=120)
    co = (j or {}).get("compute") or {}
    after = sysfs.scan_log(sysfs.kernel_log())
    ok = co and not co.get("fatal") and not co.get("failed_count")
    _ck(rep, id="gpu.recovery", title="GPU reset and recovery", category="kernel", state="PASS" if ok else "FAIL",
        duration_s=time.monotonic() - t0,
        detail=f"resets logged {len(before['gpu_reset'])}→{len(after['gpu_reset'])}; compute after reset "
               f"{'correct' if ok else 'failed'}", evidence={"after": after["gpu_reset"]})


def transitions_check(rep, cx):
    orbis = shutil.which("orbis")
    if not orbis:
        _ck(rep, id="modes.transitions", title="graphics after PC/GAMING/AI switches", category="modes", state="NOT_RUN",
            detail="orbis CLI not found (Orbis platform not installed)")
        return
    results = []
    for mode in cx.opts.get("transition_modes", ["ai", "gaming", "pc"]):
        sw = cx.run([orbis, "--json", "--direct", "mode", mode, "--policy", "fast", "--yes"], timeout=180)
        time.sleep(5)
        r, j = _probe(cx, ["--gl", "--vk", "--compute"], timeout=120)
        gl = (j or {}).get("gl") or {}
        co = (j or {}).get("compute") or {}
        ok = (sw.rc == 0 and bool(gl.get("draw_ok")) and not SOFTWARE_RENDERERS.search(gl.get("renderer") or "x")
              and co and not co.get("fatal") and not co.get("failed_count"))
        results.append({"mode": mode, "switch_rc": sw.rc, "gl_renderer": gl.get("renderer"), "gl_draw": gl.get("draw_ok"),
                        "compute_ok": bool(co) and not co.get("failed_count"), "ok": bool(ok)})
    _ck(rep, id="modes.transitions", title="graphics after PC/GAMING/AI switches", category="modes",
        state="PASS" if all(x["ok"] for x in results) else "FAIL",
        detail="; ".join(f"{x['mode']}:{'ok' if x['ok'] else 'FAIL'}" for x in results), evidence={"switches": results})


def qualify(opts: dict, run=runner.run) -> Report:
    cx = Ctx(opts, run)
    rep = Report(tool="orbis-gpu qualify", version=__version__, stage=opts.get("stage", "full-system"))
    rep.context = {"profile": opts.get("hw_profile", "cuh-1215a"), "options": {k: v for k, v in opts.items() if k != "probe"}}
    kernel_checks(rep, cx)
    userspace_checks(rep, cx)
    if opts.get("sustained"):
        sustained_check(rep, cx, int(opts["sustained"]))
    else:
        _ck(rep, id="perf.sustained", title="sustained load, throttling", category="performance", state="NOT_RUN",
            detail="opt-in: --sustained SECONDS (e.g. 600, 1800, 3600)")
    recovery_check(rep, cx)
    if opts.get("transitions"):
        transitions_check(rep, cx)
    else:
        _ck(rep, id="modes.transitions", title="graphics after PC/GAMING/AI switches", category="modes", state="NOT_RUN",
            detail="opt-in: --transitions (switches personalities; run over SSH)")
    return rep
