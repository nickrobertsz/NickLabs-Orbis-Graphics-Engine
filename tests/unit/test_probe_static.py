"""r3.1 P1: static checks of the zero-copy synchronization order in orbis-gfx-probe.c (the probe is
compiled on the build host but cannot run without a Vulkan driver)."""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "diag/probe/orbis-gfx-probe.c").read_text()


def zero_copy_body():
    i = SRC.index("static enum mb_res zero_copy(")
    return SRC[i:SRC.index("\nstatic bool host_import_supported", i)]


class ZeroCopySyncOrder(unittest.TestCase):
    def test_host_pointer_untouched_until_type_is_known(self):
        b = zero_copy_body()
        imp = b.index("mkbuf_type(c, &h, size, -1, host")
        first_touch = min(b.index("memset(host"), b.index("const volatile uint32_t *q = host"))
        hv = b.index("VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT")
        self.assertLess(imp, hv)
        self.assertLess(hv, first_touch, "host memory written before the import type was checked")
        self.assertNotIn("memset(host", b[:imp], "initial fill before the import leaves dirty lines")

    def test_flush_and_invalidate_points(self):
        b = zero_copy_body()
        fill = b.index("memset(host, 0xFF, size)")
        self.assertIn("host_flush(c, &h, type)", b[fill:fill + 300])
        gpu_write = b.index("gpu_dispatch(c, pw")
        read = b.index("const volatile uint32_t *q = host")
        self.assertIn("host_invalidate(c, &h, type)", b[gpu_write:read])
        cpu_write = b.index("w[i] = xs32(&sd)")
        submit = b.index("cb_submit(c, cb, &trd)")
        self.assertIn("host_flush(c, &h, type)", b[cpu_write:submit])

    def test_alias_or_incompatible(self):
        b = zero_copy_body()
        self.assertIn("h.map == host", b)
        self.assertIn("mapping_aliases_original_pages", b)
        self.assertEqual(len(re.findall(r"MB_INCOMPATIBLE_MEMORY_TYPE", b)) >= 4, True)

    def test_exit_codes_unchanged(self):
        self.assertIn("static const int mb_exit[MB_N] = {0, 1, 4, 4, 5, 5, 3, 3, 2};", SRC)


if __name__ == "__main__":
    unittest.main()
