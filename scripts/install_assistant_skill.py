#!/usr/bin/env python3
"""Preview or install the DAXQO handoff skill into an existing OpenClaw skills folder."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shlex
import sys
import tempfile

DEFAULT_APP = Path("/root/phone-harness")
DEFAULT_SKILLS_DIR = Path("/root/.openclaw/workspace/skills")


class InstallError(Exception):
    pass


def reject_symlink_components(path: Path) -> None:
    """Reject symlinked path components so neither source nor target escapes its intent."""
    absolute = Path(os.path.abspath(path))
    for item in reversed((absolute, *absolute.parents)):
        if item.is_symlink():
            raise InstallError(f"Refusing symlink path: {item}")


def resolve_skill(app: Path, fallback: Path) -> Path:
    reject_symlink_components(app)
    app_skill = app / "daxqo_handoff_SKILL.md"
    reject_symlink_components(app_skill)
    if app_skill.is_file():
        return app_skill
    reject_symlink_components(fallback)
    if fallback.is_file():
        return fallback
    raise InstallError("Skill source not found in --app or repository fallback")


def render_skill(source: Path, app: Path) -> bytes:
    text = source.read_text(encoding="utf-8")
    # Quote the concrete installed CLI path for the Skill's shell examples.
    cli = shlex.quote((app / "assistant_client.py").as_posix())
    text = text.replace("/root/phone-harness/assistant_client.py", cli)
    return text.encode("utf-8")


def backup_name(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name(f"{target.name}.backup-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.backup-{stamp}-{suffix}")
        suffix += 1
    return candidate


def install(app: Path, skills_dir: Path, *, do_install: bool, fallback: Path) -> str:
    app = Path(os.path.abspath(app))
    source = resolve_skill(app, fallback)
    reject_symlink_components(skills_dir)
    if not skills_dir.is_dir():
        raise InstallError("--skills-dir must name an existing skills directory")
    target_dir = skills_dir / "daxqo-handoff"
    target = target_dir / "SKILL.md"
    reject_symlink_components(target_dir)
    reject_symlink_components(target)
    content = render_skill(source, app)

    if target.is_file():
        if target.read_bytes() == content:
            return f"Already current: {target}"
        backup = backup_name(target)
        if not do_install:
            return f"Would preserve existing file as {backup} and install {target}"
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.replace(backup)
    elif not do_install:
        return f"Would install {source} to {target}"

    if do_install:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".SKILL-", dir=target_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        target.chmod(0o600)
        return f"Installed {target}" + (f"; preserved previous file as {backup}" if 'backup' in locals() else "")
    return f"Would install {source} to {target}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=DEFAULT_APP, help="Installed DAXQO app directory")
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR,
                        help="Existing OpenClaw workspace skills directory")
    parser.add_argument("--install", action="store_true", help="Write the Skill; default is preview only")
    args = parser.parse_args(argv)
    try:
        print(install(args.app, args.skills_dir, do_install=args.install,
                      fallback=Path(__file__).resolve().parents[1] / "skills" / "daxqo-handoff" / "SKILL.md"))
        return 0
    except (InstallError, OSError, UnicodeError) as exc:
        print(f"Install refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
