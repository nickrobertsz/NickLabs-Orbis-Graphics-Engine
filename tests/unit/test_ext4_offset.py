"""Real ext4-offset regression: no full image build or privileged loop device."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'integrator'))
import orbis_graphics_tool as graphics


@unittest.skipUnless(shutil.which('mke2fs') and shutil.which('debugfs'), 'requires e2fsprogs')
class Ext4OffsetTest(unittest.TestCase):
    def test_read_only_partition_offset_and_sparse_holes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            tree = folder / 'tree' / 'usr/share/orbis'
            tree.mkdir(parents=True)
            (tree / 'packages.txt').write_text('orbis-platform 0.2.0-1\nlinux-orbis-ps4 6.15.4-1\n')
            disk = folder / 'disk.img'
            with disk.open('wb') as f:
                f.truncate(18 << 20)  # genuinely sparse whole-disk image
            offset = 2 << 20
            length = 8 << 20
            subprocess.run(['mke2fs', '-q', '-F', '-t', 'ext4', '-d', str(folder / 'tree'),
                            '-E', f'offset={offset}', str(disk), str(length // 1024)], check=True)
            initial = disk.stat().st_size
            with graphics.readonly_ext4_partition(disk, offset, length) as part:
                data = graphics.debugfs_cat(part, '/usr/share/orbis/packages.txt')
                self.assertIn('orbis-platform 0.2.0-1', data)
            self.assertEqual(disk.stat().st_size, initial)

    def test_refuses_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            disk = Path(td) / 'disk.img'
            disk.write_bytes(b'\0' * 4096)
            with self.assertRaises(graphics.Refused):
                with graphics.readonly_ext4_partition(disk, 2048, 4096):
                    pass


if __name__ == '__main__':
    unittest.main()
