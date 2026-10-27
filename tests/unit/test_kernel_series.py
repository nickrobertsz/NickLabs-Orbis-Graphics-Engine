"""Kernel series consistency (r3.1): stable backports first, every patch present, ge-0017 replaces the
runtime write of ttm_resource_manager.size and fixes the teardown order. Apply checks against the
pinned tree run in the authoring environment (see the engineering report), not here."""
import re
import unittest
from pathlib import Path

P = Path(__file__).resolve().parents[2] / "kernel/patches"


def series():
    return [l.strip() for l in (P / "series").read_text().splitlines() if l.strip() and not l.startswith("#")]


class SeriesTest(unittest.TestCase):
    def test_order_and_presence(self):
        s = series()
        self.assertTrue(s[0].startswith("stable-6.15/0001") and s[1].startswith("stable-6.15/0002"), s[:2])
        ge = [x for x in s if x.startswith("ge-6.15/")]
        self.assertEqual([int(Path(x).name[:4]) for x in ge], list(range(1, 18)))
        for x in s:
            self.assertTrue((P / x).is_file(), x)
        self.assertEqual(len(s), len(set(Path(x).name for x in s)))

    def test_backports_keep_provenance(self):
        txt = (P / series()[1]).read_text()
        for k in ("1d59f36e95f7", "d0ce7b07dafe", "CVE-2026-52949"):
            self.assertIn(k, txt)

    def test_ge0017_budget_and_teardown(self):
        t = (P / [x for x in series() if Path(x).name.startswith("0017")][0]).read_text()
        self.assertIn("-\tWRITE_ONCE(man->size, val);", t)
        self.assertNotIn("+\tWRITE_ONCE(man->size", t)
        self.assertIn("+\tatomic64_set(&mgr->orbis_budget, val);", t)
        self.assertIn("amdgpu_gtt_mgr_limit(mgr)", t)
        # mutex destroyed last: the removed early destroy and the new one after the out: label
        self.assertRegex(t, r"-\tmutex_destroy\(&mgr->orbis_budget_lock\);\n \n \tret = ttm_resource_manager_evict_all")
        self.assertRegex(t, re.compile(r"\+out:\n\+\tmgr->orbis_budget_active = false;\n\+\tmutex_destroy", re.M))
        self.assertIn("drm_dev_enter(adev_to_drm(adev), &idx)", t)


if __name__ == "__main__":
    unittest.main()
