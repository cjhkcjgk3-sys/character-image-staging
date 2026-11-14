#!/usr/bin/env python3
"""Unit tests for tools/publish_work_zips.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import publish_work_zips as pub  # noqa: E402


def write_image(path: Path, size: int = 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal complete JPEG so the file is a real image suffix with content.
    data = b"\xff\xd8\xff\xd9" + b"\x00" * max(0, size - 4)
    path.write_bytes(data[:size] if size >= 4 else b"\xff\xd8\xff\xd9")


def write_checklist(work_dir: Path, body: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "00-清单.md").write_text(body, encoding="utf-8")


def setup_images_root(root: Path) -> Path:
    images = root / "images"
    yuanshen = images / "原神"
    write_checklist(
        yuanshen,
        "# 原神角色图片清单\n\n作品别名：原神、Genshin Impact\n\n- [x] [荧](荧.png)\n- [ ] 待收集\n",
    )
    write_image(yuanshen / "荧.png")
    write_image(yuanshen / "安柏.jpg")
    # Hidden / junk files must not enter the ZIP.
    (yuanshen / ".gitkeep").write_text("", encoding="utf-8")
    (yuanshen / "notes.txt").write_text("ignore me", encoding="utf-8")

    empty = images / "鸣潮"
    write_checklist(empty, "# 鸣潮角色图片清单\n\n- [ ] 今汐\n")

    arknights = images / "明日方舟"
    write_checklist(arknights, "# 明日方舟角色图片清单\n\n- [x] [能天使](能天使.webp)\n")
    write_image(arknights / "能天使.webp")
    return images


class ListWorksTests(unittest.TestCase):
    def test_skips_folders_that_only_have_a_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = setup_images_root(Path(tmp))
            names = [path.name for path in pub.list_works(images)]
            self.assertEqual(names, ["原神", "明日方舟"])


class BuildZipTests(unittest.TestCase):
    def test_zip_layout_utf8_flag_and_deflate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = setup_images_root(root)
            dest = root / "out" / "原神.zip"
            summary = pub.build_zip(images / "原神", dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(summary["work"], "原神")
            self.assertEqual(summary["images"], 2)
            self.assertGreater(summary["bytes"], 0)

            with zipfile.ZipFile(dest) as zf:
                names = zf.namelist()
                self.assertEqual(
                    names,
                    ["原神/00-清单.md", "原神/安柏.jpg", "原神/荧.png"],
                )
                self.assertNotIn("原神/.gitkeep", names)
                self.assertNotIn("原神/notes.txt", names)
                for info in zf.infolist():
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED, info.filename)
                    self.assertTrue(info.flag_bits & 0x800, f"UTF-8 flag missing on {info.filename}")
                text = zf.read("原神/00-清单.md").decode("utf-8")
                self.assertIn("[荧](荧.png)", text)
                self.assertTrue(zf.read("原神/荧.png").startswith(b"\xff\xd8"))

    def test_missing_checklist_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "原神"
            write_image(work / "荧.png")
            with self.assertRaises(FileNotFoundError):
                pub.build_zip(work, Path(tmp) / "原神.zip")

    def test_chinese_names_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = setup_images_root(Path(tmp))
            dest = Path(tmp) / "原神.zip"
            pub.build_zip(images / "原神", dest)
            extract_to = Path(tmp) / "extracted"
            with zipfile.ZipFile(dest) as zf:
                zf.extractall(extract_to)
            self.assertTrue((extract_to / "原神" / "00-清单.md").is_file())
            self.assertTrue((extract_to / "原神" / "荧.png").is_file())
            self.assertTrue((extract_to / "原神" / "安柏.jpg").is_file())


class AssetNameTests(unittest.TestCase):
    def test_default_keeps_chinese(self):
        self.assertEqual(pub.asset_name("原神"), "原神.zip")
        self.assertEqual(pub.asset_name("FGO"), "FGO.zip")

    def test_ascii_fallback_uses_known_slugs(self):
        self.assertEqual(pub.asset_name("原神", ascii_names=True), "genshin.zip")
        self.assertEqual(pub.asset_name("碧蓝航线", ascii_names=True), "azur-lane.zip")
        self.assertEqual(pub.asset_name("FGO", ascii_names=True), "fgo.zip")


class BuildAllAndNotesTests(unittest.TestCase):
    def test_build_all_and_release_notes_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = setup_images_root(root)
            out = root / "out"
            summaries = pub.build_all(images, out, ascii_names=False)
            self.assertEqual([item["work"] for item in summaries], ["原神", "明日方舟"])
            self.assertTrue((out / "原神.zip").is_file())
            self.assertTrue((out / "明日方舟.zip").is_file())
            notes = out / "RELEASE_NOTES.md"
            pub.write_release_notes(
                summaries, notes, "cjhkcjgk3-sys/character-image-staging", ascii_names=False
            )
            text = notes.read_text(encoding="utf-8")
            self.assertIn("| 原神 |", text)
            self.assertIn("| 明日方舟 |", text)
            self.assertIn("releases/download/downloads/", text)
            self.assertIn("%E5%8E%9F%E7%A5%9E.zip", text)
            self.assertIn("新加坡时间", text)
            self.assertNotIn("鸣潮", text)

    def test_ascii_notes_show_chinese_work_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = setup_images_root(root)
            out = root / "out"
            summaries = pub.build_all(images, out, ascii_names=True)
            self.assertEqual(summaries[0]["name"], "genshin.zip")
            notes = out / "notes.md"
            pub.write_release_notes(summaries, notes, "owner/repo", ascii_names=True)
            text = notes.read_text(encoding="utf-8")
            self.assertIn("genshin.zip", text)
            self.assertIn("原神（genshin.zip）", text)


if __name__ == "__main__":
    unittest.main()
