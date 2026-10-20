import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import artel
from artel import cli, cmd_agent as CA, journal, platform as P
from artel.board import open_board
from artel.errors import ArtelError, UsageError
from artel.gitutil import Repo
from tests.helpers import REPO_ROOT, RepoTestCase, git, subprocess_env

AGENT_ENV = {"ARTEL_TASK": "7", "ARTEL_STAGE": "write", "ARTEL_ROUND": "2"}


class CmdAgentTest(RepoTestCase):
    def setUp(self):
        super().setUp()
        cwd = os.getcwd()
        os.chdir(self.repo_path)
        self.addCleanup(os.chdir, cwd)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ARTEL_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = Repo.discover(self.repo_path)

    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def board(self):
        board = open_board(self.repo.board_path)
        self.addCleanup(board.close)
        return board

    def run_cmd(self, func, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(self.parse(*argv))
        return code, out.getvalue(), err.getvalue()

    # --- add ---------------------------------------------------------------------------------------------

    def test_human_add_is_open_with_flags_and_deps(self):
        code, out, _ = self.run_cmd(CA.run_add, "add", "first")
        self.assertEqual((code, out.strip()), (0, "#1 open"))
        code, out, _ = self.run_cmd(CA.run_add, "add", "second", "--body", "details ✓", "--area", "src/a",
                                    "--area", "docs", "--priority", "3", "--after", "1", "--attack", "--strong",
                                    "--review", "4", "--merge", "ask")
        self.assertEqual(out.strip(), "#2 open")
        task = self.board().get_task(2)
        self.assertEqual((task.title, task.body, task.area, task.priority, task.attack, task.strong,
                          task.review_rounds, task.merge, task.created_by),
                         ("second", "details ✓", ("src/a", "docs"), 3, True, True, 4, "ask", "human"))
        self.assertEqual(self.board().deps(2), [1])

    def test_area_spellings_are_normalized_and_escapes_rejected(self):
        """Mutation caught: storing --area as typed (src\\a and src/a would never overlap on Windows)."""
        good = {"src/a": "src/a", "src\\a": "src/a", "./src//a/": "src/a", "src/x/../a": "src/a",
                "src\\sub\\..\\a": "src/a", ".": "", "docs": "docs"}
        for raw, want in good.items():
            with self.subTest(raw=raw):
                self.run_cmd(CA.run_add, "add", "t", "--area", raw)
                self.assertEqual(self.board().list_tasks()[-1].area, (want,))
        before = len(self.board().list_tasks())
        for bad in ("", "  ", "../x", "a/../../x", "/etc", "C:\\x", "C:x", "\\\\server\\share"):
            with self.subTest(raw=bad):
                with self.assertRaises(UsageError):
                    self.run_cmd(CA.run_add, "add", "t", "--area", bad)
        self.assertEqual(len(self.board().list_tasks()), before)   # nothing stored for a rejected area
        from artel import pick
        self.assertTrue(pick.areas_overlap((CA.clean_area("src\\a"),), (CA.clean_area("src/a/b"),)))

    def test_empty_title_rejected(self):
        with self.assertRaises(UsageError):
            self.run_cmd(CA.run_add, "add", "   ")
        self.assertEqual(self.board().list_tasks(), [])

    def test_agent_add_is_proposed_with_creator(self):
        os.environ["ARTEL_TASK"] = "5"
        code, out, _ = self.run_cmd(CA.run_add, "add", "idea")
        self.assertEqual((code, out.strip()), (0, "#1 proposed"))
        self.assertEqual(self.board().get_task(1).created_by, "agent:5")

    def test_add_unknown_after_rejected(self):
        with self.assertRaises(UsageError):
            self.run_cmd(CA.run_add, "add", "x", "--after", "99")
        with self.assertRaises(UsageError):
            self.run_cmd(CA.run_add, "add", "x", "--review", "0")
        with self.assertRaises(UsageError):
            self.run_cmd(CA.run_add, "add", "x", "--after", "1", "--after", "99")   # one id valid, one not
        self.assertEqual(self.board().list_tasks(), [])   # catches validating after the insert

    # --- note --------------------------------------------------------------------------------------------

    def test_note_requires_agent_env(self):
        with self.assertRaises(ArtelError) as cm:
            self.run_cmd(CA.run_note, "note", "FACT", "x")
        self.assertIn("artel note is for agents; ARTEL_TASK is not set", str(cm.exception))
        os.environ["ARTEL_TASK"] = "7"   # the stage is missing too
        with self.assertRaises(ArtelError):
            self.run_cmd(CA.run_note, "note", "FACT", "x")

    def test_note_with_unknown_task_is_a_clean_error(self):
        os.environ.update(ARTEL_TASK="42", ARTEL_STAGE="write")
        with self.assertRaises(ArtelError) as cm:
            self.run_cmd(CA.run_note, "note", "FACT", "x")
        self.assertIn("not a task on this board", str(cm.exception))

    def test_note_appends_with_stage_and_round(self):
        self.board().insert_task(title="t", state="writing", created_by="human")
        os.environ.update(ARTEL_TASK="1", ARTEL_STAGE="write", ARTEL_ROUND="2")
        code, _, _ = self.run_cmd(CA.run_note, "note", "FAIL", "it broke", "--basis", "log.txt")
        self.assertEqual(code, 0)
        (entry,) = self.board().conn.execute("SELECT * FROM entries").fetchall()
        self.assertEqual((entry["kind"], entry["text"], entry["basis"], entry["author"], entry["task_id"],
                          entry["stage"], entry["round"]), ("FAIL", "it broke", "log.txt", "agent:write", 1,
                                                            "write", 2))
        self.assertEqual(self.board().get_task(1).state, "writing")   # note never changes task state

    def test_add_and_note_without_optional_flags_store_empty_strings(self):
        self.run_cmd(CA.run_add, "add", "plain")
        self.assertEqual(self.board().get_task(1).body, "")
        os.environ.update(ARTEL_TASK="1", ARTEL_STAGE="write")
        self.assertEqual(self.run_cmd(CA.run_note, "note", "FACT", "no basis")[0], 0)
        (entry,) = self.board().conn.execute("SELECT * FROM entries").fetchall()
        self.assertEqual((entry["basis"], entry["round"]), ("", None))

    def test_note_too_long_exit_2_with_hint(self):
        self.board().insert_task(title="t", state="writing", created_by="human")
        os.environ.update(ARTEL_TASK="1", ARTEL_STAGE="write")
        code, _, err = self.run_cmd(CA.run_note, "note", "FACT", "é" * 300)
        self.assertEqual(code, 2)
        self.assertIn("commit a file", err)
        self.assertEqual(self.board().conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    def test_note_from_linked_worktree_reaches_same_board(self):
        self.board().insert_task(title="t", state="writing", created_by="human")
        wt = self.tmp / "wt"
        git(self.repo_path, "worktree", "add", "-q", "-b", "x", str(wt))
        os.chdir(wt)
        os.environ.update(ARTEL_TASK="1", ARTEL_STAGE="write")
        self.assertEqual(self.run_cmd(CA.run_note, "note", "FACT", "from the slot")[0], 0)
        self.assertEqual(self.board().conn.execute("SELECT text FROM entries").fetchone()[0], "from the slot")

    # --- heavy -------------------------------------------------------------------------------------------

    def configure(self, **tables):
        lines = []
        for table, values in tables.items():
            lines.append(f"[{table}]")
            lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
        (self.repo_path / "artel.toml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def test_heavy_refuses_unlisted_command(self):
        self.configure(commands={"project_tools": ["Bash(npm test *)"]})
        with self.assertRaises(UsageError) as cm:
            self.run_cmd(CA.run_heavy, "heavy", "--", sys.executable, "-c", "print(1)")
        self.assertIn("commands.project_tools", str(cm.exception))
        with self.assertRaises(UsageError):
            self.run_cmd(CA.run_heavy, "heavy", "--")   # nothing to run
        marker = self.tmp / "ran.txt"
        self.configure(commands={"project_tools": ["Bash(npm test *)"],
                                 "heavy_lock": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]})
        with self.assertRaises(UsageError):   # catches checking the pattern after the lock/command started
            self.run_cmd(CA.run_heavy, "heavy", "--", sys.executable, "-c", "pass")
        self.assertFalse(marker.exists())

    def test_heavy_runs_under_prefix_and_returns_code(self):
        marker = self.tmp / "locked.txt"
        lock = self.tmp / "lock.py"
        lock.write_text("import subprocess, sys\n"
                        f"open({str(marker)!r}, 'w', encoding='utf-8').write('held')\n"
                        "sys.exit(subprocess.call(sys.argv[1:]))\n", encoding="utf-8", newline="\n")
        self.configure(commands={"project_tools": [f"Bash({sys.executable} *)"],
                                 "heavy_lock": [sys.executable, str(lock)]})
        code, _, _ = self.run_cmd(CA.run_heavy, "heavy", "--", sys.executable, "-c", "import sys; sys.exit(4)")
        self.assertEqual(code, 4)
        self.assertEqual(marker.read_text(encoding="utf-8"), "held")   # it really ran under the lock command

    def test_tool_pattern_matching(self):
        match = CA.tool_pattern_matches
        self.assertTrue(match(["Bash(npx vitest *)"], ["npx", "vitest", "run"]))
        self.assertFalse(match(["Bash(npx vitest *)"], ["npx", "jest", "run"]))
        self.assertFalse(match(["Read(src/*)", "Bash(git status *)"], ["git", "log"]))   # non-Bash ignored
        self.assertTrue(match(["Bash(make *)", "Read(x)"], ["make", "all"]))
        self.assertTrue(match([r"Bash(C:\Tools\py\python.exe *)"], [r"C:\Tools\py\python.exe", "-m", "pytest"]))
        self.assertFalse(match([], ["make"]))
        # catches fnmatch.fnmatch (case-insensitive and / == \ on Windows)
        self.assertFalse(match([r"Bash(C:\Tools\*)"], [r"c:\tools\x"]))
        self.assertFalse(match(["Bash(make *)"], ["MAKE", "all"]))

    # --- shim and env --------------------------------------------------------------------------------------

    def test_shim_runs_artel(self):
        bin_dir = CA.ensure_shim(self.repo)
        self.assertEqual(bin_dir, self.repo.artel_dir / "bin")
        out = subprocess.run(P.shim_invocation(bin_dir) + ["--version"], capture_output=True, encoding="utf-8",
                             errors="replace", env=subprocess_env(), cwd=str(REPO_ROOT))
        self.assertEqual(out.stdout.strip(), "0.1.0", out.stderr)

    def test_agent_env_sets_vars_and_path(self):
        base = {"PATH": os.pathsep.join(["/base/bin", "/usr/bin"]), "PYTHONPATH": "/pp", "KEEP": "1"}
        env = CA.agent_env(self.repo, task_id=3, stage="review", round=2, base_env=base)
        self.assertEqual((env["ARTEL_TASK"], env["ARTEL_STAGE"], env["ARTEL_ROUND"], env["KEEP"]),
                         ("3", "review", "2", "1"))
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        shim_dir = str(self.repo.artel_dir / "bin")
        self.assertEqual(env["PATH"], shim_dir + os.pathsep + base["PATH"])
        package_root = str(Path(artel.__file__).resolve().parent.parent)
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [package_root, "/pp"])
        self.assertEqual(base["PATH"], os.pathsep.join(["/base/bin", "/usr/bin"]))   # the input is not mutated
        bare = CA.agent_env(self.repo, task_id=1, stage="fix", round=1, base_env={})
        self.assertEqual(bare["PYTHONPATH"], package_root)
        self.assertEqual(bare["PATH"], str(self.repo.artel_dir / "bin"))   # no trailing empty (cwd) entry

    def test_agent_env_respects_case_of_path_key(self):
        env = CA.agent_env(self.repo, task_id=1, stage="fix", round=1, base_env={"Path": "C:\\x"})
        self.assertTrue(env["Path"].endswith("C:\\x"))
        self.assertNotIn("PATH", env)


if __name__ == "__main__":
    unittest.main()
