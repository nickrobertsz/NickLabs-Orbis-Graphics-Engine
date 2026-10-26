"""Video decode status, an honest hardware-vs-software test, and player configuration.

Hardware decode on Liverpool means UVD 4.2 (H.264/AVC up to 1080p, MPEG-2/VC-1/MPEG-4 in the
silicon; no HEVC, no VP9/AV1). It is reported as available only when BOTH the kernel exposes a
UVD ring AND the VA-API driver lists the profile; `orbis-video test` then has to decode real
frames through VA-API surfaces (hwdownload) and match a software decode frame by frame — a
software fallback cannot pass it. Everything else is labelled software decode.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from orbis_gfx import runner, sysfs

from .util import atomic_write, now_iso, p, read

RENDER_NODE = "/dev/dri/renderD128"
UVD_CODECS = {"H264": "VAProfileH264", "MPEG2": "VAProfileMPEG2", "VC1": "VAProfileVC1", "MPEG4": "VAProfileMPEG4"}
NOT_IN_HARDWARE = ["HEVC", "VP9", "AV1"]
MPV_CONF = "etc/mpv/mpv.conf"
RESULT = "var/lib/orbis-graphics/video-test.json"


def kernel_engines() -> dict:
    g = sysfs.primary_gpu()
    rings = sysfs.rings(g) if g else []
    return {"uvd_ring": any(r.startswith("uvd") for r in rings), "vce_ring": any(r.startswith("vce") for r in rings),
            "rings": rings}


def vaapi(run=runner.run) -> dict:
    r = run(["vainfo", "--display", "drm", "--device", RENDER_NODE], timeout=20)
    if r.missing:
        return {"available": False, "why": "vainfo not installed (libva-utils)"}
    text = (r.stdout or "") + (r.stderr or "")
    drv = re.search(r"Driver version:\s*(.+)", text)
    profiles = sorted(set(re.findall(r"(VAProfile\w+)\s*:\s*VAEntrypointVLD", text)))
    return {"available": r.rc == 0, "driver": drv.group(1).strip() if drv else None, "decode_profiles": profiles,
            "rc": r.rc}


def status(run=runner.run) -> dict:
    k = kernel_engines()
    va = vaapi(run)
    hw = {}
    for codec, prof in UVD_CODECS.items():
        listed = any(x.startswith(prof) for x in va.get("decode_profiles", []))
        hw[codec] = "hardware (UVD via VA-API)" if (k["uvd_ring"] and listed) else "software (CPU)"
    for codec in NOT_IN_HARDWARE:
        hw[codec] = "software (CPU): not in the Liverpool UVD 4.2 hardware"
    last = None
    try:
        last = json.loads(p(RESULT).read_text())
    except (OSError, ValueError):
        pass
    return {"kernel": k, "vaapi": va, "decode": hw,
            "encode": {"H264": "software (CPU): no VCE ring in any Orbis kernel"},
            "ffmpeg": shutil.which("ffmpeg") is not None, "mpv": shutil.which("mpv") is not None,
            "last_test": last,
            "note": None if k["uvd_ring"] else "this kernel has no UVD ring: all decode is software "
                                                "(see docs/GRAPHICS-ENGINE-VIDEO.md for the UVD research kernel)"}


def _framemd5(text: str) -> list[str]:
    return [l.split(",")[-1].strip() for l in text.splitlines() if l and not l.startswith("#")]


def test(seconds: int = 4, size: str = "1280x720", run=runner.run, keep: bool = False) -> dict:
    """Encode a synthetic H.264 clip in software, decode it in software and through VA-API, compare."""
    if not shutil.which("ffmpeg") and run is runner.run:
        return {"state": "NOT_RUN", "why": "ffmpeg not installed"}
    k = kernel_engines()
    tmp = Path(tempfile.mkdtemp(prefix="orbis-video-"))
    clip = tmp / "ref.mp4"
    res = {"time": now_iso(), "clip": {"codec": "h264", "size": size, "seconds": seconds}, "kernel": k}
    try:
        enc = run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                   "-t", str(seconds), "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", str(clip)],
                  timeout=300)
        if enc.rc != 0:
            res.update(state="BLOCKED", why=f"reference encode failed: {(enc.stderr or '')[-300:]}")
            return res
        t0 = time.monotonic()
        sw = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(clip), "-pix_fmt", "nv12", "-f", "framemd5", "-"],
                 timeout=300)
        res["software"] = {"rc": sw.rc, "seconds": round(time.monotonic() - t0, 3), "frames": len(_framemd5(sw.stdout or ""))}
        if not k["uvd_ring"]:
            res.update(state="UNSUPPORTED", why="no UVD ring in this kernel: hardware decode not possible; software decode "
                                                 f"{'works' if sw.rc == 0 else 'FAILED'}")
            return res
        t0 = time.monotonic()
        hw = run(["ffmpeg", "-nostdin", "-v", "error", "-hwaccel", "vaapi", "-hwaccel_device", RENDER_NODE,
                  "-hwaccel_output_format", "vaapi", "-i", str(clip), "-vf", "hwdownload,format=nv12",
                  "-f", "framemd5", "-"], timeout=300)
        res["hardware"] = {"rc": hw.rc, "seconds": round(time.monotonic() - t0, 3),
                           "frames": len(_framemd5(hw.stdout or "")), "stderr_tail": (hw.stderr or "")[-400:]}
        a, b = _framemd5(sw.stdout or ""), _framemd5(hw.stdout or "")
        mism = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        res["frame_mismatches"] = mism
        if hw.rc != 0 or not b:
            res.update(state="FAIL", why="VA-API decode did not produce frames")
        elif mism:
            res.update(state="FAIL", why=f"{mism} of {len(a)} frames differ from the software decode")
        else:
            res.update(state="PASS", why=f"{len(b)} frames decoded by UVD through VA-API, identical to software")
        return res
    finally:
        try:
            atomic_write(p(RESULT), json.dumps(res, indent=1) + "\n")
        except OSError:
            pass
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


MPV_HW = """# NickLabs Orbis Graphics Engine: hardware H.264 through UVD (orbis-video test PASSED {when}).
# Only H.264 is decoded in hardware on Liverpool; everything else falls back to the CPU.
hwdec=vaapi-copy
hwdec-codecs=h264
vo=gpu
# linux-psx observed decoder-initialisation failures with multi-threaded hardware setup
vd-lavc-threads=1
"""
MPV_SW = """# NickLabs Orbis Graphics Engine: software decode (no qualified UVD on this kernel; {when}).
hwdec=no
vo=gpu
# all eight Jaguar cores for the software decoder
vd-lavc-threads=8
framedrop=vo
"""


def configure(dry_run: bool = False) -> dict:
    st = status()
    last = st.get("last_test") or {}
    hw_ok = last.get("state") == "PASS" and st["kernel"]["uvd_ring"]
    text = (MPV_HW if hw_ok else MPV_SW).format(when=now_iso())
    f = p(MPV_CONF)
    prev = read(f)
    act = {"file": str(f), "mode": "hardware" if hw_ok else "software", "dry_run": dry_run}
    if not dry_run:
        if prev is not None and "NickLabs Orbis Graphics Engine" not in prev:
            atomic_write(f.with_name("mpv.conf.orbis-prev"), prev + "\n")
        atomic_write(f, text)
    return act
