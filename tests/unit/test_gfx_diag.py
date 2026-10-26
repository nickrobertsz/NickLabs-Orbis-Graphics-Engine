"""orbis_gfx: result model, sysfs reading, qualification states, health, profiles."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "diag"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gfx_fixtures import FakeRunner, ps4_root, probe_json  # noqa: E402
from orbis_gfx import cli, health, profile, qualify, sysfs  # noqa: E402
from orbis_gfx.results import Check, Report  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["ORBIS_GFX_ROOT"] = str(self.root)

    def tearDown(self):
        os.environ.pop("ORBIS_GFX_ROOT", None)
        os.environ.pop("ORBIS_GFX_KMSG_FILE", None)
        os.environ.pop("DISPLAY", None)
        self.tmp.cleanup()

    def tree(self, **kw):
        kf = ps4_root(self.root, **kw)
        os.environ["ORBIS_GFX_KMSG_FILE"] = str(kf)

    def run_q(self, runner, **opts):
        o = {"probe": "/usr/lib/orbis-graphics/orbis-gfx-probe"}
        o.update(opts)
        return qualify.qualify(o, run=runner)


class ResultsTest(unittest.TestCase):
    def test_states_validated_and_blocker_required(self):
        with self.assertRaises(ValueError):
            Check("x", "t", "OK")
        with self.assertRaises(ValueError):
            Check("x", "t", "UNSUPPORTED")
        Check("x", "t", "UNSUPPORTED", blocker="why")

    def test_overall(self):
        r = Report("t", "1", "s")
        self.assertEqual(r.overall, "NOT_RUN")
        r.add(Check("a", "a", "PASS", required=True))
        r.add(Check("b", "b", "NOT_RUN"))
        self.assertEqual(r.overall, "PASS")
        r.add(Check("c", "c", "UNSUPPORTED", blocker="k"))
        self.assertEqual(r.overall, "PASS")
        r.add(Check("d", "d", "DEGRADED"))
        self.assertEqual(r.overall, "DEGRADED")
        r.add(Check("e", "e", "BLOCKED", required=True, blocker="x"))
        self.assertEqual(r.overall, "BLOCKED")
        r.add(Check("f", "f", "FAIL"))
        self.assertEqual(r.overall, "FAIL")
        d = json.loads(r.to_json())
        self.assertEqual(d["schema"], "orbis-gfx-report/1")
        self.assertIn("OVERALL: FAIL", r.render_text())


class SysfsTest(Base):
    def test_identity_memory_connectors_firmware(self):
        self.tree()
        g = sysfs.primary_gpu()
        self.assertEqual((g.vendor, g.device, g.driver, g.name), ("0x1002", "0x9920", "amdgpu", "Liverpool (PS4)"))
        self.assertEqual(g.pci_slot, "0000:00:01.0")
        self.assertEqual(sysfs.memory(g)["mem_info_vram_total"], 1024 << 20)
        c = sysfs.connectors(g)[0]
        self.assertEqual((c["name"], c["status"], c["edid_valid_header"]), ("HDMI-A-1", "connected", True))
        fw = sysfs.firmware_info(g)
        self.assertEqual(fw["MEC2"]["version"], "0x0000000e")
        self.assertIn("gfx", sysfs.rings(g))

    def test_log_scan(self):
        h = sysfs.scan_log("[  1.0] amdgpu 0000:00:01.0: Direct firmware load for amdgpu/liverpool_uvd.bin failed\n"
                           "[  2.0] amdgpu: ring gfx timeout, signaled seq=1\n")
        self.assertEqual(len(h["fw_fail"]), 1)
        self.assertEqual(len(h["ring_timeout"]), 1)


class QualifyTest(Base):
    def test_healthy_console_without_dpm_or_uvd(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json()))
        st = {c.id: c.state for c in rep.checks}
        for cid in ("gpu.identity", "gpu.driver", "gpu.firmware", "gpu.rings", "gpu.memory", "display.connector",
                    "display.edid", "health.since_boot", "gl.renderer", "gl.draw", "gl.shader", "gl.frame_timing",
                    "vk.device", "vk.raster", "vk.compute", "vk.pipeline_cache", "cl.rusticl"):
            self.assertEqual(st[cid], "PASS", cid)
        self.assertEqual(st["pm.clocks"], "UNSUPPORTED")
        self.assertIn("CHIP_LIVERPOOL", rep.get("pm.clocks").blocker)
        self.assertEqual(st["video.decode"], "UNSUPPORTED")
        self.assertIn("UVD", rep.get("video.decode").blocker)
        self.assertEqual(st["vk.present"], "NOT_RUN")
        self.assertEqual(st["gpu.ib_test"], "NOT_RUN")
        self.assertEqual(st["perf.sustained"], "NOT_RUN")
        self.assertEqual(st["gpu.recovery"], "NOT_RUN")
        self.assertEqual(st["modes.transitions"], "NOT_RUN")
        self.assertEqual(rep.overall, "PASS")

    def test_llvmpipe_is_a_failure_not_a_pass(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json(renderer="llvmpipe (LLVM 21, 256 bits)")))
        self.assertEqual(rep.get("gl.renderer").state, "FAIL")
        self.assertIn("SOFTWARE RENDERING", rep.get("gl.renderer").detail)
        self.assertEqual(rep.overall, "FAIL")

    def test_lavapipe_only_fails_vulkan_and_blocks_dependents(self):
        self.tree()
        p = probe_json(devices=[{"name": "llvmpipe (LLVM 21)", "device_type": 4, "driver_id": 10, "driver_name": "llvmpipe"}])
        rep = self.run_q(FakeRunner(p))
        self.assertEqual(rep.get("vk.device").state, "FAIL")
        for cid in ("vk.raster", "vk.compute", "vk.pipeline_cache", "vk.present"):
            self.assertEqual(rep.get(cid).state, "BLOCKED", cid)

    def test_driver_not_bound_blocks_userspace(self):
        self.tree(driver="radeon")
        rep = self.run_q(FakeRunner(probe_json()))
        self.assertEqual(rep.get("gpu.driver").state, "FAIL")
        self.assertEqual(rep.get("gl.renderer").state, "BLOCKED")
        self.assertEqual(rep.overall, "FAIL")

    def test_device_lost_during_compute(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json(fatal=True, compute_ok=False)))
        self.assertEqual(rep.get("vk.compute").state, "FAIL")
        self.assertIn("VK_ERROR_DEVICE_LOST", rep.get("vk.compute").detail)

    def test_wrong_numbers_fail(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json(compute_ok=False, raster_ok=False, shader_ok=False)))
        for cid in ("vk.compute", "vk.raster", "gl.shader"):
            self.assertEqual(rep.get(cid).state, "FAIL", cid)

    def test_firmware_failure_and_faults(self):
        self.tree(kmsg="amdgpu 0000:00:01.0: Direct firmware load for amdgpu/liverpool_mec.bin failed with error -2\n"
                       "amdgpu: GPU fault detected: 146 0x0\n")
        rep = self.run_q(FakeRunner(probe_json()))
        self.assertEqual(rep.get("gpu.firmware").state, "FAIL")
        self.assertEqual(rep.get("health.since_boot").state, "FAIL")

    def test_headless_and_missing_edid_degrade(self):
        self.tree(connected=False)
        rep = self.run_q(FakeRunner(probe_json()))
        self.assertEqual(rep.get("display.connector").state, "DEGRADED")
        self.assertEqual(rep.get("display.edid").state, "NOT_RUN")

    def test_no_debugfs_rings_not_run(self):
        self.tree(debugfs=False)
        rep = self.run_q(FakeRunner(probe_json()))
        self.assertEqual(rep.get("gpu.rings").state, "NOT_RUN")

    def test_dpm_and_sensors_when_kernel_offers_them(self):
        self.tree(pm=True, hwmon_temp=True)
        rep = self.run_q(FakeRunner(probe_json()))
        self.assertEqual(rep.get("pm.clocks").state, "PASS")
        self.assertEqual(rep.get("sensors.gpu").state, "PASS")

    def test_opencl_states(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json(cl={"platform_count": 0, "error": "no OpenCL platform"})))
        self.assertEqual(rep.get("cl.rusticl").state, "UNSUPPORTED")
        cpu_only = {"platforms": [{"devices": [{"name": "llvmpipe", "type": 2, "ok": True}]}]}
        rep = self.run_q(FakeRunner(probe_json(cl=cpu_only)))
        self.assertEqual(rep.get("cl.rusticl").state, "DEGRADED")
        calls = FakeRunner(probe_json())
        self.run_q(calls)
        envs = [e for argv, e in calls.calls if "--opencl" in argv]
        self.assertEqual(envs[0]["RUSTICL_ENABLE"], "radeonsi")

    def test_video_with_uvd_ring(self):
        self.tree(uvd=True)
        rep = self.run_q(FakeRunner(probe_json(), vainfo="      VAProfileH264Main               :	VAEntrypointVLD\n"))
        self.assertEqual(rep.get("video.decode").state, "NOT_RUN")
        self.assertIn("--video-sample", rep.get("video.decode").detail)

    def test_present_with_display(self):
        self.tree()
        os.environ["DISPLAY"] = ":0"
        rep = self.run_q(FakeRunner(probe_json()))
        self.assertEqual(rep.get("vk.present").state, "PASS")
        rep = self.run_q(FakeRunner(probe_json(), vkcube_rc=1))
        self.assertEqual(rep.get("vk.present").state, "FAIL")

    def test_missing_probe_is_not_run_not_pass(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json(), missing={"orbis-gfx-probe"}))
        self.assertEqual(rep.get("gl.renderer").state, "NOT_RUN")
        self.assertNotEqual(rep.overall, "PASS")


class HealthProfileTest(Base):
    def test_health(self):
        self.tree(kmsg="amdgpu: ring sdma0 timeout\n")
        s = health.snapshot()
        self.assertEqual(s["status"], "FAULTED")
        self.assertIn("Orbis GPU health: FAULTED", health.render_text(s))

    def test_profile_without_dpm_skips_and_records(self):
        self.tree()
        rec = profile.apply("performance")
        knobs = {a["knob"]: a for a in rec["actions"]}
        self.assertIn("skipped", knobs["gpu_perf_level"])
        self.assertIn("skipped", knobs["cpu_governor"])
        env = self.root / "etc/environment.d/90-orbis-graphics.conf"
        self.assertIn("MESA_SHADER_CACHE_MAX_SIZE=4G", env.read_text())
        self.assertEqual(knobs["vram_mb"]["recommendation"], 2048)
        profile.reset()
        self.assertFalse(env.exists())

    def test_profile_apply_and_reset_restores_exact_values(self):
        self.tree(pm=True, governor=True)
        g = sysfs.primary_gpu()
        lvl = g.sysdir / "power_dpm_force_performance_level"
        gov = self.root / "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        profile.apply("performance")
        self.assertEqual(lvl.read_text(), "high")
        self.assertEqual(gov.read_text(), "performance")
        profile.apply("quiet")          # second profile: original values must still be the first ones
        self.assertEqual(lvl.read_text(), "low")
        profile.reset()
        self.assertEqual(lvl.read_text(), "auto")
        self.assertEqual(gov.read_text().strip(), "schedutil")
        log = (self.root / "var/log/orbis/graphics-profile.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 3)

    def test_profile_rejects_unknown_and_dry_run_changes_nothing(self):
        self.tree(pm=True)
        with self.assertRaises(KeyError):
            profile.plan("overclock")
        profile.apply("performance", dry_run=True)
        self.assertEqual((sysfs.primary_gpu().sysdir / "power_dpm_force_performance_level").read_text(), "auto\n")
        self.assertFalse((self.root / "var/lib/orbis-graphics/profile-state.json").exists())

    def test_personality_defaults_and_override(self):
        self.tree()
        self.assertEqual(profile.personality_default("gaming"), "performance")
        (self.root / "etc/orbis").mkdir(parents=True)
        (self.root / "etc/orbis/graphics.json").write_text(json.dumps({"personality_profiles": {"gaming": "balanced"}}))
        self.assertEqual(profile.personality_default("gaming"), "balanced")


class CliTest(Base):
    def test_capabilities_from_last_qualify(self):
        self.tree()
        rep = self.run_q(FakeRunner(probe_json()))
        d = self.root / "var/lib/orbis-graphics"
        d.mkdir(parents=True)
        (d / "last-qualify.json").write_text(rep.to_json())
        caps = cli.capabilities()
        self.assertEqual(caps["capabilities"]["vulkan.compute"]["state"], "PASS")
        self.assertEqual(caps["capabilities"]["power.clocks"]["state"], "UNSUPPORTED")
        self.assertEqual(caps["schema"], "orbis-gfx-capabilities/1")


if __name__ == "__main__":
    unittest.main()
