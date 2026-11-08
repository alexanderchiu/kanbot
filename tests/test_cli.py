import subprocess
import sys
import unittest
from shutil import which


class CliTest(unittest.TestCase):
    def check_help(self, command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--allowed-hosts", result.stdout)

    def test_module_help(self):
        self.check_help([sys.executable, "-m", "kanbot", "--help"])

    def test_script_help(self):
        script = which("kanbot")
        if script is None:
            self.skipTest("kanbot script isn't on PATH; run with uv run pytest")
        self.check_help([script, "--help"])
