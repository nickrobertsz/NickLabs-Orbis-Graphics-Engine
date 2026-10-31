"""r3.1 audit P0/P2: fail-closed release verification, base-image target identity, manifest lifecycle,
fingerprint precision. Runs the real ./orbis-ge on a copy of the tree with a stand-in for the
image tool (ORBIS_GE_GRAPHICS_TOOL): no kernel, Mesa or image is built."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "build"))
import fingerprint  # noqa: E402


def release_test_copy_ignore(directory, names):
    """Copy source inputs, never mutable makepkg build/output trees.

    A global ``src`` exclusion would also discard legitimate source code.
    Keep the exclusion scoped to the two Mesa package recipe directories.
    """
    ignored = set(shutil.ignore_patterns(".git", "out", "target", "__pycache__")(directory, names))
    relative = Path(directory).relative_to(REPO)
    if relative in (Path("mesa/stable"), Path("mesa/lib32")):
        ignored.update(set(names).intersection({"src", "pkg"}))
        ignored.update(name for name in names if name.startswith("mesa-") and name.endswith(".tar.xz"))
    return ignored

FAKE_TOOL = textwrap.dedent('''\
    #!/usr/bin/env python3
    # stand-in for ./orbis-graphics: inspect | integrate | stage0 | verify
    import json, os, shutil, sys
    a = sys.argv[1:]
    def opt(k):
        return a[a.index(k) + 1] if k in a else None
    if a[0] == "inspect":
        img = opt("--image")
        side = img + ".fakeinspect.json"
        if os.path.exists(side):
            print(open(side).read()); sys.exit(0)
        st = "stage0" in os.path.basename(img)
        print(json.dumps({"profile": "cuh-1215a", "stage0": st, "kernel_sha256": "00",
                          "orbis_boot": {"initramfs": {"profile": "cuh-1215a", "firmware_groups": ["liverpool-gpu", "marvell-88w8897"]}}}))
    elif a[0] in ("integrate", "stage0"):
        if os.environ.get("FAKE_NO_OUTPUT"):
            sys.exit(0)                      # "succeeds" without producing the image
        src = opt("--base-image") or opt("--base-stage0-image")
        shutil.copy(src, opt("--output"))
        with open(opt("--output"), "ab") as f:
            f.write(b"GE")
    elif a[0] == "verify":
        print(json.dumps({"result": "PASS"}))
    ''')


class ReleaseGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.w = t / "ge"
        shutil.copytree(REPO, cls.w, ignore=release_test_copy_ignore)
        cls.src = t / "orbis020"
        (cls.src / "kernel").mkdir(parents=True)
        (cls.src / "kernel/build-kernel.sh").write_text("#!/bin/sh\n")
        (cls.src / "platform/orbis").mkdir(parents=True)
        cls.tool = t / "fake-orbis-graphics"
        cls.tool.write_text(FAKE_TOOL)
        cls.tool.chmod(0o755)
        cls.base = t / "orbis-0.2.0-cuh-1215a.img"
        cls.base.write_bytes(b"BASE-IMAGE" * 100)
        cls.base0 = t / "orbis-0.2.0-stage0-cuh-1215a.img"
        cls.base0.write_bytes(b"STAGE0" * 100)
        cls.env = {**os.environ, "ORBIS_GE_GRAPHICS_TOOL": str(cls.tool), "ORBIS_GE_RUN_ID": "run-1", "ENGINE": "false"}
        # a kernel and a pack "built" earlier, with correct provenance
        cls.make_output("kernel", "kernel-ge", cls.w / "out/kernel-ge/bzImage", b"KERNEL")
        (cls.w / "out/kernel-ge/build-manifest.json").write_text('{"kernel_release": "6.15.4-orbis-ps4-ge-gtest"}')
        pack = cls.w / "out/release/ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst"
        cls.make_output("pack", "pack", pack, b"PACK")
        subprocess.run(f"cd {pack.parent} && sha256sum {pack.name} > {pack.name}.sha256", shell=True, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def ge(cls, *args, env=None, check=None):
        r = subprocess.run([str(cls.w / "orbis-ge"), *args, "--orbis-src", str(cls.src)], cwd=cls.w,
                           capture_output=True, text=True, env=env or cls.env, timeout=600)
        if check is not None:
            assert r.returncode == check, (args, r.returncode, r.stdout[-2000:], r.stderr[-2000:])
        return r

    @classmethod
    def make_output(cls, step, artifact, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        fp = cls.ge("fingerprint", step, check=0).stdout.strip().splitlines()[-1]
        subprocess.run([sys.executable, str(cls.w / "build/provenance.py"), "write", artifact, str(path.parent), str(path),
                        fp, "test", "{}"], cwd=cls.w, check=True, capture_output=True)
        (cls.w / "out/.fp").mkdir(parents=True, exist_ok=True)
        (cls.w / "out/.fp" / step).write_text(fp + "\n")

    def img(self):
        return self.w / "out/targets/cuh-1215a/images/orbis-0.2.0+ge-2.0.0-cuh-1215a.img"

    def receipt(self):
        return json.loads((self.w / "out/targets/cuh-1215a/ORBIS-GE-RELEASE-RECEIPT.json").read_text())

    def image(self, *extra, env=None, check=0):
        return self.ge("image", "--target", "cuh-1215a", "--base-image", str(self.base), *extra, env=env, check=check)

    def verify(self, *extra, check=None):
        return self.ge("verify", "--target", "cuh-1215a", "--base-image", str(self.base), *extra, check=check)

    # the scenarios share one tree and run in order (unittest sorts by name)
    def test_01_valid_cached_image(self):
        self.image()
        r = self.image()
        self.assertIn("up to date", r.stdout)
        v = self.verify(check=0)
        rec = self.receipt()
        self.assertEqual(rec["result"], "COMPLETE")
        self.assertEqual(rec["stage0_image"]["status"], "omitted")          # intentionally omitted optional stage-0
        self.assertEqual(rec["base_target_check"]["image"]["decision"], "match")
        self.assertIn("orbis-0.2.0+ge-2.0.0-cuh-1215a.img", (self.img().parent / "SHA256SUMS").read_text())
        self.assertNotIn("Done", v.stdout)

    def test_02_image_modified_without_changing_inputs(self):
        with open(self.img(), "ab") as f:
            f.write(b"tampered")
        r = self.verify(check=7)
        self.assertIn("changed after it was built", r.stderr)
        rec = self.receipt()
        self.assertEqual(rec["result"], "FAILED")
        self.assertFalse(rec["image"].get("released", True))
        self.assertFalse((self.img().parent / "SHA256SUMS").exists())
        r = self.image()                                    # never reused: rebuilt
        self.assertIn("not reusable", r.stdout)
        self.verify(check=0)

    def test_03_missing_provenance(self):
        Path(str(self.img()) + ".provenance.json").unlink()
        r = self.verify(check=7)
        self.assertIn("provenance", r.stderr)
        self.assertIn("not reusable", self.image().stdout)
        self.verify(check=0)

    def test_04_incorrect_provenance(self):
        pf = Path(str(self.img()) + ".provenance.json")
        good = pf.read_text()
        d = json.loads(good)
        d["fingerprint"] = "0" * 64
        pf.write_text(json.dumps(d))
        self.assertIn("other inputs", self.verify(check=7).stderr)
        d = json.loads(good)
        d["artifact"] = "stage0-image"
        pf.write_text(json.dumps(d))
        self.verify(check=7)
        pf.write_text(good)
        self.verify(check=0)

    def test_05_image_missing_after_successful_step(self):
        r = self.image("--rebuild", env={**self.env, "FAKE_NO_OUTPUT": "1"}, check=7)
        self.assertIn("does not verify", r.stderr)
        self.assertFalse(self.img().exists())
        self.verify(check=7)
        self.assertEqual(self.receipt()["result"], "FAILED")
        self.image()

    def test_06_requested_stage0_invalid_and_valid(self):
        self.ge("stage0", "--target", "cuh-1215a", "--base-stage0-image", str(self.base0), check=0)
        self.verify("--base-stage0-image", str(self.base0), check=0)
        self.assertEqual(self.receipt()["stage0_image"]["status"], "verified")
        s0 = self.w / "out/targets/cuh-1215a/images/orbis-0.2.0-stage0+ge-2.0.0-cuh-1215a.img"
        s0.write_bytes(b"corrupt")
        self.verify("--base-stage0-image", str(self.base0), check=7)
        rec = self.receipt()
        self.assertEqual((rec["result"], rec["stage0_image"]["status"]), ("FAILED", "failed"))
        # not requested this time: the stale file is left out, the release is complete
        self.verify(check=0)
        self.assertEqual(self.receipt()["stage0_image"]["status"], "omitted")

    def test_07_base_image_target_identity(self):
        # the reference profile on a Pro target: a known mismatch
        r = self.ge("image", "--target", "cuh-70xx", "--base-image", str(self.base), check=8)
        self.assertIn("mismatch", r.stdout + r.stderr)
        self.assertEqual(list((self.w / "out/targets/cuh-70xx").glob("images/*.img")), [])   # rejected before integration
        side = Path(str(self.base) + ".fakeinspect.json")
        try:
            side.write_text(json.dumps({"stage0": False, "orbis_boot": {"initramfs": {}}}))       # no profile
            self.assertIn("missing", self.image(check=8).stdout)
            side.write_text(json.dumps({"profile": "cuh-1215a", "stage0": True,
                                        "orbis_boot": {"initramfs": {"profile": "cuh-1215a", "firmware_groups": ["liverpool-gpu", "marvell-88w8897"]}}}))
            self.assertIn("contradictory", self.image(check=8).stdout)                            # stage-0 given as base
            side.write_text(json.dumps({"profile": "my-custom", "stage0": False,
                                        "orbis_boot": {"initramfs": {"profile": "my-custom", "firmware_groups": []}}}))
            self.assertIn("unknown", self.image(check=8).stdout)
            r = self.image("--base-profile-override", "my-custom", check=0)                         # recorded operator decision
            self.assertIn("override", r.stdout)
        finally:
            side.unlink()
        self.image()
        self.verify(check=0)

    def test_08_manifest_separates_runs(self):
        env2 = {**self.env, "ORBIS_GE_RUN_ID": "run-2"}
        self.ge("verify", "--target", "cuh-1215a", "--base-image", str(self.base), env=env2, check=0)
        man = json.loads((self.w / "out/RELEASE-MANIFEST.json").read_text())
        self.assertEqual(man["current_run"]["run_id"], "run-2")
        self.assertEqual(list(man["current_run"]["targets"]), ["cuh-1215a"])
        self.assertEqual(man["history"][0]["run_id"], "run-1")


class FingerprintPrecisionTest(unittest.TestCase):
    def test_research_and_docs_do_not_invalidate_compiled_outputs(self):
        sets = {k: fingerprint.expand(v, str(REPO))[0] for k, v in fingerprint.SETS.items()}
        for name in ("kernel", "kernel-bore", "loader", "image"):
            for f in sets[name]:
                rel = os.path.relpath(f, REPO)
                self.assertFalse(rel.startswith(("research/", "docs/", "tests/")) or rel.endswith(".md"), (name, rel))
        kernel = {os.path.relpath(f, REPO) for f in sets["kernel"]}
        self.assertIn("kernel/patches/stable-6.15/0002-drm-ttm-Fix-ttm_bo_shrink-infinite-LRU-walk-on-backu.patch", kernel)
        self.assertFalse(any(p.startswith("kernel/patches/research/") for p in kernel))
        pack = {os.path.relpath(f, REPO) for f in sets["pack"]}
        self.assertFalse(any(p.startswith(("research/", "tests/")) for p in pack))

    def test_package_cache_keys_cover_every_path_a_recipe_reads(self):
        """Stale-reuse guard: each mkc key must include every $R/... path its PKGBUILD reads."""
        import re
        icb = (REPO / "build/in-container-build.sh").read_text().replace("\\\n", " ")
        keys = {}
        for m in re.finditer(r"^\s*mkc (\S+) (\S+) \S+(.*)$", icb, re.M):
            keys[m.group(1)] = re.findall(r'"\$R/([^"]+)"', m.group(3))
        self.assertIn("packages/orbis-graphics-docs", keys)
        self.assertNotIn("docs", keys["packages/orbis-graphics"])
        for recipe, inputs in keys.items():
            pk = REPO / recipe / "PKGBUILD"
            if not pk.exists():
                continue
            for used in set(re.findall(r'\$R/?"?/?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)', pk.read_text())):
                used = used.rstrip("/")
                self.assertTrue(any(used == i or used.startswith(i.rstrip("/") + "/") for i in inputs),
                                f"{recipe}: reads $R/{used} but its cache key has {inputs}")


if __name__ == "__main__":
    unittest.main()
