"""Portability lint (spec §12): an AST pass over artel/ and tests/."""
import ast
import unittest
from pathlib import Path

OS_ONLY_MODULES = {"fcntl", "msvcrt", "ctypes", "winreg", "signal", "pwd", "grp", "termios", "resource"}
OS_ONLY_ATTRS = {
    ("os", "killpg"), ("os", "setsid"), ("os", "getpgid"), ("os", "fork"), ("os", "chmod"),
    ("os", "symlink"), ("os", "name"), ("sys", "platform"), ("os", "kill"),
}
OS_ONLY_KWARGS = {"start_new_session", "creationflags", "preexec_fn"}
SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call"}
POSIX_TOOLS = {"sh", "bash", "sleep", "cat", "printf", "echo", "true", "false"}

OS_FROM_NAMES = {"kill", "killpg", "setsid", "getpgid", "fork", "chmod", "symlink", "name"}
MODE_LETTERS = set("rwxabt+")
ALIASED_MODULES = {"os", "sys", "subprocess", "tempfile"}

PLATFORM = "artel/platform.py"
SIGNAL_NAME_EXEMPT = {"tests/test_platform.py", "tests/helpers.py"}
POSIX_TOOLS_EXEMPT = {"tests/test_platform.py", "tests/test_procs.py"}
SKIP_ALLOWED = {"tests/test_platform.py"}
OS_SWITCH_EXEMPT = {"tests/test_platform.py"}   # rule 4: OS skips live here


def _dotted(node: ast.AST, aliases: dict[str, str] | None = None) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return (aliases or {}).get(node.value.id, node.value.id), node.attr
    return None


def _aliases(tree: ast.AST) -> dict[str, str]:
    """`import os as o` -> {"o": "os"}, for the modules the rules look at."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and a.name in ALIASED_MODULES:
                    found[a.asname] = a.name
    return found


def _is_text_mode_open(call: ast.Call) -> bool:
    """A method call `<expr>.open(...)` that opens text without encoding= (Path.open)."""
    if _has_kw(call, "encoding"):
        return False
    mode = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return not call.args and not call.keywords   # `p.open()` is text mode; `zf.open(name)` is not
    return (isinstance(mode, ast.Constant) and isinstance(mode.value, str) and mode.value != ""
            and set(mode.value) <= MODE_LETTERS and "b" not in mode.value)


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
    aliases = _aliases(tree)
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
            if isinstance(node, ast.Attribute) and _dotted(node, aliases) in OS_ONLY_ATTRS:
                owner, attr = _dotted(node, aliases)
                if not (attr == "name" and filename in SIGNAL_NAME_EXEMPT) \
                        and not (attr in ("name", "platform") and filename in OS_SWITCH_EXEMPT):
                    add(node, f"{owner}.{attr} outside artel/platform.py")
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in OS_ONLY_KWARGS:
                        add(node, f"{kw.arg}= outside artel/platform.py")
                if isinstance(node.func, ast.Attribute) and node.func.attr == "symlink_to":
                    add(node, "symlink_to outside artel/platform.py")

        if isinstance(node, (ast.Import, ast.ImportFrom)) and filename != PLATFORM:
            stdlib = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(name.split(".")[0] == "platform" and not (isinstance(node, ast.ImportFrom) and node.level)
                   for name in stdlib):
                add(node, "stdlib platform module (use `from artel import platform`)")
        if isinstance(node, ast.ImportFrom) and not node.level:
            names = {a.name for a in node.names}
            if node.module == "os" and names & OS_FROM_NAMES and not in_platform \
                    and not ("name" in names and filename in SIGNAL_NAME_EXEMPT | OS_SWITCH_EXEMPT):
                add(node, f"from os import {sorted(names & OS_FROM_NAMES)[0]} bypasses the os.* rules")
            if node.module == "sys" and "platform" in names and not in_platform \
                    and filename not in OS_SWITCH_EXEMPT:
                add(node, "from sys import platform bypasses the sys.platform rule")
            if node.module == "subprocess" and names & SUBPROCESS_FUNCS:
                add(node, "from subprocess import <function> (use subprocess.<f> so the encoding rules apply)")
            if node.module == "tempfile" and not is_test:
                add(node, "from tempfile import ... in artel/ (use tempfile.<f>(dir=...))")

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                if not _open_is_binary(node) and not _has_kw(node, "encoding"):
                    add(node, "open() without encoding=")
            if isinstance(func, ast.Attribute) and func.attr == "open" and _is_text_mode_open(node):
                add(node, "<expr>.open() in text mode without encoding=")
            if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
                if not _has_kw(node, "encoding"):
                    add(node, f"{func.attr}() without encoding=")
            owner = _dotted(func, aliases)
            if owner and owner[0] == "subprocess" and owner[1] in SUBPROCESS_FUNCS:
                if (_kw_true(node, "text") or _kw_true(node, "universal_newlines")) and not _has_kw(node, "encoding"):
                    add(node, f"subprocess.{owner[1]} in text mode without encoding=")
                if _has_kw(node, "errors") and not _has_kw(node, "encoding"):
                    add(node, f"subprocess.{owner[1]} with errors= but no encoding= (locale text mode)")
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
    for top, is_test in (("artel", False), ("tests", True), ("tools", False)):
        if not (root / top).is_dir():
            continue
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

    def _rules(self, source, filename="artel/x.py", is_test=False):
        return lint_source(source, filename, is_test=is_test)

    def test_os_kill_only_in_platform(self):
        """Mutation caught: dropping ("os", "kill") from the OS-only attributes (os.kill(pid, 0) kills on Windows)."""
        self.assertEqual(self._rules("import os\nos.kill(1, 0)\n"), ["artel/x.py:2: os.kill outside artel/platform.py"])
        self.assertEqual(self._rules("import os\nos.kill(1, 0)\n", "artel/platform.py"), [])

    def test_path_open_text_without_encoding(self):
        """Mutation caught: dropping the `<expr>.open()` rule (Path.open uses the locale encoding)."""
        rule = "<expr>.open() in text mode without encoding="
        for bad in ("p.open()", "p.open('w')", "p.open(mode='r')", "p.open('r+')"):
            self.assertEqual(self._rules(bad + "\n"), [f"artel/x.py:1: {rule}"], bad)
        for good in ("p.open('rb')", "p.open('w', encoding='utf-8')", "zf.open('name')", "p.open(mode='wb')",
                     "p.open('r', encoding='utf-8')"):
            self.assertEqual(self._rules(good + "\n"), [], good)

    def test_subprocess_errors_without_encoding(self):
        """Mutation caught: dropping the errors=-without-encoding= rule (errors= switches to locale text mode)."""
        rule = "with errors= but no encoding= (locale text mode)"
        self.assertEqual(self._rules("import subprocess\nsubprocess.run(['x'], errors='replace')\n"),
                         [f"artel/x.py:2: subprocess.run {rule}"])
        self.assertEqual(self._rules("import subprocess\nsubprocess.Popen(['x'], errors='replace', encoding='utf-8')\n"), [])

    def test_from_imports_bypass(self):
        """Mutation caught: dropping any of the `from` import rules."""
        flagged = {
            "from os import kill": "from os import kill bypasses the os.* rules",
            "from os import killpg, name": "from os import killpg bypasses the os.* rules",
            "from os import chmod": "from os import chmod bypasses the os.* rules",
            "from sys import platform": "from sys import platform bypasses the sys.platform rule",
            "from subprocess import run": "from subprocess import <function> (use subprocess.<f> so the encoding rules apply)",
            "from subprocess import Popen as P": "from subprocess import <function> (use subprocess.<f> so the encoding rules apply)",
            "from tempfile import mkdtemp": "from tempfile import ... in artel/ (use tempfile.<f>(dir=...))",
        }
        for source, rule in flagged.items():
            self.assertEqual(self._rules(source + "\n"), [f"artel/x.py:1: {rule}"], source)
        self.assertEqual(self._rules("from os import path\n"), [])
        self.assertEqual(self._rules("from subprocess import CalledProcessError\n"), [])

    def test_stdlib_platform_module(self):
        """Mutation caught: dropping the stdlib `platform` rule (it shadows artel.platform in reading)."""
        rule = "stdlib platform module (use `from artel import platform`)"
        self.assertEqual(self._rules("import platform\n"), [f"artel/x.py:1: {rule}"])
        self.assertEqual(self._rules("from platform import system\n"), [f"artel/x.py:1: {rule}"])
        self.assertEqual(self._rules("from artel import platform\n"), [])
        self.assertEqual(self._rules("import platform\n", "artel/platform.py"), [])

    def test_aliases_resolved(self):
        """Mutation caught: not resolving `import x as y` (the aliased forms would slip through)."""
        self.assertEqual(self._rules("import os as o\no.killpg(1, 9)\n"), ["artel/x.py:2: os.killpg outside artel/platform.py"])
        self.assertEqual(self._rules("import sys as s\ns.platform\n"), ["artel/x.py:2: sys.platform outside artel/platform.py"])
        self.assertEqual(self._rules("import subprocess as sp\nsp.run([], text=True)\n"),
                         ["artel/x.py:2: subprocess.run in text mode without encoding="])
        self.assertEqual(self._rules("import tempfile as t\nt.mkdtemp()\n"), ["artel/x.py:2: tempfile.mkdtemp without dir="])

    def test_tempfile_from_import_in_artel(self):
        """Mutation caught: applying the from-tempfile rule to tests (they may use tempfile freely)."""
        self.assertTrue(self._rules("from tempfile import TemporaryDirectory\n"))
        self.assertEqual(self._rules("from tempfile import TemporaryDirectory\n", "tests/test_x.py", True), [])

    def test_sys_platform_allowed_in_test_platform_skips(self):
        """Mutation caught: losing the tests/test_platform.py exemption (OS skips live there, plan rule 4)."""
        src = "import sys, unittest\n@unittest.skipUnless(sys.platform == 'darwin', 'macOS')\ndef f(): pass\n"
        self.assertEqual(self._rules(src, "tests/test_platform.py", True), [])
        self.assertTrue(self._rules(src, "tests/test_other.py", True))

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
    def test_tools_are_linted(self):
        """Mutation caught: lint_tree dropping tools/ (build scripts would escape the portability rules)."""
        import tempfile
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "tools" / "x.py").write_text("open('f')\n", encoding="utf-8", newline="\n")
            self.assertEqual(lint_tree(root), ["tools/x.py:1: open() without encoding="])

    def test_repository_is_clean(self):
        self.assertEqual(lint_tree(Path(__file__).resolve().parents[1]), [])
