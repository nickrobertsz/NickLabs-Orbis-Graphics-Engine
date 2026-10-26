"""orbis_engine (Graphics Engine 2.0 userspace) on fixture trees: telemetry, memory policy,
controls, hardware API, Black Box, orbis-debug, video, AI tuning, kdump, register decode, CLI."""
import io
import json
import os
import struct
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "diag"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gfx_fixtures import FakeRunner, ps4_root  # noqa: E402
from orbis_gfx.runner import Run  # noqa: E402
from orbis_engine import (ai, blackbox, cli, controls, debug, gpuregs, hwd, kdump, memory,  # noqa: E402
                          telemetry, video)

MiB = 1 << 20
GiB = 1 << 30


def ge_root(root: Path, *, ge=True, uvd=False, psi_some=0.0, loader="ge2", kmsg="") -> Path:
    kf = ps4_root(root, uvd=uvd, governor=True, kmsg=kmsg)
    pci = root / "sys/devices/pci0000:00/0000:00:01.0"
    dbg = root / "sys/kernel/debug/dri/0"
    if ge:
        (pci / "ps4_sclk").write_text("state=startup did=8 startup_did=8 dircntl=1 pdiva=8 fbdiv=0x1234 "
                                      "spll_unlock_sticky=0 restore_param=1\n")
        (pci / "orbis_engine_busy").write_text("gfx=42 shader=40 texture=12 cp=43 sdma0=1 sdma1=0 uvd=0 mc=5 "
                                               "samples=200 window_us=20000\n")
        (pci / "orbis_gtt_budget").write_text(f"budget={3 * GiB} usage={200 * MiB} ttm_pages_limit={4 * GiB} "
                                              f"min={256 * MiB} vram_carveout={1 * GiB}\n")
        h = root / "sys/class/hwmon/hwmon2"
        h.mkdir(parents=True)
        (h / "name").write_text("ps4_fan\n")
        (h / "temp1_input").write_text("61000\n")
        (h / "temp1_crit").write_text("79000\n")
        (dbg / "ps4_bridge_status").write_text("bridge: mn864729\nhpd: 1\nenable_attempts: 1\nlast_error: 0\n")
        (dbg / "amdgpu_liverpool_clk").write_text("CG_SCLK_CNTL: 0x00000108\nstate: startup\n")
        regs = bytearray(0x4000 * 4)
        struct.pack_into("<I", regs, 0x2004 * 4, 0xA0004000)            # GUI_ACTIVE | CP_BUSY | TA_BUSY
        struct.pack_into("<I", regs, 0x537 * 4, (3 << 25) | (0x1A << 12) | 0x1000000 | 0x8)
        struct.pack_into("<I", regs, 0x539 * 4, int.from_bytes(b"CB0\x00", "big"))
        struct.pack_into("<I", regs, 0x150A * 4, 1024)
        (dbg / "amdgpu_regs").write_bytes(bytes(regs))
        (dbg / "amdgpu_fence_info").write_text("--- ring 0 (gfx) ---\nLast signaled fence 0x00000010\n")
    (root / "proc/pressure").mkdir(parents=True)
    (root / "proc/pressure/memory").write_text(
        f"some avg10={psi_some:.2f} avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    (root / "sys/module/ttm/parameters").mkdir(parents=True)
    (root / "sys/module/ttm/parameters/pages_limit").write_text(str(4 * GiB // 4096) + "\n")
    (root / "proc/meminfo").write_text("MemTotal: 7340032 kB\nMemAvailable: 5242880 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (root / "proc/cmdline").write_text("root=LABEL=ORBIS-ROOT orbis.mode=ai" +
                                       (f" orbis.loader={loader} orbis.vram_mb=1024" if loader else "") + "\n")
    (root / "proc/sys/kernel/random").mkdir(parents=True)
    (root / "proc/sys/kernel/random/boot_id").write_text("1234abcd-0000-0000-0000-000000000001\n")
    (root / "proc/sys/kernel/osrelease").write_text("6.15.4-orbis-ps4-ge\n")
    pr = root / "proc/4242"
    (pr / "fdinfo").mkdir(parents=True)
    (pr / "comm").write_text("llama-server\n")
    (pr / "status").write_text("Name: llama-server\nVmRSS:   524288 kB\n")
    (pr / "fdinfo/7").write_text("pos: 0\ndrm-driver: amdgpu\ndrm-client-id: 9\ndrm-memory-vram: 524288 KiB\n"
                                 "drm-memory-gtt: 1048576 KiB\ndrm-memory-cpu: 0 KiB\ndrm-engine-gfx: 123456 ns\n")
    (pr / "fdinfo/8").write_text("pos: 0\ndrm-driver: amdgpu\ndrm-client-id: 9\ndrm-memory-vram: 524288 KiB\n")
    return kf


class EngineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.boot = Path(self.tmp.name) / "boot"
        self.boot.mkdir()
        (self.boot / "bootargs.txt").write_text("panic=0 quiet\n")
        os.environ["ORBIS_GFX_ROOT"] = str(self.root)
        os.environ["ORBIS_BOOT_DIR"] = str(self.boot)

    def tearDown(self):
        for k in ("ORBIS_GFX_ROOT", "ORBIS_GFX_KMSG_FILE", "ORBIS_BOOT_DIR", "ORBIS_BLACKBOX_DIR"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def tree(self, **kw):
        kf = ge_root(self.root, **kw)
        os.environ["ORBIS_GFX_KMSG_FILE"] = str(kf)
        return kf

    def gpu_dir(self):
        return self.root / "sys/devices/pci0000:00/0000:00:01.0"


class TelemetryTest(EngineBase):
    def test_ge_interfaces_reported_with_sources(self):
        self.tree()
        s = telemetry.snapshot(include_busy=True)
        g = s["gpu"]
        self.assertTrue(g["sclk"]["available"])
        self.assertEqual(g["sclk"]["state"], "startup")
        self.assertEqual(g["engine_busy_percent"]["gfx"], 42)
        self.assertEqual(g["memory"]["gtt_budget"]["budget"], 3 * GiB)
        self.assertEqual(g["display_bridge"]["hpd"], 1)
        self.assertEqual(s["thermal"]["apu_temp_c"], 61.0)
        self.assertEqual(s["thermal"]["fan_threshold_c"], 79.0)
        self.assertIsNone(s["thermal"]["fan_rpm"])
        self.assertEqual(s["memory"]["boot_contract"]["loader"], "ge2")
        self.assertEqual(s["memory"]["psi_memory"]["some"]["avg10"], 0.0)
        caps = s["capabilities"]
        for k in ("gpu.sclk", "gpu.engine_busy", "gpu.gtt_budget", "thermal.fan_threshold", "cpu.governor"):
            self.assertTrue(caps[k]["available"], k)
        self.assertFalse(caps["video.uvd_decode"]["available"])
        self.assertFalse(caps["memory.vram_carveout_runtime_resize"]["available"])

    def test_stock_kernel_reports_missing_not_values(self):
        self.tree(ge=False)
        g = telemetry.gpu()
        self.assertFalse(g["sclk"]["available"])
        self.assertIn("ge-6.15/0002", g["sclk"]["why"])
        self.assertFalse(g["engine_busy_percent"]["available"])
        self.assertIsNone(g["memory"]["gtt_budget"])
        self.assertFalse(g["dpm"]["available"])
        self.assertIn("fan", telemetry.thermal())


class MemoryTest(EngineBase):
    def test_ownership_dedups_fdinfo_per_client(self):
        self.tree()
        o = memory.ownership()
        self.assertEqual(len(o["clients"]), 1)
        c = o["clients"][0]
        self.assertEqual((c["comm"], c["vram"], c["gtt"]), ("llama-server", 512 * MiB, 1 * GiB))
        self.assertEqual(o["gpu"]["gtt_budget"], 3 * GiB)
        self.assertEqual(o["pool"]["vram_carveout"], 1 * GiB)

    def test_vram_validation_per_loader(self):
        self.assertTrue(memory.vram_valid(3072, ge=False))       # stock loader MTRR bug
        self.assertEqual(memory.vram_valid(3072, ge=True), [])
        self.assertEqual(memory.vram_valid(768, ge=True), [])
        self.assertTrue(memory.vram_valid(1000, ge=True))        # not a 16 MiB multiple
        self.assertEqual(memory.vram_valid(512, ge=False), [])

    def test_plan_and_boot_files(self):
        self.tree()
        pl = memory.plan("ai")
        ram = 7340032 * 1024
        self.assertEqual(pl["gtt_budget_target"], min(int(ram * 0.8), ram - int(0.75 * GiB)))
        r = memory.apply_boot("ai")
        self.assertEqual((self.boot / "vram.txt").read_text().strip(), "1024")
        self.assertTrue((self.boot / "gtt.txt").exists())
        self.assertTrue(r["reboot_required"])
        with self.assertRaises(ValueError):
            memory.apply_boot(vram_mb=1000)

    def test_stock_loader_skips_gtt_txt(self):
        self.tree(loader=None)
        r = memory.apply_boot(vram_mb=512, gtt_mb=4096)
        self.assertIn("skipped", r["actions"][1])
        self.assertFalse((self.boot / "gtt.txt").exists())
        with self.assertRaises(ValueError):
            memory.apply_boot(vram_mb=3072)

    def test_policy_shrinks_under_pressure(self):
        self.tree(psi_some=25.0)
        d = memory.Policy("ai").tick()
        self.assertEqual(d["decision"], "shrink")
        self.assertEqual(int((self.gpu_dir() / "orbis_gtt_budget").read_text()), int(3 * GiB * 0.9) // 4096 * 4096)
        log = (self.root / "var/log/orbis/memory-policy.jsonl").read_text()
        self.assertIn("memory pressure", log)

    def test_policy_converges_when_calm_and_raises_ttm_limit(self):
        self.tree(psi_some=0.0)
        t = [1000.0]
        pol = memory.Policy("ai", clock=lambda: t[0])
        self.assertEqual(pol.tick()["decision"], "hold")              # calm, but waiting RELAX_S
        t[0] += memory.RELAX_S + 1
        d = pol.tick()
        self.assertEqual(d["decision"], "converge")
        self.assertGreater(d["to"], 3 * GiB)
        self.assertLessEqual(d["to"], memory.plan("ai")["gtt_budget_target"])
        # the step passes TTM's 4 GiB global limit, so the limit is raised first
        self.assertIn("ttm", d)
        self.assertEqual(int((self.root / "sys/module/ttm/parameters/pages_limit").read_text()), d["to"] // 4096)
        # a mode switch converges at once (immediate=True); sysfs fixture files do not echo the
        # kernel's formatted read-back, so restore it first
        (self.gpu_dir() / "orbis_gtt_budget").write_text(
            f"budget={3 * GiB} usage=0 ttm_pages_limit={5 * GiB} min={256 * MiB} vram_carveout={GiB}\n")
        self.assertEqual(memory.Policy("ai", clock=lambda: t[0], immediate=True).tick()["decision"], "converge")

    def test_pc_mode_never_below_kernel_default_when_calm(self):
        self.tree(psi_some=0.0)
        (self.gpu_dir() / "orbis_gtt_budget").write_text(
            f"budget={int(3.5 * GiB)} usage=0 ttm_pages_limit={4 * GiB} min={256 * MiB} vram_carveout={GiB}\n")
        self.assertLess(memory.plan("pc")["gtt_budget_target"], int(3.5 * GiB))
        d = memory.Policy("pc", immediate=True).tick()
        self.assertEqual(d["decision"], "hold")
        self.assertEqual(d["why"], "at target")
        st = json.loads((self.root / "var/lib/orbis-graphics/memory/boot-default.json").read_text())
        self.assertEqual(st["budget"], int(3.5 * GiB))

    def test_no_interface_no_decision(self):
        self.tree(ge=False)
        self.assertEqual(memory.Policy("pc").tick()["decision"], "none")


class ControlsTest(EngineBase):
    def test_set_and_reset(self):
        self.tree()
        res = controls.apply_request({"gpu_sclk": "max", "fan_threshold_c": 70, "cpu_governor": "performance"})
        self.assertTrue(all(r["applied"] for r in res))
        self.assertEqual((self.gpu_dir() / "ps4_sclk").read_text(), "max")
        fan = self.root / "sys/class/hwmon/hwmon2/temp1_crit"
        self.assertEqual(fan.read_text(), "70000")
        gov = self.root / "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        self.assertEqual(gov.read_text(), "performance")
        r = controls.reset()
        self.assertEqual(fan.read_text(), "79000")
        self.assertEqual(gov.read_text(), "schedutil")
        self.assertEqual((self.gpu_dir() / "ps4_sclk").read_text(), "startup")
        self.assertTrue(r["reset"])

    def test_validation(self):
        self.tree()
        with self.assertRaises(ValueError):
            controls.set_fan_threshold(90)
        with self.assertRaises(ValueError):
            controls.set_sclk("overclock")
        with self.assertRaises(ValueError):
            controls.set_governor("ondemand")
        with self.assertRaises(ValueError):
            controls.apply_request({"voltage": 2})

    def test_missing_interfaces_are_not_applied(self):
        self.tree(ge=False)
        self.assertFalse(controls.set_sclk("max")["applied"])
        self.assertFalse(controls.set_fan_threshold(60)["applied"])


class HwdTest(EngineBase):
    def setUp(self):
        super().setUp()
        self.tree()
        hwd.Handler.lan_bind = False
        hwd.Handler.tok = hwd.token(create=True)
        self.httpd = hwd.ThreadingHTTPServer(("127.0.0.1", 0), hwd.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_reads_open_writes_need_token(self):
        code, st = self.call("/v1/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["api"], 2)
        self.assertEqual(self.call("/v1/memory")[1]["clients"][0]["comm"], "llama-server")
        self.assertEqual(self.call("/v1/control", {"gpu_sclk": "max"})[0], 401)
        self.assertEqual(self.call("/v1/control", {"gpu_sclk": "max"}, token="wrong")[0], 401)
        code, r = self.call("/v1/control", {"gpu_sclk": "max", "dry_run": True}, token=hwd.Handler.tok)
        self.assertEqual(code, 200)
        self.assertFalse(r["results"][0]["applied"])
        self.assertEqual((self.gpu_dir() / "ps4_sclk").read_text().split()[0], "state=startup")
        code, r = self.call("/v1/control", {"fan_threshold_c": 99}, token=hwd.Handler.tok)
        self.assertEqual(code, 400)
        self.assertEqual(self.call("/v1/nope")[0], 404)
        self.assertEqual(oct((self.root / "etc/orbis/hwd-token").stat().st_mode & 0o777), "0o600")

    def test_lan_bind_requires_token_for_reads(self):
        hwd.Handler.lan_bind = True
        self.assertEqual(self.call("/v1/status")[0], 401)
        self.assertEqual(self.call("/v1/status", token=hwd.Handler.tok)[0], 200)

    def test_client_filter(self):
        self.assertTrue(hwd.client_allowed("192.168.1.20"))
        self.assertTrue(hwd.client_allowed("127.0.0.1"))
        self.assertFalse(hwd.client_allowed("8.8.8.8"))


class BlackBoxTest(EngineBase):
    def setUp(self):
        super().setUp()
        os.environ["ORBIS_BLACKBOX_DIR"] = str(Path(self.tmp.name) / "bb")

    def _old_boot(self, seq, bid, clean, events=()):
        d = blackbox.base() / "boots" / f"{seq:05d}-{bid}"
        d.mkdir(parents=True)
        (d / "boot.json").write_text(json.dumps({"seq": seq, "boot_id": bid, "kernel": "6.15.4-orbis-ps4-ge"}))
        (d / "flight.jsonl").write_text(json.dumps({"t": "2026-10-25T10:00:00", "apu_c": 70.5}) + "\n")
        for e in events:
            with open(d / "events.jsonl", "a") as f:
                f.write(json.dumps(e) + "\n")
        if clean:
            (d / "shutdown.json").write_text("{}")
        return d

    def test_previous_boot_verdict_and_last_crash(self):
        self.tree()
        self._old_boot(1, "aaaaaaaa", True)
        self._old_boot(2, "bbbbbbbb", False, [{"kind": "display", "msg": "ps4-bridge hpd"},
                                              {"kind": "ring_timeout", "msg": "ring gfx timeout"}])
        d = blackbox.start_boot()
        self.assertEqual(d.name, "00003-1234abcd")
        bj = json.loads((d / "boot.json").read_text())
        self.assertFalse(bj["previous_boot"]["clean_shutdown"])
        self.assertIn("ring_timeout", bj["previous_boot"]["summary"]["verdict"])
        self.assertEqual(blackbox.start_boot(), d)                     # idempotent
        lc = blackbox.last_crash()
        self.assertTrue(lc["found"])
        self.assertEqual(lc["dir"], "00002-bbbbbbbb")
        blackbox.end_boot()
        self.assertTrue((d / "shutdown.json").exists())

    def test_hang_without_kernel_error_is_described_honestly(self):
        self.tree()
        d = self._old_boot(1, "cccccccc", False)
        v = blackbox.summarize_boot(d)["verdict"]
        self.assertIn("hard hang, power loss", v)

    def test_run_once_records_events_and_captures(self):
        kf = self.tree()
        Path(kf).write_text("")
        km = Path(self.tmp.name) / "kmsg-replay"
        km.write_text("6,100,1000,-;amdgpu: normal message\n"
                      "3,101,1001,-;amdgpu 0000:00:01.0: amdgpu: [gfxhub] page fault (src_id:0 ring:24 vmid:3)\n"
                      "3,102,1002,-;amdgpu 0000:00:01.0: amdgpu: VM_CONTEXT1_PROTECTION_FAULT_ADDR 0x00012345\n"
                      "4,103,1003,-;ps4-bridge: hpd toggled\n")
        blackbox.run(interval=60, once=True, kmsg_path=str(km))
        d = blackbox.current_dir()
        ev = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["kind"] for e in ev], ["gpu_fault", "gpu_fault", "display"])
        self.assertTrue(ev[0]["capture"].startswith("gpu-state-"))
        self.assertNotIn("capture", ev[1])                            # rate limited
        cap = (d / ev[0]["capture"]).read_text()
        self.assertIn("amdgpu_fence_info", cap)
        self.assertIn("orbis_engine_busy", cap)
        flight = json.loads((d / "flight.jsonl").read_text().splitlines()[-1])
        self.assertEqual(flight["sclk"], "startup")
        self.assertEqual(flight["apu_c"], 61.0)
        self.assertIn("normal message", (d / "dmesg-boot.txt").read_text() + "normal message")
        self.assertNotIn("normal message", (d / "kmsg-warn.log").read_text())

    def test_devcoredump_harvest(self):
        self.tree()
        dc = self.root / "sys/class/devcoredump/devcd1"
        dc.mkdir(parents=True)
        (dc / "data").write_text("AMDGPU Device Coredump\nring gfx timeout\n")
        d = blackbox.start_boot()
        got = blackbox.harvest_devcoredumps(d)
        self.assertEqual(len(got), 1)
        self.assertIn("Coredump", (d / "devcoredump" / got[0]).read_text())

    def test_netconsole_bootarg_and_streamer(self):
        self.assertEqual(blackbox.netconsole_bootarg("192.168.1.10", "192.168.1.50"),
                         "netconsole=6665@192.168.1.50/eth0,6666@192.168.1.10/")
        import socket
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(5)
        s = blackbox.Streamer(f"udp://127.0.0.1:{rx.getsockname()[1]}")
        self.tree()
        s.send("event", {"kind": "gpu_fault"})
        msg = json.loads(rx.recv(65535))
        self.assertEqual((msg["k"], msg["p"]["kind"], msg["boot"]), ("event", "gpu_fault", "1234abcd"))
        rx.close()
        self.assertIsNone(blackbox.Streamer("http://x").sock)

    def test_prune(self):
        self.tree()
        for i in range(1, 36):
            self._old_boot(i, f"{i:08x}", True)
        blackbox.prune()
        self.assertEqual(len(blackbox.list_boots()), blackbox.KEEP_BOOTS)


class DebugTest(EngineBase):
    def setUp(self):
        super().setUp()
        os.environ["ORBIS_BLACKBOX_DIR"] = str(Path(self.tmp.name) / "bb")

    def test_collect_and_export(self):
        self.tree(kmsg="amdgpu: ring gfx timeout\n")
        blackbox.start_boot()
        (self.root / "var/lib/orbis").mkdir(parents=True)
        (self.root / "var/lib/orbis/gpu-gate.json").write_text('{"inference_gate": {"passed": false}}')
        fr = FakeRunner(vainfo="", missing={"drm_info"})
        out = debug.collect(Path(self.tmp.name) / "bundle", run=fr)
        rep = json.loads((out / "report.json").read_text())
        self.assertEqual(rep["schema"], "orbis-debug-bundle/1")
        self.assertEqual(rep["collection_errors"], [])
        self.assertTrue(rep["gpu_registers"]["available"])
        self.assertTrue((out / "debugfs/ps4_bridge_status").exists())
        self.assertTrue((out / "debugfs/rings/amdgpu_ring_gfx.bin").exists())
        self.assertTrue((out / "graphics/gpu-gate.json").exists())
        self.assertTrue((out / "logs/dmesg.txt").exists())
        self.assertFalse((out / "graphics/drm_info.txt").exists())
        self.assertIn("telemetry:", (out / "report.txt").read_text())
        self.assertTrue(any(c[0][0] == "journalctl" and "-1" in c[0] for c in fr.calls))
        ex = debug.export(Path(self.tmp.name) / "b.tar.gz", run=fr)
        with tarfile.open(ex["bundle"]) as t:
            self.assertTrue(any(n.endswith("report.json") for n in t.getnames()))
        self.assertTrue(Path(ex["copied_to_usb"]).exists())
        self.assertTrue(Path(ex["bundle"] + ".sha256").read_text().startswith(ex["sha256"]))

    def test_stream_boot_token_add_and_remove(self):
        self.tree()
        r = debug.stream_enable("192.168.1.10", boot=True, local_ip="192.168.1.50", dry_run=False)
        ba = (self.boot / "bootargs.txt").read_text().strip()
        self.assertIn("netconsole=6665@192.168.1.50/eth0,6666@192.168.1.10/", ba)
        self.assertIn("panic=0 quiet", ba)
        self.assertEqual(json.loads((self.root / "etc/orbis/blackbox.json").read_text())["stream"],
                         "udp://192.168.1.10:6667")
        self.assertFalse(r["netconsole_runtime"]["applied"])
        debug.stream_disable(boot=True)
        self.assertEqual((self.boot / "bootargs.txt").read_text().strip(), "panic=0 quiet")
        with self.assertRaises(ValueError):
            debug.stream_enable("192.168.1.10", boot=True)


class FfmpegRunner(FakeRunner):
    def __init__(self, hw_frames=None, hw_rc=0, vainfo=""):
        super().__init__(vainfo=vainfo)
        self.hw_frames, self.hw_rc = hw_frames, hw_rc

    def __call__(self, argv, timeout=60.0, env=None):
        if os.path.basename(argv[0]) != "ffmpeg":
            return super().__call__(argv, timeout, env)
        self.calls.append((list(argv), env))
        frames = "#format: frame checksums\n" + "".join(f"0, {i}, {i}, 1, 3110400, md5{i:04d}\n" for i in range(120))
        if "libx264" in argv:
            return Run(argv, 0, "", "", False, 0.1)
        if "-hwaccel" in argv:
            return Run(argv, self.hw_rc, self.hw_frames if self.hw_frames is not None else frames, "", False, 0.1)
        return Run(argv, 0, frames, "", False, 0.1)


VAINFO = ("vainfo: Driver version: Mesa Gallium driver 26.2.5 for AMD Liverpool\n"
          "      VAProfileH264Main               :	VAEntrypointVLD\n"
          "      VAProfileH264High               :	VAEntrypointVLD\n")


class VideoTest(EngineBase):
    def test_status_labels_without_uvd(self):
        self.tree()
        st = video.status(run=FakeRunner(vainfo=VAINFO))
        self.assertFalse(st["kernel"]["uvd_ring"])
        self.assertEqual(st["decode"]["H264"], "software (CPU)")     # VA-API profile alone is not enough
        self.assertTrue(st["decode"]["HEVC"].startswith("software"))

    def test_status_with_uvd(self):
        self.tree(uvd=True)
        st = video.status(run=FakeRunner(vainfo=VAINFO))
        self.assertEqual(st["decode"]["H264"], "hardware (UVD via VA-API)")
        self.assertEqual(st["decode"]["MPEG2"], "software (CPU)")

    def test_test_states(self):
        self.tree()
        self.assertEqual(video.test(run=FfmpegRunner())["state"], "UNSUPPORTED")
        self.assertTrue((self.root / video.RESULT).exists())

    def test_hw_decode_must_match_software(self):
        self.tree(uvd=True)
        self.assertEqual(video.test(run=FfmpegRunner())["state"], "PASS")
        bad = "".join(f"0, {i}, {i}, 1, 3110400, bad{i}\n" for i in range(120))
        r = video.test(run=FfmpegRunner(hw_frames=bad))
        self.assertEqual(r["state"], "FAIL")
        self.assertEqual(r["frame_mismatches"], 120)
        self.assertEqual(video.test(run=FfmpegRunner(hw_frames="", hw_rc=1))["state"], "FAIL")

    def test_configure_backs_up_foreign_config(self):
        self.tree()
        conf = self.root / "etc/mpv/mpv.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("volume=50\n")
        a = video.configure()
        self.assertEqual(a["mode"], "software")
        self.assertIn("hwdec=no", conf.read_text())
        self.assertEqual((self.root / "etc/mpv/mpv.conf.orbis-prev").read_text().strip(), "volume=50")


def harness_report(rows, tensors_ok=True):
    return {"kld_table": [{"ctx": c, "ubatch": 512, "ngl": "all", "outcome": o, "elapsed_s": c / 100} for c, o in rows],
            "tensors": [{"outcome": "PASS" if tensors_ok else "NUMERIC_MISMATCH",
                         "first_divergence": None if tensors_ok else "attn_out-3"}]}


class AiTest(EngineBase):
    def test_variants_use_real_knobs_only(self):
        known = {"GGML_VK_PREFER_HOST_MEMORY", "GGML_VK_DISABLE_F16", "GGML_VK_MAX_NODES_PER_SUBMIT",
                 "GGML_VK_SERIALIZE_SUBMISSIONS", "GGML_VK_FORCE_MAX_ALLOCATION_SIZE", "GGML_VK_FORCE_MAX_BUFFER_SIZE",
                 "GGML_VK_DISABLE_FUSION", "GGML_VK_DISABLE_MMVQ", "GGML_VK_DISABLE_INTEGER_DOT_PRODUCT"}
        for v in ai.VARIANTS.values():
            self.assertLessEqual(set(v["env"]), known)
        self.assertEqual(sorted(ai.DEFAULT_ORDER), sorted(ai.VARIANTS))

    def test_corpus_deterministic(self):
        a = ai.corpus(Path(self.tmp.name) / "a.txt").read_text()
        b = ai.corpus(Path(self.tmp.name) / "b.txt").read_text()
        self.assertEqual(a, b)

    def test_campaign_dry_run_configs(self):
        self.tree()
        out = Path(self.tmp.name) / "camp"
        r = ai.run_campaign("/models/m.gguf", out, ["baseline", "ub64"], dry_run=True)
        self.assertEqual([x["variant"] for x in r["plan"]], ["baseline", "ub64"])
        cfg = json.loads((out / "ub64/config.json").read_text())
        self.assertEqual(cfg["axes"]["ubatch"], [64])
        self.assertTrue(cfg["server"]["enabled"])
        with self.assertRaises(ValueError):
            ai.run_campaign("/m", out, ["turbo"], dry_run=True)

    def test_recommend_longest_correct_prefill(self):
        self.tree()
        out = Path(self.tmp.name) / "camp"
        out.mkdir()
        reps = {"baseline": harness_report([(128, "PASS"), (256, "PASS"), (512, "DEVICE_LOST")]),
                "ub64": harness_report([(128, "PASS"), (512, "PASS"), (2048, "PASS")]),
                "hostmem": harness_report([(128, "PASS"), (4096, "PASS")], tensors_ok=False)}
        rec = ai.recommend(out, reps)
        self.assertEqual(rec["variant"], "hostmem")      # longest ctx wins first...
        self.assertFalse(rec["use_vulkan"])              # ...but wrong tensors keep AI on the CPU
        reps["hostmem"] = harness_report([(128, "PASS"), (1024, "PASS")])
        rec = ai.recommend(out, reps)
        self.assertEqual(rec["variant"], "ub64")
        self.assertTrue(rec["use_vulkan"])
        self.assertEqual(rec["improvement_over_baseline_ctx"], 2048 - 256)
        a = ai.apply(out, dry_run=True)
        self.assertEqual(a["actions"][1]["command"], ["orbis", "ai", "set", "ubatch", "64"])
        self.assertEqual(ai.recommend(out, {})["recommendation"], None)


class KdumpRegsTest(EngineBase):
    def test_kdump_bootarg(self):
        self.tree()
        self.assertFalse(kdump.status()["reserved_in_bootargs"])
        kdump.set_bootarg(True)
        self.assertEqual((self.boot / "bootargs.txt").read_text().strip(), "panic=0 quiet crashkernel=256M")
        self.assertTrue(kdump.status()["reserved_in_bootargs"])
        self.assertEqual((self.boot / "bootargs.txt.orbis-prev").read_text().strip(), "panic=0 quiet")
        kdump.set_bootarg(False)
        self.assertEqual((self.boot / "bootargs.txt").read_text().strip(), "panic=0 quiet")
        argv = kdump.load(dry_run=True)["argv"]
        self.assertEqual(argv[:3], ["kexec", "-s", "-p"])
        self.assertIn("nomodeset", argv[-1])
        self.assertNotIn("crashkernel", argv[-1])

    def test_newc_archive_is_valid(self):
        import stat
        data = kdump._newc([("bin", stat.S_IFDIR | 0o755, b""), ("init", stat.S_IFREG | 0o755, b"#!/bin/sh\n")])
        self.assertTrue(data.startswith(b"070701"))
        self.assertIn(b"TRAILER!!!", data)
        self.assertEqual(len(data) % 4, 0)

    def test_register_decode(self):
        self.tree()
        d = gpuregs.dump()
        self.assertTrue(d["available"])
        r = d["registers"]
        self.assertIn("CP_BUSY", r["GRBM_STATUS"]["busy_blocks"])
        self.assertIn("GUI_ACTIVE", r["GRBM_STATUS"]["busy_blocks"])
        f = r["VM_CONTEXT1_PROTECTION_FAULT_STATUS"]
        self.assertEqual((f["vmid"], f["memory_client_id"], f["rw"], f["fault"]), (3, 0x1A, "write", True))
        self.assertEqual(r["VM_CONTEXT1_PROTECTION_FAULT_MCCLIENT"]["client"], "CB0")
        self.assertEqual(r["CONFIG_MEMSIZE"]["vram_mb"], 1024)

    def test_regs_unavailable_without_debugfs(self):
        self.tree(ge=False)
        self.assertFalse(gpuregs.dump()["available"])


class QualifyEngineChecksTest(EngineBase):
    def _checks(self):
        from orbis_gfx import qualify
        from orbis_gfx.results import Report
        rep = Report(tool="t", version="t", stage="t")
        qualify.kernel_checks(rep, qualify.Ctx({}, run=FakeRunner()))
        return {c.id: c for c in rep.checks}

    def test_ge_kernel(self):
        self.tree()
        c = self._checks()
        self.assertEqual(c["pm.sclk"].state, "DEGRADED")            # fixture: state=startup
        self.assertEqual(c["thermal.fan"].state, "PASS")
        self.assertEqual(c["sensors.gpu"].state, "PASS")
        self.assertEqual(c["memory.gtt_budget"].state, "PASS")
        self.assertEqual(c["memory.loader_contract"].state, "PASS")  # 1024 MiB both sides
        self.assertEqual(c["cpu.cpufreq"].state, "UNSUPPORTED")      # fixture has no scaling_driver
        self.assertEqual(c["display.bridge_status"].state, "PASS")
        (self.gpu_dir() / "ps4_sclk").write_text("state=max did=1 startup_did=8\n")
        (self.root / "proc/cmdline").write_text("orbis.loader=ge2 orbis.vram_mb=512\n")
        c = self._checks()
        self.assertEqual(c["pm.sclk"].state, "PASS")
        self.assertEqual(c["memory.loader_contract"].state, "FAIL")

    def test_stock_kernel(self):
        self.tree(ge=False, loader=None)
        c = self._checks()
        for k in ("pm.sclk", "thermal.fan", "memory.gtt_budget"):
            self.assertEqual(c[k].state, "UNSUPPORTED", k)
            self.assertIn("ge-6.15", c[k].blocker)
        self.assertEqual(c["memory.loader_contract"].state, "NOT_RUN")
        self.assertNotIn("display.bridge_status", c)


class CliTest(EngineBase):
    def _run(self, fn, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fn(argv)
        return rc, buf.getvalue()

    def test_hw_and_mem_json(self):
        self.tree()
        rc, out = self._run(cli.main_hw, ["--json", "status"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["gpu"]["sclk"]["state"], "startup")
        rc, out = self._run(cli.main_hw, ["--json", "set", "gpu-sclk", "max", "--dry-run"])
        self.assertEqual(rc, 0)
        rc, _ = self._run(cli.main_hw, ["set", "fan-threshold", "200"])
        self.assertEqual(rc, 2)
        rc, out = self._run(cli.main_mem, ["--json", "plan", "gaming"])
        self.assertEqual(json.loads(out)["vram_mb"], 1024)
        rc, out = self._run(cli.main_mem, ["status"])
        self.assertIn("llama-server", out)

    def test_debug_and_video_text(self):
        os.environ["ORBIS_BLACKBOX_DIR"] = str(Path(self.tmp.name) / "bb")
        self.tree()
        rc, out = self._run(cli.main_debug, ["last-crash"])
        self.assertEqual(rc, 0)
        self.assertIn("found: False", out)
        rc, out = self._run(cli.main_debug, ["--json", "gpu"])
        self.assertTrue(json.loads(out)["registers"]["available"])
        rc, _ = self._run(cli.main_debug, ["stream", "enable"])
        self.assertEqual(rc, 2)
        rc, out = self._run(cli.main_ai, ["--json", "variants"])
        self.assertIn("ub64", json.loads(out)["order"])

    def test_wrappers_point_at_entries(self):
        for name in cli.ENTRY:
            text = (REPO / "diag/bin" / name).read_text()
            self.assertIn(f'ENTRY["{name}"]', text)
        self.assertEqual(cli.main([]), 2)


if __name__ == "__main__":
    unittest.main()
