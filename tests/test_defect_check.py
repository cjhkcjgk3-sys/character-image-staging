#!/usr/bin/env python3
"""Tests for tools/defect_check.py with synthetic images (needs Pillow + numpy; skipped otherwise)."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

HAVE_DEPS = all(importlib.util.find_spec(name) for name in ("numpy", "PIL"))

if HAVE_DEPS:
    from PIL import Image, ImageDraw

    import defect_check as dc


def figure(width: int = 400, height: int = 600) -> "Image.Image":
    """A smooth, anti-aliased "character" (head, slanted torso, legs, arm), drawn 4x and downsampled.

    Outlines are curved or slanted, like hand-drawn art, so no edge is exactly axis-aligned.
    """
    s = 4 * width / 400
    big = Image.new("RGBA", (round(width * 4), round(height * 4)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)

    def pts(*xy):
        return [(x * s, y * s) for x, y in xy]

    draw.ellipse(tuple(v * s for v in (152, 40, 248, 146)), fill=(240, 200, 180, 255))          # head
    draw.polygon(pts((170, 135), (232, 138), (292, 400), (215, 560), (118, 548), (140, 380)),
                 fill=(60, 80, 160, 255))                                                     # torso / skirt
    draw.polygon(pts((150, 170), (118, 190), (64, 410), (88, 418)), fill=(240, 200, 180, 255))  # arm
    draw.ellipse(tuple(v * s for v in (230, 300, 330, 380)), fill=(200, 60, 60, 255))          # prop
    return big.resize((width, height), Image.Resampling.LANCZOS)


def clear_rect(image: "Image.Image", box: tuple[int, int, int, int]) -> "Image.Image":
    out = image.copy()
    hole = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    out.paste(hole, box[:2])
    return out


@unittest.skipUnless(HAVE_DEPS, "需要 Pillow 和 numpy")
class DefectCheckTests(unittest.TestCase):
    def analyze(self, image: "Image.Image") -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            image.save(path)
            return dc.analyze_image(path, use_face=False)

    def codes(self, record: dict) -> set[str]:
        return {f["code"] for f in record["findings"]}

    def test_clean_figure_has_no_cut_or_hole(self):
        record = self.analyze(figure())
        self.assertFalse({"rect_hole", "straight_cut", "short_cut"} & self.codes(record), record["findings"])
        self.assertEqual(record["level"], "ok")

    def test_rectangular_hole_in_face_is_flagged(self):
        record = self.analyze(clear_rect(figure(), (165, 60, 235, 130)))
        self.assertIn("rect_hole", self.codes(record))
        self.assertEqual(record["level"], "high")

    def test_head_cut_off_by_rectangle_is_flagged(self):
        record = self.analyze(clear_rect(figure(), (100, 0, 300, 200)))
        self.assertTrue({"straight_cut", "rect_hole"} & self.codes(record), record["findings"])
        self.assertEqual(record["level"], "high")

    def test_hole_survives_downscaling(self):
        holed = clear_rect(figure(800, 1200), (330, 120, 470, 260))
        record = self.analyze(holed.resize((560, 840), Image.Resampling.LANCZOS))
        self.assertEqual(record["level"], "high", record["findings"])

    def test_crop_at_canvas_border_is_not_a_cut(self):
        cropped = figure().crop((0, 300, 400, 600))  # body runs off the top edge of the canvas
        record = self.analyze(cropped)
        self.assertFalse({"rect_hole", "straight_cut"} & self.codes(record), record["findings"])

    def test_opaque_image_skips_alpha_checks(self):
        record = self.analyze(Image.new("RGB", (300, 400), (200, 180, 160)))
        self.assertFalse(record["transparent"])
        self.assertEqual(record["findings"], [])


if __name__ == "__main__":
    unittest.main()
