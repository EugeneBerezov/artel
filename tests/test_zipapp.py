import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from artel import __version__
from tests.helpers import REPO_ROOT


class ZipappTest(unittest.TestCase):
    def test_pyz_builds_and_prints_version(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "tools" / "build_pyz.py"), "--out", str(tmp_path / "artel.pyz")],
                cwd=tmp_path, capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stderr)
            pyz = tmp_path / "artel.pyz"
            self.assertTrue(pyz.is_file())
            run = subprocess.run([sys.executable, str(pyz), "--version"], cwd=tmp_path,
                                 capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), __version__)
            self.assertEqual(__version__, "0.1.0")
