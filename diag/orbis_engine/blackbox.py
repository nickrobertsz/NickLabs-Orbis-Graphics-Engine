"""NickLabs Orbis Black Box: persistent evidence that survives hangs, panics and power-offs.

Design (no graceful shutdown is assumed):
  * boot record   written as soon as the root filesystem is writable: boot id, kernel, cmdline,
                  loader memory contract, versions, early kernel log, and the verdict on the
                  previous boot (a boot without shutdown.json ended uncleanly).
  * flight log    one compact telemetry sample every few seconds, fsync'd: after a hard hang the
                  last line says what the GPU, clocks, temperatures and memory looked like.
  * event log     /dev/kmsg is followed continuously; GPU/display/memory/lockup messages are
                  written and fsync'd immediately, together with a GPU state capture (debugfs
                  fences, rings, VM info, bridge status) when a fault or reset appears.
  * devcoredumps  amdgpu's reset dumps (/sys/class/devcoredump) are copied before they expire.
  * streaming     optional: the same events/samples as UDP JSON to a NickLabs LAN machine, and
                  kernel netconsole (panic messages included) — see `orbis-debug stream`.
  * kdump         optional, experimental: a crash-capture kernel saves the dmesg of a panic.

Everything lives under /var/lib/orbis-blackbox (configurable); old boots are pruned by count.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

from orbis_gfx import sysfs

from . import __version__, telemetry
from .util import append_jsonl, atomic_write, cmdline_params, now_iso, p, read

BASE = "var/lib/orbis-blackbox"
KEEP_BOOTS = 30
FLIGHT_MAX_LINES = 4000
EVENT_PATTERNS = [
    ("gpu_fault", re.compile(r"amdgpu.*(VM_CONTEXT1_PROTECTION_FAULT|GPU fault detected|page fault|\[gfxhub\]|PROTECTION_FAULT)", re.I)),
    ("ring_timeout", re.compile(r"(ring \S+ timeout|amdgpu_job_timedout|job timed ?out)", re.I)),
    ("gpu_reset", re.compile(r"amdgpu.*(GPU reset|gpu_recover|GPU recovery|reset (succeeded|failed)|ASIC reset)", re.I)),
    ("ih_overflow", re.compile(r"IH ring buffer overflow", re.I)),
    ("firmware", re.compile(r"(failed to load firmware|Direct firmware load for .* failed)", re.I)),
    ("display", re.compile(r"(ps4[_-]bridge|link training failed|clock recovery|drm.*\*ERROR\*)", re.I)),
    ("video", re.compile(r"(uvd|vce).*(fail|timeout|error|not responding)", re.I)),
    ("clock", re.compile(r"Liverpool CLK", re.I)),
    ("lockup", re.compile(r"(soft lockup|hard LOCKUP|hung_task|blocked for more than|workqueue lockup|rcu.*stall)", re.I)),
    ("oom", re.compile(r"(Out of memory|oom-kill|invoked oom-killer)", re.I)),
    ("kernel_bug", re.compile(r"(BUG:|Oops|general protection fault|kernel panic|WARNING: CPU)", re.I)),
    ("thermal", re.compile(r"(ps4[-_]fan|thermal|overheat)", re.I)),
]
CAPTURE_ON = {"gpu_fault", "ring_timeout", "gpu_reset", "ih_overflow", "kernel_bug", "lockup"}
CAPTURE_MIN_GAP_S = 10.0      # a VM-fault storm prints hundreds of lines; one capture per window
CAPTURE_MAX_PER_BOOT = 50
DEBUGFS_CAPTURE = ["amdgpu_fence_info", "amdgpu_vm_info", "amdgpu_sa_info", "amdgpu_firmware_info",
                   "amdgpu_liverpool_clk", "ps4_bridge_status", "amdgpu_gem_info"]


def base() -> Path:
    return Path(os.environ.get("ORBIS_BLACKBOX_DIR") or p(BASE))


def boot_id() -> str:
    return read(p("proc/sys/kernel/random/boot_id")) or uuid.uuid4().hex


def config() -> dict:
    try:
        return json.loads(p("etc/orbis/blackbox.json").read_text())
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ boots
def list_boots() -> list[dict]:
    d = base() / "boots"
    out = []
    for b in sorted(d.iterdir()) if d.is_dir() else []:
        if not b.is_dir():
            continue
        info = {"dir": b.name}
        try:
            bj = json.loads((b / "boot.json").read_text())
            info.update({k: bj.get(k) for k in ("boot_id", "time", "kernel", "seq")})
        except (OSError, ValueError):
            pass
        sj = b / "shutdown.json"
        info["clean_shutdown"] = sj.exists()
        out.append(info)
    return out


def current_dir() -> Path | None:
    bid = boot_id()
    d = base() / "boots"
    for b in sorted(d.glob(f"*-{bid[:8]}")) if d.is_dir() else []:
        return b
    return None


def _time_source() -> dict | None:
    try:
        v = json.loads(p("run/orbis/time-source.json").read_text())
    except (OSError, ValueError):
        return None
    return {k: v.get(k) for k in ("source", "valid", "steps") if k in v}


def start_boot() -> Path:
    """Create this boot's record (idempotent) and judge the previous boot."""
    existing = current_dir()
    if existing:
        return existing
    boots = list_boots()
    seq = (max((b.get("seq") or 0) for b in boots) + 1) if boots else 1
    bid = boot_id()
    d = base() / "boots" / f"{seq:05d}-{bid[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    prev = boots[-1] if boots else None
    verdict = None
    if prev:
        verdict = {"dir": prev["dir"], "clean_shutdown": prev["clean_shutdown"]}
        if not prev["clean_shutdown"]:
            verdict["summary"] = summarize_boot(base() / "boots" / prev["dir"])
    cl = cmdline_params()
    rec = {"schema": "orbis-blackbox-boot/1", "seq": seq, "boot_id": bid, "time": now_iso(),
           "blackbox": __version__, "kernel": read(p("proc/sys/kernel/osrelease")), "cmdline": sysfs.cmdline(),
           "loader": {"token": cl.get("orbis.loader"), "vram_mb": cl.get("orbis.vram_mb"), "gtt_mb": cl.get("orbis.gtt_mb")},
           "versions": telemetry.versions(), "previous_boot": verdict,
           # where this boot's wall clock came from (orbis-time); a 1970 "time" above means no source
           "clock": {"loader_time": cl.get("orbis.time") or cl.get("time"), "source": _time_source()}}
    try:
        g = sysfs.primary_gpu()
        rec["gpu"] = {"id": f"{g.vendor}:{g.device}", "driver": g.driver, "firmware": sysfs.firmware_info(g)} if g else None
    except OSError:
        rec["gpu"] = None
    atomic_write(d / "boot.json", json.dumps(rec, indent=1, default=str) + "\n")
    try:
        atomic_write(d / "dmesg-boot.txt", sysfs.kernel_log())
    except OSError:
        pass
    prune()
    return d


def prune(keep: int = KEEP_BOOTS) -> None:
    d = base() / "boots"
    boots = sorted(x for x in d.iterdir() if x.is_dir()) if d.is_dir() else []
    for b in boots[:-keep] if len(boots) > keep else []:
        shutil.rmtree(b, ignore_errors=True)


def end_boot(reason: str = "orderly shutdown") -> None:
    d = current_dir()
    if d:
        atomic_write(d / "shutdown.json", json.dumps({"time": now_iso(), "reason": reason}) + "\n")


# ------------------------------------------------------------------ analysis
def _tail_jsonl(path: Path, n: int) -> list[dict]:
    out = []
    try:
        lines = path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"corrupt": line[:200]})
    return out


def summarize_boot(d: Path) -> dict:
    events = _tail_jsonl(d / "events.jsonl", 400)
    flight = _tail_jsonl(d / "flight.jsonl", 3)
    kinds = {}
    for e in events:
        kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
    last_sample = flight[-1] if flight else None
    first_bad = next((e for e in events if e.get("kind") in CAPTURE_ON), None)
    out = {"dir": d.name, "clean_shutdown": (d / "shutdown.json").exists(),
           "event_counts": kinds, "first_serious_event": first_bad, "last_events": events[-15:],
           "last_flight_sample": last_sample,
           "devcoredumps": sorted(x.name for x in (d / "devcoredump").glob("*")) if (d / "devcoredump").is_dir() else [],
           "gpu_captures": sorted(x.name for x in d.glob("gpu-state-*.txt"))}
    if not out["clean_shutdown"]:
        if first_bad:
            out["verdict"] = f"ended without shutdown after a {first_bad.get('kind')} event: {first_bad.get('msg', '')[:160]}"
        elif last_sample:
            out["verdict"] = ("ended without shutdown and without a recorded kernel error; last telemetry at "
                              f"{last_sample.get('t')} — hard hang, power loss or a crash before the log reached disk "
                              "(netconsole/kdump capture panics)")
        else:
            out["verdict"] = "ended before the Black Box started recording (early boot problem: see netconsole/stage-0)"
    else:
        out["verdict"] = "clean shutdown"
    return out


def last_crash() -> dict:
    boots = list_boots()
    cur = current_dir()
    for b in reversed(boots):
        if cur and b["dir"] == cur.name:
            continue
        if not b["clean_shutdown"]:
            return {"found": True, **summarize_boot(base() / "boots" / b["dir"])}
    return {"found": False, "why": "every recorded earlier boot shut down cleanly", "boots_recorded": len(boots)}


# ------------------------------------------------------------------ live recording
class Streamer:
    """Optional UDP JSON mirror to a NickLabs LAN machine (orbis-blackbox-receiver)."""

    def __init__(self, url: str | None):
        self.addr = None
        self.sock = None
        if url:
            m = re.match(r"^udp://([^:/]+):(\d+)$", url)
            if m:
                self.addr = (m.group(1), int(m.group(2)))
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, kind: str, payload) -> None:
        if not self.sock:
            return
        try:
            msg = json.dumps({"k": kind, "t": time.time(), "boot": boot_id()[:8], "p": payload}, default=str)
            self.sock.sendto(msg.encode()[:60000], self.addr)
        except OSError:
            pass


def flight_sample() -> dict:
    g = telemetry.gpu(include_busy=True)
    th = telemetry.thermal()
    mem = telemetry.memory()
    busy = g.get("engine_busy_percent") or {}
    return {"t": now_iso(), "gpu_busy": {k: busy.get(k) for k in ("gfx", "shader", "sdma0", "uvd") if k in busy},
            "sclk": (g.get("sclk") or {}).get("state"), "apu_c": th.get("apu_temp_c"),
            "fan_thr_c": th.get("fan_threshold_c"),
            "cpu_c": next(iter((th.get("cpu_k10temp") or {}).values()), None),
            "vram_used": (g.get("memory") or {}).get("mem_info_vram_used"),
            "gtt_used": (g.get("memory") or {}).get("mem_info_gtt_used"),
            "gtt_budget": ((g.get("memory") or {}).get("gtt_budget") or {}).get("budget"),
            "mem_avail": mem.get("linux_ram_available"),
            "psi_mem10": (((mem.get("psi_memory") or {}).get("some")) or {}).get("avg10")}


def capture_gpu_state(d: Path, why: str) -> Path | None:
    g = sysfs.primary_gpu()
    if not g or not g.debugfs:
        return None
    parts = [f"# Orbis Black Box GPU capture {now_iso()} — {why}"]
    for name in DEBUGFS_CAPTURE:
        txt = read(g.debugfs / name)
        if txt is not None:
            parts.append(f"## {name}\n{txt[:65536]}")
    busy = read(g.sysdir / "orbis_engine_busy")
    if busy:
        parts.append(f"## orbis_engine_busy\n{busy}")
    out = d / f"gpu-state-{int(time.time())}.txt"
    atomic_write(out, "\n\n".join(parts) + "\n")
    return out


def harvest_devcoredumps(d: Path) -> list[str]:
    src = p("sys/class/devcoredump")
    got = []
    for dc in sorted(src.glob("devcd*")) if src.is_dir() else []:
        data = dc / "data"
        try:
            blob = data.read_bytes()
        except OSError:
            continue
        dst = d / "devcoredump" / f"{int(time.time())}-{dc.name}.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dst, blob)
        try:
            with open(data, "w") as fh:   # writing frees the dump in the kernel
                fh.write("1")
        except OSError:
            pass
        got.append(dst.name)
    return got


def classify(msg: str) -> str | None:
    for kind, rx in EVENT_PATTERNS:
        if rx.search(msg):
            return kind
    return None


def _parse_kmsg(rec: str) -> tuple[int, str]:
    head, sep, msg = rec.partition(";")
    if not sep:
        return 6, rec.rstrip("\n")
    try:
        prio = int(head.split(",")[0]) & 7
    except ValueError:
        prio = 6
    return prio, msg.rstrip("\n").split("\n")[0]


def _kmsg_records(fd: int, chardev: bool = True) -> list[tuple[int, str]]:
    """/dev/kmsg returns one record per read(); a regular file (tests, replays) is line based."""
    out = []
    if not chardev:
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return [_parse_kmsg(l) for l in b"".join(chunks).decode("utf-8", "replace").splitlines() if l.strip()]
    for _ in range(4096):
        try:
            rec = os.read(fd, 8192)
        except BlockingIOError:
            break
        except OSError:      # EPIPE: older messages were overwritten; keep reading
            continue
        if not rec:
            break
        out.append(_parse_kmsg(rec.decode("utf-8", "replace")))
    return out


def run(interval: float = 5.0, once: bool = False, kmsg_path: str | None = None) -> int:
    """The orbis-blackbox daemon loop."""
    d = start_boot()
    cfg = config()
    stream = Streamer(cfg.get("stream"))
    stream.send("boot", json.loads((d / "boot.json").read_text()))
    src = kmsg_path or os.environ.get("ORBIS_BLACKBOX_KMSG", "/dev/kmsg")
    fd = None
    chardev = True
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NONBLOCK)
        import stat as _stat
        chardev = _stat.S_ISCHR(os.fstat(fd).st_mode)
        if chardev:
            os.lseek(fd, 0, os.SEEK_END)   # the boot log is already in dmesg-boot.txt
    except OSError:
        fd = None
    kmsg_file = d / "kmsg-warn.log"
    last_capture = -CAPTURE_MIN_GAP_S
    captures = len(list(d.glob("gpu-state-*.txt")))
    next_sample = 0.0
    lines = 0
    while True:
        now = time.monotonic()
        if fd is not None:
            recs = _kmsg_records(fd, chardev)
            if recs:
                with open(kmsg_file, "a", encoding="utf-8") as kf:
                    for prio, msg in recs:
                        kind = classify(msg)
                        if prio <= 4 or kind:
                            kf.write(f"{now_iso()} <{prio}> {msg}\n")
                        if kind:
                            ev = {"t": now_iso(), "kind": kind, "prio": prio, "msg": msg[:500]}
                            if (kind in CAPTURE_ON and captures < CAPTURE_MAX_PER_BOOT
                                    and time.monotonic() - last_capture >= CAPTURE_MIN_GAP_S):
                                cap = capture_gpu_state(d, f"{kind}: {msg[:120]}")
                                ev["capture"] = cap.name if cap else None
                                last_capture = time.monotonic()
                                captures += 1
                            append_jsonl(d / "events.jsonl", ev)
                            stream.send("event", ev)
                    kf.flush()
                    os.fsync(kf.fileno())
            dumps = harvest_devcoredumps(d)
            for name in dumps:
                ev = {"t": now_iso(), "kind": "devcoredump", "file": name}
                append_jsonl(d / "events.jsonl", ev)
                stream.send("event", ev)
        if now >= next_sample:
            try:
                s = flight_sample()
            except Exception as e:  # never die on a telemetry glitch
                s = {"t": now_iso(), "error": f"{type(e).__name__}: {e}"}
            append_jsonl(d / "flight.jsonl", s)
            stream.send("flight", s)
            lines += 1
            if lines % 500 == 0:
                _trim(d / "flight.jsonl", FLIGHT_MAX_LINES)
            next_sample = now + interval
        if once:
            break
        if fd is not None and chardev:
            select.select([fd], [], [], min(1.0, interval))
        else:
            time.sleep(min(1.0, interval))
    if fd is not None:
        os.close(fd)
    return 0


def _trim(path: Path, keep: int) -> None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    if len(lines) > keep:
        atomic_write(path, "\n".join(lines[-keep // 2:]) + "\n")


# ------------------------------------------------------------------ netconsole (kernel log over LAN)
def _arp_mac(ip: str) -> str | None:
    for line in (read(p("proc/net/arp")) or "").splitlines()[1:]:
        f = line.split()
        if len(f) >= 4 and f[0] == ip and f[3] != "00:00:00:00:00:00":
            return f[3]
    return None


def _route_dev(ip: str) -> tuple[str | None, str | None]:
    """(device, local source ip) the kernel would use to reach ip."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((ip, 9))
        local = s.getsockname()[0]
        s.close()
    except OSError:
        return None, None
    dev = None
    try:
        r = subprocess.run(["ip", "-o", "route", "get", ip], capture_output=True, text=True, timeout=5)
        m = re.search(r"\bdev (\S+)", r.stdout)
        dev = m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        pass
    if not dev:
        base = p("sys/class/net")
        for n in sorted(base.iterdir()) if base.is_dir() else []:
            if n.name != "lo" and read(n / "operstate") == "up":
                dev = n.name
                break
    return dev, local


def netconsole_enable(target_ip: str, port: int = 6666, dry_run: bool = False) -> dict:
    """Runtime netconsole via configfs (CONFIG_NETCONSOLE_DYNAMIC): panics and oopses reach the LAN machine."""
    dev, local = _route_dev(target_ip)
    try:   # one packet so the kernel resolves the target's MAC (netconsole needs it)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"orbis", (target_ip, port))
    except OSError:
        pass
    time.sleep(0.3)
    mac = _arp_mac(target_ip) or "ff:ff:ff:ff:ff:ff"
    t = p("sys/kernel/config/netconsole/orbis")
    plan = {"dev_name": dev, "local_ip": local, "remote_ip": target_ip, "remote_port": str(port), "remote_mac": mac}
    if dry_run or not dev or not local:
        return {"applied": False, "plan": plan, "why": None if dev and local else "no route/interface to target"}
    if not p("sys/kernel/config/netconsole").is_dir():
        return {"applied": False, "plan": plan, "why": "configfs netconsole missing (needs CONFIG_NETCONSOLE_DYNAMIC and configfs mounted)"}
    t.mkdir(exist_ok=True)
    try:
        (t / "enabled").write_text("0")
    except OSError:
        pass
    for k, v in plan.items():
        (t / k).write_text(v)
    if (t / "extended").exists():
        (t / "extended").write_text("1")
    (t / "enabled").write_text("1")
    return {"applied": True, "plan": plan}


def netconsole_bootarg(target_ip: str, local_ip: str, port: int = 6666, dev: str = "eth0") -> str:
    """Kernel command-line form (active from early boot; needs a static local address)."""
    return f"netconsole=6665@{local_ip}/{dev},{port}@{target_ip}/"
