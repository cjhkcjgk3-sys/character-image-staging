#!/usr/bin/env python3
"""自动找出疑似残缺的角色立绘（缺头缺脸、被矩形裁掉、脸部透明空洞等）。

这是可选的复核辅助工具，只读图片和清单，不修改任何文件；结果是“建议人工看一眼”
的名单，不是判决。check.py 和 CI 不依赖它。

依赖：Python 3.9+、Pillow、numpy；人脸检测另需 opencv-python-headless：

    python3 -m pip install pillow numpy opencv-python-headless

没有 OpenCV 时只跑透明通道检查（直线裁切、矩形空洞、顶边贴边），并给出提示。

信号与等级：
- 高（high）：straight_cut 透明边缘上出现一条完全水平/竖直、边缘轮廓逐像素一致的
  长直线，即主体被矩形整块抠掉；rect_hole 是其中的空洞情况（直线另一侧不远处又有
  主体，或两条直线构成直角）。碧蓝航线拆包缺图层的残图几乎都属于这一类。
- 低（low）：no_face 动漫人脸检测器没找到可信的脸；no_figure 没找到脸且几乎没有肤色
  （常见于只剩舞台、特效或舰装图层的错图）；top_edge 主体在画布最上边被截断；
  short_cut 较短的直线裁切。这些信号误报较多，只用来排序和提示。
- wide_top（主体最上方就很宽，像是从肩膀开始）单独出现时误报太多，只在已有其他
  信号时附带列出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

if sys.version_info < (3, 9):
    sys.stderr.write("需要 Python 3.9 或更高版本。\n")
    raise SystemExit(2)

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover - depends on environment
    sys.stderr.write("需要 Pillow 和 numpy：python3 -m pip install pillow numpy opencv-python-headless\n")
    raise SystemExit(2)

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - depends on environment
    cv2 = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check import find_work_dirs, parse_checklist, repo_root, resolve_image  # noqa: E402

CASCADE_PATH = Path(__file__).resolve().parent / "data" / "lbpcascade_animeface.xml"

# --- 透明通道直线检测参数 ---------------------------------------------------
OPAQUE = 250          # 视为完全不透明
CLEAR = 8             # 视为完全透明
EDGE_WIDTH = 3        # 不透明到透明最多过渡 3 像素（允许缩放带来的轻微模糊）
MIN_RUN = 6           # 单段直线最短 6 像素
PROFILE_STD_MAX = 8.0  # 直线上每列过渡像素的 alpha 标准差上限：真裁切逐像素一致
CUT_HIGH = 0.045      # 同一行直线总长 ≥ 画布长边 4.5% → 高
CUT_LOW = 0.025       # 2.5%～4.5% → 低
CUT_MIN_PIXELS = 40   # 无论图多小，直线总长至少 40 像素才算
CORNER_MIN = 0.012    # 参与直角判定的直线最短占长边比例
CORNER_TOL = 5        # 直角端点容差（像素）
HOLE_REACH = 0.25     # 透明一侧在长边 25% 内又碰到主体，算作空洞
HOLE_HIT_FRACTION = 0.6
# --- 其他弱信号 -------------------------------------------------------------
TOP_EDGE_COVER = 0.15   # 画布第一行有 ≥15% 宽度是主体 → 顶边截断
WIDE_TOP_BAND = 0.03    # 主体最上方 3% 高度
WIDE_TOP_FRACTION = 0.45  # 该带内主体左右跨度 ≥ 主体宽度 45%
FACE_MIN_SCORE = 3      # 检测器邻域票数低于 3 视为没找到脸
FACE_LONG_EDGE = 1024
FACE_WEAK_SCORE = 6     # 票数低于 6 的脸只算勉强找到
SKIN_FRACTION_MIN = 0.02   # 没有脸且肤色像素占主体不到 2% → no_figure
SKIN_FRACTION_NONE = 0.005  # 脸勉强找到但几乎没有肤色（< 0.5%）→ 也算 no_figure

DIRECTIONS = ("上方主体、下方透明", "下方主体、上方透明", "左侧主体、右侧透明", "右侧主体、左侧透明")
HORIZONTAL = {0, 1}

_CASCADE = None


def _oriented(alpha: "np.ndarray", k: int) -> "np.ndarray":
    """把四个方向统一成“主体在上、透明在下”的视角。"""
    if k == 0:
        return alpha
    if k == 1:
        return alpha[::-1]
    if k == 2:
        return alpha.T
    return alpha.T[::-1]


def _to_original(k: int, y: int, a: int, b: int, width: int, height: int) -> tuple[int, int, int, int]:
    """方向坐标中的直线 (行 y, 列 a..b) → 原图坐标 (x0, y0, x1, y1)。"""
    if k == 0:
        return a, y, b, y
    if k == 1:
        return a, height - 1 - y, b, height - 1 - y
    if k == 2:
        return y, a, y, b
    return width - 1 - y, a, width - 1 - y, b


def find_lines(alpha: "np.ndarray") -> list[dict]:
    """找出透明边缘上完全水平/竖直、过渡轮廓逐像素一致的直线。

    自然画出的边缘（含抗锯齿、略微倾斜的线条）在同一行上连不成长段，或过渡像素
    的 alpha 沿线变化；拆包缺图层留下的矩形裁切则在同一行逐像素一致。
    """
    height, width = alpha.shape
    lines = []
    for k in range(4):
        arr = np.ascontiguousarray(_oriented(alpha, k))
        rows = arr.shape[0]
        if rows <= EDGE_WIDTH + 2:
            continue
        hi = arr >= OPAQUE
        canon = hi[: -EDGE_WIDTH - 1] & ~hi[1:-EDGE_WIDTH] & (arr[EDGE_WIDTH + 1 :] <= CLEAR)
        counts = canon.sum(axis=1)
        for y in np.nonzero(counts >= MIN_RUN)[0]:
            # 紧贴画布边缘的不算（那是画布本身的边界）
            if y < 2 or y > rows - EDGE_WIDTH - 4:
                continue
            row = canon[y].astype(np.int8)
            diff = np.diff(np.concatenate(([0], row, [0])))
            starts = np.nonzero(diff == 1)[0]
            ends = np.nonzero(diff == -1)[0]
            lengths = ends - starts
            keep = lengths >= MIN_RUN
            if not keep.any():
                continue
            starts, ends, lengths = starts[keep], ends[keep], lengths[keep]
            idx = np.concatenate([np.arange(s, e) for s, e in zip(starts, ends)])
            profile = arr[y + 1 : y + EDGE_WIDTH + 1][:, idx].astype(np.float32)
            std = float(profile.std(axis=1).max())
            if std > PROFILE_STD_MAX:
                continue
            # 透明一侧往外走，看多远会再碰到主体（空洞 vs. 外轮廓裁切）
            reach = max(8, int(HOLE_REACH * max(width, height)))
            below = arr[y + 1 : min(rows, y + 1 + reach)][:, idx] >= 128
            hit = below.any(axis=0)
            x0, y0, x1, y1 = _to_original(k, int(y), int(starts.min()), int(ends.max()) - 1, width, height)
            lines.append(
                {
                    "dir": k,
                    "mass": int(lengths.sum()),
                    "longest": int(lengths.max()),
                    "segments": int(len(lengths)),
                    "profile_std": round(std, 1),
                    "hole_hit": round(float(hit.mean()), 2),
                    "box": [x0, y0, x1, y1],
                }
            )
    lines.sort(key=lambda line: -line["mass"])
    return lines


def find_corners(lines: list[dict], long_edge: int) -> list[dict]:
    """水平线和竖直线的端点相接（构成直角）时，基本可以肯定是矩形裁切。"""
    min_len = max(MIN_RUN * 2, CORNER_MIN * long_edge)
    hs = [l for l in lines if l["dir"] in HORIZONTAL and l["mass"] >= min_len]
    vs = [l for l in lines if l["dir"] not in HORIZONTAL and l["mass"] >= min_len]
    corners = []
    for h in hs:
        hx0, hy, hx1, _ = h["box"]
        for v in vs:
            vx, vy0, _, vy1 = v["box"]
            if min(abs(vx - hx0), abs(vx - hx1)) <= CORNER_TOL and min(abs(hy - vy0), abs(hy - vy1)) <= CORNER_TOL:
                corners.append({"x": vx, "y": hy, "size": min(h["mass"], v["mass"])})
    corners.sort(key=lambda c: -c["size"])
    return corners


def alpha_findings(alpha: "np.ndarray") -> tuple[list[dict], dict]:
    height, width = alpha.shape
    long_edge = max(width, height)
    findings: list[dict] = []
    solid = alpha >= 128
    ys, xs = np.nonzero(solid)
    if ys.size == 0:
        return [{"code": "empty", "level": "high", "detail": "图里几乎没有不透明的主体"}], {}
    fx0, fx1, fy0, fy1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    fig_w, fig_h = fx1 - fx0 + 1, fy1 - fy0 + 1

    lines = find_lines(alpha)
    corners = find_corners(lines, long_edge)
    best = lines[0] if lines else None
    score = best["mass"] / long_edge if best else 0.0
    metrics = {
        "cut_score": round(score, 4),
        "figure_box": [fx0, fy0, fx1, fy1],
        "lines": lines[:5],
        "corners": corners[:3],
    }

    def where(line: dict) -> str:
        x0, y0, x1, y1 = line["box"]
        cy = (y0 + y1) / 2
        rel = (cy - fy0) / max(1, fig_h)
        part = "上部" if rel < 0.35 else ("中部" if rel < 0.7 else "下部")
        return f"位于人物{part}（{x0},{y0}→{x1},{y1}，{DIRECTIONS[line['dir']]}）"

    if best and score >= CUT_LOW and best["mass"] >= CUT_MIN_PIXELS:
        hole = best["hole_hit"] >= HOLE_HIT_FRACTION or any(c["size"] >= CUT_LOW * long_edge * 0.5 for c in corners)
        level = "high" if score >= CUT_HIGH or (hole and corners) else "low"
        code = "rect_hole" if hole else "straight_cut"
        label = "矩形透明空洞" if hole else "被直线整块裁掉"
        if level == "low":
            code = "short_cut" if not hole else code
        findings.append(
            {
                "code": code,
                "level": level,
                "detail": f"{label}：直线总长 {best['mass']} 像素（长边 {score:.1%}）{where(best)}",
            }
        )
    elif corners and corners[0]["size"] >= CORNER_MIN * long_edge:
        c = corners[0]
        findings.append(
            {"code": "rect_hole", "level": "low", "detail": f"透明边缘有直角（{c['x']},{c['y']}），边长 {c['size']} 像素"}
        )

    top_cover = float((alpha[0] >= 128).mean())
    metrics["top_edge_cover"] = round(top_cover, 3)
    if top_cover >= TOP_EDGE_COVER:
        findings.append(
            {"code": "top_edge", "level": "low", "detail": f"主体在画布最上边被截断（顶边 {top_cover:.0%} 宽度有主体）"}
        )

    band = max(2, int(round(fig_h * WIDE_TOP_BAND)))
    band_rows = solid[fy0 : fy0 + band, fx0 : fx1 + 1]
    # 忽略零星像素：每列至少 25% 的行有主体才算占用
    occupied = np.nonzero(band_rows.mean(axis=0) >= 0.25)[0]
    span = (occupied.max() - occupied.min() + 1) / fig_w if occupied.size else 0.0
    metrics["top_span"] = round(float(span), 3)
    if span >= WIDE_TOP_FRACTION:
        findings.append({"code": "wide_top", "level": "low", "detail": f"主体最上方 {WIDE_TOP_BAND:.0%} 就占了 {span:.0%} 宽，像是从肩部开始"})
    return findings, metrics


def _cascade():
    global _CASCADE
    if _CASCADE is None and cv2 is not None:
        cv2.setNumThreads(1)
        cascade = cv2.CascadeClassifier(str(CASCADE_PATH))
        _CASCADE = cascade if not cascade.empty() else False
    return _CASCADE or None


def face_score(image: "Image.Image") -> tuple[int, list[int] | None]:
    """返回最佳动漫人脸的邻域票数和框（原图坐标）；分别在灰底、白底和水平翻转上检测。"""
    cascade = _cascade()
    if cascade is None:
        return -1, None
    rgba = image.convert("RGBA")
    scale = min(1.0, FACE_LONG_EDGE / max(rgba.size))
    if scale < 1.0:
        rgba = rgba.resize((max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale))), Image.Resampling.BOX)
    has_alpha = rgba.getchannel("A").getextrema()[0] < 250
    best, best_box = 0, None
    for color in ((160, 160, 160, 255), (255, 255, 255, 255)) if has_alpha else ((0, 0, 0, 255),):
        canvas = Image.new("RGBA", rgba.size, color)
        canvas.alpha_composite(rgba)
        gray = cv2.equalizeHist(np.asarray(canvas.convert("L")))
        for flip in (False, True):
            g = np.ascontiguousarray(gray[:, ::-1]) if flip else gray
            rects, votes = cascade.detectMultiScale2(g, scaleFactor=1.08, minNeighbors=2, minSize=(24, 24))
            for (x, y, w, h), n in zip(rects, votes):
                if int(n) > best:
                    if flip:
                        x = g.shape[1] - x - w
                    best = int(n)
                    best_box = [int(v / scale) for v in (x, y, w, h)]
        if best >= 8:
            break
    return best, best_box


def skin_fraction(image: "Image.Image") -> float:
    """主体中接近动漫肤色的像素比例（粗略，只用于给“没有人物”的错图排序）。"""
    small = image.convert("RGBA")
    small.thumbnail((512, 512))
    solid = np.asarray(small.getchannel("A")) >= 200
    hsv = np.asarray(small.convert("RGB").convert("HSV")).astype(np.float32)
    hue = hsv[..., 0] * (360.0 / 255.0)
    sat = hsv[..., 1] / 255.0
    val = hsv[..., 2] / 255.0
    skin = solid & ((hue <= 35) | (hue >= 345)) & (sat >= 0.08) & (sat <= 0.55) & (val >= 0.65)
    return float(skin.sum() / max(1, solid.sum()))


def analyze_image(path: Path, use_face: bool = True) -> dict:
    result: dict = {"path": str(path), "findings": [], "metrics": {}}
    try:
        with Image.open(path) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            result["size"] = list(image.size)
            has_alpha = "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)
            if has_alpha:
                image = image.convert("RGBA")
                alpha = np.asarray(image.getchannel("A"))
                if alpha.min() < 250:
                    findings, metrics = alpha_findings(alpha)
                    result["findings"].extend(findings)
                    result["metrics"].update(metrics)
                else:
                    has_alpha = False
            result["transparent"] = bool(has_alpha)
            if use_face:
                score, box = face_score(image)
                result["metrics"]["face_score"] = score
                result["metrics"]["face_box"] = box
                if 0 <= score < FACE_WEAK_SCORE:
                    skin = skin_fraction(image)
                    result["metrics"]["skin_fraction"] = round(skin, 4)
                    if (score < FACE_MIN_SCORE and skin < SKIN_FRACTION_MIN) or skin < SKIN_FRACTION_NONE:
                        result["findings"].append(
                            {"code": "no_figure", "level": "low",
                             "detail": f"没找到可信的脸（票数 {score}），肤色只占主体 {skin:.1%}，可能只剩背景、特效或舰装图层"}
                        )
                    elif score < FACE_MIN_SCORE:
                        result["findings"].append(
                            {"code": "no_face", "level": "low", "detail": f"动漫人脸检测器没找到可信的脸（票数 {score}）"}
                        )
    except Exception as exc:  # 坏图也列出来
        result["findings"].append({"code": "unreadable", "level": "high", "detail": f"无法完整解码：{type(exc).__name__}: {exc}"})
    # wide_top 单独出现时几乎都是误报（头发、帽子、舰装），只在有其他信号时保留
    if all(f["code"] == "wide_top" for f in result["findings"]):
        result["findings"] = []
    levels = {f["level"] for f in result["findings"]}
    result["level"] = "high" if "high" in levels else ("low" if levels else "ok")
    # 排序分：高信号按直线长度，低信号按数量
    result["rank"] = round(
        (10 if result["level"] == "high" else 0)
        + result["metrics"].get("cut_score", 0) * 20
        + len(result["findings"])
        + (3 if any(f["code"] == "no_figure" for f in result["findings"]) else 0),
        3,
    )
    return result


def _worker(task: tuple[str, str, str, str, bool]) -> dict:
    work, name, rel, path, use_face = task
    record = analyze_image(Path(path), use_face)
    record.update({"work": work, "name": name, "relative_path": rel})
    return record


def collect_tasks(work_dirs: list[Path], use_face: bool) -> tuple[list[tuple], list[dict]]:
    tasks, missing = [], []
    for work_dir in work_dirs:
        checklist = work_dir / "00-清单.md"
        for item in parse_checklist(checklist.read_text(encoding="utf-8")):
            if not item["checked"] or not item["image_path"]:
                continue
            path = resolve_image(work_dir, item["image_path"])
            if path is None or not path.is_file():
                missing.append({"work": work_dir.name, "name": item["name"], "relative_path": item["image_path"]})
                continue
            tasks.append((work_dir.name, item["name"], item["image_path"], str(path), use_face))
    return tasks, missing


def _font(size: int):
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _thumbnail(record: dict, size: tuple[int, int]) -> "Image.Image":
    with Image.open(record["path"]) as opened:
        opened.load()
        image = ImageOps.exif_transpose(opened).convert("RGBA")
    width, height = image.size
    canvas = Image.new("RGBA", image.size, (255, 0, 255, 255))  # 品红底，透明空洞一眼可见
    canvas.alpha_composite(image)
    rgb = canvas.convert("RGB")
    draw = ImageDraw.Draw(rgb)
    stroke = max(3, max(width, height) // 120)
    for line in record["metrics"].get("lines", [])[:3]:
        if line["mass"] >= CUT_LOW * max(width, height) * 0.5:
            draw.line(line["box"], fill=(0, 255, 0), width=stroke)
    box = record["metrics"].get("face_box")
    if box:
        x, y, w, h = box
        draw.rectangle((x, y, x + w, y + h), outline=(0, 160, 255), width=stroke)
    rgb.thumbnail(size, Image.Resampling.LANCZOS)
    return rgb


def write_sheets(records: list[dict], out_dir: Path, tag: str, per_sheet: int = 24, cols: int = 6) -> list[Path]:
    font = _font(18)
    small = _font(14)
    tw, th, label_h = 300, 340, 64
    paths = []
    for start in range(0, len(records), per_sheet):
        chunk = records[start : start + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, rows * (th + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, record in enumerate(chunk):
            x, y = (i % cols) * tw, (i // cols) * (th + label_h)
            try:
                thumb = _thumbnail(record, (tw - 8, th - 4))
                sheet.paste(thumb, (x + (tw - thumb.width) // 2, y + label_h))
            except Exception:
                draw.text((x + 6, y + label_h + 6), "无法打开", fill=(200, 0, 0), font=font)
            color = (200, 0, 0) if record["level"] == "high" else (180, 110, 0)
            draw.text((x + 4, y + 2), f"{start + i + 1}. {record['name']}"[:16], fill=color, font=font)
            codes = "、".join(dict.fromkeys(f["code"] for f in record["findings"]))
            draw.text((x + 4, y + 26), f"{record['work']} {codes}"[:34], fill=(60, 60, 60), font=small)
            draw.rectangle((x, y, x + tw - 1, y + th + label_h - 1), outline=(210, 210, 210))
        path = out_dir / f"{tag}-{start // per_sheet + 1:02d}.jpg"
        sheet.save(path, quality=85)
        paths.append(path)
    return paths


def write_summary(records: list[dict], missing: list[dict], out_dir: Path, elapsed: float, face_enabled: bool) -> Path:
    lines = ["# 残图自动检查结果", ""]
    lines.append(f"共检查 {len(records)} 张已勾选图片，用时 {elapsed:.1f} 秒。" + ("" if face_enabled else "（未安装 OpenCV，跳过人脸检测）"))
    lines.append("高 = 很可能是残图，请逐张打开确认；低 = 误报较多，扫一眼联系表即可。")
    lines.append("")
    by_work: dict[str, list[dict]] = {}
    for record in records:
        by_work.setdefault(record["work"], []).append(record)
    lines.append("| 作品 | 检查 | 高 | 低 |")
    lines.append("|---|---:|---:|---:|")
    for work, items in by_work.items():
        lines.append(
            f"| {work} | {len(items)} | {sum(r['level'] == 'high' for r in items)} | {sum(r['level'] == 'low' for r in items)} |"
        )
    for level, title in (("high", "高：很可能是残图"), ("low", "低：建议扫一眼")):
        lines += ["", f"## {title}", ""]
        for record in sorted((r for r in records if r["level"] == level), key=lambda r: (r["work"], -r["rank"])):
            reasons = "；".join(f["detail"] for f in record["findings"])
            lines.append(f"- {record['work']}／{record['name']}（{record['relative_path']}）：{reasons}")
    if missing:
        lines += ["", "## 清单已勾选但找不到图片", ""]
        lines += [f"- {m['work']}／{m['name']}（{m['relative_path']}）" for m in missing]
    path = out_dir / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def resolve_works(names: list[str], root: Path) -> list[Path]:
    all_works = find_work_dirs(root)
    if not names:
        return all_works
    chosen = []
    for name in names:
        candidate = Path(name)
        if not candidate.is_dir():
            candidate = root / "images" / name
        if not (candidate / "00-清单.md").is_file():
            raise SystemExit(f"找不到作品：{name}")
        chosen.append(candidate.resolve())
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="找出疑似残缺的角色立绘（只读，不修改清单和图片）")
    parser.add_argument("works", nargs="*", help="作品文件夹名或路径，默认全部作品")
    parser.add_argument("--json", action="store_true", help="把完整结果以 JSON 打印到标准输出")
    parser.add_argument("--sheet", action="store_true", help="为被标记的图片生成联系表（缩略图 + 原因）")
    parser.add_argument("--all-levels", action="store_true", help="联系表也包含“低”等级（默认只画“高”）")
    parser.add_argument("--out", help="输出目录，默认 out/defect_check")
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)), help="并行进程数")
    parser.add_argument("--no-face", action="store_true", help="跳过人脸检测（更快）")
    parser.add_argument("--strict", action="store_true", help="有“高”等级结果时以退出码 1 结束（默认总是 0）")
    args = parser.parse_args(argv)

    root = repo_root()
    work_dirs = resolve_works(args.works, root)
    use_face = not args.no_face and cv2 is not None and CASCADE_PATH.is_file()
    if not args.no_face and not use_face:
        print("提示：未安装 opencv-python-headless 或缺少人脸模型，跳过人脸检测。", file=sys.stderr)
    tasks, missing = collect_tasks(work_dirs, use_face)
    out_dir = Path(args.out) if args.out else root / "out" / "defect_check"
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    records: list[dict] = []
    if args.workers > 1 and len(tasks) > 1:
        from multiprocessing import Pool

        with Pool(args.workers) as pool:
            for i, record in enumerate(pool.imap_unordered(_worker, tasks, chunksize=4), 1):
                records.append(record)
                if i % 100 == 0:
                    print(f"已检查 {i}/{len(tasks)}", file=sys.stderr, flush=True)
    else:
        for task in tasks:
            records.append(_worker(task))
    elapsed = time.time() - started
    order = {task[3]: i for i, task in enumerate(tasks)}
    records.sort(key=lambda r: order[r["path"]])

    (out_dir / "results.json").write_text(
        json.dumps({"elapsed_seconds": round(elapsed, 1), "face_detection": use_face, "records": records, "missing": missing},
                   ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    summary = write_summary(records, missing, out_dir, elapsed, use_face)
    sheets: list[Path] = []
    if args.sheet:
        for work in dict.fromkeys(r["work"] for r in records):
            flagged = sorted((r for r in records if r["work"] == work and r["level"] == "high"), key=lambda r: -r["rank"])
            if flagged:
                sheets += write_sheets(flagged, out_dir, f"{work}-高")
            if args.all_levels:
                low = sorted((r for r in records if r["work"] == work and r["level"] == "low"), key=lambda r: -r["rank"])
                if low:
                    sheets += write_sheets(low, out_dir, f"{work}-低")

    high = sum(r["level"] == "high" for r in records)
    low = sum(r["level"] == "low" for r in records)
    if args.json:
        print(json.dumps({"records": records, "missing": missing}, ensure_ascii=False, indent=1))
    else:
        print(f"检查 {len(records)} 张，用时 {elapsed:.1f} 秒：高 {high} 张，低 {low} 张。")
        for record in records:
            if record["level"] == "high":
                print(f"  [高] {record['work']}／{record['name']}：" + "；".join(f["detail"] for f in record["findings"]))
        print(f"完整结果：{summary}、{out_dir / 'results.json'}")
        if sheets:
            print(f"联系表 {len(sheets)} 张：{out_dir}")
        print("这些只是提示：打开被标记的图确认后，再决定是否取消勾选或重新找图。")
    return 1 if args.strict and high else 0


if __name__ == "__main__":
    raise SystemExit(main())
