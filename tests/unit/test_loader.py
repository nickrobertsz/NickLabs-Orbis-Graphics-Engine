"""The Graphics Engine loader patch: VRAM MTRR split and cmdline composition, compiled on the host
from the exact text inside loader/0001-*.patch (no copy of the code is kept elsewhere)."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATCH = next((REPO / "loader").glob("0001-*.patch"))
CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def new_file(patch: str, path: str) -> str:
    m = re.search(r"^\+\+\+ b/" + re.escape(path) + r"\n@@ [^\n]*\n(.*?)(?=^diff --git |\Z)", patch, re.S | re.M)
    assert m, path
    return "".join(l[1:] + "\n" for l in m.group(1).splitlines() if l.startswith("+"))


def added_block(patch: str, start: str, last_function: str) -> str:
    """Added lines from `start` through the end of `last_function` (its closing brace)."""
    lines = [l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
    text = "\n".join(lines)
    i = text.index(start)
    j = text.index("\n}\n", text.index(last_function, i)) + 3
    return text[i:j]


@unittest.skipUnless(CC, "no C compiler")
class LoaderPatchTest(unittest.TestCase):
    def setUp(self):
        self.patch = PATCH.read_text()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build_run(self, src, *extra):
        exe = self.d / "t"
        r = subprocess.run([CC, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(self.d), *extra,
                            "-o", str(exe), str(src)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_vram_split(self):
        (self.d / "orbis_vram.h").write_text(new_file(self.patch, "linux/ps4-kexec-common/orbis_vram.h"))
        out = self.build_run(REPO / "tests/loader/test_orbis_vram.c")
        self.assertIn("OK", out)

    def test_cmdline_compose(self):
        block = added_block(self.patch, "/* NickLabs Orbis: append", "static int orbis_cmdline_compose")
        (self.d / "extracted_cmdline.inc").write_text(block + "\n")
        out = self.build_run(REPO / "tests/loader/test_cmdline.c", "-Wno-unused-function")
        self.assertIn("cmdline OK", out)

    def test_patch_targets_pinned_loader(self):
        src = (REPO / "manifest/sources.json").read_text()
        self.assertIn("d0f4d4c0bbc9e8b4870300781b352605ed2bc91d", src)
        self.assertIn("orbis_vram_split", self.patch)
        self.assertIn("orbis_vram_valid(vram_mb)", self.patch)


if __name__ == "__main__":
    unittest.main()
