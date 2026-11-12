#!/usr/bin/env python3
"""Validate a staging catalog without installing third-party dependencies."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"ready", "needs_review", "failed"}
SELECTIONS = {"automatic", "manual", "unconfirmed"}
MIME_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
}
REQUIRED = {
    "id", "roster_name", "work", "character", "variant", "status", "selection",
    "image_path", "sha256", "mime_type", "source_page", "source_image", "note",
}


def file_matches_mime(path, mime):
    with path.open("rb") as image:
        header = image.read(12)
    if mime == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if mime == "image/webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    return False


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as image:
        for chunk in iter(lambda: image.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(data, check_hashes):
    errors = []
    if not isinstance(data, dict):
        return ["清单根节点必须是对象"], 0
    if data.get("schema_version") != "1.0.0":
        errors.append("schema_version 必须为 1.0.0")
    items = data.get("items")
    if not isinstance(items, list):
        return errors + ["items 必须是数组"], 0

    ids = set()
    combinations = set()
    for index, item in enumerate(items, 1):
        label = f"第 {index} 项"
        if not isinstance(item, dict):
            errors.append(f"{label} 必须是对象")
            continue
        missing = REQUIRED - item.keys()
        if missing:
            errors.append(f"{label} 缺少字段：{', '.join(sorted(missing))}")
            continue
        for key in ("id", "roster_name", "work", "character", "variant"):
            if not isinstance(item[key], str) or not item[key].strip():
                errors.append(f"{label} 的 {key} 必须为非空文本")
        item_id = item["id"]
        if not isinstance(item_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", item_id):
            errors.append(f"{label} 的 id 必须由小写 ASCII 字母、数字、点、横线或下划线组成")
        elif item_id in ids:
            errors.append(f"{label} 的 id 重复：{item_id}")
        else:
            ids.add(item_id)
        roster_name, variant = item["roster_name"], item["variant"]
        if isinstance(roster_name, str) and isinstance(variant, str):
            pair = (roster_name.strip(), variant.strip())
            if pair in combinations:
                errors.append(f"{label} 的名单名和版本重复：{pair[0]} / {pair[1]}")
            combinations.add(pair)
        status, selection = item["status"], item["selection"]
        if not isinstance(status, str) or status not in STATUSES:
            errors.append(f"{label} 的 status 无效")
        if not isinstance(selection, str) or selection not in SELECTIONS:
            errors.append(f"{label} 的 selection 无效")
        for key in ("source_page", "source_image"):
            if item[key] is not None and not isinstance(item[key], str):
                errors.append(f"{label} 的 {key} 必须是文本或 null")
        if not isinstance(item["note"], str):
            errors.append(f"{label} 的 note 必须是文本")

        if status != "ready":
            for key in ("image_path", "sha256", "mime_type"):
                if item[key] is not None and not isinstance(item[key], str):
                    errors.append(f"{label} 的 {key} 必须是文本或 null")
            continue

        if selection == "unconfirmed":
            errors.append(f"{label} 已标 ready，但 selection 仍为 unconfirmed")
        rel = item["image_path"]
        sha = item["sha256"]
        mime = item["mime_type"]
        if not isinstance(rel, str) or not rel.startswith("images/") or "\\" in rel:
            errors.append(f"{label} 的 image_path 必须是 images/ 下的相对路径")
            continue
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"{label} 的 image_path 不能使用绝对路径或 ..")
            continue
        image_path = (ROOT / relative).resolve()
        if not image_path.is_relative_to((ROOT / "images").resolve()):
            errors.append(f"{label} 的 image_path 越出了图片目录")
            continue
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            errors.append(f"{label} 的 sha256 必须是 64 位小写十六进制")
        if not isinstance(mime, str) or mime not in MIME_EXTENSIONS:
            errors.append(f"{label} 的 mime_type 不受支持")
            continue
        if image_path.suffix.lower() not in MIME_EXTENSIONS[mime]:
            errors.append(f"{label} 的文件扩展名与 mime_type 不一致")
        if not image_path.is_file():
            errors.append(f"{label} 的图片文件不存在：{rel}")
            continue
        try:
            if not file_matches_mime(image_path, mime):
                errors.append(f"{label} 的图片文件头与 mime_type 不一致：{rel}")
            if check_hashes and isinstance(sha, str) and file_sha256(image_path) != sha:
                errors.append(f"{label} 的 SHA-256 不一致：{rel}")
        except OSError as error:
            errors.append(f"{label} 的图片无法读取：{error}")
    return errors, len(items)


def main():
    parser = argparse.ArgumentParser(description="检查角色图片中转清单")
    parser.add_argument("catalog", nargs="?", default="catalog/catalog.json", help="清单路径")
    parser.add_argument("--check-hashes", action="store_true", help="计算并比较实际图片 SHA-256")
    args = parser.parse_args()
    path = Path(args.catalog)
    if not path.is_absolute():
        path = ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"无法读取清单：{error}", file=sys.stderr)
        return 1
    errors, count = validate(data, args.check_hashes)
    if errors:
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
        return 1
    print(f"清单通过：{count} 项；哈希检查{'已开启' if args.check_hashes else '未开启'}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
