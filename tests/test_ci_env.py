import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import REPO_ROOT, py_cmd, subprocess_env

WARN = ("PYTHONWARNDEFAULTENCODING", "PYTHONWARNINGS")


class CiEnvTest(unittest.TestCase):
    def test_ci_workflow_sets_encoding_env_for_children(self):
        """Mutation caught: deleting the test job's env block (children then ignore a missing encoding=)."""
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = re.search(r"^  test:\n(.*?)^  \w+:", text + "\n  end:", re.S | re.M).group(1)
        env_block = re.search(r"^    env:.*\n((?:      .*\n)+)", job, re.M)
        self.assertIsNotNone(env_block, "the test job needs a job-level env:")
        self.assertIn("PYTHONWARNDEFAULTENCODING", env_block.group(1))
        self.assertIn("error::EncodingWarning", env_block.group(1))

    @unittest.skipUnless(all(os.environ.get(k) for k in WARN), "encoding warning env not set (CI sets it)")
    def test_child_open_without_encoding_fails(self):
        """Mutation caught: subprocess_env() dropping the variables (a child would not complain)."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "x.txt"
            out = subprocess.run(py_cmd(f"open({str(target)!r}, 'w').close()"), env=subprocess_env(),
                                 capture_output=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("EncodingWarning", out.stderr)

    def test_subprocess_env_keeps_encoding_vars(self):
        """Mutation caught: subprocess_env() building the child environment from scratch."""
        with mock.patch.dict(os.environ, {"PYTHONWARNDEFAULTENCODING": "1",
                                          "PYTHONWARNINGS": "error::EncodingWarning"}):
            env = subprocess_env()
        self.assertEqual(env["PYTHONWARNDEFAULTENCODING"], "1")
        self.assertEqual(env["PYTHONWARNINGS"], "error::EncodingWarning")
