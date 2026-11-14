#!/usr/bin/env python3
"""Pack each work folder that has images into a ZIP for the GitHub Release download page.

The ZIP layout is:

    <作品名>/00-清单.md
    <作品名>/<角色名>.png

so the Novel-Auto-Generator plugin can import it and use「从清单添加到名单」.
Filenames are stored as UTF-8 with the language encoding flag set, and every
entry is deflated (method 8), which is what the plugin's ZIP reader accepts.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MANIFEST_NAME = "00-清单.md"
RELEASE_TAG = "downloads"
RELEASE_TITLE = "图片下载"
SINGAPORE = ZoneInfo("Asia/Singapore")

# GitHub Release asset names: non-ASCII is rewritten to default.zip, so publish
# uses these English slugs. Keep in sync with the release-body table.
ASSET_SLUGS = {
    "原神": "genshin",
    "异环": "yihuan",
    "明日方舟": "arknights",
    "星穹铁道": "star-rail",
    "终末地": "endfield",
    "绝区零": "zzz",
    "鸣潮": "wuthering-waves",
    "FGO": "fgo",
    "主机与PC游戏": "console-and-pc",
    "偶像大师灰姑娘女孩": "imas-cinderella",
    "公主连结": "princess-connect",
    "妮姬": "nikke",
    "少女前线": "girls-frontline",
    "少女前线2追放": "girls-frontline-2",
    "尘白禁区": "snowbreak",
    "崩坏3": "honkai-3rd",
    "战双帕弥什": "pgr",
    "碧蓝幻想": "granblue",
    "蔚蓝档案": "blue-archive",
    "赛马娘": "umamusume",
    "重返未来1999": "reverse-1999",
    "阴阳师": "onmyoji",
    "碧蓝航线": "azur-lane",
}


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES


def list_work_images(work_dir: Path) -> list[Path]:
    return sorted((path for path in work_dir.iterdir() if is_image(path)), key=lambda p: p.name)


def list_works(images_dir: Path) -> list[Path]:
    if not images_dir.is_dir():
        raise FileNotFoundError(f"找不到作品目录：{images_dir}")
    works = []
    for path in sorted(images_dir.iterdir(), key=lambda p: p.name):
        if path.is_dir() and list_work_images(path):
            works.append(path)
    return works


def asset_name(work_name: str, ascii_names: bool = False) -> str:
    if not ascii_names:
        return f"{work_name}.zip"
    slug = ASSET_SLUGS.get(work_name)
    if not slug:
        # Fallback: keep ASCII as-is, otherwise a conservative slug.
        slug = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-" for ch in work_name)
        slug = "-".join(part for part in slug.split("-") if part) or "work"
    return f"{slug}.zip"


def _zipinfo(path: Path, arcname: str) -> zipfile.ZipInfo:
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    info = zipfile.ZipInfo(filename=arcname, date_time=mtime.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.flag_bits |= 0x800  # UTF-8 language encoding flag
    info.external_attr = 0o644 << 16
    return info


def build_zip(work_dir: Path, dest: Path) -> dict:
    """Write dest as a work ZIP. Returns a small summary for release notes."""
    images = list_work_images(work_dir)
    if not images:
        raise ValueError(f"{work_dir} 里没有图片")
    manifest = work_dir / MANIFEST_NAME
    if not manifest.is_file():
        raise FileNotFoundError(f"{work_dir} 有图片但缺少 {MANIFEST_NAME}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    folder = work_dir.name
    with zipfile.ZipFile(tmp, "w") as zf:
        for path in [manifest, *images]:
            zf.writestr(_zipinfo(path, f"{folder}/{path.name}"), path.read_bytes())
    tmp.replace(dest)
    return {
        "work": folder,
        "path": dest,
        "name": dest.name,
        "images": len(images),
        "bytes": dest.stat().st_size,
    }


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        mb = size / (1024 * 1024)
        return f"{mb:.1f} MB" if mb < 10 else f"{mb:.0f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def default_repo() -> str:
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env:
        return env
    try:
        out = subprocess.check_output(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return out
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "cjhkcjgk3-sys/character-image-staging"


def write_release_notes(summaries: list[dict], dest: Path, repo: str, ascii_names: bool) -> None:
    page = f"https://github.com/{repo}/releases/tag/{RELEASE_TAG}"
    now = datetime.now(SINGAPORE).strftime("%Y-%m-%d %H:%M（新加坡时间）")
    lines = [
        "手机点下面的作品即可下载 ZIP，再在小说自动生成器里「导入作品 ZIP」。",
        "只有已经收集到图片的作品会出现在这里；图片更新后这个页面会自动换成最新包。",
        "",
        "| 作品 | 下载 | 图片数 | 大小 |",
        "|---|---|---:|---:|",
    ]
    for item in summaries:
        encoded = quote(item["name"])
        url = f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{encoded}"
        label = item["name"]
        if ascii_names and item["work"] != Path(item["name"]).stem:
            label = f"{item['work']}（{item['name']}）"
        lines.append(
            f"| {item['work']} | [{label}]({url}) | {item['images']} | {format_size(item['bytes'])} |"
        )
    lines += [
        "",
        f"更新时间：{now}",
        f"发布页：{page}",
        "",
    ]
    dest.write_text("\n".join(lines), encoding="utf-8")


def _gh(args: list[str], repo: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["gh", "-R", repo, *args] if repo else ["gh", *args]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    return result


def release_exists(repo: str) -> bool:
    try:
        _gh(["release", "view", RELEASE_TAG], repo)
        return True
    except subprocess.CalledProcessError:
        return False


def current_asset_names(repo: str) -> list[str]:
    result = subprocess.run(
        ["gh", "-R", repo, "release", "view", RELEASE_TAG, "--json", "assets", "--jq", ".assets[].name"],
        check=True,
        text=True,
        capture_output=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def publish(summaries: list[dict], notes: Path, repo: str) -> None:
    if not summaries:
        raise SystemExit("没有可发布的 ZIP")
    if release_exists(repo):
        _gh(["release", "edit", RELEASE_TAG, "--title", RELEASE_TITLE, "--notes-file", str(notes)], repo)
    else:
        _gh(["release", "create", RELEASE_TAG, "--title", RELEASE_TITLE, "--notes-file", str(notes)], repo)

    files = [str(item["path"]) for item in summaries]
    _gh(["release", "upload", RELEASE_TAG, *files, "--clobber"], repo)

    wanted = {item["name"] for item in summaries}
    for name in current_asset_names(repo):
        if name not in wanted:
            _gh(["release", "delete-asset", RELEASE_TAG, name, "--yes"], repo)
            print(f"已删除过期包：{name}", flush=True)
    print(f"已发布 {len(summaries)} 个作品到 https://github.com/{repo}/releases/tag/{RELEASE_TAG}", flush=True)


def build_all(images_dir: Path, out_dir: Path, ascii_names: bool) -> list[dict]:
    works = list_works(images_dir)
    skipped = []
    if images_dir.is_dir():
        for path in sorted(images_dir.iterdir(), key=lambda p: p.name):
            if path.is_dir() and not list_work_images(path):
                skipped.append(path.name)
    if skipped:
        print("跳过没有图片的文件夹：" + "、".join(skipped), flush=True)
    if not works:
        raise SystemExit(f"{images_dir} 下没有已收集图片的作品")

    summaries = []
    for work in works:
        dest = out_dir / asset_name(work.name, ascii_names=ascii_names)
        print(f"正在打包 {work.name} → {dest.name} …", flush=True)
        item = build_zip(work, dest)
        item["name"] = dest.name
        print(f"  {dest.name}：{item['images']} 张，{format_size(item['bytes'])}", flush=True)
        summaries.append(item)
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为每个有图的作品打包 ZIP，并可发布到 GitHub Release。")
    parser.add_argument("--images", default="images", help="作品目录，默认 images/")
    parser.add_argument("--out", default="out/work-zips", help="ZIP 输出目录")
    parser.add_argument("--repo", default="", help="owner/name，默认取 GITHUB_REPOSITORY 或当前 gh 仓库")
    parser.add_argument("--ascii-names", action=argparse.BooleanOptionalAction, default=True, help="发布时用英文文件名（默认开启：GitHub 会把中文资源名改成 default.zip）")
    parser.add_argument("--publish", action="store_true", help="打包后发布到标签 downloads 的 Release")
    parser.add_argument("--publish-only", action="store_true", help="不重新打包，只把 --out 里已有的 ZIP 发布上去")
    args = parser.parse_args(argv)

    images_dir = Path(args.images)
    out_dir = Path(args.out)
    repo = args.repo or default_repo()
    ascii_names = args.ascii_names

    if args.publish_only:
        # Recover summaries from existing zips; work name is the folder inside each zip.
        summaries = []
        for path in sorted(out_dir.glob("*.zip")):
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                folder = names[0].split("/")[0] if names else path.stem
            summaries.append(
                {
                    "work": folder,
                    "path": path,
                    "name": path.name,
                    "images": max(0, len(names) - 1),
                    "bytes": path.stat().st_size,
                }
            )
        if not summaries:
            raise SystemExit(f"{out_dir} 里没有 ZIP")
    else:
        summaries = build_all(images_dir, out_dir, ascii_names)

    notes = out_dir / "RELEASE_NOTES.md"
    write_release_notes(summaries, notes, repo, ascii_names)
    print(f"已写入发布说明：{notes}", flush=True)

    if args.publish or args.publish_only:
        publish(summaries, notes, repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
