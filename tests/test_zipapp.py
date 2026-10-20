import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from artel import __version__
from tests.helpers import REPO_ROOT, make_repo

_spec = importlib.util.spec_from_file_location("build_pyz", REPO_ROOT / "tools" / "build_pyz.py")
build_pyz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_pyz)


class ZipappTest(unittest.TestCase):
    def test_pyz_builds_and_prints_version(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "tools" / "build_pyz.py"), "--out", str(tmp_path / "artel.pyz"),
                 "--staging", str(tmp_path / "stage")],
                cwd=tmp_path, capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stderr)
            pyz = tmp_path / "artel.pyz"
            self.assertTrue(pyz.is_file())
            run = subprocess.run([sys.executable, str(pyz), "--version"], cwd=tmp_path,
                                 capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), __version__)
            self.assertEqual(__version__, "0.1.0")


class BuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def build(self):
        pyz = self.tmp / "artel.pyz"
        build_pyz.build(pyz, self.tmp / "stage")
        return pyz

    def test_pyz_has_shebang_and_no_pycache(self):
        """Mutation caught: dropping the ignore patterns (stale .pyc inside the archive)."""
        from artel import cli   # make sure __pycache__ exists in the checkout
        self.assertTrue(cli)
        pyz = self.build()
        self.assertEqual(pyz.read_bytes().split(b"\n", 1)[0], b"#!/usr/bin/env python3")
        names = zipfile.ZipFile(pyz).namelist()
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")], names)

    def test_pyz_dispatches_a_command(self):
        """Mutation caught: a __main__.py that never reaches cli.main's error handling."""
        pyz = self.build()
        outside = self.tmp / "outside"
        outside.mkdir()
        run = subprocess.run([sys.executable, str(pyz), "status"], cwd=outside, capture_output=True,
                             encoding="utf-8", errors="replace")
        self.assertIn(run.returncode, (1, 3), run.stderr)
        self.assertTrue(run.stderr.startswith("artel"), run.stderr)
        self.assertNotIn("Traceback", run.stderr)

    def test_build_refuses_foreign_nonempty_staging(self):
        """Mutation caught: an unconditional rmtree of the staging directory (`--staging .`)."""
        stage = self.tmp / "stage"
        stage.mkdir()
        keep = stage / "precious.txt"
        keep.write_text("keep\n", encoding="utf-8", newline="\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            build_pyz.build(self.tmp / "artel.pyz", stage)
        self.assertEqual(cm.exception.code, 2)
        self.assertTrue(keep.exists())

    def test_build_refuses_staging_that_is_a_file(self):
        """Mutation caught: a traceback (NotADirectoryError) instead of the exit-2 message."""
        target = self.tmp / "stage"
        target.write_text("file\n", encoding="utf-8", newline="\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            build_pyz.build(self.tmp / "artel.pyz", target)
        self.assertEqual(cm.exception.code, 2)

    def test_build_reuses_own_staging_and_empty_dirs(self):
        """Mutation caught: refusing a staging directory the build itself created."""
        pyz = self.build()
        self.build()
        self.assertTrue(pyz.is_file())
        empty = self.tmp / "empty"
        empty.mkdir()
        build_pyz.build(self.tmp / "b.pyz", empty)
        self.assertTrue((self.tmp / "b.pyz").is_file())

    def test_relative_out_is_under_repo_root(self):
        """Mutation caught: resolving --out against the current directory instead of the repo root."""
        target = REPO_ROOT / "dist" / "t28-relative.pyz"
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        run = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "build_pyz.py"), "--out",
                              "dist/t28-relative.pyz", "--staging", str(self.tmp / "stage")],
                             cwd=self.tmp, capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(target.is_file())


class PyzTemplatesTest(unittest.TestCase):
    def test_builtin_templates_load_from_pyz(self):
        """Mutation caught: reading templates through __file__ paths / leaving builtin_templates out of the archive."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp).resolve()
            pyz = root / "artel.pyz"
            build_pyz.build(pyz, root / "stage")
            repo = make_repo(root)
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}   # the archive must stand alone
            env["PYTHONIOENCODING"] = "utf-8"
            run = subprocess.run([sys.executable, str(pyz), "templates", "export"], cwd=repo, env=env,
                                 capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(run.returncode, 0, run.stderr)
            names = ("vet.md", "attack.md", "write.md", "review.md", "review-closure.md", "fix.md", "unattended.md")
            for name in names:
                self.assertGreater((repo / ".artel" / "templates" / name).stat().st_size, 50, name)
