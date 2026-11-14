#!/usr/bin/env python3
"""为角色立绘计算裁剪框（默认只读），需要时再就地裁剪并压缩。

默认只读：给每张图算一个建议裁剪框，写出品红底对照联系表和 JSON。
加 --apply 时，只对 status=auto 的图就地裁剪，再按质量从 98 逐档压到
小于 1,000,000 字节。status=manual 的图绝不会被自动改写。

处理顺序是先裁后压。人物本体必须完整保留：头（含头发、耳朵、头顶光环）、
脸、躯干、手臂、手、腿、脚、服装、头发。伸出本体之外的武器和特效可以裁掉。
裁框四周留约 3%–5% 的边距，人物居中。禁止放大。

依赖：Python 3.9+、Pillow、numpy。人脸检测和更快的形态学运算需要
opencv-python-headless；没有 OpenCV 时仍可按透明外接框做保守裁剪，
并把没有把握的带背景图标成 manual。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
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

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_EXCLUSIVE = 1_000_000
QUALITY_STEPS = (98, 97, 96, 95, 94, 92, 90, 88, 86, 84, 82, 80)
# 边距占裁剪后画面的比例，落在 3%–5%。
MARGIN_RATIO = 0.04
MARGIN_RATIO_MIN = 0.03
MARGIN_RATIO_MAX = 0.05
# 分析用的最长边，裁剪框再映射回原图像素。
ANALYZE_LONG_EDGE = 512
FACE_DETECT_LONG_EDGE = 1024
ALPHA_SUBJECT = 16
MAGENTA = (255, 0, 255)
CASCADE_PATH = Path(__file__).resolve().parent / "data" / "lbpcascade_animeface.xml"

# Box is (x0, y0, x1, y1) with exclusive bottom-right, in pixel coordinates.
Box = tuple[int, int, int, int]

_CASCADE = None


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def clip_box(box: Box, width: int, height: int) -> Box | None:
    x0, y0, x1, y1 = box
    x0 = max(0, min(width, int(x0)))
    y0 = max(0, min(height, int(y0)))
    x1 = max(0, min(width, int(x1)))
    y1 = max(0, min(height, int(y1)))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def box_union(a: Box | None, b: Box | None) -> Box | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def box_intersect(a: Box | None, b: Box | None) -> Box | None:
    if a is None or b is None:
        return None
    box = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def box_area(box: Box | None) -> int:
    if box is None:
        return 0
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def scale_box(box: Box, scale: float) -> Box:
    """Map a box from a uniformly scaled image back (or forth) by ``scale``."""
    return (
        int(round(box[0] * scale)),
        int(round(box[1] * scale)),
        int(round(box[2] * scale)),
        int(round(box[3] * scale)),
    )


def content_bbox(mask: np.ndarray) -> Box | None:
    """Tight bounding box of nonzero (or True) mask pixels, exclusive on the right/bottom."""
    if mask.size == 0:
        return None
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return None
    y0, y1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
    x0, x1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
    return x0, y0, x1, y1


def margin_inset(body_w: int, body_h: int, ratio: float = MARGIN_RATIO) -> tuple[int, int]:
    """Margin in pixels so that it is ``ratio`` of the final crop size.

    crop = body + 2 * margin  ⇒  margin = ratio * crop = ratio * (body + 2 * margin)
    ⇒ margin = ratio / (1 - 2 * ratio) * body.
    """
    ratio = min(MARGIN_RATIO_MAX, max(MARGIN_RATIO_MIN, float(ratio)))
    denom = 1.0 - 2.0 * ratio
    if denom <= 0:
        denom = 0.9
    mx = max(1, int(round((ratio / denom) * body_w))) if body_w > 0 else 1
    my = max(1, int(round((ratio / denom) * body_h))) if body_h > 0 else 1
    return mx, my


def add_margin_and_center(
    body: Box,
    width: int,
    height: int,
    ratio: float = MARGIN_RATIO,
) -> Box:
    """Expand ``body`` by ~3%–5% on each side so the body stays centered.

    If a side hits the image edge, the opposite side is not padded extra
    (that would off-center the body and pull in more background / effects).
    The result is clipped to the image and never larger than the original.
    """
    clipped = clip_box(body, width, height)
    if clipped is None:
        return 0, 0, width, height
    x0, y0, x1, y1 = clipped
    bw, bh = x1 - x0, y1 - y0
    mx, my = margin_inset(bw, bh, ratio)
    # Equal margins first so the body is centered in the proposed crop.
    crop = clip_box((x0 - mx, y0 - my, x1 + mx, y1 + my), width, height)
    if crop is None:
        return 0, 0, width, height
    return crop


def face_safe_box(face: tuple[int, int, int, int], width: int, height: int, generous: bool = True) -> Box:
    """Axis-aligned box that should cover a standing anime body given a face box.

    ``face`` is (x, y, w, h). ``generous=True`` is the keep-at-least estimate
    (hair / halo / long legs); ``False`` is a tighter protected minimum.
    """
    fx, fy, fw, fh = (int(v) for v in face)
    cx = fx + fw / 2.0
    head = max(fw, fh)
    if generous:
        top = fy - 1.8 * fh
        bottom = fy + 8.2 * fh
        half = 2.5 * max(fw, fh * 0.75)
    else:
        top = fy - 1.25 * fh
        bottom = fy + 7.0 * fh
        half = 1.65 * max(fw, fh * 0.8)
    box = (int(round(cx - half)), int(round(top)), int(round(cx + half)), int(round(bottom)))
    out = clip_box(box, width, height)
    if out is None:
        return 0, 0, width, height
    return out


def head_keep_box(face: tuple[int, int, int, int], width: int, height: int) -> Box:
    """Region around the face that must keep hair, ears and a halo on the head."""
    fx, fy, fw, fh = (int(v) for v in face)
    cx = fx + fw / 2.0
    cy = fy + fh / 2.0
    box = (
        int(round(cx - 1.6 * fw)),
        int(round(fy - 1.9 * fh)),
        int(round(cx + 1.6 * fw)),
        int(round(cy + 1.15 * fh)),
    )
    out = clip_box(box, width, height)
    if out is None:
        return clip_box((fx, fy, fx + fw, fy + fh), width, height) or (0, 0, width, height)
    return out


def face_cap_box(face: tuple[int, int, int, int], width: int, height: int) -> Box:
    """Upper bound for an auto crop: standing body plus slack.

    Dense skill bursts, companions and scenic backgrounds that sit outside
    this box may be trimmed. Hair that flows farther is added back separately.
    """
    fx, fy, fw, fh = (int(v) for v in face)
    cx = fx + fw / 2.0
    top = fy - 2.2 * fh
    bottom = fy + 8.8 * fh
    half = 3.3 * max(fw, fh * 0.8)
    box = (
        int(round(cx - half)),
        int(round(top)),
        int(round(cx + half)),
        int(round(bottom)),
    )
    out = clip_box(box, width, height)
    if out is None:
        return 0, 0, width, height
    return out


def hair_zone_box(face: tuple[int, int, int, int], width: int, height: int) -> Box:
    """A wide band around the head so long hair, ears and hats are not capped off."""
    fx, fy, fw, fh = (int(v) for v in face)
    cx = fx + fw / 2.0
    head = max(fw, fh)
    box = (
        int(round(cx - 4.3 * head)),
        int(round(fy - 2.6 * fh)),
        int(round(cx + 4.3 * head)),
        int(round(fy + 2.4 * fh)),
    )
    out = clip_box(box, width, height)
    if out is None:
        return head_keep_box(face, width, height)
    return out


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------

def _rgba(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGBA":
        return image.convert("RGBA")
    return image


def has_useful_alpha(alpha: np.ndarray) -> bool:
    """True when the image has a real transparent (or nearly transparent) background."""
    if alpha.size == 0:
        return False
    return bool(alpha.min() < ALPHA_SUBJECT and (alpha > ALPHA_SUBJECT).any())


def subject_from_alpha(alpha: np.ndarray) -> np.ndarray:
    return alpha > ALPHA_SUBJECT


def border_color_stats(rgb: np.ndarray, border: int = 10) -> tuple[np.ndarray, float]:
    """Median RGB of the border pixels and their mean absolute deviation."""
    h, w, _ = rgb.shape
    b = max(1, min(border, h // 4, w // 4))
    strips = np.concatenate(
        [
            rgb[:b].reshape(-1, 3),
            rgb[-b:].reshape(-1, 3),
            rgb[b:-b, :b].reshape(-1, 3) if h > 2 * b else rgb[:, :b].reshape(-1, 3),
            rgb[b:-b, -b:].reshape(-1, 3) if h > 2 * b else rgb[:, -b:].reshape(-1, 3),
        ],
        axis=0,
    )
    median = np.median(strips, axis=0)
    mad = float(np.mean(np.abs(strips.astype(np.float32) - median.astype(np.float32))))
    return median.astype(np.float32), mad


def subject_from_chroma(rgb: np.ndarray, max_mad: float = 18.0, min_delta: float = 28.0) -> tuple[np.ndarray | None, str]:
    """If the border is a fairly uniform background, treat pixels unlike it as the subject."""
    median, mad = border_color_stats(rgb)
    if mad > max_mad:
        return None, f"border_mad={mad:.1f}"
    delta = np.sqrt(((rgb.astype(np.float32) - median) ** 2).sum(axis=2))
    # Threshold a bit above border noise.
    thresh = max(min_delta, mad * 3.0 + 8.0)
    mask = delta > thresh
    return mask, f"chroma_mad={mad:.1f},thr={thresh:.1f}"


def downscale_mask(mask: np.ndarray, long_edge: int = ANALYZE_LONG_EDGE) -> tuple[np.ndarray, float]:
    """Nearest-neighbour shrink so analysis stays quick. Returns (mask, scale_to_original)."""
    h, w = mask.shape
    longest = max(h, w)
    if longest <= long_edge:
        return mask.astype(bool), 1.0
    scale = long_edge / longest
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    # Pillow nearest keeps binary masks clean.
    img = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    small = np.asarray(img.resize((nw, nh), Image.Resampling.NEAREST)) > 127
    return small, 1.0 / scale


def distance_transform(mask: np.ndarray) -> np.ndarray:
    """Foreground distance to the nearest background pixel."""
    if cv2 is not None:
        src = (mask.astype(np.uint8) * 255)
        return cv2.distanceTransform(src, cv2.DIST_L2, 5)
    # Two-pass chamfer (3, 4) approximation; fine on the downscaled mask.
    h, w = mask.shape
    inf = float(h + w + 1)
    dist = np.where(mask, inf, 0.0).astype(np.float32)
    ortho, diag = 1.0, 1.41421356
    for y in range(h):
        for x in range(w):
            v = dist[y, x]
            if v == 0:
                continue
            if y:
                v = min(v, dist[y - 1, x] + ortho)
                if x:
                    v = min(v, dist[y - 1, x - 1] + diag)
                if x + 1 < w:
                    v = min(v, dist[y - 1, x + 1] + diag)
            if x:
                v = min(v, dist[y, x - 1] + ortho)
            dist[y, x] = v
    for y in range(h - 1, -1, -1):
        for x in range(w - 1, -1, -1):
            v = dist[y, x]
            if v == 0:
                continue
            if y + 1 < h:
                v = min(v, dist[y + 1, x] + ortho)
                if x:
                    v = min(v, dist[y + 1, x - 1] + diag)
                if x + 1 < w:
                    v = min(v, dist[y + 1, x + 1] + diag)
            if x + 1 < w:
                v = min(v, dist[y, x + 1] + ortho)
            dist[y, x] = v
    return dist


def _label(mask: np.ndarray) -> tuple[int, np.ndarray]:
    if cv2 is not None:
        n, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
        return int(n) - 1, labels
    # Simple 8-connected component labeler (used on the downscaled mask).
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    current = 0
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if not row[x] or labels[y, x]:
                continue
            current += 1
            stack = [(y, x)]
            labels[y, x] = current
            while stack:
                cy, cx = stack.pop()
                for ny in range(max(0, cy - 1), min(h, cy + 2)):
                    for nx in range(max(0, cx - 1), min(w, cx + 2)):
                        if mask[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = current
                            stack.append((ny, nx))
    return current, labels


def drop_small_blobs(mask: np.ndarray, min_fraction: float = 0.002, protect: Box | None = None) -> np.ndarray:
    """Remove isolated specks (typical particle effects), but never touch ``protect``."""
    subject = int(mask.sum())
    if subject == 0:
        return mask
    min_area = max(8, int(round(subject * min_fraction)))
    n, labels = _label(mask)
    if n <= 1:
        return mask
    keep = np.zeros_like(mask)
    prot = np.zeros_like(mask)
    if protect is not None:
        x0, y0, x1, y1 = protect
        prot[y0:y1, x0:x1] = True
    for idx in range(1, n + 1):
        comp = labels == idx
        if int(comp.sum()) >= min_area or (protect is not None and np.any(comp & prot)):
            keep |= comp
    return keep if keep.any() else mask


def pick_component(mask: np.ndarray, prefer: Box | None = None) -> np.ndarray:
    """Keep the component that overlaps ``prefer`` (e.g. the face); else the largest."""
    if not mask.any():
        return mask
    n, labels = _label(mask)
    if n <= 1:
        return mask
    best_idx, best_score = 1, -1
    for idx in range(1, n + 1):
        comp = labels == idx
        area = int(comp.sum())
        score = area
        if prefer is not None:
            x0, y0, x1, y1 = prefer
            overlap = int(comp[y0:y1, x0:x1].sum())
            if overlap:
                score = area + overlap * 10
        if score > best_score:
            best_idx, best_score = idx, score
    return labels == best_idx


def components_touching(candidate: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """Components of ``candidate`` that overlap ``seed``."""
    if not candidate.any() or not seed.any():
        return np.zeros_like(candidate)
    n, labels = _label(candidate)
    keep = np.zeros_like(candidate)
    for idx in range(1, n + 1):
        comp = labels == idx
        if np.any(comp & seed):
            keep |= comp
    return keep


def _fill_box(mask: np.ndarray, box: Box) -> np.ndarray:
    out = np.zeros_like(mask)
    x0, y0, x1, y1 = box
    out[y0:y1, x0:x1] = True
    return out


def body_box_from_mask(
    mask: np.ndarray,
    face: tuple[int, int, int, int] | None = None,
) -> tuple[Box | None, str]:
    """Conservative body bounding box: keep the solid figure, trim thin far protrusions."""
    h, w = mask.shape
    content = content_bbox(mask)
    if content is None:
        return None, "empty"
    face_box: Box | None = None
    if face is not None:
        fx, fy, fw, fh = (int(v) for v in face)
        face_box = clip_box((fx, fy, fx + fw, fy + fh), w, h)

    cleaned = drop_small_blobs(mask, min_fraction=0.002, protect=face_box)
    content = content_bbox(cleaned) or content

    dist = distance_transform(cleaned)
    max_d = float(dist.max())
    if max_d < 1.5:
        # Almost everything is a thin outline; do not guess, keep the content box.
        return content, "thin_subject"

    core_t = max(2.0, 0.28 * max_d)
    core = pick_component(dist >= core_t, prefer=face_box)

    keep = core.copy()
    if face_box is not None:
        safe = face_safe_box(face, w, h, generous=True)
        prot = face_safe_box(face, w, h, generous=False)
        head = head_keep_box(face, w, h)
        keep |= cleaned & _fill_box(cleaned, box_intersect(safe, content) or safe)
        keep |= cleaned & _fill_box(cleaned, head)
        # Anisotropic grow from the core: more vertical (torso / legs / hair)
        # than horizontal, so side weapons get less of a free pass.
        fx, fy, fw, fh = (int(v) for v in face)
        rad_x = max(3, int(round(0.85 * max(fw, 0.5 * max_d * 2))))
        rad_y = max(4, int(round(1.15 * fh)))
    else:
        prot = None
        rad_x = max(3, int(round(0.45 * max_d * 2)))
        rad_y = max(4, int(round(0.90 * max_d * 2)))

    grown = _dilate(core, rad_x, rad_y) & cleaned
    keep |= grown

    # Medium-thickness parts attached to the body (arms, coats, tails, hands).
    med_t = max(1.6, 0.12 * max_d)
    medium = (dist >= med_t) & cleaned
    keep |= components_touching(medium, keep)

    box = content_bbox(keep)
    if prot is not None:
        box = box_union(box, box_intersect(prot, content))
    if face_box is not None:
        # Face, hair margin and a minimum body estimate must stay inside.
        head = head_keep_box(face, w, h)
        must = box_union(face_box, box_intersect(head, content))
        box = box_union(box, must)
        # Cap far-away dense effects / scenery, but let long hair out of the cap.
        cap = face_cap_box(face, w, h)
        hair = hair_zone_box(face, w, h)
        allowed = _fill_box(cleaned, cap)
        head_seed = cleaned & _fill_box(cleaned, head)
        hair_mask = cleaned & _fill_box(cleaned, hair)
        allowed |= components_touching(hair_mask, head_seed)
        # Thick clothing just outside the cap (a cloak edge, a wide dress).
        slack = _dilate(_fill_box(cleaned, cap), max(2, int(round(0.35 * max_d))), max(2, int(round(0.35 * max_d))))
        allowed |= slack & (dist >= max(1.6, 0.12 * max_d)) & cleaned
        trimmed = content_bbox(keep & allowed)
        box = box_union(trimmed, box_intersect(box, cap))
        box = box_union(box, must)
    box = box_intersect(box, content) or content
    reason = "face+mass" if face_box is not None else "alpha_mass"
    return box, reason


def _dilate(mask: np.ndarray, rx: int, ry: int) -> np.ndarray:
    rx, ry = max(0, int(rx)), max(0, int(ry))
    if rx == 0 and ry == 0:
        return mask
    if cv2 is not None:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (rx * 2 + 1, ry * 2 + 1))
        return cv2.dilate(mask.astype(np.uint8), kernel).astype(bool)
    # Separable max-filter fallback.
    h, w = mask.shape
    src = mask.astype(np.uint8)
    tmp = np.zeros_like(src)
    for y in range(h):
        y0, y1 = max(0, y - ry), min(h, y + ry + 1)
        tmp[y] = src[y0:y1].max(axis=0)
    out = np.zeros_like(src)
    for x in range(w):
        x0, x1 = max(0, x - rx), min(w, x + rx + 1)
        out[:, x] = tmp[:, x0:x1].max(axis=1)
    return out.astype(bool)


# ---------------------------------------------------------------------------
# Face detection
# ---------------------------------------------------------------------------

def _cascade():
    global _CASCADE
    if _CASCADE is None and cv2 is not None and CASCADE_PATH.is_file():
        cv2.setNumThreads(1)
        cascade = cv2.CascadeClassifier(str(CASCADE_PATH))
        _CASCADE = cascade if not cascade.empty() else False
    return _CASCADE or None


def detect_face(image: Image.Image) -> tuple[int, tuple[int, int, int, int] | None]:
    """Best anime face (score, (x, y, w, h) in original pixels). Score 0 means none."""
    cascade = _cascade()
    if cascade is None:
        return 0, None
    rgba = _rgba(image)
    scale = min(1.0, FACE_DETECT_LONG_EDGE / max(rgba.size))
    work = rgba
    if scale < 1.0:
        work = rgba.resize(
            (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale))),
            Image.Resampling.BOX,
        )
    has_alpha = work.getchannel("A").getextrema()[0] < 250
    best, best_box = 0, None
    backgrounds = ((160, 160, 160, 255), (255, 255, 255, 255)) if has_alpha else ((0, 0, 0, 255),)
    for color in backgrounds:
        canvas = Image.new("RGBA", work.size, color)
        canvas.alpha_composite(work)
        gray = cv2.equalizeHist(np.asarray(canvas.convert("L")))
        for flip in (False, True):
            g = np.ascontiguousarray(gray[:, ::-1]) if flip else gray
            rects, votes = cascade.detectMultiScale2(
                g, scaleFactor=1.08, minNeighbors=2, minSize=(24, 24)
            )
            for (x, y, fw, fh), n in zip(rects, votes):
                if int(n) > best:
                    if flip:
                        x = g.shape[1] - x - fw
                    best = int(n)
                    best_box = (int(x / scale), int(y / scale), int(fw / scale), int(fh / scale))
        if best >= 8:
            break
    return best, best_box


# ---------------------------------------------------------------------------
# Propose / apply / compress
# ---------------------------------------------------------------------------

def load_image(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        opened.load()
        return ImageOps.exif_transpose(opened)


def _analyze_subject(image: Image.Image) -> tuple[np.ndarray, str, str]:
    """Return (subject_mask at original size, kind, note). kind: alpha / chroma / none."""
    rgba = _rgba(image)
    alpha = np.asarray(rgba.getchannel("A"))
    if has_useful_alpha(alpha):
        return subject_from_alpha(alpha), "alpha", "transparent"
    rgb = np.asarray(rgba.convert("RGB"))
    mask, note = subject_from_chroma(rgb)
    if mask is not None and mask.any():
        fill = float(mask.mean())
        # A usable chroma key: subject is neither almost empty nor the whole frame.
        if 0.02 <= fill <= 0.96:
            return mask, "chroma", note
        return mask, "chroma", note + f",fill={fill:.2f}_unreliable"
    return np.ones(alpha.shape, dtype=bool), "none", note if mask is None else note


def propose_crop(
    image: Image.Image,
    margin_ratio: float = MARGIN_RATIO,
    face_hint: tuple[int, int, tuple[int, int, int, int] | None] | None = None,
) -> dict:
    """Compute a crop proposal for one image. Does not write files.

    Returns a dict with box, status ('auto' | 'manual' | 'noop'), reason, face, etc.
    """
    width, height = image.size
    full: Box = (0, 0, width, height)
    mask, kind, kind_note = _analyze_subject(image)
    if face_hint is None:
        face_score, face = detect_face(image)
    else:
        face_score, face = face_hint

    small, to_orig = downscale_mask(mask)
    face_small = None
    if face is not None:
        inv = 1.0 / to_orig
        fx, fy, fw, fh = face
        face_small = (
            int(round(fx * inv)),
            int(round(fy * inv)),
            max(1, int(round(fw * inv))),
            max(1, int(round(fh * inv))),
        )

    body_small, mass_reason = body_box_from_mask(small, face_small)
    if body_small is None:
        return {
            "box": full,
            "status": "manual",
            "reason": "empty_subject",
            "kind": kind,
            "kind_note": kind_note,
            "face": face,
            "face_score": face_score,
            "body": None,
        }

    body = clip_box(scale_box(body_small, to_orig), width, height) or full
    # Recompute the protected / head boxes in original coordinates and union them.
    content = clip_box(scale_box(content_bbox(small) or (0, 0, small.shape[1], small.shape[0]), to_orig), width, height)
    if face is not None and content is not None:
        prot = box_intersect(face_safe_box(face, width, height, generous=False), content)
        head = box_intersect(head_keep_box(face, width, height), content)
        body = box_union(body, box_union(prot, head)) or body
        body = box_intersect(body, content) or body

    crop = add_margin_and_center(body, width, height, margin_ratio)
    crop_area = box_area(crop)
    full_area = width * height
    # Almost no change: still report the (possibly tiny) trim, but call it noop
    # when the crop is ≥ 98% of the original area.
    almost_full = crop_area >= 0.98 * full_area

    # Confidence.
    status = "auto"
    reason_parts = [kind, mass_reason]
    if kind == "alpha":
        if face is None:
            reason_parts.append("no_face_conservative")
        status = "auto"
    elif kind == "chroma":
        fill_ok = "unreliable" not in kind_note
        if fill_ok and (face is not None and face_score >= 5):
            status = "auto"
            reason_parts.append("uniform_bg+face")
        elif fill_ok and face is None:
            # Uniform background but no face: propose, but ask for a look.
            status = "manual"
            reason_parts.append("uniform_bg_no_face")
        else:
            status = "manual"
            reason_parts.append("chroma_uncertain")
    else:
        if face is not None and face_score >= 8:
            # Busy / opaque background: we have a face-sized body estimate only.
            status = "manual"
            reason_parts.append("opaque_face_estimate")
        else:
            status = "manual"
            reason_parts.append("opaque_uncertain")
            crop = full
            body = full

    if almost_full and status == "auto":
        status = "noop"
        reason_parts.append("already_tight")

    return {
        "box": crop,
        "body": body,
        "status": status,
        "reason": ",".join(reason_parts),
        "kind": kind,
        "kind_note": kind_note,
        "face": face,
        "face_score": int(face_score),
        "image_size": [width, height],
        "margin_ratio": margin_ratio,
    }


def compress_cropped(image: Image.Image, dest: Path, original_suffix: str) -> tuple[Path, int, int]:
    """Write ``image`` under 1,000,000 bytes. Returns (path, size, quality).

    quality=100 means a lossless / original-format save that already fitted.
    Images with alpha are always written as WebP. Never upscales.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    has_alpha = image.mode in ("RGBA", "LA", "P") or (
        "A" in image.getbands() and image.getextrema()[-1][0] < 255
    )
    if has_alpha and image.mode != "RGBA":
        image = image.convert("RGBA")
    elif not has_alpha and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    suffix = original_suffix.lower()
    # Try to keep a lossless original format when the cropped result already fits.
    if not has_alpha and suffix in {".jpg", ".jpeg"}:
        trial = dest.with_suffix(".jpg")
        for q in (95, 92, 90, 88, 86, 84, 82, 80):
            image.convert("RGB").save(trial, format="JPEG", quality=q, optimize=True)
            size = trial.stat().st_size
            if size < MAX_EXCLUSIVE:
                return trial, size, q
    if not has_alpha and suffix == ".png":
        trial = dest.with_suffix(".png")
        image.save(trial, format="PNG", optimize=True)
        size = trial.stat().st_size
        if size < MAX_EXCLUSIVE:
            return trial, size, 100

    out = dest.with_suffix(".webp")
    to_save = image if has_alpha else image.convert("RGB")
    used_q = QUALITY_STEPS[-1]
    size = MAX_EXCLUSIVE
    for q in QUALITY_STEPS:
        to_save.save(out, format="WEBP", quality=q, method=6, exact=True)
        size = out.stat().st_size
        used_q = q
        if size < MAX_EXCLUSIVE:
            break
    if size >= MAX_EXCLUSIVE:
        raise ValueError(f"质量 80 仍超出 1 MB，不要继续盲目降质：{out}")
    return out, size, used_q


def apply_crop(image: Image.Image, box: Box, dest: Path, original_suffix: str) -> tuple[Path, int, int]:
    """Crop ``image`` to ``box`` (no upscale) and compress into ``dest``'s stem."""
    width, height = image.size
    clipped = clip_box(box, width, height)
    if clipped is None:
        raise ValueError(f"invalid crop box {box} for {width}x{height}")
    x0, y0, x1, y1 = clipped
    cropped = image.crop((x0, y0, x1, y1))
    return compress_cropped(cropped, dest, original_suffix)


# ---------------------------------------------------------------------------
# Contact sheets
# ---------------------------------------------------------------------------

def _font(size: int):
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _composite_magenta(image: Image.Image) -> Image.Image:
    rgba = _rgba(image)
    canvas = Image.new("RGBA", rgba.size, MAGENTA + (255,))
    canvas.alpha_composite(rgba)
    return canvas.convert("RGB")


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    fitted = image.copy()
    fitted.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), MAGENTA)
    canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return canvas


def render_pair(
    original: Image.Image,
    box: Box | None,
    cropped: Image.Image | None,
    thumb: tuple[int, int],
) -> Image.Image:
    """Before (red box) | after, both on a magenta background."""
    tw, th = thumb
    before = _composite_magenta(original)
    ow, oh = before.size
    draw = ImageDraw.Draw(before)
    if box is not None:
        x0, y0, x1, y1 = box
        # Line width scales with the original so it stays visible after the thumbnail.
        width = max(4, min(ow, oh) // 180)
        for i in range(width):
            draw.rectangle((x0 - i, y0 - i, x1 - 1 + i, y1 - 1 + i), outline=(255, 0, 0))
    left = _fit(before, tw, th)
    if cropped is None:
        right = Image.new("RGB", (tw, th), MAGENTA)
    else:
        right = _fit(_composite_magenta(cropped), tw, th)
    pair = Image.new("RGB", (tw * 2 + 8, th), (40, 40, 40))
    pair.paste(left, (0, 0))
    pair.paste(right, (tw + 8, 0))
    return pair


def write_sheets(
    records: list[dict],
    out_dir: Path,
    tag: str,
    max_width: int = 2000,
    pairs_per_row: int = 2,
    per_sheet: int = 12,
) -> list[Path]:
    """Write before/after contact sheets. Each sheet is at most ``max_width`` px wide."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font = _font(18)
    small_font = _font(14)
    gutter = 10
    label_h = 44
    cols = max(1, pairs_per_row)
    cell_w = (max_width - gutter * (cols + 1)) // cols
    thumb_h = int(cell_w * 0.48)
    thumb_w = (cell_w - 8) // 2
    paths: list[Path] = []

    for start in range(0, len(records), per_sheet):
        chunk = records[start : start + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        sheet_w = gutter + cols * cell_w + (cols - 1) * 0 + gutter
        # cell already accounts for width; recompute exact
        sheet_w = min(max_width, gutter * (cols + 1) + cols * cell_w)
        sheet_h = gutter + rows * (thumb_h + label_h + gutter)
        sheet = Image.new("RGB", (sheet_w, sheet_h), (30, 30, 30))
        draw = ImageDraw.Draw(sheet)
        for i, rec in enumerate(chunk):
            r, c = divmod(i, cols)
            x = gutter + c * (cell_w + 0) + c * 0
            # actually columns spaced by gutter
            x = gutter + c * (cell_w + gutter)
            y = gutter + r * (thumb_h + label_h + gutter)
            src = rec.get("_original")
            cropped = rec.get("_cropped")
            box = rec.get("box")
            if src is None and rec.get("path"):
                src = load_image(Path(rec["path"]))
            if src is not None:
                if cropped is None and box is not None and rec.get("status") != "manual":
                    x0, y0, x1, y1 = box
                    cropped = src.crop((x0, y0, x1, y1))
                elif cropped is None and box is not None and rec.get("status") == "manual":
                    # Still show the proposed box / tentative crop for review.
                    x0, y0, x1, y1 = box
                    cropped = src.crop((x0, y0, x1, y1))
                pair = render_pair(src, box, cropped, (thumb_w, thumb_h))
                sheet.paste(pair, (x, y + label_h))
            name = rec.get("name") or Path(str(rec.get("path", "?"))).stem
            status = rec.get("status", "?")
            size = rec.get("image_size") or ((src.size[0], src.size[1]) if src is not None else ("?", "?"))
            if box:
                bw, bh = box[2] - box[0], box[3] - box[1]
                geom = f"{size[0]}x{size[1]} → {bw}x{bh}"
            else:
                geom = f"{size[0]}x{size[1]}"
            label = f"{name}  [{status}]  {geom}"
            draw.text((x, y), label, fill=(255, 255, 255), font=font)
            reason = rec.get("reason") or ""
            if reason:
                draw.text((x, y + 20), reason[:80], fill=(200, 200, 200), font=small_font)
        index = start // per_sheet + 1
        path = out_dir / f"{tag}-{index:02d}.jpg"
        sheet.save(path, format="JPEG", quality=90, optimize=True)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def collect_images(paths: list[Path], root: Path) -> list[tuple[str, Path]]:
    """Resolve work dirs, work names, or file paths into (name, image_path) pairs."""
    found: list[tuple[str, Path]] = []
    for raw in paths:
        path = raw if raw.is_absolute() else (Path.cwd() / raw).resolve()
        if not path.exists():
            # Treat it as a work folder name under images/.
            candidate = root / "images" / raw.name
            if not candidate.exists():
                candidate = root / "images" / raw.as_posix()
            path = candidate
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            found.append((path.stem, path))
            continue
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES:
                    found.append((child.stem, child))
            continue
        raise FileNotFoundError(f"找不到作品目录或图片：{raw}")
    return found


def record_from_proposal(name: str, path: Path, proposal: dict) -> dict:
    rec = {
        "name": name,
        "path": str(path),
        "work": path.parent.name,
        "original_bytes": path.stat().st_size,
        **proposal,
    }
    # JSON-friendly
    if rec.get("box"):
        rec["box"] = [int(v) for v in rec["box"]]
    if rec.get("body"):
        rec["body"] = [int(v) for v in rec["body"]]
    if rec.get("face"):
        rec["face"] = [int(v) for v in rec["face"]]
    return rec


def maybe_size_if_applied(image: Image.Image, rec: dict) -> None:
    """Estimate cropped file size at q98+ without touching the source file."""
    if rec.get("status") == "manual" or not rec.get("box"):
        return
    box = tuple(rec["box"])  # type: ignore[arg-type]
    x0, y0, x1, y1 = box
    cropped = image.crop((x0, y0, x1, y1))
    rec["_cropped"] = cropped
    rec["crop_size"] = [cropped.width, cropped.height]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "crop"
            out, size, quality = compress_cropped(cropped, dest, Path(rec["path"]).suffix)
            rec["cropped_bytes"] = size
            rec["quality"] = quality
            rec["cropped_suffix"] = out.suffix
    except ValueError as exc:
        rec["cropped_bytes"] = None
        rec["quality"] = None
        rec["compress_error"] = str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="为角色立绘计算先裁后压的裁剪框（默认只读，不改原图）。"
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="作品目录、images/<作品>，或若干图片文件",
    )
    parser.add_argument("--apply", action="store_true", help="就地写入 auto 裁剪结果（manual 不会被改）")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="联系表和 JSON 的输出目录（默认 out/crop_preview，已在 .gitignore）",
    )
    parser.add_argument("--json", type=Path, default=None, help="JSON 路径（默认写到 --out/crops.json）")
    parser.add_argument("--no-sheet", action="store_true", help="不生成对照联系表")
    parser.add_argument("--margin", type=float, default=MARGIN_RATIO, help="裁剪边距占画面的比例（默认 0.04）")
    parser.add_argument("--tag", default="crop", help="联系表文件名前缀")
    parser.add_argument("--sheet-width", type=int, default=2000, help="联系表最大宽度")
    args = parser.parse_args(argv)

    margin = min(MARGIN_RATIO_MAX, max(MARGIN_RATIO_MIN, args.margin))
    root = repo_root()
    try:
        items = collect_images(args.paths, root)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if not items:
        sys.stderr.write("没有找到图片。\n")
        return 2

    out_dir = args.out or (root / "out" / "crop_preview")
    out_dir = out_dir if out_dir.is_absolute() else (Path.cwd() / out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    applied = skipped_manual = errors = 0
    for name, path in items:
        image = load_image(path)
        proposal = propose_crop(image, margin_ratio=margin)
        rec = record_from_proposal(name, path, proposal)
        rec["_original"] = image
        status = rec["status"]
        if args.apply and status == "auto":
            try:
                dest_stem = path.with_suffix("")
                out, size, quality = apply_crop(image, tuple(rec["box"]), dest_stem, path.suffix)
                rec["cropped_bytes"] = size
                rec["quality"] = quality
                rec["applied"] = True
                rec["applied_path"] = str(out)
                rec["crop_size"] = [rec["box"][2] - rec["box"][0], rec["box"][3] - rec["box"][1]]
                rec["_cropped"] = load_image(out)
                applied += 1
                if out.resolve() != path.resolve() and path.exists():
                    # Format changed (e.g. PNG → WebP); leave the old file and warn.
                    sys.stderr.write(f"已写出 {out.name}，原文件 {path.name} 仍在，请更新清单链接后删除旧文件。\n")
            except Exception as exc:  # noqa: BLE001 - report and continue
                rec["applied"] = False
                rec["compress_error"] = str(exc)
                errors += 1
                sys.stderr.write(f"裁剪失败 {path}: {exc}\n")
        elif args.apply and status == "manual":
            rec["applied"] = False
            skipped_manual += 1
            maybe_size_if_applied(image, rec)
        else:
            maybe_size_if_applied(image, rec)
        records.append(rec)

    json_path = args.json or (out_dir / "crops.json")
    payload = {
        "margin_ratio": margin,
        "count": len(records),
        "auto": sum(1 for r in records if r["status"] == "auto"),
        "manual": sum(1 for r in records if r["status"] == "manual"),
        "noop": sum(1 for r in records if r["status"] == "noop"),
        "applied": applied,
        "records": [
            {k: v for k, v in rec.items() if not k.startswith("_")}
            for rec in records
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sheets: list[Path] = []
    if not args.no_sheet:
        sheets = write_sheets(records, out_dir, args.tag, max_width=args.sheet_width)

    print(
        f"共 {len(records)} 张：auto {payload['auto']}，manual {payload['manual']}，"
        f"noop {payload['noop']}"
        + (f"，已应用 {applied}，跳过 manual {skipped_manual}，失败 {errors}" if args.apply else "")
    )
    print(f"JSON {json_path}")
    if sheets:
        print(f"联系表 {len(sheets)} 张：{out_dir}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
