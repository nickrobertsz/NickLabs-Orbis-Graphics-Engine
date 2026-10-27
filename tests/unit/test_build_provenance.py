"""GE-A05 / GE-A06: the 0.2.0 checkout guard and explicit artifact selection (host-only, no builds)."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prov = _load("provenance", "build/provenance.py")
guard = _load("orbis_src_guard", "build/orbis_src_guard.py")


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        for d in ("out/kernel-ge", "out/kernel-ge-bore", "kernel/patches", "kernel/config", "manifest"):
            Path(d).mkdir(parents=True)
        Path("kernel/patches/series").write_text("ge-6.15/0001.patch\n")
        Path("manifest/sources.json").write_text("{}")
        Path("out/kernel-ge/bzImage").write_bytes(b"kernel A")
        Path("out/kernel-ge-bore/bzImage").write_bytes(b"old bore kernel")
        with redirect_stdout(io.StringIO()):
            prov.write("kernel-ge", "out/kernel-ge", "out/kernel-ge/bzImage", "fpA", "cuh-1215a", {})
            prov.write("kernel-ge-bore", "out/kernel-ge-bore", "out/kernel-ge-bore/bzImage", "fpOLD", "cuh-1215a", {})

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def sel(self, *specs):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = prov.select("sel.json", "cuh-1215a", {}, list(specs))
        return rc, json.loads(Path("sel.json").read_text())

    def test_unrequested_old_bore_kernel_never_slips_in(self):
        rc, s = self.sel("kernel-ge=out/kernel-ge:out/kernel-ge/bzImage:fpA:required",
                         "kernel-ge-bore=out/kernel-ge-bore:out/kernel-ge-bore/bzImage:fpNEW:not-requested")
        self.assertEqual(rc, 0)
        self.assertTrue(s["artifacts"]["kernel-ge"]["selected"])
        self.assertFalse(s["artifacts"]["kernel-ge-bore"]["selected"])
        self.assertIn("not requested", s["artifacts"]["kernel-ge-bore"]["detail"])
        self.assertEqual(s["artifacts"]["kernel-ge"]["provenance"]["sha256"], prov.sha256("out/kernel-ge/bzImage"))

    def test_requested_but_stale_artifact_fails(self):
        rc, s = self.sel("kernel-ge=out/kernel-ge:out/kernel-ge/bzImage:fpA:required",
                         "kernel-ge-bore=out/kernel-ge-bore:out/kernel-ge-bore/bzImage:fpNEW:requested")
        self.assertEqual(rc, 1)
        self.assertFalse(s["ok"])
        self.assertIn("other inputs", s["errors"][0])

    def test_replaced_file_is_detected(self):
        Path("out/kernel-ge/bzImage").write_bytes(b"kernel B (copied in by hand)")
        rc, s = self.sel("kernel-ge=out/kernel-ge:out/kernel-ge/bzImage:fpA:required")
        self.assertEqual(rc, 1)
        self.assertIn("changed after it was built", s["errors"][0])

    def test_missing_provenance_is_not_current(self):
        Path("out/kernel-ge/bzImage.provenance.json").unlink()
        ok, why = prov.check("kernel-ge", "out/kernel-ge", "out/kernel-ge/bzImage", "fpA")
        self.assertFalse(ok)
        self.assertIn("rebuild", why)

    def test_provenance_records_inputs(self):
        rec = json.loads(Path("out/kernel-ge/bzImage.provenance.json").read_text())
        for k in ("target", "source_revision", "patch_set_sha256", "config_sha256", "toolchain", "options", "fingerprint", "sha256"):
            self.assertIn(k, rec)
        before = rec["patch_set_sha256"]
        Path("kernel/patches/series").write_text("ge-6.15/0001.patch\nge-6.15/0002.patch\n")
        self.assertNotEqual(prov.patch_set_hash(), before)


class GuardTest(unittest.TestCase):
    def test_detects_any_write_into_the_checkout(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "orbis"
            (src / "platform/orbis").mkdir(parents=True)
            (src / "platform/orbis/__init__.py").write_text("__version__ = '0.2.0'\n")
            (src / "out/keys/gnupg").mkdir(parents=True)
            (src / "out/keys/orbis-repo-key.fingerprint").write_text("ABC\n")
            state = str(Path(t) / "g.json")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(guard.main(["g", "snapshot", str(src), state]), 0)
                self.assertEqual(guard.main(["g", "verify", str(src), state]), 0)
            # what gpg would do with a read-write key mount
            (src / "out/keys/gnupg/random_seed").write_bytes(b"x")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(guard.main(["g", "verify", str(src), state]), 9)
            self.assertIn("added: out/keys/gnupg/random_seed", err.getvalue())

    def test_build_pack_never_writes_into_the_checkout(self):
        """Static check of build-pack.sh: no make -C on the original, keys mounted from a private copy."""
        s = (ROOT / "build/build-pack.sh").read_text()
        self.assertNotIn('make -C "$orbis_src"', s)
        self.assertNotIn('"$orbis_src/out/keys":/orbis/keys:z', s)
        self.assertIn('-v "$priv/keys":/orbis/keys:z', s)
        for line in s.splitlines():
            if "-v \"$orbis_src" in line:
                self.assertIn(":ro", line, line)
        self.assertIn("orbis_src_guard.py verify", s)


class OrbisGeSelectionWiringTest(unittest.TestCase):
    def test_pack_step_selects_before_building(self):
        s = (ROOT / "orbis-ge").read_text()
        body = s[s.index("step_pack() {"):s.index("step_image() {")]
        self.assertLess(body.index("select_artifacts"), body.index("build/build-pack.sh"))
        ic = (ROOT / "build/in-container-build.sh").read_text()
        self.assertIn('if [ "$bore" = 1 ] && [ -f /build/src/out/kernel-ge-bore/build-manifest.json ]', ic)
        self.assertNotIn('if [ -f "$R/out/kernel-ge-bore/build-manifest.json" ]; then', ic)
        r = subprocess.run(["bash", "-n", str(ROOT / "orbis-ge")], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
