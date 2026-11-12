#!/usr/bin/env python3
"""Generate and validate per-work image catalogs.

Requires Python 3.9+. Uses only the standard library.
The checklist ``00-清单.md`` in each work folder is the source of truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write("需要 Python 3.9 或更高版本。\n")
    raise SystemExit(2)

SIZE_MIN = 500_000  # 500 KB
SIZE_MAX = 2_000_000  # 2 MB
SCHEMA_VERSION = "1.1.0"
CHECKLIST_NAME = "00-清单.md"
CATALOG_NAME = "catalog.json"
DEFAULT_VARIANT = "默认立绘"
MIME_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
}
ITEM_KEYS = (
    "id",
    "roster_name",
    "work",
    "character",
    "variant",
    "status",
    "image_path",
    "sha256",
    "mime_type",
    "size_bytes",
    "source_page",
    "source_image",
    "note",
)

CHECK_RE = re.compile(r"^-\s+\[(?P<check>[ xX])\]\s+(?P<rest>.+)$")
LINK_RE = re.compile(r"^\[(?P<text>[^\]]+)\]\((?P<path>[^)]+)\)\s*(?P<rest>.*)$")
VARIANT_RE = re.compile(r"^(?P<name>.+?)（(?P<variant>[^）]+)）$")
NOTE_RE = re.compile(r"（(?P<note>[^）]*)）\s*$")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def make_id(work: str, character: str, variant: str) -> str:
    payload = f"{work}\0{character}\0{variant}".encode()
    return "item-" + hashlib.sha256(payload).hexdigest()[:12]


def roster_name(character: str, work: str) -> str:
    return f"{character}（{work}）"


def split_display_name(display: str) -> tuple[str, str]:
    display = display.strip()
    match = VARIANT_RE.fullmatch(display)
    if match:
        return match.group("name").strip(), match.group("variant").strip()
    return display, DEFAULT_VARIANT


def extract_note(text: str) -> tuple[str, str]:
    text = text.strip()
    match = NOTE_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()].strip(), match.group("note").strip()


def parse_checklist(text: str) -> list[dict]:
    items = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff")
        match = CHECK_RE.match(line)
        if not match:
            continue
        checked = match.group("check") in {"x", "X"}
        rest = match.group("rest").strip()
        link = LINK_RE.match(rest)
        if link:
            display = link.group("text").strip()
            image_path = link.group("path").strip() or None
            _, note = extract_note(link.group("rest"))
        else:
            display, note = extract_note(rest)
            image_path = None
        if not display:
            raise ValueError(f"第 {lineno} 行缺少角色名")
        character, variant = split_display_name(display)
        if not character:
            raise ValueError(f"第 {lineno} 行缺少角色名")
        status = "ready" if checked else "pending"
        if not checked and note.startswith("失败"):
            status = "failed"
        items.append(
            {
                "lineno": lineno,
                "checked": checked,
                "character": character,
                "variant": variant,
                "image_path": image_path,
                "note": note,
                "status": status,
            }
        )
    return items


def load_checklist(path: Path) -> list[dict]:
    return parse_checklist(path.read_text(encoding="utf-8"))


def load_catalog(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "work": "", "items": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("清单根节点必须是对象")
    items = data.get("items", [])
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    return data


def dump_catalog(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def normalize_item(item: dict) -> dict:
    return {key: item.get(key) for key in ITEM_KEYS}


def is_safe_relative(work_dir: Path, rel: str) -> Path | None:
    if not rel or not isinstance(rel, str):
        return None
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


def existing_index(items: list) -> dict[tuple[str, str], dict]:
    index = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        character = item.get("character")
        variant = item.get("variant") or DEFAULT_VARIANT
        if isinstance(character, str) and character:
            index[(character, variant)] = item
    return index


def lookup_existing(index: dict, parsed: dict, by_path: dict) -> dict:
    key = (parsed["character"], parsed["variant"])
    if key in index:
        return index[key]
    if parsed["variant"] == DEFAULT_VARIANT:
        matches = [
            item
            for (character, _variant), item in index.items()
            if character == parsed["character"]
        ]
        if len(matches) == 1:
            return matches[0]
    path = parsed.get("image_path")
    if path and path in by_path:
        return by_path[path]
    return {}


def inspect_image(work_dir: Path, rel: str) -> tuple[Path | None, list[str], dict]:
    errors = []
    info = {"sha256": None, "mime_type": None, "size_bytes": None, "path": rel}
    resolved = is_safe_relative(work_dir, rel)
    if resolved is None:
        errors.append(f"图片路径不合法或越出作品目录：{rel}")
        return None, errors, info
    if not resolved.is_file():
        errors.append(f"图片文件不存在：{rel}")
        return resolved, errors, info
    mime = sniff_mime(resolved)
    expected = extension_mime(resolved)
    if expected is None:
        errors.append(f"不支持的图片扩展名：{rel}")
    elif mime is None:
        errors.append(f"无法识别图片文件头：{rel}")
    elif mime != expected:
        errors.append(f"图片文件头与扩展名不一致：{rel}")
    info["mime_type"] = mime or expected
    info["sha256"] = file_sha256(resolved)
    info["size_bytes"] = resolved.stat().st_size
    return resolved, errors, info


def size_ok(size: int | None) -> bool:
    return size is not None and SIZE_MIN <= size <= SIZE_MAX


def build_item(work: str, work_dir: Path, parsed: dict, previous: dict) -> tuple[dict, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    previous = previous or {}
    variant = parsed["variant"]
    label = f"{parsed['character']}（{variant}）"
    status = parsed["status"]
    rel = parsed["image_path"]
    sha = None
    mime = None
    size = None

    if rel:
        _resolved, image_errors, info = inspect_image(work_dir, rel)
        sha = info["sha256"]
        mime = info["mime_type"]
        size = info["size_bytes"]
        if parsed["checked"]:
            for message in image_errors:
                errors.append(f"{label}：{message}")
            if not image_errors and not size_ok(size):
                errors.append(
                    f"{label}：文件大小为 {size} 字节，需要在 {SIZE_MIN} 与 {SIZE_MAX} 字节之间"
                )
        else:
            for message in image_errors:
                warnings.append(f"{label}：{message}")
            if size is not None and not size_ok(size):
                warnings.append(
                    f"{label}：文件大小为 {size} 字节，未达 {SIZE_MIN}–{SIZE_MAX} 字节范围，保持未勾选"
                )
    elif parsed["checked"]:
        errors.append(f"{label}：已勾选，但清单中没有图片链接")
    else:
        warnings.append(f"{label}：尚未收集")

    if parsed["checked"] and errors:
        status = "pending"

    item_id = previous.get("id")
    if not isinstance(item_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", item_id):
        item_id = make_id(work, parsed["character"], variant)

    item = normalize_item(
        {
            "id": item_id,
            "roster_name": roster_name(parsed["character"], work),
            "work": work,
            "character": parsed["character"],
            "variant": variant,
            "status": status,
            "image_path": rel,
            "sha256": sha,
            "mime_type": mime,
            "size_bytes": size,
            "source_page": previous.get("source_page"),
            "source_image": previous.get("source_image"),
            "note": parsed["note"],
        }
    )
    return item, errors, warnings


def generate_catalog(work_dir: Path, work: str | None = None) -> tuple[dict, list[str], list[str]]:
    work_dir = work_dir.resolve()
    work = work or work_dir.name
    checklist_path = work_dir / CHECKLIST_NAME
    if not checklist_path.is_file():
        raise FileNotFoundError(f"找不到 {CHECKLIST_NAME}：{checklist_path}")
    parsed_items = load_checklist(checklist_path)
    previous = load_catalog(work_dir / CATALOG_NAME)
    prev_items = previous.get("items") if isinstance(previous.get("items"), list) else []
    index = existing_index(prev_items)
    by_path = {}
    for item in prev_items:
        if isinstance(item, dict) and isinstance(item.get("image_path"), str):
            by_path[item["image_path"]] = item

    errors: list[str] = []
    warnings: list[str] = []
    seen_keys = set()
    seen_paths = set()
    items = []
    for parsed in parsed_items:
        key = (parsed["character"], parsed["variant"])
        if key in seen_keys:
            errors.append(f"角色与版本重复：{parsed['character']} / {parsed['variant']}")
            continue
        seen_keys.add(key)
        rel = parsed.get("image_path")
        if rel:
            if rel in seen_paths:
                errors.append(f"图片路径重复：{rel}")
            seen_paths.add(rel)
        item, item_errors, item_warnings = build_item(
            work, work_dir, parsed, lookup_existing(index, parsed, by_path)
        )
        errors.extend(item_errors)
        warnings.extend(item_warnings)
        items.append(item)

    for path in sorted(work_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name in {CHECKLIST_NAME, CATALOG_NAME, "清单.md", ".gitkeep"}:
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            if path.name not in seen_paths:
                warnings.append(f"作品目录中有未列入清单的图片：{path.name}")

    catalog = {
        "schema_version": SCHEMA_VERSION,
        "work": work,
        "items": items,
    }
    return catalog, errors, warnings


def find_work_dirs(root: Path) -> list[Path]:
    images = root / "images"
    if not images.is_dir():
        return []
    return [
        child
        for child in sorted(images.iterdir(), key=lambda path: path.name)
        if child.is_dir() and (child / CHECKLIST_NAME).is_file()
    ]


def write_catalog(path: Path, catalog: dict) -> None:
    path.write_text(dump_catalog(catalog), encoding="utf-8")


def process_work(work_dir: Path, check_only: bool) -> int:
    catalog, errors, warnings = generate_catalog(work_dir)
    catalog_path = work_dir / CATALOG_NAME
    current = dump_catalog(catalog)
    existing_text = catalog_path.read_text(encoding="utf-8") if catalog_path.is_file() else None
    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if check_only:
        if existing_text is None:
            errors.append(f"缺少 {CATALOG_NAME}，请先运行不带 --check 的命令生成")
        elif existing_text != current:
            errors.append(f"{catalog_path} 与清单生成结果不一致，请重新生成并提交")
        if errors:
            for error in errors:
                print(f"错误：{error}", file=sys.stderr)
            return 1
        ready = sum(1 for item in catalog["items"] if item["status"] == "ready")
        print(f"通过：{work_dir}（{len(catalog['items'])} 项，其中 {ready} 项可用）")
        return 0

    write_catalog(catalog_path, catalog)
    if errors:
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
        print(f"已写入 {catalog_path}，但存在错误。", file=sys.stderr)
        return 1
    ready = sum(1 for item in catalog["items"] if item["status"] == "ready")
    print(f"已更新 {catalog_path}（{len(catalog['items'])} 项，其中 {ready} 项可用）")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="根据 00-清单.md 生成并检查作品目录中的 catalog.json"
    )
    parser.add_argument(
        "work_dir",
        nargs="?",
        help="作品目录（含 00-清单.md）。也可用于已解压的作品包",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="处理仓库 images/ 下的全部作品目录",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查不写入；catalog.json 与生成结果不一致或有错误时返回非 0",
    )
    return parser.parse_args(argv)


def resolve_work_dir(value: str) -> Path:
    work_dir = Path(value)
    if not work_dir.is_absolute():
        candidate = Path.cwd() / work_dir
        if not candidate.is_dir() and (repo_root() / work_dir).is_dir():
            return repo_root() / work_dir
        return candidate
    return work_dir


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.all and args.work_dir:
        print("请只使用 --all 或指定一个作品目录，不要同时使用。", file=sys.stderr)
        return 2
    if not args.all and not args.work_dir:
        print("请指定作品目录，或使用 --all。", file=sys.stderr)
        return 2

    if args.all:
        works = find_work_dirs(repo_root())
        if not works:
            print("未在 images/ 下找到含 00-清单.md 的作品目录。", file=sys.stderr)
            return 1
        exit_code = 0
        for work_dir in works:
            result = process_work(work_dir, args.check)
            if result != 0:
                exit_code = result
        return exit_code

    work_dir = resolve_work_dir(args.work_dir)
    if not work_dir.is_dir():
        print(f"找不到作品目录：{work_dir}", file=sys.stderr)
        return 1
    return process_work(work_dir, args.check)


if __name__ == "__main__":
    raise SystemExit(main())
