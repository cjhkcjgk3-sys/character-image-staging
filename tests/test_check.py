#!/usr/bin/env python3
"""Unit tests for tools/check.py. Requires Python 3.9+."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check as chk  # noqa: E402

JPEG_HEADER = b"\xff\xd8\xff\xd9"
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
WEBP_HEADER = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8


def write_image(path: Path, header: bytes, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((header + b"\x00" * max(0, size - len(header)))[:size])


def write_checklist(work_dir: Path, body: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "00-清单.md").write_text(body, encoding="utf-8")


class ParseTests(unittest.TestCase):
    def test_checked_link_and_trailing_note_ignored(self):
        items = chk.parse_checklist(
            "# 标题\n\n- [x] [温迪](温迪.jpg)\n- [ ] [芙宁娜](芙宁娜.webp)（需换 500KB 以上的图）\n"
        )
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["checked"])
        self.assertEqual(items[0]["name"], "温迪")
        self.assertEqual(items[0]["image_path"], "温迪.jpg")
        self.assertFalse(items[1]["checked"])
        self.assertEqual(items[1]["image_path"], "芙宁娜.webp")

    def test_url_encoded_link(self):
        encoded = quote("温迪.jpg", safe="")
        items = chk.parse_checklist(f"- [x] [温迪]({encoded})\n")
        self.assertEqual(items[0]["image_path"], "温迪.jpg")

    def test_unchecked_without_link(self):
        items = chk.parse_checklist("- [ ] 待收集角色\n")
        self.assertFalse(items[0]["checked"])
        self.assertIsNone(items[0]["image_path"])


class CheckWorkTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work = Path(self._temp.name) / "原神"
        self.work.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_checked_valid_image_passes(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        errors, warnings, count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(count, 1)

    def test_checked_missing_file_fails(self):
        write_checklist(self.work, "- [x] [钟离](钟离.jpg)\n")
        errors, _warnings, _count = chk.check_work(self.work)
        self.assertTrue(any("不存在" in error for error in errors))

    def test_checked_undersized_file_fails(self):
        write_image(self.work / "芙宁娜.webp", WEBP_HEADER, 487_808)
        write_checklist(self.work, "- [x] [芙宁娜](芙宁娜.webp)\n")
        errors, _warnings, _count = chk.check_work(self.work)
        self.assertTrue(any("文件大小" in error for error in errors))

    def test_unchecked_missing_file_is_fine(self):
        write_checklist(self.work, "- [ ] [待收集](待收集.jpg)\n- [ ] 另一角色\n")
        errors, warnings, count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(count, 2)

    def test_path_traversal_rejected(self):
        write_checklist(self.work, "- [x] [入侵](../secret.jpg)\n")
        errors, _warnings, _count = chk.check_work(self.work)
        self.assertTrue(any("越出" in error or "不合法" in error for error in errors))

    def test_magic_bytes_must_match_extension(self):
        write_image(self.work / "温迪.jpg", WEBP_HEADER, chk.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        errors, _warnings, _count = chk.check_work(self.work)
        self.assertTrue(any("文件头与扩展名不一致" in error for error in errors))

    def test_url_encoded_link_resolves(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, chk.SIZE_MIN)
        encoded = quote("温迪.jpg", safe="")
        write_checklist(self.work, f"- [x] [温迪]({encoded})\n")
        errors, warnings, _count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_orphan_image_is_warning(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_image(self.work / "多余.png", PNG_HEADER, chk.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        errors, warnings, _count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertTrue(any("未列入清单" in warning for warning in warnings))

    def test_png_and_standalone_folder(self):
        pack = Path(self._temp.name) / "extracted" / "星穹铁道"
        write_image(pack / "角色甲.png", PNG_HEADER, chk.SIZE_MIN)
        write_checklist(pack, "- [x] [角色甲](角色甲.png)\n")
        errors, warnings, _count = chk.check_work(pack)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_find_work_folders(self):
        root = Path(self._temp.name) / "repo"
        write_checklist(root / "images" / "原神", "- [ ] 角色甲\n")
        write_checklist(root / "images" / "崩坏3", "- [ ] 角色乙\n")
        (root / "images" / "空目录").mkdir(parents=True)
        found = [path.name for path in chk.find_work_dirs(root)]
        self.assertEqual(found, ["原神", "崩坏3"])


class RepoSampleTests(unittest.TestCase):
    def test_genshin_checklist_passes_with_two_unchecked(self):
        work = ROOT / "images" / "原神"
        if not (work / "00-清单.md").is_file():
            self.skipTest("原神作品目录不在当前工作区")
        errors, _warnings, count = chk.check_work(work)
        self.assertEqual(errors, [])
        self.assertEqual(count, 8)
        items = chk.parse_checklist((work / "00-清单.md").read_text(encoding="utf-8"))
        checked = [item["name"] for item in items if item["checked"]]
        unchecked = [item["name"] for item in items if not item["checked"]]
        self.assertEqual(checked, ["温迪", "钟离", "雷电将军", "纳西妲", "玛薇卡", "安娜丝塔夏"])
        self.assertEqual(unchecked, ["芙宁娜", "芙卡洛斯"])


class CliTests(unittest.TestCase):
    def test_missing_directory_returns_2(self):
        self.assertEqual(chk.main(["/tmp/does-not-exist-check-work"]), 2)


if __name__ == "__main__":
    unittest.main()
