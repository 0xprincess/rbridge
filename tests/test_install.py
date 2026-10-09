"""Installation ownership and failed-upgrade boundaries, without network access."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and shutil.which("ssh-keygen"), "Linux installer prerequisites")
class InstallerTests(unittest.TestCase):
    def install(self, prefix):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
        environment.update(PIP_NO_INDEX="1", PIP_CONFIG_FILE=os.devnull)
        return subprocess.run(["sh", str(ROOT / "install.sh"), "--prefix", str(prefix), "--python", sys.executable],
                              cwd="/tmp", env=environment, capture_output=True, timeout=60)

    def test_unrelated_command_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / "bin").mkdir()
            command = prefix / "bin/rbridge"
            command.write_text("unrelated user command\n")
            result = self.install(prefix)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(command.read_text(), "unrelated user command\n")
            self.assertFalse((prefix / "share/rbridge").exists())

    def test_dependency_failure_keeps_previous_release_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            data = prefix / "share/rbridge"
            old = data / "releases/previous"
            old.mkdir(parents=True)
            (old / "rbridge").write_text("previous command\n")
            (data / ".rbridge-install").write_text("rbridge-install-v1\n")
            (data / "current").symlink_to(old)
            (prefix / "bin").mkdir()
            (prefix / "bin/rbridge").symlink_to(data / "current/rbridge")
            # A fresh virtualenv cannot satisfy the pinned Ansible requirement offline.
            result = self.install(prefix)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((data / "current").resolve(), old)
            self.assertEqual((prefix / "bin/rbridge").read_text(), "previous command\n")
            self.assertEqual(list((data / "releases").iterdir()), [old])


if __name__ == "__main__":
    unittest.main()
