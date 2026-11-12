#!/usr/bin/env python3
"""Check work-folder image checklists. Requires Python 3.9+. Stdlib only."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

if sys.version_info < (3, 9):
    sys.stderr.write("需要 Python 3.9 或更高版本。\n")
    raise SystemExit(2)

SIZE_MIN = 500_000
SIZE_MAX = 2_000_000
CHECKLIST_NAME = "00-清单.md"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MIME_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
}

CHECK_RE = re.compile(r"^-\s+\[(?P<check>[ xX])\]\s+(?P<rest>.+)$")
LINK_RE = re.compile(r"^\[(?P<text>[^\]]+)\]\((?P<path>[^)]+)\)")
ALIAS_RE = re.compile(r"别名[:：]\s*(.+)$")
ALIAS_SPLIT = re.compile(r"[、,，/／]+")
SEPARATORS = re.compile(r"[\s·・‧•∙⋅\-–—/／]+")
BRACKETS = re.compile(r"[\[\]【】()（）『』「」<>《》]+")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def sniff_mime(path: Path) -> str | None:
    with path.open("rb") as handle:
        header = handle.read(12)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def extension_mime(path: Path) -> str | None:
    suffix = path.suffix.lower()
    for mime, extensions in MIME_EXTENSIONS.items():
        if suffix in extensions:
            return mime
    return None


def to_halfwidth(text: str) -> str:
    chars = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(char)
    return "".join(chars)


def normalize_name(text: str, work: str | None = None) -> str:
    """Normalize a character name for flexible matching. Safe to copy into NAG."""
    value = SEPARATORS.sub("", to_halfwidth(text or "").strip())
    value = BRACKETS.sub("", value).casefold()
    if work:
        token = normalize_name(work)
        if token and value.startswith(token) and len(value) > len(token):
            value = value[len(token):]
        elif token and value.endswith(token) and len(value) > len(token):
            value = value[: -len(token)]
    return value


def entry_keys(entry: dict, work: str | None = None) -> set[str]:
    keys = {normalize_name(entry.get("name") or "", work)}
    for alias in entry.get("aliases") or []:
        keys.add(normalize_name(alias, work))
    path = entry.get("image_path")
    if path:
        keys.add(normalize_name(Path(path).stem, work))
    keys.discard("")
    return keys


def match_name(query: str, entries: list[dict], work: str | None = None) -> tuple[dict | None, str]:
    """Match a roster query to one checklist entry.

    Returns (entry, "match") on a unique hit, or (None, "none" / "ambiguous").
    """
    needle = normalize_name(query, work)
    if not needle:
        return None, "none"
    exact = [entry for entry in entries if needle in entry_keys(entry, work)]
    if len(exact) == 1:
        return exact[0], "match"
    if len(exact) > 1:
        return None, "ambiguous"
    contained = []
    for entry in entries:
        if any(needle in key or key in needle for key in entry_keys(entry, work)):
            contained.append(entry)
    if len(contained) == 1:
        return contained[0], "match"
    if len(contained) > 1:
        return None, "ambiguous"
    return None, "none"


def parse_aliases(text: str) -> list[str]:
    match = ALIAS_RE.search(text)
    if not match:
        return []
    return [part.strip() for part in ALIAS_SPLIT.split(match.group(1)) if part.strip()]


def parse_checklist(text: str) -> list[dict]:
    items = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("\ufeff")
        match = CHECK_RE.match(line)
        if not match:
            continue
        rest = match.group("rest").strip()
        link = LINK_RE.match(rest)
        if link:
            name = link.group("text").strip()
            image_path = unquote(link.group("path").strip()) or None
            remainder = rest[link.end():].strip()
        else:
            image_path = None
            remainder = rest
            alias_match = ALIAS_RE.search(remainder)
            name = remainder[: alias_match.start()].strip() if alias_match else remainder
        items.append(
            {
                "lineno": lineno,
                "checked": match.group("check") in {"x", "X"},
                "name": name,
                "image_path": image_path,
                "aliases": parse_aliases(remainder),
            }
        )
    return items


def resolve_image(work_dir: Path, rel: str) -> Path | None:
    if not rel:
        return None
    rel = unquote(rel).strip()
    if rel.startswith("/") or rel.startswith("\\") or "\\" in rel:
        return None
    if "://" in rel or rel.startswith("data:"):
        return None
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    resolved = (work_dir / relative).resolve()
    if not resolved.is_relative_to(work_dir.resolve()):
        return None
    return resolved


def inspect_checked(work_dir: Path, rel: str) -> list[str]:
    errors = []
    resolved = resolve_image(work_dir, rel)
    if resolved is None:
        return [f"图片路径不合法或越出作品目录：{rel}"]
    if not resolved.is_file():
        return [f"图片文件不存在：{rel}"]
    expected = extension_mime(resolved)
    mime = sniff_mime(resolved)
    if expected is None:
        errors.append(f"不支持的图片扩展名：{rel}")
    elif mime is None:
        errors.append(f"无法识别图片文件头：{rel}")
    elif mime != expected:
        errors.append(f"图片文件头与扩展名不一致：{rel}")
    size = resolved.stat().st_size
    if not (SIZE_MIN <= size <= SIZE_MAX):
        errors.append(
            f"文件大小为 {size} 字节，需要在 {SIZE_MIN} 与 {SIZE_MAX} 字节之间：{rel}"
        )
    return errors


def find_work_dirs(root: Path) -> list[Path]:
    images = root / "images"
    if not images.is_dir():
        return []
    return [
        child
        for child in sorted(images.iterdir(), key=lambda path: path.name)
        if child.is_dir() and (child / CHECKLIST_NAME).is_file()
    ]


def check_work(work_dir: Path) -> tuple[list[str], list[str], int]:
    work_dir = work_dir.resolve()
    checklist = work_dir / CHECKLIST_NAME
    if not checklist.is_file():
        return [f"找不到 {CHECKLIST_NAME}：{checklist}"], [], 0
    items = parse_checklist(checklist.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    linked: set[str] = set()
    seen_keys: dict[str, str] = {}
    work = work_dir.name
    for item in items:
        label = item["name"] or f"第 {item['lineno']} 行"
        rel = item["image_path"]
        if rel:
            linked.add(Path(rel).name)
        for key in entry_keys(item, work):
            previous = seen_keys.get(key)
            if previous and previous != label:
                warnings.append(f"别名或名称冲突：「{key}」同时对应 {previous} 与 {label}")
            else:
                seen_keys[key] = label
        if item["checked"]:
            if not rel:
                errors.append(f"{label}：已勾选，但没有指向图片的链接")
                continue
            for message in inspect_checked(work_dir, rel):
                errors.append(f"{label}：{message}")
    for path in sorted(work_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            if path.name not in linked:
                warnings.append(f"作品目录中有未列入清单的图片：{path.name}")
    return errors, warnings, len(items)


def resolve_work_dir(value: str) -> Path:
    work_dir = Path(value)
    if work_dir.is_absolute():
        return work_dir
    candidate = Path.cwd() / work_dir
    if not candidate.is_dir() and (repo_root() / work_dir).is_dir():
        return repo_root() / work_dir
    return candidate


def report_work(work_dir: Path) -> int:
    errors, warnings, count = check_work(work_dir)
    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
        return 1
    print(f"通过：{work_dir}（清单 {count} 项）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查作品目录中的 00-清单.md 与图片")
    parser.add_argument(
        "work_dir",
        nargs="?",
        help="作品目录。省略时检查仓库 images/ 下的全部作品",
    )
    args = parser.parse_args(argv)
    if args.work_dir:
        work_dir = resolve_work_dir(args.work_dir)
        if not work_dir.is_dir():
            print(f"找不到作品目录：{work_dir}", file=sys.stderr)
            return 2
        return report_work(work_dir)

    works = find_work_dirs(repo_root())
    if not works:
        print("未在 images/ 下找到含 00-清单.md 的作品目录。", file=sys.stderr)
        return 1
    exit_code = 0
    for work_dir in works:
        if report_work(work_dir) != 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
