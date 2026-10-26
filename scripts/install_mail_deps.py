#!/usr/bin/env python3
"""Install pinned mail verification libraries inside an existing DAXQO APP."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys

import install_linux


class MailDepsError(RuntimeError):
    pass


OFFICIAL_INDEX_URL = "https://pypi.org/simple"
USTC_INDEX_URL = "https://mirrors.ustc.edu.cn/pypi/simple"
INDEX_URLS = (OFFICIAL_INDEX_URL, USTC_INDEX_URL)


def _owned_directory(path: Path, *, required: bool) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise MailDepsError(f"Required APP directory does not exist: {path}")
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MailDepsError(f"Expected a real directory: {path}")
    if info.st_uid != os.getuid():
        raise MailDepsError(f"Directory must be owned by the current user: {path}")
    return True


def resolve_paths(app_arg: Path) -> tuple[Path, Path, Path]:
    app = Path(os.path.abspath(os.fspath(app_arg.expanduser())))
    install_linux._check_no_symlink_components(app)
    _owned_directory(app, required=True)

    runtime = app / "runtime"
    install_linux._check_no_symlink_components(runtime)
    _owned_directory(runtime, required=False)

    target = runtime / "mail-libs"
    install_linux._check_no_symlink_components(target)
    _owned_directory(target, required=False)

    requirements = Path(__file__).resolve().with_name("mail-requirements.txt")
    if not requirements.is_file() or requirements.is_symlink():
        raise MailDepsError(f"Pinned requirements file is missing or unsafe: {requirements}")
    return app, target, requirements


def install(app_arg: Path, index_url: str = OFFICIAL_INDEX_URL) -> None:
    if index_url not in INDEX_URLS:
        raise MailDepsError("Unsupported PyPI index URL")
    app, target, requirements = resolve_paths(app_arg)
    runtime = app / "runtime"
    install_linux._mkdir_owned(runtime)
    install_linux._mkdir_owned(target)

    command = [
        sys.executable, "-m", "pip", "install",
        "--require-hashes", "--no-deps", "--disable-pip-version-check",
        "--no-compile", "--index-url", index_url,
        "--target", str(target), "-r", str(requirements),
    ]
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    print(f"Installing pinned mail libraries into {target}", flush=True)
    subprocess.run(command, check=True, env=env)
    print("Mail libraries installed. Restart the DAXQO backend for them to take effect.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install pinned mail verification dependencies under APP/runtime/mail-libs."
    )
    parser.add_argument(
        "--app", type=Path, default=Path.home() / "phone-harness",
        help="existing DAXQO APP directory (default: ~/phone-harness)",
    )
    parser.add_argument(
        "--index-url", choices=INDEX_URLS, default=OFFICIAL_INDEX_URL,
        help="fixed package index URL (default: official PyPI)",
    )
    args = parser.parse_args(argv)
    try:
        install(args.app, index_url=args.index_url)
    except (MailDepsError, install_linux.InstallError, OSError,
            subprocess.CalledProcessError) as exc:
        print(f"Mail dependency installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
