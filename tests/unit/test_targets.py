"""Porting Kit: registry, safe probe, deterministic matching, qualification and PortForge scenarios.

Scenarios (Part C): CUH-1215A reference, simulated Belize Slim, simulated Baikal Slim, unknown
hardware, conflicting identifiers, missing patch (apply-check fails), stale artifact (candidate
changed after review), plus the unreviewed-change and assistant-provenance rules.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "diag"))

from orbis_targets import compat, match, portforge, probe  # noqa: E402
from orbis_targets.cli import hw_main, target_main  # noqa: E402
from orbis_targets.registry import Registry, generated_path  # noqa: E402

BELIZE = ["0x90c8", "0x90c9", "0x90ca", "0x90cb", "0x90cc", "0x90cd", "0x90ce", "0x90cf"]
BAIKAL = ["0x90d7", "0x90d8", "0x90d9", "0x90da", "0x90db", "0x90dc", "0x90dd", "0x90de"]


def probe_doc(gpu=None, sb=(), model=None, logs=(), drivers=True):
    pci = []
    if gpu:
        pci.append({"slot": "0000:00:01.0", "vendor": "0x1002", "device": gpu, "class": "0x030000", "driver": "amdgpu" if drivers else None})
    for i, d in enumerate(sb):
        pci.append({"slot": f"0000:00:14.{i}", "vendor": "0x104d", "device": d, "class": "0x088000", "driver": "ps4_apcie" if drivers else None})
    return {"schema": "orbis-hw-probe/1", "pci": pci, "cpu": {"family": "22"}, "gpu": {}, "logs": list(logs),
            "hints": {"model": model}}


def run_quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*a)
    return rc, out.getvalue(), err.getvalue()


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.reg = Registry()

    def test_registry_valid_generated_and_honest(self):
        self.assertEqual(self.reg.reference(), "cuh-1215a")
        rc, out, err = run_quiet(target_main, ["check"])
        self.assertEqual(rc, 0, err)
        fams = {self.reg.resolve(t)["family"] for t in self.reg.targets()}
        self.assertEqual(fams, {"fat", "slim", "pro"})
        for t in self.reg.targets():
            e = self.reg.resolve(t)
            for cap, c in e["capabilities"].items():
                self.assertIn(c["state"], ("verified", "awaiting-hardware", "unsupported"), (t, cap))
                self.assertNotEqual(c["state"], "verified", f"{t}.{cap}: nothing has run on a PS4 yet")
                if c["state"] == "unsupported":
                    self.assertTrue(c.get("reason"), (t, cap))
            if e["southbridge"] == "baikal":
                self.assertEqual(e["status"], "unsupported")
                self.assertIn("5.4.247", e["kernel"]["reason"] + json.dumps(e["southbridge_info"]))
        self.assertTrue(generated_path().exists())

    def test_belize2_and_baikal_boards_per_chassis(self):
        # psdevwiki: chassis 2100/2200/7100/7200 exist with Belize 2 and with Baikal boards
        for fam in ("21xx", "22xx", "71xx", "72xx"):
            self.assertIn(f"cuh-{fam}-baikal", self.reg.targets())
            self.assertIn(f"cuh-{fam}-belize2", self.reg.targets())

    def test_schema_rejects_bad_states(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "targets"
            shutil.copytree(REPO / "targets", root)
            bad = json.loads((root / "models/cuh-12xx.json").read_text())
            bad["capabilities"] = {"gpu-3d": {"state": "verified"}}          # verified without evidence
            (root / "models/cuh-12xx.json").write_text(json.dumps(bad))
            with self.assertRaises(Exception) as cm:
                Registry(root)
            self.assertIn("verified needs evidence", str(cm.exception))


class ProbeTest(unittest.TestCase):
    def test_probe_is_read_only_by_construction(self):
        src = (REPO / "diag/orbis_targets/probe.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""', "*")) and "never" not in l)
        for forbidden in ("/dev/mem", "/resource", "mmap", "ioperm", "open(\"/dev", "write_text", ".write("):
            self.assertNotIn(forbidden, code, forbidden)

    def test_probe_on_fixture_and_redaction(self):
        with tempfile.TemporaryDirectory() as t:
            r = Path(t)
            for slot, ven, dev, cls in (("0000:00:01.0", "0x1002", "0x9922", "0x030000"), ("0000:00:14.4", "0x104d", "0x90cc", "0x060400")):
                d = r / "sys/bus/pci/devices" / slot
                d.mkdir(parents=True)
                for k, v in (("vendor", ven), ("device", dev), ("class", cls)):
                    (d / k).write_text(v + "\n")
            (r / "proc").mkdir()
            (r / "proc/cpuinfo").write_text("processor\t: 0\ncpu family\t: 22\nmodel name\t: AMD Jaguar\nflags\t: sse4_2 avx\n\n")
            os.environ["ORBIS_GFX_ROOT"] = str(r)
            try:
                log = "Linux version 6.15.4-orbis\nconsole serial: AB123456789012 mac 00:11:22:33:44:55 CUH-1215A board SAC-001 belize\n"
                doc = probe.probe(logs=[("serial.log", log)])
            finally:
                os.environ.pop("ORBIS_GFX_ROOT")
            txt = json.dumps(doc)
            self.assertNotIn("AB123456789012", txt)
            self.assertNotIn("00:11:22:33:44:55", txt)
            f = probe.facts(doc)
            self.assertEqual(f["gpu_pci_device"], "0x9922")
            self.assertIn("SAC-001", f["motherboards"])
            self.assertTrue(f["cpu_jaguar"])


class MatchScenarios(unittest.TestCase):
    def setUp(self):
        self.reg = Registry()

    def m(self, **kw):
        return match.match(self.reg, probe.facts(probe_doc(**kw)))

    def test_reference_1215a_confirmed(self):
        r = self.m(gpu="0x9922", sb=BELIZE, model="CUH-1215A")
        self.assertEqual((r["result"], r["target"]), ("confirmed", "cuh-1215a"))

    def test_fat_12xx_without_model_is_the_family_not_the_reference(self):
        r = self.m(gpu="0x9922", sb=BELIZE)
        self.assertEqual((r["result"], r["target"]), ("confirmed", "cuh-12xx"))   # never silently cuh-1215a

    def test_simulated_belize_slim(self):
        r = self.m(gpu="0x9923", sb=BELIZE, model="CUH-2016A")
        self.assertEqual((r["result"], r["target"]), ("confirmed", "cuh-20xx-belize"))
        self.assertEqual(r["status"], "awaiting-hardware")

    def test_simulated_baikal_slim(self):
        r = self.m(gpu="0x9923", sb=BAIKAL, model="CUH-2215B")
        self.assertEqual((r["result"], r["target"]), ("confirmed", "cuh-22xx-baikal"))
        self.assertEqual(r["status"], "unsupported")

    def test_model_alone(self):
        self.assertEqual(self.m(model="CUH-1215A")["result"], "probable")
        r = self.m(model="CUH-2215B")      # Belize 2 or Baikal board
        self.assertEqual(r["result"], "unknown")
        self.assertEqual(sorted(r["candidates"]), ["cuh-22xx-baikal", "cuh-22xx-belize2"])
        self.assertIn("southbridge", r["needed_evidence"][0])

    def test_subsys_id_separates_belize2(self):
        log = probe.import_log("hw.sce_subsys_subid = 0x40100\n", "orbisos.txt")
        r = self.m(gpu="0x9923", model="CUH-2215B", logs=[log])
        self.assertEqual((r["result"], r["target"]), ("confirmed", "cuh-22xx-belize2"))

    def test_unknown_hardware(self):
        r = self.m(gpu="0x1234", sb=["0x9999"])
        self.assertEqual(r["result"], "unknown")
        self.assertIn("0x9999", r["unrecognized"])
        self.assertEqual(self.m()["result"], "unknown")

    def test_conflicting_identifiers(self):
        r = self.m(gpu="0x9923", sb=BELIZE, model="CUH-1215A")
        self.assertEqual(r["result"], "conflicting")
        r2 = self.m(gpu="0x9922", sb=BAIKAL)
        self.assertEqual(r2["result"], "conflicting")

    def test_identify_exit_codes(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "p.json"
            f.write_text(json.dumps(probe_doc(gpu="0x9923", sb=BELIZE, model="CUH-1215A")))
            rc, out, _ = run_quiet(hw_main, "identify", ["--probe", str(f), "--json"])
            self.assertEqual((rc, json.loads(out)["result"]), (5, "conflicting"))
            f.write_text(json.dumps(probe_doc(model="CUH-2215B")))
            rc, _, err = run_quiet(hw_main, "compatibility", ["--probe", str(f)])
            self.assertEqual(rc, 4)
            self.assertIn("no target is assumed", err)


class QualifyTest(unittest.TestCase):
    def test_qualify_never_applies_verified(self):
        reg = Registry()
        with tempfile.TemporaryDirectory() as t:
            os.environ["ORBIS_GFX_ROOT"] = t
            try:
                q = compat.qualify(reg, "cuh-1215a", probe_doc(gpu="0x9922", sb=BELIZE, model="CUH-1215A"))
                blocked = compat.qualify(reg, "cuh-1215a", probe_doc(gpu="0x9923", sb=BELIZE))
            finally:
                os.environ.pop("ORBIS_GFX_ROOT")
        self.assertEqual(q["checks"]["southbridge-io"]["result"], "PASS")
        self.assertEqual(q["checks"]["display-hdmi"]["result"], "NOT_RUN")
        self.assertIn("southbridge-io", q["registry_patch"])
        self.assertTrue(q["review_required"])
        self.assertEqual(q["result"], "INCOMPLETE")
        self.assertEqual(Registry().resolve("cuh-1215a")["capabilities"]["southbridge-io"]["state"], "awaiting-hardware")
        self.assertEqual(blocked["result"], "BLOCKED")


class PortForgeScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        os.environ["ORBIS_PORTFORGE_DIR"] = str(self.t / "pf")
        self.targets = self.t / "targets"
        shutil.copytree(REPO / "targets", self.targets)
        os.environ["ORBIS_TARGETS_DIR"] = str(self.targets)

    def tearDown(self):
        os.environ.pop("ORBIS_PORTFORGE_DIR", None)
        os.environ.pop("ORBIS_TARGETS_DIR", None)
        self.tmp.cleanup()

    def pf(self, *argv):
        return run_quiet(portforge.main, list(argv))

    def new(self, tid, doc):
        p = self.t / f"{tid}.json"
        p.write_text(json.dumps(doc))
        rc, out, err = self.pf("new", tid, "--probe", str(p))
        self.assertEqual(rc, 0, err)
        return Path(os.environ["ORBIS_PORTFORGE_DIR"]) / tid

    def kernel_tree(self):
        k = self.t / "ktree"
        (k / "drivers/ps4").mkdir(parents=True)
        (k / "drivers/ps4/ps4-apcie.c").write_text("static int a;\n")
        (k / "Makefile").write_text("# fake\n")
        subprocess.run(["git", "init", "-q", str(k)], check=True)
        subprocess.run(["git", "-C", str(k), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(k), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], check=True)
        return k

    def test_baikal_slim_gap_report(self):
        ws = self.new("cuh-2215b-nicklabs", probe_doc(gpu="0x9923", sb=BAIKAL, model="CUH-2215B"))
        gr = json.loads((ws / "gap-report.json").read_text())
        self.assertEqual(gr["southbridge"], "baikal")
        self.assertTrue(any(g["layer"] == "kernel" for g in gr["gaps"]))
        self.assertEqual(gr["capabilities_if_ported"]["gpu-3d"], "unsupported")
        self.assertTrue(any("5.4.247" in json.dumps(k) for k in gr["known"]))

    def test_unknown_hardware_new_target(self):
        ws = self.new("mystery-board", probe_doc(gpu="0x99ff", sb=["0x90f0"]))
        gr = json.loads((ws / "gap-report.json").read_text())
        layers = {g["layer"] for g in gr["gaps"]}
        self.assertTrue({"apu", "southbridge"} <= layers, layers)

    def test_1215a_and_belize_slim_have_no_kernel_gap(self):
        for tid, doc in (("ref-copy", probe_doc(gpu="0x9922", sb=BELIZE, model="CUH-1215A")),
                         ("belize-slim", probe_doc(gpu="0x9923", sb=BELIZE, model="CUH-2016A"))):
            gr = json.loads((self.new(tid, doc) / "gap-report.json").read_text())
            self.assertFalse([g for g in gr["gaps"] if g["layer"] == "kernel"], gr)

    def test_missing_patch_and_review_rules(self):
        ws = self.new("baikal-port", probe_doc(gpu="0x9923", sb=BAIKAL, model="CUH-2215B"))
        k = self.kernel_tree()
        good = self.t / "good.patch"
        good.write_text("--- a/drivers/ps4/ps4-apcie.c\n+++ b/drivers/ps4/ps4-apcie.c\n@@ -1 +1,2 @@\n static int a;\n+static int b;\n")
        missing = self.t / "missing.patch"   # against a file the tree does not have: the patch is missing its base
        missing.write_text("--- a/drivers/ps4/baikal-icc.c\n+++ b/drivers/ps4/baikal-icc.c\n@@ -1 +1,2 @@\n int x;\n+int y;\n")
        self.assertEqual(self.pf("candidate", "baikal-port", "--patch", str(good), "--origin", "community",
                                 "--source", "sony-jaguar-devs/ps4-linux@5.4.247-baikal-dfaus:drivers/ps4")[0], 0)
        self.assertEqual(self.pf("candidate", "baikal-port", "--patch", str(missing), "--origin", "manual", "--source", "test")[0], 0)
        rc, out, _ = self.pf("check", "baikal-port", "--kernel-tree", str(k))
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["apply_failed"], ["c002"])
        # unreviewed: recipe refuses, promote refuses
        self.pf("report", "baikal-port")
        r = subprocess.run(["bash", str(ws / "recipe.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("not approved", r.stderr)
        self.assertEqual(self.pf("promote", "baikal-port")[0], 3)
        # review needs a reviewer; approve c001, reject c002
        self.assertEqual(self.pf("review", "baikal-port", "c001", "--approve")[0], 2)
        self.assertEqual(self.pf("review", "baikal-port", "c001", "--approve", "--reviewer", "Nick")[0], 0)
        self.assertEqual(self.pf("review", "baikal-port", "c002", "--reject", "--reviewer", "Nick")[0], 0)
        self.assertEqual(self.pf("promote", "baikal-port")[0], 3)       # rejected is not approved

    def test_stale_artifact_candidate_changed_after_review(self):
        ws = self.new("belize-slim2", probe_doc(gpu="0x9923", sb=BELIZE, model="CUH-2016A"))
        p = self.t / "x.patch"
        p.write_text("--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+x\n")
        self.pf("candidate", "belize-slim2", "--patch", str(p), "--origin", "manual", "--source", "test")
        self.pf("review", "belize-slim2", "c001", "--approve", "--reviewer", "Nick")
        self.pf("report", "belize-slim2")
        (ws / "candidates/c001.patch").write_text("--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+evil\n")   # tampered after review
        r = subprocess.run(["bash", str(ws / "recipe.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("changed after review", r.stderr)
        rc, _, err = self.pf("review", "belize-slim2", "c001", "--approve", "--reviewer", "Nick")
        self.assertEqual(rc, 1)
        self.assertIn("changed after it was recorded", err)

    def test_assistant_output_is_an_unreviewed_candidate_with_provenance(self):
        ws = self.new("assist-test", probe_doc(gpu="0x9923", sb=BAIKAL, model="CUH-2215B"))
        fake = self.t / "fake-assistant.py"
        fake.write_text("import sys\nsys.stdin.read()\nprint('--- a/f\\n+++ b/f\\n@@ -0,0 +1 @@\\n+y')\n")
        rc, out, err = self.pf("assist", "assist-test", "--assistant", f"cmd:{sys.executable} {fake}")
        self.assertEqual(rc, 0, err)
        c = json.loads((ws / "candidates/c001.json").read_text())
        self.assertEqual((c["origin"], c["review"]["state"], c["may_run_on_ps4"]), ("assistant", "unreviewed", False))
        self.assertEqual(len(c["provenance"]["prompt_sha256"]), 64)
        self.assertEqual(self.pf("assist", "assist-test", "--assistant", "openai")[0], 2)

    def test_diff_trees_includes_new_files(self):
        """Regression: files that exist only in the community tree must become new-file hunks."""
        self.new("tree-diff", probe_doc(gpu="0x9923", sb=BAIKAL, model="CUH-2215B"))
        k = self.kernel_tree()
        other = self.t / "community"
        (other / "drivers/ps4").mkdir(parents=True)
        (other / "drivers/ps4/ps4-apcie.c").write_text("static int a;\n")
        (other / "drivers/ps4/ps4-bpcie.c").write_text("int bpcie_status(void) { return 0; }\n")
        rc, out, err = self.pf("diff-trees", "tree-diff", "--base", str(k), "--other", str(other),
                               "--paths", "drivers/ps4/ps4-bpcie.c,drivers/ps4/ps4-apcie.c", "--source", "test@x")
        self.assertEqual(rc, 0, err)
        patch = (Path(os.environ["ORBIS_PORTFORGE_DIR"]) / "tree-diff/candidates/c001.patch").read_text()
        self.assertIn("new file mode", patch)
        self.assertIn("+++ b/drivers/ps4/ps4-bpcie.c", patch)
        self.assertIn("diff --git a/drivers/ps4/ps4-bpcie.c b/drivers/ps4/ps4-bpcie.c", patch)
        rc, out, _ = self.pf("check", "tree-diff", "--kernel-tree", str(k))
        self.assertEqual((rc, json.loads(out)["apply_failed"]), (0, []))

    def test_promote_after_full_review(self):
        self.new("cuh-2016a-lab", probe_doc(gpu="0x9923", sb=BELIZE, model="CUH-2016A"))
        rc, out, err = self.pf("promote", "cuh-2016a-lab")
        self.assertEqual(rc, 0, err)
        reg = Registry(self.targets)
        self.assertIn("cuh-2016a-lab", reg.targets())
        self.assertEqual(reg.resolve("cuh-2016a-lab")["status"], "awaiting-hardware")


class OrbisGeTargetCommands(unittest.TestCase):
    def test_port_probe_and_unsupported_build(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "p.json"
            f.write_text(json.dumps(probe_doc(gpu="0x9922", sb=BELIZE, model="CUH-1215A")))
            r = subprocess.run([str(REPO / "orbis-ge"), "port", "probe", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("confirmed", r.stdout)
            self.assertIn("cuh-1215a", r.stdout)
        r = subprocess.run([str(REPO / "orbis-ge"), "status", "--target", "cuh-22xx-baikal"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 6)
        self.assertIn("unsupported", r.stderr)


if __name__ == "__main__":
    unittest.main()
