"""Offline checks for safe PyPI index selection in the mail installer."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import install_mail_deps


class MailDependencyIndexTests(unittest.TestCase):
    def test_default_and_mirror_index_are_passed_with_hash_pinning_and_clean_pip_env(self):
        for chosen, expected in ((None, install_mail_deps.OFFICIAL_INDEX_URL),
                                 (install_mail_deps.USTC_INDEX_URL,
                                  install_mail_deps.USTC_INDEX_URL)):
            with self.subTest(index=expected):
                app = Path("fixture-app")
                target = Path("fixture-app/runtime/mail-libs")
                requirements = Path("fixture-requirements.txt")
                with (patch.object(install_mail_deps, "resolve_paths",
                                   return_value=(app, target, requirements)),
                      patch.object(install_mail_deps.install_linux, "_mkdir_owned"),
                      patch.object(install_mail_deps.subprocess, "run") as run,
                      patch.dict(os.environ, {
                          "PIP_INDEX_URL": "https://untrusted.example/simple",
                          "pip_extra_index_url": "https://another.example/simple",
                          "PIP_CONFIG_FILE": "untrusted.ini",
                      }),
                      contextlib.redirect_stdout(io.StringIO())):
                    if chosen is None:
                        install_mail_deps.install(app)
                    else:
                        install_mail_deps.install(app, index_url=chosen)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--index-url") + 1], expected)
                self.assertIn("--require-hashes", command)
                self.assertIn("--no-deps", command)
                self.assertIn("--no-compile", command)
                self.assertTrue(run.call_args.kwargs["check"])
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
                self.assertFalse(any(key.upper().startswith("PIP_") and key != "PIP_CONFIG_FILE"
                                     for key in env))

    def test_untrusted_direct_url_is_rejected_before_path_resolution_or_subprocess(self):
        for url in ("https://pypi.org/simple/", "http://pypi.org/simple",
                    "https://pypi.org.evil.example/simple"):
            with self.subTest(url=url), \
                    patch.object(install_mail_deps, "resolve_paths") as resolve, \
                    patch.object(install_mail_deps.subprocess, "run") as run, \
                    self.assertRaises(install_mail_deps.MailDepsError):
                install_mail_deps.install(Path("fixture-app"), index_url=url)
            resolve.assert_not_called()
            run.assert_not_called()

    def test_cli_defaults_and_passes_explicit_index(self):
        with patch.object(install_mail_deps, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install_mail_deps.main([]), 0)
        self.assertEqual(install.call_args.kwargs["index_url"],
                         install_mail_deps.OFFICIAL_INDEX_URL)

        with patch.object(install_mail_deps, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install_mail_deps.main([
                "--index-url", install_mail_deps.USTC_INDEX_URL]), 0)
        self.assertEqual(install.call_args.kwargs["index_url"],
                         install_mail_deps.USTC_INDEX_URL)

    def test_cli_rejects_untrusted_index_before_install(self):
        with patch.object(install_mail_deps, "install") as install, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            install_mail_deps.main(["--index-url", "https://evil.example/simple"])
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
