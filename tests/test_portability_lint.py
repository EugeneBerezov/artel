"""Portability lint (spec §12): an AST pass over artel/ and tests/."""
import ast
import unittest
from pathlib import Path

OS_ONLY_MODULES = {"fcntl", "msvcrt", "ctypes", "winreg", "signal", "pwd", "grp", "termios", "resource"}
OS_ONLY_ATTRS = {
    ("os", "killpg"), ("os", "setsid"), ("os", "getpgid"), ("os", "fork"), ("os", "chmod"),
    ("os", "symlink"), ("os", "name"), ("sys", "platform"),
}
OS_ONLY_KWARGS = {"start_new_session", "creationflags", "preexec_fn"}
SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call"}
POSIX_TOOLS = {"sh", "bash", "sleep", "cat", "printf", "echo", "true", "false"}

PLATFORM = "artel/platform.py"
SIGNAL_NAME_EXEMPT = {"tests/test_platform.py", "tests/helpers.py"}
POSIX_TOOLS_EXEMPT = {"tests/test_platform.py", "tests/test_procs.py"}
SKIP_ALLOWED = {"tests/test_platform.py"}
OS_SWITCH_EXEMPT = {"tests/test_platform.py"}   # rule 4: OS skips live here


def _dotted(node: ast.AST) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _kw_true(call: ast.Call, name: str) -> bool:
    return any(k.arg == name and isinstance(k.value, ast.Constant) and k.value.value is True
               for k in call.keywords)


def _open_is_binary(call: ast.Call) -> bool:
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def _mentions_os_switch(node: ast.AST) -> bool:
    return any(_dotted(n) in {("os", "name"), ("sys", "platform")} for n in ast.walk(node))


def lint_source(source: str, filename: str, *, is_test: bool) -> list[str]:
    tree = ast.parse(source, filename=filename)
    found: list[str] = []

    def add(node: ast.AST, rule: str) -> None:
        found.append(f"{filename}:{node.lineno}: {rule}")

    in_platform = filename == PLATFORM
    for node in ast.walk(tree):
        if not in_platform:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    top = name.split(".")[0]
                    if top in OS_ONLY_MODULES and not (top == "signal" and filename in SIGNAL_NAME_EXEMPT):
                        add(node, f"import of {top} outside artel/platform.py")
            if isinstance(node, ast.Attribute) and _dotted(node) in OS_ONLY_ATTRS:
                owner, attr = _dotted(node)
                if not (attr == "name" and filename in SIGNAL_NAME_EXEMPT) \
                        and not (attr in ("name", "platform") and filename in OS_SWITCH_EXEMPT):
                    add(node, f"{owner}.{attr} outside artel/platform.py")
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in OS_ONLY_KWARGS:
                        add(node, f"{kw.arg}= outside artel/platform.py")
                if isinstance(node.func, ast.Attribute) and node.func.attr == "symlink_to":
                    add(node, "symlink_to outside artel/platform.py")

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                if not _open_is_binary(node) and not _has_kw(node, "encoding"):
                    add(node, "open() without encoding=")
            if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
                if not _has_kw(node, "encoding"):
                    add(node, f"{func.attr}() without encoding=")
            owner = _dotted(func)
            if owner and owner[0] == "subprocess" and owner[1] in SUBPROCESS_FUNCS:
                if (_kw_true(node, "text") or _kw_true(node, "universal_newlines")) and not _has_kw(node, "encoding"):
                    add(node, f"subprocess.{owner[1]} in text mode without encoding=")
            if not is_test and owner and owner[0] == "tempfile" and not _has_kw(node, "dir"):
                add(node, f"tempfile.{owner[1]} without dir=")

        if not is_test and isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.startswith("/tmp"):
            add(node, "string literal starting with /tmp")

        if is_test:
            if isinstance(node, ast.List) and node.elts and filename not in POSIX_TOOLS_EXEMPT:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and first.value in POSIX_TOOLS:
                    add(node, f"POSIX tool {first.value!r} in a test command (use py_cmd)")
            if isinstance(node, ast.Call) and filename not in SKIP_ALLOWED:
                name = node.func.attr if isinstance(node.func, ast.Attribute) else None
                if name in ("skipIf", "skipUnless") and any(_mentions_os_switch(a) for a in node.args):
                    add(node, f"{name} on the OS only in tests/test_platform.py")
    return found


def lint_tree(root: Path) -> list[str]:
    found: list[str] = []
    for top, is_test in (("artel", False), ("tests", True)):
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            found.extend(lint_source(path.read_text(encoding="utf-8"), rel, is_test=is_test))
    return found


class LintRulesTest(unittest.TestCase):
    def test_open_without_encoding(self):
        self.assertTrue(lint_source("open('x')\n", "artel/a.py", is_test=False))
        self.assertTrue(lint_source("open('x', 'w')\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("open('x', 'rb')\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("open('x', mode='wb')\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("open('x', encoding='utf-8')\n", "artel/a.py", is_test=False))

    def test_read_write_text_without_encoding(self):
        self.assertTrue(lint_source("p.read_text()\n", "artel/a.py", is_test=False))
        self.assertTrue(lint_source("p.write_text('x')\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("p.read_text(encoding='utf-8')\n", "artel/a.py", is_test=False))

    def test_subprocess_text_without_encoding(self):
        bad = "import subprocess\nsubprocess.run(['x'], text=True)\n"
        self.assertTrue(lint_source(bad, "artel/a.py", is_test=False))
        self.assertTrue(lint_source("subprocess.Popen(['x'], universal_newlines=True)\n", "artel/a.py", is_test=False))
        good = "import subprocess\nsubprocess.run(['x'], text=True, encoding='utf-8')\n"
        self.assertFalse(lint_source(good, "artel/a.py", is_test=False))
        self.assertFalse(lint_source("subprocess.run(['x'])\n", "artel/a.py", is_test=False))

    def test_os_specific_only_in_platform(self):
        src = "import fcntl\nimport os\nos.killpg(1, 9)\n"
        self.assertEqual(len(lint_source(src, "artel/board.py", is_test=False)), 2)
        self.assertEqual(lint_source(src, "artel/platform.py", is_test=False), [])
        kw = "subprocess.Popen(['x'], start_new_session=True)\n"
        self.assertTrue(lint_source(kw, "artel/board.py", is_test=False))
        self.assertEqual(lint_source(kw, "artel/platform.py", is_test=False), [])
        self.assertTrue(lint_source("p.symlink_to(q)\n", "artel/board.py", is_test=False))

    def test_signal_import_only_in_platform(self):
        self.assertTrue(lint_source("import signal\n", "artel/runner.py", is_test=False))
        self.assertTrue(lint_source("from signal import SIGTERM\n", "tests/test_x.py", is_test=True))
        self.assertFalse(lint_source("import signal\n", "tests/helpers.py", is_test=True))
        self.assertFalse(lint_source("import signal\n", "artel/platform.py", is_test=False))

    def test_os_name_only_in_platform(self):
        self.assertTrue(lint_source("import os\nx = os.name\n", "artel/cli.py", is_test=False))
        self.assertTrue(lint_source("import sys\nx = sys.platform\n", "artel/cli.py", is_test=False))
        self.assertFalse(lint_source("import os\nx = os.name\n", "artel/platform.py", is_test=False))
        self.assertFalse(lint_source("import os\nx = os.name\n", "tests/helpers.py", is_test=True))
        self.assertFalse(lint_source("import sys\nx = sys.platform\n", "tests/test_platform.py", is_test=True))
        self.assertTrue(lint_source("import sys\nx = sys.platform\n", "tests/test_board.py", is_test=True))

    def test_tmp_literal_and_tempfile_without_dir(self):
        self.assertTrue(lint_source("x = '/tmp/foo'\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("x = '/tmp/foo'\n", "tests/test_a.py", is_test=True))
        self.assertTrue(lint_source("import tempfile\ntempfile.mkdtemp()\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("import tempfile\ntempfile.mkdtemp(dir=d)\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("import tempfile\ntempfile.mkdtemp()\n", "tests/test_a.py", is_test=True))

    def test_posix_tools_in_tests(self):
        self.assertTrue(lint_source("x = ['sh', '-c', 'true']\n", "tests/test_x.py", is_test=True))
        self.assertTrue(lint_source("x = ['sleep', '1']\n", "tests/test_x.py", is_test=True))
        self.assertFalse(lint_source("x = ['sh', '-c', 'true']\n", "tests/test_platform.py", is_test=True))
        self.assertFalse(lint_source("x = ['sh', '-c', 'true']\n", "tests/test_procs.py", is_test=True))
        self.assertFalse(lint_source("x = ['sh', '-c', 'true']\n", "artel/a.py", is_test=False))
        self.assertFalse(lint_source("x = ['git', 'status']\n", "tests/test_x.py", is_test=True))

    def test_os_skip_only_in_test_platform(self):
        src = "import unittest, os\n@unittest.skipIf(os.name == 'nt', 'x')\ndef f(): pass\n"
        self.assertTrue(any("skipIf" in m for m in lint_source(src, "tests/test_x.py", is_test=True)))
        self.assertEqual(lint_source(src, "tests/test_platform.py", is_test=True), [])
        other = "@unittest.skipIf(not HAVE_GIT, 'x')\ndef f(): pass\n"
        self.assertEqual(lint_source(other, "tests/test_x.py", is_test=True), [])


class TreeTest(unittest.TestCase):
    def test_repository_is_clean(self):
        self.assertEqual(lint_tree(Path(__file__).resolve().parents[1]), [])
