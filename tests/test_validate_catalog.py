#!/usr/bin/env python3
"""Unit tests for tools/validate_catalog.py. Requires Python 3.9+."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import validate_catalog as vc  # noqa: E402


JPEG_HEADER = b"\xff\xd8\xff\xd9"
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
WEBP_HEADER = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8


def write_image(path: Path, header: bytes, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = header + b"\x00" * max(0, size - len(header))
    path.write_bytes(payload[:size])


def write_checklist(work_dir: Path, body: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "00-清单.md").write_text(body, encoding="utf-8")


class ParseChecklistTests(unittest.TestCase):
    def test_checked_link_and_note(self):
        items = vc.parse_checklist(
            "# 标题\n\n- [x] [温迪](温迪.jpg)\n- [ ] [芙宁娜](芙宁娜.webp)（略小于下限）\n"
        )
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["checked"])
        self.assertEqual(items[0]["character"], "温迪")
        self.assertEqual(items[0]["variant"], "默认立绘")
        self.assertEqual(items[0]["image_path"], "温迪.jpg")
        self.assertEqual(items[0]["status"], "ready")
        self.assertEqual(items[1]["status"], "pending")
        self.assertEqual(items[1]["note"], "略小于下限")

    def test_variant_in_link_text(self):
        items = vc.parse_checklist("- [x] [角色甲（演出服）](角色甲（演出服）.webp)\n")
        self.assertEqual(items[0]["character"], "角色甲")
        self.assertEqual(items[0]["variant"], "演出服")
        self.assertEqual(items[0]["image_path"], "角色甲（演出服）.webp")

    def test_failed_and_plain_pending(self):
        items = vc.parse_checklist(
            "- [ ] 待收集角色\n- [ ] 失踪角色（失败：未找到合适图片）\n"
        )
        self.assertEqual(items[0]["character"], "待收集角色")
        self.assertIsNone(items[0]["image_path"])
        self.assertEqual(items[0]["status"], "pending")
        self.assertEqual(items[1]["status"], "failed")
        self.assertEqual(items[1]["note"], "失败：未找到合适图片")

    def test_uppercase_checkbox_and_blank_lines(self):
        items = vc.parse_checklist("\n说明文字\n\n- [X] [钟离](钟离.jpg)\n\n")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["checked"])


class CatalogGenerationTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work = Path(self._temp.name) / "原神"
        self.work.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_checked_valid_image_is_ready(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, vc.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        catalog, errors, warnings = vc.generate_catalog(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(catalog["work"], "原神")
        self.assertEqual(catalog["schema_version"], "1.1.0")
        item = catalog["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["roster_name"], "温迪（原神）")
        self.assertEqual(item["image_path"], "温迪.jpg")
        self.assertEqual(item["mime_type"], "image/jpeg")
        self.assertEqual(item["size_bytes"], vc.SIZE_MIN)
        self.assertEqual(item["sha256"], vc.file_sha256(self.work / "温迪.jpg"))
        self.assertEqual(warnings, [])

    def test_checked_undersized_file_is_error_and_not_ready(self):
        write_image(self.work / "芙宁娜.webp", WEBP_HEADER, 487_808)
        write_checklist(self.work, "- [x] [芙宁娜](芙宁娜.webp)\n")
        catalog, errors, warnings = vc.generate_catalog(self.work)
        self.assertTrue(any("文件大小" in error for error in errors))
        self.assertEqual(catalog["items"][0]["status"], "pending")
        self.assertEqual(warnings, [])

    def test_unchecked_undersized_file_is_warning(self):
        write_image(self.work / "芙卡洛斯.webp", WEBP_HEADER, 120_234)
        write_checklist(
            self.work,
            "- [ ] [芙卡洛斯](芙卡洛斯.webp)（需要更大的图片，当前小于 500KB）\n",
        )
        catalog, errors, warnings = vc.generate_catalog(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(catalog["items"][0]["status"], "pending")
        self.assertTrue(any("120234 字节" in warning for warning in warnings))

    def test_missing_checked_file_is_error(self):
        write_checklist(self.work, "- [x] [钟离](钟离.jpg)\n")
        catalog, errors, _warnings = vc.generate_catalog(self.work)
        self.assertTrue(any("不存在" in error for error in errors))
        self.assertEqual(catalog["items"][0]["status"], "pending")

    def test_path_traversal_rejected(self):
        write_checklist(self.work, "- [x] [入侵](../secret.jpg)\n")
        _catalog, errors, _warnings = vc.generate_catalog(self.work)
        self.assertTrue(any("越出" in error or "不合法" in error for error in errors))

    def test_magic_bytes_must_match_extension(self):
        write_image(self.work / "温迪.jpg", WEBP_HEADER, vc.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        _catalog, errors, _warnings = vc.generate_catalog(self.work)
        self.assertTrue(any("文件头与扩展名不一致" in error for error in errors))

    def test_preserves_id_and_sources_on_update(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, vc.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        seed = {
            "schema_version": "1.1.0",
            "work": "原神",
            "items": [
                {
                    "id": "genshin-venti-default",
                    "character": "温迪",
                    "variant": "默认立绘",
                    "image_path": "温迪.jpg",
                    "source_page": "https://example.test/venti",
                    "source_image": "https://example.test/venti.jpg",
                }
            ],
        }
        (self.work / "catalog.json").write_text(vc.dump_catalog(seed), encoding="utf-8")
        catalog, errors, _warnings = vc.generate_catalog(self.work)
        self.assertEqual(errors, [])
        item = catalog["items"][0]
        self.assertEqual(item["id"], "genshin-venti-default")
        self.assertEqual(item["source_page"], "https://example.test/venti")
        self.assertEqual(item["source_image"], "https://example.test/venti.jpg")

    def test_check_mode_detects_drift_and_writes_nothing(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, vc.SIZE_MIN)
        write_checklist(self.work, "- [x] [温迪](温迪.jpg)\n")
        self.assertEqual(vc.process_work(self.work, check_only=False), 0)
        (self.work / "00-清单.md").write_text(
            "- [x] [温迪](温迪.jpg)\n- [ ] 待收集角色\n",
            encoding="utf-8",
        )
        before = (self.work / "catalog.json").read_text(encoding="utf-8")
        self.assertEqual(vc.process_work(self.work, check_only=True), 1)
        self.assertEqual((self.work / "catalog.json").read_text(encoding="utf-8"), before)

    def test_standalone_pack_uses_folder_name_as_work(self):
        pack = Path(self._temp.name) / "extracted" / "星穹铁道"
        write_image(pack / "角色甲.png", PNG_HEADER, vc.SIZE_MIN)
        write_checklist(pack, "- [x] [角色甲](角色甲.png)\n")
        catalog, errors, _warnings = vc.generate_catalog(pack)
        self.assertEqual(errors, [])
        self.assertEqual(catalog["work"], "星穹铁道")
        self.assertEqual(catalog["items"][0]["roster_name"], "角色甲（星穹铁道）")
        self.assertEqual(catalog["items"][0]["image_path"], "角色甲.png")
        self.assertEqual(catalog["items"][0]["mime_type"], "image/png")

    def test_find_all_work_folders(self):
        root = Path(self._temp.name) / "repo"
        first = root / "images" / "原神"
        second = root / "images" / "崩坏3"
        write_checklist(first, "- [ ] 角色甲\n")
        write_checklist(second, "- [ ] 角色乙\n")
        (root / "images" / "空目录").mkdir(parents=True)
        found = [path.name for path in vc.find_work_dirs(root)]
        self.assertEqual(found, ["原神", "崩坏3"])

    def test_orphan_image_and_duplicate_entries_are_reported(self):
        write_image(self.work / "温迪.jpg", JPEG_HEADER, vc.SIZE_MIN)
        write_image(self.work / "多余.jpg", JPEG_HEADER, vc.SIZE_MIN)
        write_checklist(
            self.work,
            "- [x] [温迪](温迪.jpg)\n- [x] [温迪](温迪.jpg)\n",
        )
        _catalog, errors, warnings = vc.generate_catalog(self.work)
        self.assertTrue(any("重复" in error for error in errors))
        self.assertTrue(any("未列入清单" in warning for warning in warnings))


class RepoSampleTests(unittest.TestCase):
    def test_genshin_folder_matches_generated_catalog(self):
        work = ROOT / "images" / "原神"
        if not (work / "00-清单.md").is_file() or not (work / "catalog.json").is_file():
            self.skipTest("原神作品目录或 catalog.json 不在当前工作区")
        catalog, errors, warnings = vc.generate_catalog(work)
        self.assertEqual(errors, [])
        self.assertEqual(vc.dump_catalog(catalog), (work / "catalog.json").read_text(encoding="utf-8"))
        statuses = {item["character"]: item["status"] for item in catalog["items"]}
        self.assertEqual(statuses.get("芙宁娜"), "pending")
        self.assertEqual(statuses.get("芙卡洛斯"), "pending")
        ready = [item["character"] for item in catalog["items"] if item["status"] == "ready"]
        self.assertEqual(
            ready,
            ["温迪", "钟离", "雷电将军", "纳西妲", "玛薇卡", "安娜丝塔夏"],
        )
        self.assertTrue(any("芙宁娜" in warning and "字节" in warning for warning in warnings))
        self.assertTrue(any("芙卡洛斯" in warning and "字节" in warning for warning in warnings))


class CliGuardTests(unittest.TestCase):
    def test_requires_work_dir_or_all(self):
        self.assertEqual(vc.main([]), 2)
        self.assertEqual(vc.main(["--all", "images/原神"]), 2)


if __name__ == "__main__":
    unittest.main()
