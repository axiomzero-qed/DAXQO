#!/usr/bin/env python3
"""Install and start DAXQO in an already-running Linux environment."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import (HTTPRedirectHandler, ProxyHandler, Request,
                            build_opener)

VERSION = "0.163.0"
APP_VERSION = "0.6.1"
REGISTRY = "registry.npmjs.org"
BACKEND_PORT = 18880
FLAT_FILES = (
    "server.py", "supervisor.py", "runtime_control.py", "test_server.py",
    "test_supervisor.py", "test_runtime_control.py", "models-deepseek.json",
    "WORKSPACE_AGENTS.md",
    "artifacts.py", "test_artifacts.py", "test_email_attachments.py",
    "task_services.py", "test_task_services.py",
    "android_mcp_server.py", "test_android_mcp.py", "phone_backends.py", "test_phone_backends.py",
    "remote_protocol.py", "test_remote_protocol.py", "email_control.py", "test_email_control.py",
    "test_remote_email_mcp.py", "agent_context.py", "test_agent_context.py", "assistant_mcp_server.py", "test_assistant_mcp.py",
)
SCRIPT_FLAT_FILES = ("remote_email_mcp.py", "assistant_client.py", "test_assistant_client.py", "install_assistant_skill.py", "test_install_assistant_skill.py")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
MAX_METADATA = 4 * 1024 * 1024
MAX_TARBALL = 1024 * 1024 * 1024
MAX_UNPACKED = 1536 * 1024 * 1024
MAX_FILE = 768 * 1024 * 1024
MAX_MEMBERS = 50000
SERVER_ID = "DAXQO/"


class InstallError(RuntimeError):
    pass


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_no_symlink_components(path: Path) -> None:
    path = _absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"Symlink in installation path: {current}")


def _mkdir_owned(path: Path) -> Path:
    path = _absolute(path)
    _check_no_symlink_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise InstallError(f"Directory must be owned by the current user: {path}")
    path.chmod(0o700)
    return path


def _regular_file(path: Path, *, executable: bool = False) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode)
            and (not executable or bool(info.st_mode & 0o111)))


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    parent = _mkdir_owned(path.parent)
    if path.is_symlink():
        raise InstallError(f"Refusing to replace a symlink: {path}")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _capability(state: Path) -> tuple[str, bool]:
    path = state / "capability"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        _check_no_symlink_components(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise InstallError("Existing capability file is not a regular file owned by this user")
        try:
            token = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise InstallError("Existing capability file cannot be read") from exc
        if len(token) < 32 or not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
            raise InstallError("Existing capability file is invalid; it was left unchanged")
        path.chmod(0o600)
        return token, False
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(token + "\n")
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o600)
    return token, True


def _source_files(repo: Path) -> list[tuple[Path, str]]:
    backend = repo / "backend"
    if backend.is_symlink() or not backend.is_dir():
        raise InstallError("backend source directory is missing or unsafe")
    files: list[tuple[Path, str]] = []
    for name in FLAT_FILES:
        source = backend / name
        if not _regular_file(source):
            raise InstallError(f"Required backend source is missing or unsafe: {name}")
        files.append((source, name))
    scripts = repo / "scripts"
    if scripts.is_symlink() or not scripts.is_dir():
        raise InstallError("scripts source directory is missing or unsafe")
    for name in SCRIPT_FLAT_FILES:
        source = scripts / name
        if not _regular_file(source):
            raise InstallError(f"Required script source is missing or unsafe: {name}")
        files.append((source, name))
    static = backend / "static"
    skill = repo / "skills" / "daxqo-handoff" / "SKILL.md"
    if not _regular_file(skill):
        raise InstallError("DAXQO handoff Skill source is missing or unsafe")
    files.append((skill, "daxqo_handoff_SKILL.md"))
    if static.is_symlink() or not static.is_dir():
        raise InstallError("backend/static is missing or unsafe")
    for current, directories, names in os.walk(static, followlinks=False):
        root = Path(current)
        for name in directories:
            item = root / name
            if item.is_symlink() or not item.is_dir():
                raise InstallError("backend/static contains an unsafe directory")
        for name in names:
            source = root / name
            if not _regular_file(source):
                raise InstallError("backend/static contains a non-regular file")
            files.append((source, source.relative_to(backend).as_posix()))
    return files


def _copy_backend(repo: Path, app: Path, log) -> None:
    files = _source_files(repo)
    for source, relative in files:
        target = app.joinpath(*PurePosixPath(relative).parts)
        parent = _mkdir_owned(target.parent)
        if target.is_symlink():
            raise InstallError(f"Refusing to replace a symlink: {target}")
        fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", dir=parent)
        try:
            with source.open("rb") as original, os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(original, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o600)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    log(f"Installed {len(files)} backend files")


def _ensure_workspace_template(repo: Path, workspace: Path, log) -> None:
    template = repo / "backend" / "WORKSPACE_AGENTS.md"
    target = workspace / "AGENTS.md"
    if target.exists() or target.is_symlink():
        log("Preserved existing workspace/AGENTS.md")
        return
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        log("Preserved workspace/AGENTS.md created by another process")
        return
    with os.fdopen(fd, "wb") as output, template.open("rb") as source:
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    log("Created workspace/AGENTS.md from the repository template")


class _RegistryRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if (parsed.scheme != "https" or parsed.hostname != REGISTRY
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise InstallError("Registry attempted a redirect outside registry.npmjs.org")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _registry_opener():
    return build_opener(ProxyHandler({}), _RegistryRedirects())


def _read_metadata(opener, url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "DAXQO-installer/0.2"})
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_METADATA + 1)
    except (OSError, URLError, HTTPError) as exc:
        raise InstallError("Could not retrieve package metadata from the official npm registry") from exc
    if len(raw) > MAX_METADATA:
        raise InstallError("Registry package metadata is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise InstallError("Official npm package metadata is invalid") from exc
    if not isinstance(value, dict):
        raise InstallError("Official npm package metadata is invalid")
    return value


def _package_details(opener, architecture: str) -> tuple[str, str]:
    package_name = "@openai/codex"
    package_version = f"{VERSION}-linux-{architecture}"
    encoded_name = quote(package_name, safe="@")
    metadata = _read_metadata(opener, f"https://{REGISTRY}/{encoded_name}/{package_version}")
    if metadata.get("name") != package_name or metadata.get("version") != package_version:
        raise InstallError("Official npm package name or version did not match the pinned runtime")
    dist = metadata.get("dist")
    integrity = dist.get("integrity") if isinstance(dist, dict) else None
    tarball = dist.get("tarball") if isinstance(dist, dict) else None
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise InstallError("Official npm package does not provide SHA-512 integrity metadata")
    try:
        base64.b64decode(integrity[7:], validate=True)
    except ValueError as exc:
        raise InstallError("Official npm package SHA-512 metadata is invalid") from exc
    parsed = urlsplit(tarball or "")
    expected_path = f"/@openai/codex/-/codex-{package_version}.tgz"
    if (parsed.scheme != "https" or parsed.hostname != REGISTRY
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or unquote(parsed.path) != expected_path or parsed.query or parsed.fragment):
        raise InstallError("Official npm package tarball URL is not the expected registry URL")
    return integrity, tarball


def _download_tarball(opener, url: str, integrity: str, cache: Path) -> Path:
    archive = cache / f"codex-{VERSION}-{platform.machine().lower()}.tgz"
    if archive.exists() or archive.is_symlink():
        if archive.is_symlink() or not _regular_file(archive):
            raise InstallError("Runtime cache entry is not a regular file")
        if _verify_file(archive, integrity):
            return archive
        archive.unlink()
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "DAXQO-installer/0.2"})
    fd, temporary = tempfile.mkstemp(prefix=".codex-download-", dir=cache)
    digest = hashlib.sha512()
    total = 0
    try:
        with os.fdopen(fd, "wb") as output, opener.open(request, timeout=90) as response:
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > MAX_TARBALL:
                        raise InstallError("Runtime package exceeds the download size limit")
                except ValueError:
                    raise InstallError("Registry returned an invalid package size")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TARBALL:
                    raise InstallError("Runtime package exceeds the download size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o600)
        expected = base64.b64decode(integrity[7:], validate=True)
        if not hmac.compare_digest(digest.digest(), expected):
            raise InstallError("Runtime package failed npm SHA-512 integrity verification")
        os.replace(temporary, archive)
        return archive
    except InstallError:
        raise
    except (OSError, URLError, HTTPError) as exc:
        raise InstallError("Could not download the official Codex runtime tarball") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _verify_file(path: Path, integrity: str) -> bool:
    try:
        digest = hashlib.sha512()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        expected = base64.b64decode(integrity[7:], validate=True)
        return hmac.compare_digest(digest.digest(), expected)
    except (OSError, ValueError):
        return False


def _member_parts(name: str, is_directory: bool) -> tuple[str, ...]:
    raw = name[:-1] if is_directory and name.endswith("/") else name
    if not raw or "\\" in raw or "\x00" in raw or raw.startswith("/"):
        raise InstallError("Runtime tarball contains an unsafe path")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InstallError("Runtime tarball contains an unsafe path")
    if parts[0].endswith(":"):
        raise InstallError("Runtime tarball contains an absolute path")
    return tuple(parts)


def _safe_extract(archive: Path, destination: Path) -> list[tuple[tuple[str, ...], int, int]]:
    """Extract only regular files/directories after validating the complete archive."""
    destination = _mkdir_owned(destination)
    entries: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()
    total = 0
    try:
        source = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise InstallError("Official runtime tarball is not a valid gzip tar archive") from exc
    with source:
        for count, member in enumerate(source, start=1):
            if count > MAX_MEMBERS:
                raise InstallError("Runtime tarball contains too many entries")
            if member.isdir():
                parts = _member_parts(member.name, True)
            elif member.isfile():
                parts = _member_parts(member.name, False)
                if member.size < 0 or member.size > MAX_FILE:
                    raise InstallError("Runtime tarball contains a file over the size limit")
                total += member.size
                if total > MAX_UNPACKED:
                    raise InstallError("Runtime tarball exceeds the unpacked size limit")
            else:
                raise InstallError("Runtime tarball contains a link or special file")
            if parts in seen:
                raise InstallError("Runtime tarball contains duplicate paths")
            seen.add(parts)
            entries.append((member, parts))
        kinds = {parts: ("directory" if member.isdir() else "file")
                 for member, parts in entries}
        for _member, parts in entries:
            for index in range(1, len(parts)):
                ancestor = parts[:index]
                if kinds.get(ancestor) == "file":
                    raise InstallError("Runtime tarball contains a file/directory path conflict")
        regular = [(member, parts) for member, parts in entries if member.isfile()]
        codex_files = [(member, parts) for member, parts in regular
                       if parts[-1] == "codex" and member.mode & 0o111]
        if len(codex_files) != 1:
            raise InstallError("Runtime tarball must contain exactly one executable named codex")
        for member, parts in entries:
            target = destination.joinpath(*parts)
            if not _inside(target, destination):
                raise InstallError("Runtime tarball path escaped its destination")
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.chmod(0o700)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise InstallError("Runtime tarball contains a file/directory conflict")
            input_file = source.extractfile(member)
            if input_file is None:
                raise InstallError("Runtime tarball file could not be read")
            with input_file, target.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = input_file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise InstallError("Runtime tarball file was truncated")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return [(parts, member.mode, member.size) for member, parts in entries if member.isfile()]


def _walk_regular_tree(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise InstallError("Runtime directory is missing or unsafe")
    _check_no_symlink_components(root)
    files: list[Path] = []
    total = 0
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            item = current_path / name
            if item.is_symlink() or not item.is_dir():
                raise InstallError("Runtime directory contains a symlink or special directory")
        for name in names:
            item = current_path / name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise InstallError("Runtime directory contains a symlink or special file")
            if info.st_size > MAX_FILE:
                raise InstallError("Runtime directory contains a file over the size limit")
            total += info.st_size
            if total > MAX_UNPACKED:
                raise InstallError("Runtime directory exceeds the copy size limit")
            files.append(item)
            if len(files) > MAX_MEMBERS:
                raise InstallError("Runtime directory contains too many files")
    return files


def _copy_runtime_files(files: list[Path], source_root: Path, destination: Path) -> Path:
    executable_candidates = [item for item in files if item.name == "codex" and os.access(item, os.X_OK)]
    if len(executable_candidates) != 1:
        raise InstallError("Runtime input must contain exactly one executable named codex")
    binary_source = executable_candidates[0]
    for source in files:
        relative = source.relative_to(source_root)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with source.open("rb") as original, target.open("xb") as output:
            shutil.copyfileobj(original, output, 1024 * 1024)
        target.chmod(0o755 if os.access(source, os.X_OK) else 0o644)
    binary_relative = binary_source.relative_to(source_root)
    binary = destination.joinpath(*binary_relative.parts)
    if binary_relative.parts != ("bin", "codex"):
        canonical = destination / "bin" / "codex"
        canonical.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if canonical.exists() or canonical.is_symlink():
            raise InstallError("Runtime already contains a conflicting bin/codex path")
        shutil.copyfile(binary, canonical)
        canonical.chmod(0o755)
        binary = canonical
    return binary


def _probe_codex(binary: Path) -> None:
    if not _regular_file(binary, executable=True):
        raise InstallError("Codex runtime executable is missing or not executable")
    safe_env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
                if key in os.environ}
    try:
        result = subprocess.run([str(binary), "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", timeout=15,
                                check=False, env=safe_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("Codex runtime failed its version probe") from exc
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 or not re.search(r"(?<!\d)" + re.escape(VERSION) + r"(?!\d)", output):
        raise InstallError("Runtime is not the pinned Codex version 0.163.0")


def _runtime_dir_source(source: Path, destination: Path) -> Path:
    source = _absolute(source)
    files = _walk_regular_tree(source)
    return _copy_runtime_files(files, source, destination)


def _registry_runtime(architecture: str, cache: Path, staging: Path, log) -> Path:
    opener = _registry_opener()
    package_version = f"{VERSION}-linux-{architecture}"
    log(f"Looking up pinned official package @openai/codex@{package_version}")
    integrity, tarball_url = _package_details(opener, architecture)
    archive = _download_tarball(opener, tarball_url, integrity, cache)
    extract_root = Path(tempfile.mkdtemp(prefix=".codex-extract-", dir=cache))
    try:
        entries = _safe_extract(archive, extract_root)
        package_root = extract_root / "package"
        if not package_root.exists():
            package_root = extract_root
        prefix = package_root.relative_to(extract_root).parts
        files = [package_root.joinpath(*parts[len(prefix):])
                 for parts, _mode, _size in entries
                 if parts[:len(prefix)] == prefix]
        files = [item for item in files if _regular_file(item)]
        binary = _copy_runtime_files(files, package_root, staging)
        log("Verified and installed the official npm tarball using its SHA-512 integrity")
        return binary
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


def _host_architecture() -> str:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x64"
    raise InstallError(f"Unsupported Linux architecture: {machine or 'unknown'}")


def _runtime_version_dir(app: Path) -> Path:
    return app / "runtime" / VERSION


def _install_runtime(app: Path, runtime_dir: Path | None, architecture: str,
                     cache: Path, log) -> tuple[Path, bool]:
    runtime_root = _mkdir_owned(app / "runtime")
    final = _runtime_version_dir(app)
    if final.exists() or final.is_symlink():
        _check_no_symlink_components(final)
        info = final.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise InstallError("Existing runtime version directory is unsafe")
        binary = final / "bin" / "codex"
        try:
            _probe_codex(binary)
            return final, False
        except InstallError:
            raise InstallError("Existing 0.163.0 runtime is invalid; it was left unchanged")

    staging = Path(tempfile.mkdtemp(prefix=".0.163.0-install-", dir=runtime_root))
    try:
        if runtime_dir:
            log("Reusing the supplied extracted Codex runtime directory")
            binary = _runtime_dir_source(runtime_dir, staging)
        else:
            binary = _registry_runtime(architecture, cache, staging, log)
        _probe_codex(binary)
        os.rename(staging, final)
        return final, True
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_runtime_json(app: Path, version_dir: Path) -> None:
    binary = version_dir / "bin" / "codex"
    data = {"version": VERSION, "binary": str(binary), "persistent_runtime": str(version_dir)}
    path = app / "runtime.json"
    if path.is_symlink():
        raise InstallError("Refusing to replace a symlink runtime.json")
    _atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), 0o600)


def _opener_without_redirects():
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            return None
    return build_opener(ProxyHandler({}), NoRedirect())


def _preflight_backend() -> bool | None:
    connection = http.client.HTTPConnection("127.0.0.1", BACKEND_PORT, timeout=2)
    try:
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{BACKEND_PORT}"})
        response = connection.getresponse()
        is_daxqo = response.getheader("Server", "").startswith(SERVER_ID)
        response.read(4096)
        return is_daxqo
    except (ConnectionRefusedError, ConnectionResetError):
        return None
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _backend_status(capability: str) -> dict | None:
    opener = _opener_without_redirects()
    request = Request(f"http://127.0.0.1:{BACKEND_PORT}/api/status",
                      headers={"Host": f"127.0.0.1:{BACKEND_PORT}",
                               "Authorization": "Bearer " + capability,
                               "Accept": "application/json"})
    try:
        with opener.open(request, timeout=3) as response:
            if response.getheader("Server", "").startswith(SERVER_ID) is False:
                raise InstallError("Port 18880 responded as an unknown service")
            if response.status != 200:
                raise InstallError("DAXQO backend health check returned an error")
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        if exc.headers.get("Server", "").startswith(SERVER_ID):
            if exc.code == 401:
                raise InstallError("DAXQO is already listening, but its local capability differs") from exc
            raise InstallError("DAXQO backend health check returned an error") from exc
        raise InstallError("Port 18880 responded as an unknown service") from exc
    except (OSError, URLError) as exc:
        return None
    if len(raw) > 1024 * 1024:
        raise InstallError("DAXQO backend health response is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise InstallError("DAXQO backend health response is invalid") from exc
    app_data = data.get("app") if isinstance(data, dict) else None
    if (not isinstance(app_data, dict) or app_data.get("name") != "DAXQO"
            or app_data.get("version") != APP_VERSION):
        raise InstallError("Port 18880 did not return the DAXQO application status")
    return data


def _supervisor_environment(app: Path) -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("PATH", os.defpath)
    env.setdefault("HOME", str(Path.home()))
    env["PYTHONUNBUFFERED"] = "1"
    env["DAXQO_STAGE"] = str(app / "update-stage")
    return env


def _start_supervisor(app: Path, sandbox: str, log_path: Path) -> int:
    supervisor = app / "supervisor.py"
    if not _regular_file(supervisor):
        raise InstallError("Installed supervisor.py is missing")
    _check_no_symlink_components(supervisor)
    _mkdir_owned(app / "state")
    _mkdir_owned(app / "update-stage")
    if log_path.is_symlink():
        raise InstallError("Supervisor log path is a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        log_fd = os.open(log_path, flags, 0o600)
    except OSError as exc:
        raise InstallError("Could not safely open the supervisor log") from exc
    with os.fdopen(log_fd, "ab", buffering=0) as output:
        os.fchmod(output.fileno(), 0o600)
        process = subprocess.Popen(
            [sys.executable, str(supervisor), "--app", str(app), "--sandbox", sandbox],
            cwd=str(app), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            close_fds=True, start_new_session=True, env=_supervisor_environment(app),
        )
    return process.pid


def _start_and_check(app: Path, capability: str, sandbox: str,
                     log_path: Path, log, timeout: int = 90) -> tuple[int | None, dict]:
    preflight = _preflight_backend()
    if preflight is False:
        raise InstallError("Port 18880 is occupied by an unknown service; no capability was sent")
    if preflight is True:
        status = _backend_status(capability)
        if status is None:
            raise InstallError("Existing DAXQO service did not complete its health check")
        return None, status

    pid = _start_supervisor(app, sandbox, log_path)
    log(f"Started the DAXQO supervisor in an independent session (PID {pid})")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        preflight = _preflight_backend()
        if preflight is False:
            time.sleep(1)
            continue
        if preflight is True:
            status = _backend_status(capability)
            if status is not None:
                return pid, status
        time.sleep(1)
    raise InstallError(f"DAXQO did not pass its health check within {timeout} seconds; supervisor remains running")


class InstallLog:
    def __init__(self, path: Path):
        _mkdir_owned(path.parent)
        if path.is_symlink():
            raise InstallError("Install log path is a symlink")
        self.path = path
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise InstallError("Could not safely open the install log") from exc
        self.stream = os.fdopen(fd, "a", encoding="utf-8")
        os.fchmod(fd, 0o600)

    def __call__(self, message: str) -> None:
        line = time.strftime("%Y-%m-%dT%H:%M:%S%z ") + message
        self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".local" / "share" / "daxqo",
                        help="DAXQO app/state/workspace directory (default: ~/.local/share/daxqo)")
    parser.add_argument("--runtime-dir", type=Path,
                        help="Reuse an already extracted official Codex runtime directory")
    parser.add_argument("--sandbox", choices=SANDBOXES, default="danger-full-access",
                        help="Codex execution permission (default: danger-full-access; no automatic fallback)")
    parser.add_argument("--print-pairing", action="store_true",
                        help="Explicitly display the local one-time Android pairing link containing the capability")
    parser.add_argument("--pairing-scheme", choices=("daxqo", "daxqo-xm"), default="daxqo",
                        help="Select the Android app to pair with")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not sys.platform.startswith("linux"):
        raise SystemExit("This installer only runs inside an already-running Linux environment.")
    repo = Path(__file__).resolve().parents[1]
    home = _absolute(args.home)
    _mkdir_owned(home)
    log = InstallLog(home / "logs" / "install.log")
    try:
        state = _mkdir_owned(home / "state")
        workspace = _mkdir_owned(home / "workspace")
        cache = _mkdir_owned(home / "cache")
        app = home
        _copy_backend(repo, app, log)
        _ensure_workspace_template(repo, workspace, log)
        capability, created = _capability(state)
        arch = _host_architecture()
        runtime_dir = _absolute(args.runtime_dir) if args.runtime_dir else None
        version_dir, runtime_created = _install_runtime(app, runtime_dir, arch, cache, log)
        _write_runtime_json(app, version_dir)
        pid, status = _start_and_check(app, capability, args.sandbox, state / "supervisor.log", log)
        log(f"Health check passed for DAXQO {status.get('app', {}).get('version', APP_VERSION)}")

        print(f"DAXQO is ready at {app}")
        print(f"Codex runtime: {VERSION} linux-{arch}" + (" (installed)" if runtime_created else " (reused)"))
        print(f"Sandbox: {args.sandbox}")
        print("Supervisor: already running" if pid is None else f"Supervisor PID: {pid}")
        print(f"Install log: {log.path}")
        if created:
            print("A private local capability was created in state/capability.")
        else:
            print("The existing private capability was preserved.")
        if args.print_pairing:
            print("Pairing link (contains the local capability; keep it private):")
            print(args.pairing_scheme + "://connect#token=" + capability)
        else:
            print("Run again with --print-pairing only when you intentionally need to display the pairing link.")
        return 0
    except InstallError as exc:
        log("Installation stopped: " + str(exc))
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
