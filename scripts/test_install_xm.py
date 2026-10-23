"""Installation-only and pairing isolation checks; no network or model calls."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_linux


@unittest.skipUnless(sys.platform.startswith("linux"), "Installer requires POSIX ownership")
class XmInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / "app"
        self.runtime = patch.object(install_linux, "_install_runtime",
                                    return_value=(self.app / "runtime" / install_linux.VERSION, True))
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def test_install_only_never_starts_a_process_or_prints_secrets(self):
        output = io.StringIO()
        with patch.object(install_linux, "_start_and_check") as start, contextlib.redirect_stdout(output):
            code = install_linux.main(["--home", str(self.app), "--no-start", "--pairing-scheme", "daxqo-xm"])
        self.assertEqual(code, 0)
        start.assert_not_called()
        token = (self.app / "state/capability").read_text().strip()
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token, (self.app / "logs/install.log").read_text())
        self.assertTrue((self.app / "workspace/AGENTS.md").is_file())
        self.assertTrue((self.app / "runtime_control.py").is_file())
        self.assertTrue((self.app / "runtime.json").is_file())

    def test_default_start_and_both_pairing_schemes_preserve_the_capability(self):
        token = None
        for scheme in ("daxqo", "daxqo-xm"):
            output = io.StringIO()
            with patch.object(install_linux, "_start_and_check", return_value=(123, {})) as start, contextlib.redirect_stdout(output):
                args = ["--home", str(self.app), "--print-pairing"]
                if scheme != "daxqo":
                    args += ["--pairing-scheme", scheme]
                self.assertEqual(install_linux.main(args), 0)
            start.assert_called_once()
            current = (self.app / "state/capability").read_text().strip()
            if token:
                self.assertEqual(current, token)
            token = current
            self.assertIn(scheme + "://connect#token=" + token, output.getvalue())

    def test_unknown_pairing_scheme_is_rejected_before_installation(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            install_linux.main(["--home", str(self.app), "--pairing-scheme", "https"])
        self.assertFalse(self.app.exists())


if __name__ == "__main__":
    unittest.main()
