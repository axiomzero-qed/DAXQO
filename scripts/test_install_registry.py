"""Offline tests for the installer's pinned npm registry choices."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request
from unittest.mock import patch

import install_linux


def package_metadata(registry: str, architecture: str, *, name="@openai/codex",
                     version=None, integrity=None, tarball=None):
    version = version or f"{install_linux.VERSION}-linux-{architecture}"
    host = install_linux.REGISTRY_HOSTS[registry]
    return {
        "name": name,
        "version": version,
        "dist": {
            "integrity": integrity or install_linux.PINNED_RUNTIME_INTEGRITY[
                (install_linux.VERSION, architecture)],
            "tarball": tarball or (
                f"https://{host}/@openai/codex/-/codex-{version}.tgz"),
        },
    }


class RegistryMetadataTests(unittest.TestCase):
    def test_official_and_mirror_metadata_are_pinned_for_both_architectures(self):
        for registry, host in install_linux.REGISTRY_HOSTS.items():
            for architecture in ("arm64", "x64"):
                with self.subTest(registry=registry, architecture=architecture):
                    metadata = package_metadata(registry, architecture)
                    opener = object()
                    with patch.object(install_linux, "_read_metadata", return_value=metadata) as read:
                        integrity, tarball = install_linux._package_details(
                            opener, architecture, registry)
                    expected_url = (
                        f"https://{host}/@openai%2Fcodex/"
                        f"{install_linux.VERSION}-linux-{architecture}")
                    read.assert_called_once_with(opener, expected_url)
                    self.assertEqual(
                        integrity,
                        install_linux.PINNED_RUNTIME_INTEGRITY[
                            (install_linux.VERSION, architecture)])
                    self.assertEqual(
                        tarball,
                        f"https://{host}/@openai/codex/-/codex-"
                        f"{install_linux.VERSION}-linux-{architecture}.tgz")

    def test_wrong_name_version_or_integrity_is_rejected_for_both_sources(self):
        for registry in install_linux.REGISTRY_HOSTS:
            for field, value in (("name", "@attacker/codex"),
                                 ("version", "0.163.1-linux-arm64"),
                                 ("integrity", "sha512-" + "A" * 88)):
                with self.subTest(registry=registry, field=field):
                    changes = {field: value}
                    metadata = package_metadata(registry, "arm64", **changes)
                    with patch.object(install_linux, "_read_metadata", return_value=metadata):
                        with self.assertRaises(install_linux.InstallError):
                            install_linux._package_details(object(), "arm64", registry)

    def test_integrity_pin_does_not_roll_forward_with_the_package_version(self):
        with patch.object(install_linux, "VERSION", "0.164.0"), \
                patch.object(install_linux, "_read_metadata") as read:
            with self.assertRaises(install_linux.InstallError):
                install_linux._package_details(object(), "arm64", "npmmirror")
        read.assert_not_called()

    def test_tarball_must_use_exact_selected_registry_url(self):
        invalid_urls = (
            "https://evil.example/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "http://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "https://user:pass@registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "https://@registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz?x=1",
            "https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz#x",
            "https://registry.npmmirror.com:444/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "https://registry.npmmirror.com:/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "https://registry.npmmirror.com/@openai/codex/-/other.tgz",
            "https://cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                metadata = package_metadata("npmmirror", "arm64", tarball=url)
                with patch.object(install_linux, "_read_metadata", return_value=metadata):
                    with self.assertRaises(install_linux.InstallError):
                        install_linux._package_details(object(), "arm64", "npmmirror")

    def test_metadata_urls_are_limited_to_pinned_package_and_version(self):
        for url in (
            "https://evil.example/@openai/codex/0.163.0-linux-arm64",
            "https://registry.npmmirror.com/@openai%2Fcodex/0.163.1-linux-arm64",
            "http://registry.npmmirror.com/@openai%2Fcodex/0.163.0-linux-arm64",
            "https://u:p@registry.npmmirror.com/@openai%2Fcodex/0.163.0-linux-arm64",
            "https://registry.npmmirror.com:444/@openai%2Fcodex/0.163.0-linux-arm64",
            "https://registry.npmmirror.com/@openai%2Fcodex/0.163.0-linux-arm64?x=1",
            "https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
        ):
            with self.subTest(url=url):
                with self.assertRaises(install_linux.InstallError):
                    install_linux._validate_registry_url(
                        url, "npmmirror", "arm64", "metadata")


class RegistryRedirectTests(unittest.TestCase):
    def _redirect(self, registry: str, architecture: str, target: str):
        handler = install_linux._RegistryRedirects(registry, architecture)
        request = Request(f"https://{install_linux.REGISTRY_HOSTS[registry]}"
                          f"{install_linux._registry_metadata_path(architecture)}")
        return handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_exact_official_and_mirror_redirects_are_allowed(self):
        for architecture in ("arm64", "x64"):
            official = (
                f"https://{install_linux.REGISTRY}/"
                f"@openai/codex/-/codex-{install_linux.VERSION}-linux-{architecture}.tgz")
            mirror_registry = (
                f"https://registry.npmmirror.com/@openai/codex/-/"
                f"codex-{install_linux.VERSION}-linux-{architecture}.tgz")
            mirror_cdn = f"https://cdn.npmmirror.com{install_linux._mirror_cdn_path(architecture)}"
            with self.subTest(architecture=architecture):
                self.assertIsNotNone(self._redirect("official", architecture, official))
                self.assertIsNotNone(self._redirect("npmmirror", architecture, mirror_registry))
                self.assertIsNotNone(self._redirect("npmmirror", architecture, mirror_cdn))

    def test_bad_redirect_hosts_schemes_credentials_queries_and_paths_are_rejected(self):
        invalid = (
            "https://evil.example/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "http://cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz",
            "https://u:p@cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz",
            "https://@cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz",
            "https://cdn.npmmirror.com:444/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz",
            "https://cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz?x=1",
            "https://cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz#x",
            "https://cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-x64/codex-0.163.0-linux-x64.tgz",
            "https://registry.npmjs.org/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(install_linux.InstallError):
                    self._redirect("npmmirror", "arm64", url)

    def test_official_redirects_cannot_use_either_mirror_host(self):
        for url in (
            "https://registry.npmmirror.com/@openai/codex/-/codex-0.163.0-linux-arm64.tgz",
            "https://cdn.npmmirror.com/packages/%40openai/codex/0.163.0-linux-arm64/codex-0.163.0-linux-arm64.tgz",
        ):
            with self.subTest(url=url):
                with self.assertRaises(install_linux.InstallError):
                    self._redirect("official", "arm64", url)

    def test_opener_selection_keeps_proxy_disabled(self):
        for registry in ("official", "npmmirror"):
            opener = install_linux._registry_opener(registry, "arm64")
            redirect = next(h for h in opener.handlers
                            if isinstance(h, install_linux._RegistryRedirects))
            self.assertEqual(redirect.registry, registry)
            self.assertEqual(redirect.architectures, ("arm64",))
            self.assertFalse(any(h.__class__.__name__ == "ProxyHandler"
                                 for h in opener.handlers))

    def test_registry_runtime_forwards_selection_to_opener_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            opener = object()
            binary = staging / "bin" / "codex"
            log = lambda _message: None
            with (patch.object(install_linux, "_registry_opener", return_value=opener) as open_source,
                  patch.object(install_linux, "_package_details",
                               return_value=(install_linux.PINNED_RUNTIME_INTEGRITY[
                                   (install_linux.VERSION, "arm64")],
                                   "https://registry.npmmirror.com/@openai/codex/-/"
                                   "codex-0.163.0-linux-arm64.tgz")) as details,
                  patch.object(install_linux, "_download_tarball", return_value=root / "package.tgz"),
                  patch.object(install_linux, "_safe_extract", return_value=[]),
                  patch.object(install_linux, "_copy_runtime_files", return_value=binary)):
                result = install_linux._registry_runtime(
                    "arm64", root, staging, log, registry="npmmirror")
            self.assertEqual(result, binary)
            open_source.assert_called_once_with(registry="npmmirror", architecture="arm64")
            details.assert_called_once_with(opener, "arm64", registry="npmmirror")


class RegistryCliTests(unittest.TestCase):
    def test_registry_defaults_to_official_and_accepts_npmmirror(self):
        self.assertEqual(install_linux._parse_args([]).registry, "official")
        self.assertEqual(install_linux._parse_args(["--registry", "npmmirror"]).registry,
                         "npmmirror")

    def test_cli_selection_reaches_runtime_installer(self):
        class FakeLog:
            path = Path("/tmp/fake-install.log")
            def __call__(self, _message): pass
            def close(self): pass

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "app"
            with (patch.object(install_linux.sys, "platform", "linux"),
                  patch.object(install_linux, "_mkdir_owned", side_effect=lambda p: Path(p)),
                  patch.object(install_linux, "InstallLog", return_value=FakeLog()),
                  patch.object(install_linux, "_copy_backend"),
                  patch.object(install_linux, "_ensure_workspace_template"),
                  patch.object(install_linux, "_capability", return_value=("x" * 40, True)),
                  patch.object(install_linux, "_host_architecture", return_value="arm64"),
                  patch.object(install_linux, "_install_runtime",
                               return_value=(home / "runtime" / install_linux.VERSION, True)) as runtime,
                  patch.object(install_linux, "_write_runtime_json"),
                  patch.object(install_linux, "_start_and_check", return_value=(123, {})),
                  contextlib.redirect_stdout(io.StringIO())):
                result = install_linux.main(["--home", str(home), "--registry", "npmmirror"])
        self.assertEqual(result, 0)
        self.assertEqual(runtime.call_args.kwargs["registry"], "npmmirror")

    def test_install_runtime_forwards_source_through_registry_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            cache = root / "cache"
            cache.mkdir()
            def mkdir_owned(path):
                path = Path(path)
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                return path

            with (patch.object(install_linux, "_mkdir_owned", side_effect=mkdir_owned),
                  patch.object(install_linux, "_registry_runtime",
                               return_value=root / "staging-bin") as registry_runtime,
                  patch.object(install_linux, "_probe_codex")):
                version_dir, created = install_linux._install_runtime(
                    app, None, "arm64", cache, lambda _message: None,
                    registry="npmmirror")
        self.assertTrue(created)
        self.assertEqual(version_dir, app / "runtime" / install_linux.VERSION)
        self.assertEqual(registry_runtime.call_args.kwargs["registry"], "npmmirror")


if __name__ == "__main__":
    unittest.main()
