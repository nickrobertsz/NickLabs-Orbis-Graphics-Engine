"""Unified CPU/GPU memory for the PS4's single 8 GiB GDDR5 pool.

What the hardware and kernel allow (and therefore what this module does):

* Boot time: the loader carves a fixed VRAM range out of the pool (vram.txt; E820 + GPU
  framebuffer window + MTRRs). It cannot move while Linux runs. The Graphics Engine loader
  accepts any 16 MiB multiple that fits five MTRRs (e.g. 384, 768, 1536, 3072); the stock
  loader only programs power-of-two sizes correctly.
* Runtime: everything else is ordinary Linux RAM. GPU buffers outside the carve-out are GTT
  buffers (system pages mapped through GART/GPUVM; Liverpool is an APU, so Mesa treats it as
  unified memory). The GE kernel makes amdgpu's GTT budget writable at runtime
  (orbis_gtt_budget) and TTM's global page limit is a runtime parameter, so the CPU/GPU split
  of the non-carve-out pool can follow the PC/GAMING/AI mode and memory pressure.
* Ownership: per-process GPU memory comes from DRM fdinfo (drm-memory-vram/gtt/cpu),
  CPU memory from /proc. The same page is never owned by two consumers; TTM evicts GTT
  buffers to system pages when the budget shrinks.
* Not possible on GFX7: recoverable GPU page faults (HMM/SVM, CUDA-style on-demand
  migration). Migration therefore stays explicit (TTM placement/eviction), see
  docs/GRAPHICS-ENGINE-MEMORY.md.
"""
from __future__ import annotations

import errno
import json
import os
import re
import time
from pathlib import Path

from orbis_gfx import sysfs

from . import telemetry
from .util import append_jsonl, atomic_write, cmdline_params, kv, now_iso, p, read, read_int, write_sysfs

MiB = 1 << 20
GiB = 1 << 30
PAGE = 4096

# Mode policy for the runtime GTT budget: fraction of Linux RAM GPU buffers may use, and the
# headroom Linux always keeps. These are policy starting points, recorded in every decision.
MODE_POLICY = {
    "pc": {"gtt_fraction": 0.40, "linux_headroom": int(1.5 * GiB), "vram_mb": 512},
    "gaming": {"gtt_fraction": 0.60, "linux_headroom": 1 * GiB, "vram_mb": 1024},
    "ai": {"gtt_fraction": 0.80, "linux_headroom": int(0.75 * GiB), "vram_mb": 1024},
}
GTT_FLOOR = 512 * MiB
PSI_HIGH = 10.0      # memory "some" avg10 (%): Linux is stalling on memory -> shrink GPU budget
PSI_LOW = 1.0        # below this for RELAX_S -> move back toward the mode target
RELAX_S = 60
STEP_FRACTION = 0.10
STATE_DIR = "var/lib/orbis-graphics/memory"
LOG = "var/log/orbis/memory-policy.jsonl"


# ------------------------------------------------------------------ ownership (DRM fdinfo)
_UNIT = {"": 1, "B": 1, "KiB": 1024, "MiB": MiB, "GiB": GiB, "kB": 1024}


def _bytes(v: str) -> int:
    m = re.match(r"\s*(\d+)\s*([KMG]i?B|kB|B)?", v)
    if not m:
        return 0
    return int(m.group(1)) * _UNIT.get(m.group(2) or "", 1)


def gpu_clients() -> list[dict]:
    """GPU memory per DRM client from fdinfo (amdgpu), GE-A07.

    One entry per DRM client, identified by (drm-pdev, drm-client-id): a client is one opened
    DRM file, and the same file is visible in every process that inherited or received it, so
    keying by PID would count it once per process. Each entry lists the PIDs (and names) that
    hold the client; "pid" is the lowest of them."""
    out = {}
    proc = p("proc")
    for d in proc.glob("[0-9]*") if proc.is_dir() else []:
        fdinfo = d / "fdinfo"
        try:
            fds = list(fdinfo.iterdir())
        except OSError:
            continue
        for f in fds:
            txt = read(f)
            if not txt or "drm-driver:" not in txt:
                continue
            info = {}
            for line in txt.splitlines():
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
            if info.get("drm-driver") != "amdgpu":
                continue
            cid = info.get("drm-client-id")
            # without a client id (very old kernels) every fd is its own client
            key = (info.get("drm-pdev", ""), cid) if cid else ("fd", d.name, f.name)
            rec = {"pid": int(d.name), "comm": read(d / "comm"), "client_id": cid, "pdev": info.get("drm-pdev"),
                   "vram": _bytes(info.get("drm-memory-vram", "0")), "gtt": _bytes(info.get("drm-memory-gtt", "0")),
                   "cpu": _bytes(info.get("drm-memory-cpu", "0")),
                   "gfx_busy_ns": int(info.get("drm-engine-gfx", "0 ns").split()[0] or 0)}
            for k in ("drm-total-vram", "drm-resident-vram", "drm-total-gtt", "drm-resident-gtt",
                      "drm-shared-vram", "drm-shared-gtt", "drm-total-cpu", "drm-shared-cpu",
                      "drm-purgeable-gtt", "drm-purgeable-cpu"):
                if k in info:
                    rec[k.replace("drm-", "").replace("-", "_")] = _bytes(info[k])
            # private = buffers only this client holds; shared ones also appear in other clients
            for r in ("vram", "gtt", "cpu"):
                tot = rec.get(f"total_{r}")
                if tot is not None:
                    rec[f"private_{r}"] = max(0, tot - (rec.get(f"shared_{r}") or 0))
            status = read(d / "status") or ""
            m = re.search(r"VmRSS:\s+(\d+) kB", status)
            rec["rss"] = int(m.group(1)) * 1024 if m else None
            prev = out.get(key)
            if prev:   # the same client seen through another fd or another process
                prev["_pids"].add(int(d.name))
                if rec["comm"]:
                    prev["_comms"].add(rec["comm"])
                for k, v in rec.items():
                    if k not in ("pid", "rss") and isinstance(v, int) and v > (prev.get(k) or 0):
                        prev[k] = v
                if rec.get("rss") is not None:
                    prev["_rss"][int(d.name)] = rec["rss"]
                continue
            rec["_pids"] = {int(d.name)}
            rec["_comms"] = {rec["comm"]} if rec["comm"] else set()
            rec["_rss"] = {int(d.name): rec["rss"]} if rec.get("rss") is not None else {}
            out[key] = rec
    clients = []
    for rec in out.values():
        pids = sorted(rec.pop("_pids"))
        rec["pids"] = pids
        rec["pid"] = pids[0]
        rec["comms"] = sorted(rec.pop("_comms"))
        # process RSS is physical RAM of the holding processes, not GPU memory: kept apart
        rec["rss_by_pid"] = {str(k): v for k, v in sorted(rec.pop("_rss").items())}
        rec["rss"] = rec["rss_by_pid"].get(str(pids[0]))
        clients.append(rec)
    return sorted(clients, key=lambda r: -(r["vram"] + r["gtt"]))


def client_totals(clients: list[dict]) -> dict:
    """Per-client sums without double counting: private buffers are summed exactly; shared
    buffers appear in every client that holds them, so only a range is known (largest single
    client .. sum). The device counters (gpu.vram_used / gpu.gtt_used) remain the truth."""
    out = {}
    for r in ("vram", "gtt", "cpu"):
        split = [c for c in clients if c.get(f"private_{r}") is not None]
        legacy = [c for c in clients if c.get(f"private_{r}") is None]
        shared = [c.get(f"shared_{r}") or 0 for c in split]
        out[r] = {"private_sum": sum(c[f"private_{r}"] for c in split),
                  "shared_min": max(shared, default=0), "shared_max": sum(shared),
                  # clients whose fdinfo has no drm-total/drm-shared split (older format):
                  # their value may include buffers shared with others
                  "unsplit_sum": sum(c.get(r) or 0 for c in legacy)}
    return out


def accounting_check(totals: dict, device: dict) -> dict:
    """Compare per-client sums with the device counters (GE-A07). The counters include kernel
    buffers (page tables, rings, firmware) that no client owns, so for each region:
        private_sum + shared_min <= device_used        must hold
        device_used - (private_sum + shared_max)       is the least memory nobody accounts for
    A violation means the fdinfo totals are inconsistent with the device (or were read at
    different moments under load); it is reported, never corrected silently."""
    out = {}
    for r, dev_key in (("vram", "vram_used"), ("gtt", "gtt_used")):
        t = totals.get(r) or {}
        used = device.get(dev_key)
        if used is None:
            out[r] = {"checked": False, "why": f"device counter {dev_key} not available"}
            continue
        low = (t.get("private_sum") or 0) + (t.get("shared_min") or 0)
        high = (t.get("private_sum") or 0) + (t.get("shared_max") or 0) + (t.get("unsplit_sum") or 0)
        ok = low <= used
        out[r] = {"checked": True, "device_used": used, "clients_min": low, "clients_max": high,
                  "unattributed_min": max(0, used - high), "unattributed_max": max(0, used - low),
                  "consistent": ok}
        if not ok:
            out[r]["violation"] = f"clients hold at least {low} bytes but the device reports {used}"
    out["consistent"] = all(v.get("consistent", True) for v in out.values() if isinstance(v, dict))
    return out


def reclaim_status(g) -> dict:
    """ge-0014 shrinker counters (pages -> bytes) and what really happened to the pages:
    backed-up GPU pages live in shmem; they only leave RAM when the core VM swaps them (zram)."""
    raw = read(g.sysdir / "orbis_gtt_reclaim") if g else None
    if raw is None:
        return {"available": False, "why": "kernel without ge-0014 (orbis_gtt_reclaim)"}
    d = kv(raw)
    if d.get("enabled") != 1:
        return {"available": True, "enabled": False, "why": d.get("reason"),
                "enable": "add amdgpu.orbis_shrinker=1 to bootargs.txt (orbis-mem reclaim enable) and reboot"}
    ps = d.get("page_size") or PAGE
    b = {k: (d.get(k) or 0) * ps for k in ("shrinkable_pages", "purgeable_pages", "backed_up_pages",
                                            "shrunk_total", "purged_total", "restored_total")}
    return {"available": True, "enabled": True,
            "shrinkable_bytes": b["shrinkable_pages"], "purgeable_bytes": b["purgeable_pages"],
            "backed_up_bytes": b["backed_up_pages"], "shrunk_total_bytes": b["shrunk_total"],
            "purged_total_bytes": b["purged_total"], "restored_total_bytes": b["restored_total"],
            "scans": d.get("scans"), "trim_evictions": d.get("trim_evictions"),
            "note": "backed_up_bytes are in shmem (still RAM until swapped); see swap/zram for what was actually compressed"}


def compressed_status() -> dict:
    """Swap and zram as the kernel reports them (actual compression, not estimates)."""
    mi = telemetry.meminfo_full()
    out = {"swap_total": mi.get("SwapTotal"), "swap_used": (mi.get("SwapTotal") or 0) - (mi.get("SwapFree") or 0),
           "shmem": mi.get("Shmem"), "zram": []}
    for z in sorted(p("sys/block").glob("zram*")) if p("sys/block").is_dir() else []:
        f = read(z / "mm_stat")
        if not f:
            continue
        v = [int(x) for x in f.split()[:3] if x.isdigit()]
        if len(v) == 3:
            out["zram"].append({"dev": z.name, "orig_data_bytes": v[0], "compressed_bytes": v[1],
                                "mem_used_bytes": v[2], "ratio": round(v[0] / v[1], 2) if v[1] else None,
                                "algorithm": (read(z / "comp_algorithm") or "").split("[")[-1].split("]")[0] or None})
    return out


def gtt_cgroup_status() -> dict:
    """The GE kernel's own account of the 'gtt' dmem region (ge-0016 orbis_gtt_cgroup)."""
    g = sysfs.primary_gpu()
    raw = read(g.sysdir / "orbis_gtt_cgroup") if g else None
    if raw is None:
        return {"state": "unknown", "reason": "kernel without ge-0016 (no orbis_gtt_cgroup)"}
    state, _, rest = raw.strip().partition(" ")
    return {"state": state, **kv(rest)}


def dmem_regions() -> dict:
    """cgroup v2 device-memory regions (amdgpu 'vram'; 'gtt' with ge-0015).

    gtt_limits is True only when the region exists; otherwise it carries the reason, so a
    profile that requires GTT enforcement can report "unavailable" instead of running without it."""
    raw = read(p("sys/fs/cgroup/dmem.capacity"))
    if raw is None:
        return {"available": False, "why": "kernel without CONFIG_CGROUP_DMEM", "gtt_limits": False,
                "gtt_unavailable_reason": "kernel without CONFIG_CGROUP_DMEM"}
    regs = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("drm/"):
            regs[parts[0].rsplit("/", 1)[-1]] = {"region": parts[0], "capacity": int(parts[1]) if parts[1].isdigit() else parts[1]}
    out = {"available": True, "regions": regs, "gtt_limits": "gtt" in regs, "gtt_status": gtt_cgroup_status()}
    if "gtt" not in regs:
        st = out["gtt_status"]
        out["gtt_unavailable_reason"] = st.get("reason") if st.get("state") != "registered" else \
            "kernel registered the region but dmem.capacity does not list it"
    return out


def gtt_budget_status(text: str | None) -> dict:
    """Parse orbis_gtt_budget: the budget line, plus the 'last_trim ...' line of ge-0016 kernels
    (kept apart: both lines carry 'requested')."""
    out: dict = {}
    for line in (text or "").splitlines():
        if line.startswith("last_trim"):
            out["last_trim"] = kv(line.partition(" ")[2])
        else:
            out.update(kv(line))
    return out


def top_owners(clients: list[dict], n: int = 3) -> list[dict]:
    key = lambda c: c.get("private_gtt") if c.get("private_gtt") is not None else c.get("gtt") or 0
    return [{"pid": c["pid"], "pids": c.get("pids", [c["pid"]]), "comm": c.get("comm"), "client_id": c.get("client_id"),
             "private_gtt": c.get("private_gtt"), "gtt": c.get("gtt"),
             "vram": c.get("vram")} for c in sorted(clients, key=key, reverse=True)[:n] if key(c)]


def ownership() -> dict:
    """Who owns the pool right now: carve-out, GPU buffers in it and in GTT, Linux, per process."""
    mem = telemetry.memory()
    g = sysfs.primary_gpu()
    gm = sysfs.memory(g) if g else {}
    budget = gtt_budget_status(read(g.sysdir / "orbis_gtt_budget")) if g else {}
    clients = gpu_clients()
    mi = telemetry.meminfo_full()
    total_ram = mi.get("MemTotal") or 0
    avail = mi.get("MemAvailable") or 0
    gtt_used = gm.get("mem_info_gtt_used") or 0
    return {
        "schema": "orbis-memory-ownership/1", "time": now_iso(),
        "pool": {"vram_carveout": gm.get("mem_info_vram_total"), "linux_ram": total_ram,
                 "physical_estimate": total_ram + (gm.get("mem_info_vram_total") or 0),
                 "physical_note": "Linux RAM + carve-out; firmware/OrbisOS reservations of the 8 GiB are not visible to Linux"},
        "gpu": {"vram_used": gm.get("mem_info_vram_used"), "vis_vram_used": gm.get("mem_info_vis_vram_used"),
                # x86-64 APU: amdgpu maps the whole carve-out for the CPU (no small-BAR window)
                "cpu_visible_vram": gm.get("mem_info_vis_vram_total"),
                "gtt_used": gtt_used, "gtt_total": gm.get("mem_info_gtt_total"),
                "gtt_budget": budget.get("budget"), "ttm_pages_limit_bytes": (mem.get("ttm_pages_limit") or 0) * PAGE or None},
        "linux": {"available": avail, "in_use_excluding_gtt": max(0, total_ram - avail - gtt_used),
                  "psi_memory": mem.get("psi_memory")},
        "clients": clients,
        "client_totals": (ct := client_totals(clients)),
        "accounting_check": accounting_check(ct, {"vram_used": gm.get("mem_info_vram_used"), "gtt_used": gtt_used if g else None}),
        "top_gtt_owners": top_owners(clients),
        "reclaim": reclaim_status(g),
        "compressed": compressed_status(),
        "dmem": dmem_regions(),
        "boot_contract": mem.get("boot_contract"),
        "note": ("GTT pages are counted once (in gpu.gtt_used); they are ordinary RAM pages mapped for the GPU. "
                 "Vulkan heaps and the GTT budget are limits over the same pool, never added to it. "
                 "GPU accounting (clients, client_totals: buffers by DRM client) and physical RAM "
                 "(linux.*, clients[].rss_by_pid: process memory) are different measures and are never summed."),
    }


# ------------------------------------------------------------------ boot-time carve-out plan
def ge_loader() -> bool:
    return cmdline_params().get("orbis.loader") == "ge2"


def vram_valid(mb: int, ge: bool | None = None) -> list[str]:
    """Problems with a carve-out size (empty = OK) for the loader in use."""
    ge = ge_loader() if ge is None else ge
    probs = []
    if ge:
        if not (64 <= mb <= 4096) or mb % 16:
            probs.append(f"{mb} MiB: the Graphics Engine loader needs a multiple of 16 MiB in 64..4096")
        elif bin(mb).count("1") > 5:
            probs.append(f"{mb} MiB needs more than five MTRR blocks")
    else:
        if mb not in (32, 64, 128, 256, 512, 1024, 2048, 4096):
            probs.append(f"{mb} MiB: the stock loader programs the VRAM MTRR correctly only for powers of two "
                         "(3072 gives an invalid mask); use the Graphics Engine loader for other sizes")
    return probs


def plan(mode: str) -> dict:
    """Recommended boot carve-out + runtime GTT budget for a mode, from measured RAM."""
    pol = MODE_POLICY[mode]
    mi = telemetry.meminfo_full()
    ram = mi.get("MemTotal") or 0
    target = max(GTT_FLOOR, min(int(ram * pol["gtt_fraction"]), ram - pol["linux_headroom"]))
    limit = (read_int(p("sys/module/ttm/parameters/pages_limit")) or 0) * PAGE
    return {"mode": mode, "vram_mb": pol["vram_mb"], "vram_problems": vram_valid(pol["vram_mb"]),
            "gtt_budget_target": target, "ttm_pages_limit_needed": max(target, limit) // PAGE,
            "gtt_txt_mb": max(target, limit) // MiB, "policy": pol, "linux_ram": ram,
            "reason": f"{int(pol['gtt_fraction'] * 100)}% of Linux RAM for GPU buffers, at least "
                      f"{pol['linux_headroom'] // MiB} MiB always left to Linux"}


def boot_dir() -> Path:
    return Path(os.environ.get("ORBIS_BOOT_DIR") or p("boot/ps4"))


def write_boot_file(name: str, value: str, dry_run: bool = False) -> dict:
    """Write vram.txt / gtt.txt on the PS4 boot partition, keeping one .orbis-prev copy."""
    d = boot_dir()
    f = d / name
    if not d.is_dir():
        raise OSError(f"boot partition not mounted at {d}")
    prev = read(f)
    act = {"file": str(f), "old": prev, "new": value, "dry_run": dry_run}
    if not dry_run:
        if prev is not None:
            atomic_write(d / (name + ".orbis-prev"), prev + "\n")
        atomic_write(f, value + "\n")
        append_jsonl(p(LOG), {"time": now_iso(), "action": "boot-file", **act})
    return act


def apply_boot(mode: str | None = None, vram_mb: int | None = None, gtt_mb: int | None = None,
               dry_run: bool = False) -> dict:
    pl = plan(mode) if mode else {}
    vram = vram_mb if vram_mb is not None else pl.get("vram_mb")
    gtt = gtt_mb if gtt_mb is not None else pl.get("gtt_txt_mb")
    acts = []
    if vram is not None:
        probs = vram_valid(int(vram))
        if probs:
            raise ValueError("; ".join(probs))
        acts.append(write_boot_file("vram.txt", str(int(vram)), dry_run))
    if gtt is not None:
        if not ge_loader():
            acts.append({"file": "gtt.txt", "skipped": "the stock loader ignores gtt.txt; boot the Graphics Engine "
                                                         "loader (orbis-ge-*.elf) or add ttm.pages_limit to bootargs.txt"})
        else:
            acts.append(write_boot_file("gtt.txt", str(int(gtt)), dry_run))
    return {"plan": pl or None, "actions": acts, "reboot_required": any("new" in a for a in acts) and not dry_run}


# ------------------------------------------------------------------ runtime budget (orbis-memd)
def set_gtt_budget(nbytes: int, why: str, dry_run: bool = False) -> dict:
    g = sysfs.primary_gpu()
    f = g.sysdir / "orbis_gtt_budget" if g else None
    if not f or not f.exists():
        return {"applied": False, "why": "orbis_gtt_budget not available (kernel without ge-6.15/0008)"}
    before = gtt_budget_status(read(f))
    limit = before.get("ttm_pages_limit") or 0
    nbytes = max(GTT_FLOOR, int(nbytes))
    nbytes -= nbytes % PAGE
    rec = {"time": now_iso(), "action": "gtt-budget", "from": before.get("budget"), "to": nbytes,
           "usage": before.get("usage"), "why": why, "dry_run": dry_run}
    cap = before.get("capacity")   # ge-0017: the GTT manager's constant capacity bounds the budget
    if isinstance(cap, int) and cap and nbytes > cap:
        rec["clamped_to_capacity"] = cap
        nbytes = cap - cap % PAGE
        rec["to"] = nbytes
    if limit and nbytes > limit:
        # TTM's global page limit caps every GTT budget; it is a runtime parameter too
        rec["ttm"] = set_ttm_pages_limit(nbytes // PAGE, f"raise TTM limit for: {why}", dry_run)
    if dry_run:
        rec["applied"] = False
        return rec
    # ge-0016: a write whose active trim cannot bring usage under the budget fails with EBUSY
    # (EINTR when interrupted) while the budget itself stays applied to new allocations.
    # Report that as applied-but-not-enforced, never as success; other errors applied nothing.
    err = None
    if before.get("budget") != nbytes or before.get("state") not in (None, "enforced"):
        try:
            write_sysfs(f, str(nbytes))
        except OSError as e:
            err = e
    after = gtt_budget_status(read(f))
    # a kernel always echoes "budget=..."; without it (no readback) trust the write's own result
    rec["applied"] = after.get("budget") == nbytes if "budget" in after else err is None
    rec["usage_after"] = after.get("usage")
    rec["state"] = after.get("state") or ("enforced" if (after.get("usage") or 0) <= nbytes else "over_budget")
    rec["enforced"] = rec["state"] == "enforced"
    if after.get("last_trim"):
        rec["trim"] = after["last_trim"]
    if err is not None:
        rec["error"] = {"errno": err.errno, "name": errno.errorcode.get(err.errno or 0, str(err.errno)), "message": err.strerror}
    append_jsonl(p(LOG), rec)
    return rec


def budget_exit_code(rec: dict) -> int:
    """orbis-mem set-budget: 0 enforced, 1 budget not applied, 3 applied but usage still above it."""
    if not rec.get("applied"):
        return 1
    return 0 if rec.get("enforced", True) else 3


def set_ttm_pages_limit(pages: int, why: str, dry_run: bool = False) -> dict:
    f = p("sys/module/ttm/parameters/pages_limit")
    old = read_int(f)
    rec = {"time": now_iso(), "action": "ttm-pages-limit", "from": old, "to": int(pages), "why": why, "dry_run": dry_run}
    if not dry_run and old != int(pages):
        write_sysfs(f, str(int(pages)))
        append_jsonl(p(LOG), rec)
    return rec


def boot_default_budget() -> int | None:
    """The GTT budget amdgpu chose at this boot, before orbis-memd touched it (stored per boot id).
    When memory is calm the policy never goes below it, so every mode is at least as generous
    to the GPU as the stock kernel; only memory pressure shrinks below it."""
    f = p(STATE_DIR) / "boot-default.json"
    bid = read(p("proc/sys/kernel/random/boot_id"))
    try:
        st = json.loads(f.read_text())
        if st.get("boot_id") == bid and isinstance(st.get("budget"), int):
            return st["budget"]
    except (OSError, ValueError):
        pass
    g = sysfs.primary_gpu()
    cur = kv(read(g.sysdir / "orbis_gtt_budget")).get("budget") if g else None
    if isinstance(cur, int):
        try:
            atomic_write(f, json.dumps({"boot_id": bid, "budget": cur, "time": now_iso()}) + "\n")
        except OSError:
            pass
        return cur
    return None


class Policy:
    """Pressure-driven GTT budget for the active mode (one decision per tick)."""

    def __init__(self, mode: str, clock=time.monotonic, immediate: bool = False):
        """immediate=True (daemon start, mode switch): converge on the next calm tick instead of
        waiting RELAX_S, so entering AI mode grows the GPU budget straight away."""
        self.mode = mode if mode in MODE_POLICY else "pc"
        self.clock = clock
        self.calm_since: float | None = (clock() - RELAX_S) if immediate else None
        self.floor = boot_default_budget()

    def tick(self, dry_run: bool = False) -> dict:
        own = ownership()
        g = own["gpu"]
        cur = g.get("gtt_budget")
        target = max(plan(self.mode)["gtt_budget_target"], self.floor or 0)
        if cur is None:
            return {"decision": "none", "why": "no runtime GTT budget interface"}
        psi = ((own["linux"].get("psi_memory") or {}).get("some") or {}).get("avg10")
        used = g.get("gtt_used") or 0
        now = self.clock()
        if psi is not None and psi >= PSI_HIGH:
            self.calm_since = None
            new = max(GTT_FLOOR, int(cur * (1 - STEP_FRACTION)))
            # with ge-0014 a budget below usage trims at once and the shrinker can reclaim the
            # evicted buffers; without it, stay at usage (a lower budget would only fail allocations)
            if not own["reclaim"].get("enabled"):
                new = max(new, used)
            if new < cur:
                return {"decision": "shrink", "top_gtt_owners": own["top_gtt_owners"],
                        "reclaim": {k: own["reclaim"].get(k) for k in ("backed_up_bytes", "shrunk_total_bytes")},
                        **set_gtt_budget(new, f"memory pressure some.avg10={psi}%", dry_run)}
            return {"decision": "hold", "why": f"pressure {psi}% but budget already at floor/usage"}
        if psi is None or psi <= PSI_LOW:
            self.calm_since = self.calm_since if self.calm_since is not None else now
            if cur != target and now - self.calm_since >= RELAX_S:
                step = max(64 * MiB, int(abs(target - cur) * 0.5))
                new = min(target, cur + step) if cur < target else max(target, cur - step)
                return {"decision": "converge", **set_gtt_budget(new, f"{self.mode} target, memory calm", dry_run)}
            return {"decision": "hold", "why": "calm; waiting" if cur != target else "at target"}
        self.calm_since = None
        return {"decision": "hold", "why": f"moderate pressure {psi}%"}


# --membench result vocabulary and exit codes: identical to orbis-gfx-probe (mb_name/mb_exit),
# so the JSON, the probe's exit code and `orbis-mem bench`'s exit code always agree (GE-A08).
MEMBENCH_EXIT = {"PASS": 0, "FAIL": 1, "NOT_RUN": 2, "DEVICE_LOST": 3, "TIMEOUT": 3,
                 "UNSUPPORTED_EXTENSION": 4, "INCOMPATIBLE_MEMORY_TYPE": 4,
                 "ALLOCATION_FAILED": 5, "SYNC_FAILED": 5}


def bench_exit_code(out: dict) -> int:
    return MEMBENCH_EXIT.get(out.get("result"), 1)


def membench(size_mb: int = 128, timeout_s: float = 300.0, run=None) -> dict:
    """U27/U6 on the console: orbis-gfx-probe --membench (bandwidth per memory kind, copies and the
    end-to-end zero-copy check). Short and bounded; the result is stored for orbis-debug collect.

    "result" is the zero-copy verdict in the probe's vocabulary; NOT_RUN (probe or Vulkan
    missing) stays distinct from FAIL, and a probe whose exit code contradicts its own JSON is
    reported as FAIL with the contradiction."""
    from orbis_gfx import runner
    run = run or runner.run
    r = run([runner.probe_path(), "--membench", "--membench-mb", str(int(size_mb)), "--timeout-ms", "10000"], timeout=timeout_s)
    if r.missing:
        out = {"schema": "orbis-membench/2", "time": now_iso(), "result": "NOT_RUN", "zero_copy": "NOT_RUN",
               "why": "orbis-gfx-probe not installed (orbis-graphics package)"}
    else:
        try:
            doc = json.loads(r.stdout)
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            out = {"schema": "orbis-membench/2", "time": now_iso(), "result": "FAIL", "zero_copy": "NOT_RUN",
                   "why": "probe output was not JSON" + (" (timed out)" if getattr(r, "timed_out", False) else ""),
                   "stderr": (r.stderr or "")[-2000:], "rc": r.rc}
        else:
            mb = doc.get("membench")
            out = {"schema": "orbis-membench/2", "time": now_iso(), "probe_exit": doc.get("exit_code"), "rc": r.rc}
            if not isinstance(mb, dict):
                out.update(result="NOT_RUN", zero_copy="NOT_RUN", membench=None,
                           why=(doc.get("vulkan") or {}).get("error") or "no membench section (no Vulkan device?)")
            else:
                zc = (mb.get("zero_copy_host_memory") or {}).get("result")
                res = mb.get("result") or zc or ("NOT_RUN" if mb.get("error") else "FAIL")
                out.update(membench=mb, zero_copy=zc or "NOT_RUN", result=res if res in MEMBENCH_EXIT else "FAIL")
                want = MEMBENCH_EXIT.get(res)
                got = doc.get("exit_code")
                if want is not None and isinstance(got, int) and got != want:
                    out["result"] = "FAIL"
                    out["why"] = f"probe exit code {got} disagrees with its result {res} (expected {want})"
    try:
        atomic_write(p("var/lib/orbis-graphics/membench.json"), json.dumps(out, indent=1) + "\n")
    except OSError:
        pass
    return out


def reclaim_scan(pages: int, writeback: bool = False, dry_run: bool = False) -> dict:
    g = sysfs.primary_gpu()
    f = g.sysdir / "orbis_gtt_reclaim" if g else None
    if not f or not f.exists():
        raise OSError("orbis_gtt_reclaim not present (GE kernel with ge-0014 required)")
    before = reclaim_status(g)
    cmd = f"scan {int(pages)}" + (" writeback" if writeback else "")
    if not dry_run:
        write_sysfs(f, cmd)
    return {"command": cmd, "before": before, "after": reclaim_status(g), "dry_run": dry_run}


def reclaim_bootarg(enable: bool, dry_run: bool = False) -> dict:
    """amdgpu.orbis_shrinker=1 in bootargs.txt (the capability is fixed at buffer creation, so a reboot is needed)."""
    f = boot_dir() / "bootargs.txt"
    cur = (read(f) or "").splitlines()[0] if read(f) else ""
    toks = [t for t in cur.split() if not t.startswith("amdgpu.orbis_shrinker=")]
    if enable:
        toks.append("amdgpu.orbis_shrinker=1")
    new = " ".join(toks)
    if not dry_run and new != cur:
        atomic_write(f.with_name("bootargs.txt.orbis-prev"), cur + "\n")
        atomic_write(f, new + "\n")
    return {"file": str(f), "old": cur, "new": new, "dry_run": dry_run, "reboot_required": new != cur}


def current_mode() -> str:
    m = read(p("var/lib/orbis/desired-mode"))
    return m if m in MODE_POLICY else "pc"


def daemon(interval: float = 5.0, once: bool = False) -> int:
    pol = Policy(current_mode(), immediate=True)
    while True:
        m = current_mode()
        if m != pol.mode:
            pol = Policy(m, immediate=True)
            print(json.dumps({"decision": "mode", "mode": m, "time": now_iso()}), flush=True)
        try:
            d = pol.tick()
        except OSError as e:
            d = {"decision": "error", "why": str(e)}
        if d.get("decision") not in ("hold", "none"):
            print(json.dumps(d), flush=True)
        if once:
            return 0
        time.sleep(interval)
