"""Resolve explicitly referenced workspace deliverables for the GUI and email.

No model calls, recursive workspace discovery, or external URL fetching.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
import re
import stat
from typing import Callable
from urllib.parse import unquote, urlsplit, parse_qs

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ARTIFACTS = 128
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
Resolver = Callable[[str], tuple[Path, str]]


def file_type(path: str) -> tuple[str, bool]:
    image = IMAGE_TYPES.get(Path(path).suffix.lower())
    return image or mimetypes.guess_type(path)[0] or "application/octet-stream", bool(image)


def image_type(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if raw.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")): return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP": return "image/webp"
    if raw.startswith(b"BM"): return "image/bmp"
    raise ValueError("Only PNG, JPEG, GIF, WebP and BMP images can be previewed")


def read_file(resolve: Resolver, relative: str, limit: int = MAX_FILE_BYTES) -> tuple[str, bytes]:
    path, display = resolve(relative)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode): raise ValueError("Path must be a regular file")
    if before.st_size > limit: raise OverflowError("File exceeds 20 MiB")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        current, _ = resolve(relative)
        after = current.stat()
        if (current != path or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)):
            raise ValueError("File changed while opening")
        raw = handle.read(limit + 1)
    if len(raw) > limit: raise OverflowError("File exceeds 20 MiB")
    return display, raw


def _references(text: str, workspace: Path):
    text = text[:200000]
    markdown = re.compile(r'!?\[[^\]\r\n]*\]\(\s*(<[^>\r\n]+>|[^)\r\n]+)\s*\)')
    inline = re.compile(r'(?<!`)`([^`\r\n]+)`(?!`)')
    for match in markdown.finditer(text):
        value = match.group(1).strip()
        yield value[1:-1] if value.startswith("<") and value.endswith(">") else re.sub(r'\s+"[^"\r\n]*"$', "", value)
    for match in inline.finditer(text):
        yield match.group(1)
    text = inline.sub(' ', markdown.sub(' ', text))
    text = re.sub(r'\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>]+', ' ', text)
    # A bare absolute path in prose is common in agent results.
    root = re.escape(workspace.as_posix().rstrip("/"))
    for match in re.finditer(root + r'(?:/[^\s<>\[\]`"\'，。；！？）]+)?', text):
        yield match.group(0).rstrip(".,;:!?)")
    # Also accept existing relative paths and simple filenames, never URLs.
    for match in re.finditer(r'(?<![\w:/])(?:\./)?[\w\-\u0080-\uffff]+(?:/[\w.\-\u0080-\uffff]+)*\.[A-Za-z0-9]{1,12}(?![\w/])', text):
        yield match.group(0).rstrip("，。；！？）")


def _relative(value: str, root: Path) -> str:
    value = value.strip()
    if value.startswith("sandbox:"): value = value[len("sandbox:"):]
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost"): raise ValueError("External file host")
        value = parsed.path
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value): value = value[1:]
    elif value.startswith("/api/files/"):
        value = parse_qs(urlsplit(value).query).get("path", [""])[0]
    elif "://" in value:
        raise ValueError("External URL")
    value = unquote(value).replace("\\", "/")
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    candidate = Path(value)
    if ".." in candidate.parts: raise ValueError("Parent traversal")
    if candidate.is_absolute():
        return candidate.resolve(strict=True).relative_to(root).as_posix()
    if not value or value.startswith("/"): raise ValueError("Empty or foreign absolute path")
    return value


def referenced_files(text: str, workspace: Path, resolve: Resolver, *, expand_dirs: bool = False) -> list[dict]:
    if not isinstance(text, str) or not text: return []
    root = workspace.resolve(strict=True)
    found: list[dict] = []
    seen: set[str] = set()

    def add(relative: str, depth: int = 0):
        if len(found) >= MAX_ARTIFACTS or depth > 16: return
        try:
            path, display = resolve(relative)
            # Runtime instructions and hidden/private inputs are not deliverables.
            if any(part.startswith(".") for part in Path(display).parts) or path.name == "AGENTS.md": return
            key = display.casefold() if os.name == "nt" else display
            if key in seen: return
            seen.add(key)
            info = path.stat()
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and not stat.S_ISREG(info.st_mode): return
            if directory and expand_dirs:
                for entry in sorted(path.iterdir(), key=lambda p: p.name.casefold()):
                    if not entry.is_symlink():
                        add(entry.relative_to(root).as_posix(), depth + 1)
                    if len(found) >= MAX_ARTIFACTS: break
                return
            mime, image = file_type(display)
            found.append({"path": display, "name": path.name or "工作区", "size": 0 if directory else info.st_size,
                          "mime": "inode/directory" if directory else mime,
                          "isDirectory": directory, "isImage": image and not directory,
                          "modifiedAt": info.st_mtime_ns})
        except (OSError, ValueError, RuntimeError):
            return

    for reference in _references(text, root):
        try: relative = _relative(reference, root)
        except (ValueError, OSError): continue
        add(relative)
        if len(found) >= MAX_ARTIFACTS: break
    return found
