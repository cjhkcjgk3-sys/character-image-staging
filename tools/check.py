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
# Prefer <path> so names with half-width parentheses can still be linked.
LINK_RE = re.compile(r"^\[(?P<text>[^\]]+)\]\((?P<path><[^>]+>|[^)]+)\)")
ALIAS_RE = re.compile(r"别名[:：]\s*(.+)$")
WORK_ALIAS_RE = re.compile(r"^作品别名[:：]\s*(.*)$")
ALIAS_SPLIT = re.compile(r"[、,，/／]+")
SEPARATORS = re.compile(r"[\s·・‧•∙⋅\-–—/／:：]+")
BRACKETS = re.compile(r"[\[\]【】()（）『』「」<>《》]+")
# Capture a leading or trailing bracket group, including full-width pairs.
BRACKET_GROUP_RE = re.compile(
    r"[\[【(（『「<《]([^\[\]【】()（）『』「」<>《》]*)[\]】)）』」>》]"
)
SEP_CHARS = set(" \t·・‧•∙⋅-–—/／:：")

# Built-in work-name aliases. A 作品别名： line in 00-清单.md replaces this list
# (the folder name is always kept). New works can skip a code change by using that line.
DEFAULT_WORK_ALIASES = {
    "星穹铁道": ["崩坏：星穹铁道", "崩坏星穹铁道", "星铁"],
    "崩坏3": ["崩坏三", "崩3"],
    "终末地": ["明日方舟：终末地"],
    "战双帕弥什": ["战双"],
    "绝区零": [],
    "鸣潮": [],
    "明日方舟": ["方舟"],
    "碧蓝航线": [],
    "异环": [],
    "原神": [],
}


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


def fold_token(text: str) -> str:
    """Fold a token for comparison: no work-wrapper logic, no version meaning."""
    value = SEPARATORS.sub("", to_halfwidth(text or "").strip())
    return BRACKETS.sub("", value).casefold()


def resolve_work_tokens(work: str | None, work_aliases: list[str] | None = None) -> set[str]:
    if work_aliases is not None:
        raw = list(work_aliases)
    elif work:
        raw = list(DEFAULT_WORK_ALIASES.get(work, []))
    else:
        raw = []
    if work:
        raw = [work, *raw]
    return {fold_token(alias) for alias in raw if fold_token(alias)}


def _is_sep_char(char: str) -> bool:
    return char in SEP_CHARS


def strip_work_wrappers(text: str, work_tokens: set[str]) -> str:
    """Remove work-name prefixes/suffixes and work-only brackets. Keep version brackets."""
    if not work_tokens:
        return text
    value = text
    changed = True
    while changed and value:
        changed = False
        stripped = value.strip()
        # Leading bracket group that names the work: 【星铁】姬子
        lead = re.match(
            r"^([\[【(（『「<《]([^\[\]【】()（）『』「」<>《》]*)[\]】)）』」>》])[ \t·・‧•∙⋅\-–—/／:：]*",
            stripped,
        )
        if lead and fold_token(lead.group(2)) in work_tokens:
            value = stripped[lead.end() :]
            changed = True
            continue
        # Trailing bracket group that names the work: 姬子（星穹铁道）
        trail = re.search(
            r"[ \t·・‧•∙⋅\-–—/／:：]*([\[【(（『「<《]([^\[\]【】()（）『』「」<>《》]*)[\]】)）』」>》])$",
            stripped,
        )
        if trail and fold_token(trail.group(2)) in work_tokens:
            value = stripped[: trail.start()]
            changed = True
            continue
        # Leading work + separator: 崩坏星穹铁道·姬子 / 原神-温迪
        for index, char in enumerate(stripped):
            if not _is_sep_char(char):
                continue
            left, right = stripped[:index], stripped[index + 1 :]
            if fold_token(left) in work_tokens and fold_token(right):
                value = right
                changed = True
                break
        if changed:
            continue
        # Trailing separator + work
        for index, char in enumerate(stripped):
            if not _is_sep_char(char):
                continue
            left, right = stripped[:index], stripped[index + 1 :]
            if fold_token(right) in work_tokens and fold_token(left):
                value = left
                changed = True
                break
    return value.strip()


def normalize_name(
    text: str,
    work: str | None = None,
    work_aliases: list[str] | None = None,
) -> str:
    """Normalize a character name for flexible matching. Safe to copy into NAG.

    Only work-name wrappers are stripped (folder name or work aliases).
    Version brackets such as 三月七（巡猎） keep their inner text, so they
    do not collapse to the base name 三月七.
    """
    value = to_halfwidth(text or "").strip()
    value = strip_work_wrappers(value, resolve_work_tokens(work, work_aliases))
    value = BRACKETS.sub("", value)
    return SEPARATORS.sub("", value).casefold()


def name_identity(
    text: str,
    work: str | None = None,
    work_aliases: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (core_name, version_tokens) after removing work wrappers only."""
    value = to_halfwidth(text or "").strip()
    value = strip_work_wrappers(value, resolve_work_tokens(work, work_aliases))
    versions: list[str] = []

    def _keep_version(match: re.Match[str]) -> str:
        token = fold_token(match.group(1))
        if token:
            versions.append(token)
        return ""

    core = fold_token(BRACKET_GROUP_RE.sub(_keep_version, value))
    return core, versions


def entry_keys(
    entry: dict,
    work: str | None = None,
    work_aliases: list[str] | None = None,
) -> set[str]:
    keys = {normalize_name(entry.get("name") or "", work, work_aliases)}
    for alias in entry.get("aliases") or []:
        keys.add(normalize_name(alias, work, work_aliases))
    path = entry.get("image_path")
    if path:
        keys.add(normalize_name(Path(path).stem, work, work_aliases))
    keys.discard("")
    return keys


def _identity_sets(
    entry: dict,
    work: str | None,
    work_aliases: list[str] | None,
) -> list[list[str]]:
    sources = [entry.get("name") or ""]
    sources.extend(entry.get("aliases") or [])
    path = entry.get("image_path")
    if path:
        sources.append(Path(path).stem)
    identities = []
    for source in sources:
        core, versions = name_identity(source, work, work_aliases)
        tokens = [token for token in [core, *versions] if token]
        if tokens:
            identities.append(tokens)
    return identities


def _coverage(
    needle: str,
    entry: dict,
    work: str | None,
    work_aliases: list[str] | None,
) -> int:
    """How much of the query this entry explains. 0 means no loose match."""
    scores = []
    for tokens in _identity_sets(entry, work, work_aliases):
        if all(token in needle for token in tokens):
            # The query contains this entry's core name and any version tags.
            scores.append(sum(len(token) for token in tokens))
    for key in entry_keys(entry, work, work_aliases):
        if key in needle:
            scores.append(len(key))
        elif needle in key:
            # A short query contained by several names should stay ambiguous,
            # so every such hit gets the same score (the query length).
            scores.append(len(needle))
    return max(scores) if scores else 0


def match_name(
    query: str,
    entries: list[dict],
    work: str | None = None,
    work_aliases: list[str] | None = None,
) -> tuple[dict | None, str]:
    """Match a roster query to one checklist entry.

    Returns (entry, "match") on a unique hit, or (None, "none" / "ambiguous").
    Exact key matches win over contains / token coverage.
    """
    needle = normalize_name(query, work, work_aliases)
    if not needle:
        return None, "none"
    exact = [entry for entry in entries if needle in entry_keys(entry, work, work_aliases)]
    if len(exact) == 1:
        return exact[0], "match"
    if len(exact) > 1:
        return None, "ambiguous"
    scored: list[tuple[dict, int]] = []
    for entry in entries:
        score = _coverage(needle, entry, work, work_aliases)
        if score:
            scored.append((entry, score))
    if not scored:
        return None, "none"
    best = max(item[1] for item in scored)
    winners = [entry for entry, score in scored if score == best]
    if len(winners) == 1:
        return winners[0], "match"
    return None, "ambiguous"


def parse_aliases(text: str) -> list[str]:
    match = ALIAS_RE.search(text)
    if not match:
        return []
    return [part.strip() for part in ALIAS_SPLIT.split(match.group(1)) if part.strip()]


def parse_work_aliases(text: str) -> list[str] | None:
    """Return aliases from a 作品别名： line, or None if the line is absent."""
    found: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        match = WORK_ALIAS_RE.match(line)
        if not match:
            continue
        found = [part.strip() for part in ALIAS_SPLIT.split(match.group(1)) if part.strip()]
    return found


def _link_path(path: str) -> str:
    value = path.strip()
    if value.startswith("<") and value.endswith(">") and len(value) >= 2:
        value = value[1:-1].strip()
    return unquote(value)


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
            image_path = _link_path(link.group("path")) or None
            remainder = rest[link.end() :].strip()
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
    text = checklist.read_text(encoding="utf-8")
    items = parse_checklist(text)
    work_aliases = parse_work_aliases(text)
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
        for key in entry_keys(item, work, work_aliases):
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
