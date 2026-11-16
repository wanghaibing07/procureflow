"""Check demo token file creation without using real credentials."""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
NAMES = {"MYSQL_PASSWORD", "PROCUREFLOW_BUYER_TOKEN", "PROCUREFLOW_APPROVER_TOKEN", "PROCUREFLOW_ERP_TOKEN"}


class DemoEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="procureflow-demo-env-test-")
        self.root = Path(self.temporary.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.script = scripts / "demo_env.py"
        shutil.copyfile(ROOT / "scripts/demo_env.py", self.script)
        self.addCleanup(self.temporary.cleanup)

    def run_generator(self, zero_umask=False):
        if zero_umask:
            command = [sys.executable, "-X", "utf8", "-c",
                       "import os,runpy,sys; os.umask(0); runpy.run_path(sys.argv[1], run_name='__main__')",
                       str(self.script)]
        else:
            command = [sys.executable, "-X", "utf8", str(self.script)]
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)

    def test_creates_distinct_tokens_without_printing_them(self):
        result = self.run_generator()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = (self.root / ".env").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(NAMES))
        entries = dict(line.split("=", 1) for line in lines)
        self.assertEqual(set(entries), NAMES)
        self.assertEqual(len(set(entries.values())), len(NAMES))
        for value in entries.values():
            self.assertRegex(value, re.compile(r"^[0-9a-f]{48}$"))
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_existing_file_is_never_overwritten(self):
        path = self.root / ".env"
        original = b"existing synthetic config\n"
        path.write_bytes(original)
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions do not describe Windows ACLs")
    def test_file_is_private_even_with_zero_umask(self):
        result = self.run_generator(zero_umask=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE((self.root / ".env").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
