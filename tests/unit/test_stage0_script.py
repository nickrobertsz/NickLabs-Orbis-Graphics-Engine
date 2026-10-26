"""diag/stage0/overlay/bin/orbis-gfx-stage0 under a POSIX shell with a fixture /sys."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gfx_fixtures import ps4_root  # noqa: E402

SCRIPT = REPO / "diag/stage0/overlay/bin/orbis-gfx-stage0"


class Stage0ScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.root = self.d / "root"
        self.out = self.d / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, cmdline="orbis.stage0=1 orbis.gfxdiag=1", miniroot=None, **tree):
        kf = ps4_root(self.root, **tree)
        (self.root / "proc/cmdline").write_text(cmdline + "\n")
        (self.root / "proc/uptime").write_text("12.34 5.00\n")
        env = dict(os.environ, ORBIS_GFX_ROOT=str(self.root), ORBIS_GFX_KMSG_FILE=str(kf), ORBIS_GFX_OUT_DIR=str(self.out))
        if miniroot:
            env["ORBIS_GFX_MINIROOT"] = str(miniroot)
        r = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def report(self, n="001"):
        return json.loads((self.out / f"stage0-{n}-gpu.json").read_text())

    def test_without_miniroot_kernel_only_report(self):
        self.run_script()
        rep = self.report()
        st = {c["id"]: c["state"] for c in rep["checks"]}
        self.assertEqual(st["gpu.identity"], "PASS")
        self.assertEqual(st["gpu.driver"], "PASS")
        self.assertEqual(st["gpu.firmware"], "PASS")
        self.assertEqual(st["gpu.rings"], "PASS")
        self.assertEqual(st["display.connector"], "PASS")
        self.assertEqual(st["gl.renderer"], "NOT_RUN")
        self.assertEqual(rep["overall"], "DEGRADED")    # required GPU userspace checks not run
        k = (self.out / "stage0-001-gpu-kernel.txt").read_text()
        for needle in ("0x1002:0x9920", "mem_info_vram_total", "HDMI-A-1: status=connected", "MEC2", "rings:"):
            self.assertIn(needle, k)
        prog = (self.out / "stage0-001-gpu-progress.txt").read_text()
        self.assertIn("no diagnostic mini-root", prog)
        self.assertTrue(prog.strip().endswith("done"))

    def test_numbering_follows_existing_logs(self):
        self.out.mkdir(parents=True)
        (self.out / "stage0-001.txt").write_text("from 0.2.0\n")
        self.run_script()
        self.assertTrue((self.out / "stage0-002-gpu.json").exists())

    def test_driver_missing_blocks_userspace(self):
        self.run_script(driver="radeon", kmsg="amdgpu: Direct firmware load for amdgpu/liverpool_me.bin failed\n")
        st = {c["id"]: c for c in self.report()["checks"]}
        self.assertEqual(st["gpu.driver"]["state"], "FAIL")
        self.assertEqual(st["gpu.firmware"]["state"], "FAIL")
        self.assertEqual(st["vk.compute"]["state"], "BLOCKED")
        self.assertIn("amdgpu is not bound", st["vk.compute"]["blocker"])
        self.assertEqual(self.report()["overall"], "FAIL")

    def _miniroot(self, body: str) -> Path:
        mr = self.d / "mr"
        mr.mkdir()
        (mr / "MINIROOT").write_text("orbis-gfxdiag 1.0.0 test\n")
        rq = mr / "run-qualify"
        rq.write_text("#!/bin/sh\n" + body)
        rq.chmod(0o755)
        return mr

    def test_miniroot_report_is_used(self):
        mr = self._miniroot('printf \'{"schema":"orbis-gfx-report/1","overall":"PASS","checks":[]}\' > "$1/$2-gpu.json"\n'
                            'echo text > "$1/$2-gpu.txt"\n')
        self.run_script(miniroot=mr)
        self.assertEqual(self.report()["overall"], "PASS")
        self.assertIn("orbis-gpu qualify exit 0", (self.out / "stage0-001-gpu-progress.txt").read_text())

    def test_hung_miniroot_is_killed_and_reported_as_fail(self):
        mr = self._miniroot("sleep 30\n")
        self.run_script(cmdline="orbis.stage0=1 orbis.gfxdiag=1 orbis.gfxdiag.timeout=2", miniroot=mr)
        rep = self.report()
        st = {c["id"]: c for c in rep["checks"]}
        self.assertEqual(st["gl.renderer"]["state"], "FAIL")
        self.assertIn("killed", st["gl.renderer"]["detail"])
        self.assertEqual(rep["overall"], "FAIL")
        self.assertIn("KILLED after 2s", (self.out / "stage0-001-gpu-progress.txt").read_text())


if __name__ == "__main__":
    unittest.main()
