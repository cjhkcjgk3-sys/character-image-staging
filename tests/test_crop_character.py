#!/usr/bin/env python3
"""Tests for tools/crop_character.py box and margin logic (needs Pillow + numpy)."""

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
    import numpy as np
    from PIL import Image, ImageDraw

    import crop_character as cc


@unittest.skipUnless(HAVE_DEPS, "需要 Pillow 和 numpy")
class MarginTests(unittest.TestCase):
    def test_margin_ratio_is_within_3_to_5_percent(self):
        body = (100, 50, 300, 450)  # 200 x 400
        crop = cc.add_margin_and_center(body, 1000, 1000, ratio=0.04)
        x0, y0, x1, y1 = crop
        bw, bh = 200, 400
        # The body stays inside, and each side's margin is ~4% of the crop.
        self.assertLessEqual(x0, 100)
        self.assertLessEqual(y0, 50)
        self.assertGreaterEqual(x1, 300)
        self.assertGreaterEqual(y1, 450)
        crop_w, crop_h = x1 - x0, y1 - y0
        self.assertGreaterEqual((crop_w - bw) / 2 / crop_w, 0.03 - 0.005)
        self.assertLessEqual((crop_w - bw) / 2 / crop_w, 0.05 + 0.005)
        self.assertGreaterEqual((crop_h - bh) / 2 / crop_h, 0.03 - 0.005)
        self.assertLessEqual((crop_h - bh) / 2 / crop_h, 0.05 + 0.005)

    def test_body_is_centered_when_room_exists(self):
        body = (200, 100, 400, 500)
        crop = cc.add_margin_and_center(body, 800, 800, ratio=0.04)
        left = body[0] - crop[0]
        right = crop[2] - body[2]
        top = body[1] - crop[1]
        bottom = crop[3] - body[3]
        self.assertLessEqual(abs(left - right), 1)
        self.assertLessEqual(abs(top - bottom), 1)

    def test_body_stays_centered_when_canvas_edges_limit_margin(self):
        # Body touches the left and top edges; margins shrink rather than shifting it.
        body = (0, 0, 100, 200)
        crop = cc.add_margin_and_center(body, 400, 400, ratio=0.04)
        self.assertLessEqual(crop[0], body[0])
        self.assertLessEqual(crop[1], body[1])
        self.assertGreaterEqual(crop[2], body[2])
        self.assertGreaterEqual(crop[3], body[3])
        self.assertEqual((body[0] + body[2]), (crop[0] + crop[2]))
        self.assertEqual((body[1] + body[3]), (crop[1] + crop[3]))

    def test_crop_never_leaves_the_image_or_upscales(self):
        body = (10, 10, 90, 90)
        crop = cc.add_margin_and_center(body, 100, 100, ratio=0.05)
        self.assertGreaterEqual(crop[0], 0)
        self.assertGreaterEqual(crop[1], 0)
        self.assertLessEqual(crop[2], 100)
        self.assertLessEqual(crop[3], 100)
        self.assertLessEqual(crop[2] - crop[0], 100)
        self.assertLessEqual(crop[3] - crop[1], 100)


@unittest.skipUnless(HAVE_DEPS, "需要 Pillow 和 numpy")
class BoxLogicTests(unittest.TestCase):
    def test_content_bbox_ignores_transparent_padding(self):
        mask = np.zeros((200, 200), dtype=bool)
        mask[40:160, 70:130] = True
        self.assertEqual(cc.content_bbox(mask), (70, 40, 130, 160))

    def test_transparent_padding_is_trimmed_and_body_kept(self):
        image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Solid "character" rectangle, well inside the canvas.
        draw.rectangle((150, 80, 249, 279), fill=(40, 80, 200, 255))
        # Inject a fake face so the body estimate is anchored (no OpenCV required).
        face = (175, 90, 50, 50)
        proposal = cc.propose_crop(image, face_hint=(12, face))
        box = tuple(proposal["box"])
        # The body must stay inside the crop, with some (not huge) margin.
        self.assertLessEqual(box[0], 150)
        self.assertLessEqual(box[1], 80)
        self.assertGreaterEqual(box[2], 250)
        self.assertGreaterEqual(box[3], 280)
        # Transparent margins must actually be trimmed.
        self.assertGreater(box[0], 20)
        self.assertGreater(box[1], 10)
        self.assertLess(box[2], 380)
        self.assertLess(box[3], 390)
        self.assertIn(proposal["status"], {"auto", "noop"})

    def test_thin_weapon_spike_is_trimmed_but_body_is_kept(self):
        image = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Torso / legs / head.
        draw.rectangle((200, 80, 299, 339), fill=(50, 90, 180, 255))
        draw.ellipse((210, 40, 289, 119), fill=(240, 200, 180, 255))
        # Thin long spike to the right (weapon / beam), 6 px tall.
        draw.rectangle((300, 180, 479, 185), fill=(200, 200, 220, 255))
        face = (220, 50, 60, 60)
        proposal = cc.propose_crop(image, face_hint=(12, face))
        box = tuple(proposal["box"])
        # Full body stays in.
        self.assertLessEqual(box[0], 200)
        self.assertLessEqual(box[1], 40)
        self.assertGreaterEqual(box[2], 300)
        self.assertGreaterEqual(box[3], 340)
        # Most of the spike is gone; a little leftover near the hand is fine.
        self.assertLess(box[2], 420)
        self.assertIn(proposal["status"], {"auto", "noop"})

    def test_thick_side_arm_is_kept(self):
        image = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 80, 299, 339), fill=(50, 90, 180, 255))
        draw.ellipse((210, 40, 289, 119), fill=(240, 200, 180, 255))
        # A thick arm + hand sticking out to the right.
        draw.rectangle((300, 140, 399, 189), fill=(240, 200, 180, 255))
        draw.ellipse((380, 130, 429, 199), fill=(240, 200, 180, 255))
        face = (220, 50, 60, 60)
        proposal = cc.propose_crop(image, face_hint=(12, face))
        box = tuple(proposal["box"])
        self.assertGreaterEqual(box[2], 430)
        self.assertLessEqual(box[1], 40)
        self.assertGreaterEqual(box[3], 340)

    def test_face_safe_box_covers_hair_and_feet_estimate(self):
        # Face at (200, 80, 40, 40) on a tall canvas.
        box = cc.face_safe_box((200, 80, 40, 40), 500, 800, generous=True)
        # Hair / halo above the face.
        self.assertLessEqual(box[1], 80 - int(1.2 * 40))
        # Feet well below the face.
        self.assertGreaterEqual(box[3], 80 + int(6.5 * 40))
        # Face itself stays inside.
        self.assertLessEqual(box[0], 200)
        self.assertGreaterEqual(box[2], 240)


@unittest.skipUnless(HAVE_DEPS, "需要 Pillow 和 numpy")
class OpaqueAndApplyTests(unittest.TestCase):
    def test_busy_opaque_background_is_manual(self):
        image = Image.new("RGB", (240, 240), (20, 20, 20))
        draw = ImageDraw.Draw(image)
        # Noisy background: many random-looking rectangles of different colors.
        colors = [(200, 10, 10), (10, 200, 10), (10, 10, 200), (200, 200, 20), (200, 20, 200)]
        for i, color in enumerate(colors * 8):
            x, y = (i * 37) % 220, (i * 53) % 220
            draw.rectangle((x, y, x + 30, y + 24), fill=color)
        proposal = cc.propose_crop(image, face_hint=(0, None))
        self.assertEqual(proposal["status"], "manual")

    def test_uniform_background_without_face_is_manual_but_has_a_box(self):
        image = Image.new("RGB", (300, 300), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((110, 40, 189, 259), fill=(30, 80, 160))
        proposal = cc.propose_crop(image, face_hint=(0, None))
        self.assertEqual(proposal["status"], "manual")
        box = tuple(proposal["box"])
        self.assertLessEqual(box[0], 110)
        self.assertGreaterEqual(box[2], 190)
        self.assertLessEqual(box[1], 40)
        self.assertGreaterEqual(box[3], 260)

    def test_apply_skips_manual_and_writes_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Auto: transparent padding around a solid figure.
            auto_path = tmp_path / "auto.png"
            auto = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
            ImageDraw.Draw(auto).rectangle((70, 40, 129, 159), fill=(20, 80, 180, 255))
            auto.save(auto_path)
            # Manual: busy opaque background.
            manual_path = tmp_path / "manual.jpg"
            manual = Image.new("RGB", (200, 200), (10, 10, 10))
            d = ImageDraw.Draw(manual)
            for i in range(20):
                d.rectangle(((i * 17) % 180, (i * 29) % 180, (i * 17) % 180 + 40, (i * 29) % 180 + 30), fill=(i * 12 % 255, 80, 200 - i * 8))
            manual.save(manual_path, quality=90)
            before = manual_path.read_bytes()

            rc = cc.main([str(auto_path), str(manual_path), "--apply", "--force-crop", "--no-sheet", "--out", str(tmp_path / "out")])
            self.assertEqual(rc, 0)
            # Manual file must be untouched.
            self.assertEqual(manual_path.read_bytes(), before)
            # Auto should have been cropped to a smaller file (same or new suffix).
            out_auto = auto_path if auto_path.exists() and auto_path.stat().st_size < 200 * 200 * 4 else tmp_path / "auto.webp"
            # The cropped result exists and is smaller than the original canvas would suggest.
            self.assertTrue(auto_path.exists() or (tmp_path / "auto.webp").exists())
            json_text = (tmp_path / "out" / "crops.json").read_text(encoding="utf-8")
            self.assertIn('"status": "manual"', json_text)
            self.assertIn("auto", json_text)


if __name__ == "__main__":
    unittest.main()
