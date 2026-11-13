#!/usr/bin/env python3
"""Build a local review gallery for checked character images.

The gallery does not decide whether an image is acceptable. It makes it quick
to visually scan every checked image and highlights a small alpha-edge
heuristic as a review hint only.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from urllib.parse import quote

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.stderr.write("需要 Pillow：python -m pip install Pillow\n")
    raise SystemExit(2)

from check import parse_checklist, resolve_image


THUMBNAIL_SIZE = (176, 232)
TOP_BAND_FRACTION = 0.02
TOP_BAND_OCCUPANCY_WARNING = 0.40
ALPHA_VISIBLE_THRESHOLD = 32


def top_edge_warning(image: Image.Image) -> str | None:
    """Return a warning when a transparent image begins unusually wide at top.

    This is deliberately only a review hint. Hair, rigging, effects, and
    intentional close crops can all trigger it; opaque-background images are
    outside the scope of this heuristic.
    """
    if "A" not in image.getbands():
        return None
    alpha = image.getchannel("A")
    alpha_min, _ = alpha.getextrema()
    if alpha_min >= 255:
        return None

    band_height = max(1, round(image.height * TOP_BAND_FRACTION))
    # Ignore faint anti-aliased fringe pixels, which otherwise make transparent
    # art appear to occupy a broad part of the top edge.
    visible_alpha = alpha.point(
        [255 if value > ALPHA_VISIBLE_THRESHOLD else 0 for value in range(256)]
    )
    band = visible_alpha.crop((0, 0, image.width, band_height))
    band_bytes = band.tobytes()
    maximum = 0.0
    for y in range(band.height):
        row = band_bytes[y * image.width : (y + 1) * image.width]
        occupied = image.width - row.count(0)
        maximum = max(maximum, occupied / image.width)

    if maximum >= TOP_BAND_OCCUPANCY_WARNING:
        return (
            "透明图顶部 2% 有一行明显主体覆盖超过 40% 画宽；"
            "请确认头脸没有被原图构图截掉（提示，不是错误）"
        )
    return None


def safe_slug(index: int) -> str:
    return f"{index:04d}"


def build_gallery(work_dir: Path, output_dir: Path, page_size: int) -> dict:
    checklist = work_dir / "00-清单.md"
    if not checklist.is_file():
        raise FileNotFoundError(f"找不到清单：{checklist}")

    rows = [item for item in parse_checklist(checklist.read_text(encoding="utf-8"))
            if item["checked"]]
    output_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = output_dir / "thumbs"
    thumb_dir.mkdir(exist_ok=True)

    records: list[dict] = []
    for index, row in enumerate(rows, 1):
        if index == 1 or index % 50 == 0 or index == len(rows):
            print(f"处理缩略图：{index}/{len(rows)}", file=sys.stderr, flush=True)
        name = row["name"] or f"清单第 {row['lineno']} 行"
        rel = row["image_path"]
        path = resolve_image(work_dir, rel) if rel else None
        record = {
            "index": index,
            "name": name,
            "relative_path": rel,
            "image_path": str(path) if path else None,
            "thumbnail": None,
            "width": None,
            "height": None,
            "size": None,
            "warning": None,
            "error": None,
        }
        if path is None:
            record["error"] = "清单没有有效的图片路径"
        elif not path.is_file():
            record["error"] = "图片文件不存在"
        else:
            record["size"] = path.stat().st_size
            try:
                with Image.open(path) as opened:
                    opened.load()
                    image = ImageOps.exif_transpose(opened)
                    record["width"], record["height"] = image.size
                    record["warning"] = top_edge_warning(image)
                    if "A" in image.getbands() and image.getchannel("A").getextrema()[0] < 255:
                        thumb = image.convert("RGBA")
                    else:
                        thumb = image.convert("RGB")
                    thumb.thumbnail(THUMBNAIL_SIZE, Image.Resampling.BOX)
                    thumbnail_path = thumb_dir / f"{safe_slug(index)}.webp"
                    thumb.save(thumbnail_path, "WEBP", quality=82, method=5)
                    record["thumbnail"] = thumbnail_path.relative_to(output_dir).as_posix()
            except Exception as exc:  # Keep bad files visible in the report.
                record["error"] = f"读取或完整解码失败：{type(exc).__name__}: {exc}"
        records.append(record)

    pages: list[str] = []
    for start in range(0, len(records), page_size):
        page_number = start // page_size + 1
        filename = f"page-{page_number:03d}.html"
        pages.append(filename)
        page_records = records[start:start + page_size]
        cards = "\n".join(render_card(record) for record in page_records)
        navigation = render_navigation(pages, page_number, total_pages=(len(records) + page_size - 1) // page_size)
        page_html = page_template(
            title=f"{html.escape(work_dir.name)} 构图复核 · 第 {page_number} 页",
            body=f"<nav>{navigation}</nav><main class=grid>{cards}</main><nav>{navigation}</nav>",
        )
        (output_dir / filename).write_text(page_html, encoding="utf-8")

    warnings = [record for record in records if record["warning"]]
    errors = [record for record in records if record["error"]]
    summary = {
        "work": work_dir.name,
        "checked_images": len(records),
        "warning_count": len(warnings),
        "error_count": len(errors),
        "warnings": [
            {key: record[key] for key in ("index", "name", "relative_path", "warning")}
            for record in warnings
        ],
        "errors": [
            {key: record[key] for key in ("index", "name", "relative_path", "error")}
            for record in errors
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    page_links = "\n".join(
        f'<li><a href="{filename}">第 {i} 页</a>（{min(page_size, len(records) - (i - 1) * page_size)} 张）</li>'
        for i, filename in enumerate(pages, 1)
    ) or "<li>清单中没有已勾选图片。</li>"
    warning_list = "\n".join(
        f'<li>#{r["index"]} {html.escape(r["name"])}：{html.escape(r["warning"])}'
        f' — <a href="{pages[(r["index"] - 1) // page_size]}#image-{r["index"]}">定位</a></li>'
        for r in warnings
    ) or "<li>没有触发此项提示。</li>"
    error_list = "\n".join(
        f'<li>#{r["index"]} {html.escape(r["name"])}：{html.escape(r["error"] or "")}</li>'
        for r in errors
    ) or "<li>没有读取错误。</li>"
    index_html = page_template(
        title=f"{html.escape(work_dir.name)} 图片构图复核",
        body=(
            f"<h1>{html.escape(work_dir.name)} 图片构图复核</h1>"
            f"<p>共检查清单中已勾选的 {len(records)} 张图。先按页扫缩略图；点击缩略图可打开原图。"
            "边缘提示只用于定位复核，不会自动判错。</p>"
            f"<p>边缘提示 {len(warnings)} 项；读取错误 {len(errors)} 项。</p>"
            f"<h2>分批总览</h2><ul>{page_links}</ul>"
            f"<h2>边缘风险提示</h2><ul>{warning_list}</ul>"
            f"<h2>读取错误</h2><ul>{error_list}</ul>"
        ),
    )
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    return summary


def render_card(record: dict) -> str:
    index = record["index"]
    name = html.escape(record["name"])
    classes = ["card"]
    if record["warning"]:
        classes.append("warning")
    if record["error"]:
        classes.append("error")

    if record["thumbnail"] and record["image_path"]:
        thumb = html.escape(record["thumbnail"], quote=True)
        original = Path(record["image_path"]).resolve().as_uri()
        image_markup = f'<a href="{html.escape(original, quote=True)}" target="_blank"><img loading="lazy" src="{thumb}" alt="{name}"></a>'
    else:
        image_markup = '<div class="missing">没有可显示的缩略图</div>'

    dimensions = ""
    if record["width"] and record["height"]:
        dimensions = f"{record['width']} × {record['height']} · {record['size'] / 1024:.0f} KB"
    note = record["warning"] or record["error"] or ""
    note_markup = f'<p class="note">{html.escape(note)}</p>' if note else ""
    return (
        f'<article class="{" ".join(classes)}" id="image-{index}">'
        f'<div class="picture">{image_markup}</div>'
        f'<div class="caption"><b>#{index} {name}</b>'
        f'<small>{html.escape(dimensions)}</small>{note_markup}</div></article>'
    )


def render_navigation(pages: list[str], current: int, total_pages: int) -> str:
    links = ['<a href="index.html">目录</a>']
    if current > 1:
        links.append(f'<a href="page-{current - 1:03d}.html">上一页</a>')
    links.append(f'<span>第 {current} / {total_pages} 页</span>')
    if current < total_pages:
        links.append(f'<a href="page-{current + 1:03d}.html">下一页</a>')
    return "　".join(links)


def page_template(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;margin:1rem;background:#f5f5f5;color:#222}}
nav{{position:sticky;top:0;background:#fff;padding:.7rem;z-index:2;border-bottom:1px solid #ddd}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-top:12px}}
.card{{background:#fff;border:1px solid #ccc;border-radius:6px;overflow:hidden;min-width:0}}
.card.warning{{border:2px solid #d88a00}}.card.error{{border:2px solid #c33}}
.picture{{height:240px;display:flex;align-items:center;justify-content:center;background-color:#eee;background-image:linear-gradient(45deg,#ddd 25%,transparent 25%),linear-gradient(-45deg,#ddd 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#ddd 75%),linear-gradient(-45deg,transparent 75%,#ddd 75%);background-size:20px 20px;background-position:0 0,0 10px,10px -10px,-10px 0}}
.picture img{{max-width:100%;max-height:100%;object-fit:contain;display:block}}
.missing{{padding:1rem;color:#a00}}
.caption{{padding:7px;font-size:13px;overflow-wrap:anywhere}}.caption b{{display:block}}.caption small{{display:block;color:#555;margin-top:4px}}
.note{{color:#8a5100;margin:6px 0 0;font-size:12px}}
@media(max-width:600px){{.grid{{grid-template-columns:repeat(3,minmax(0,1fr));gap:5px}}.picture{{height:150px}}.caption{{font-size:11px;padding:4px}}}}
</style></head><body>{body}</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="为作品清单中已勾选的图片生成分批构图复核图库（只提示，不自动判错）"
    )
    parser.add_argument("work_dir", help="作品目录，例如 images/碧蓝航线")
    parser.add_argument("--output", help="输出目录；默认写入仓库 .image-audit/<作品名>")
    parser.add_argument("--page-size", type=int, default=48, help="每页缩略图数量，默认 48")
    args = parser.parse_args(argv)
    if args.page_size < 1:
        parser.error("--page-size 必须大于 0")

    script_root = Path(__file__).resolve().parent.parent
    work_dir = Path(args.work_dir)
    if not work_dir.is_absolute():
        candidate = Path.cwd() / work_dir
        work_dir = candidate if candidate.is_dir() else script_root / work_dir
    work_dir = work_dir.resolve()
    if not work_dir.is_dir():
        print(f"找不到作品目录：{work_dir}", file=sys.stderr)
        return 2

    output_dir = Path(args.output).resolve() if args.output else script_root / ".image-audit" / work_dir.name
    try:
        summary = build_gallery(work_dir, output_dir, args.page_size)
    except Exception as exc:
        print(f"生成复核图库失败：{exc}", file=sys.stderr)
        return 2

    print(f"已生成：{output_dir / 'index.html'}")
    print(
        f"已勾选图片 {summary['checked_images']} 张；"
        f"边缘提示 {summary['warning_count']} 项；读取错误 {summary['error_count']} 项。"
    )
    print("浏览 index.html，逐页扫过所有缩略图；橙框只提示复核，不代表图片有错。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
