#!/usr/bin/env python3
"""Unit tests for tools/check.py. Requires Python 3.9+."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check as chk  # noqa: E402

JPEG_HEADER = b"\xff\xd8\xff\xd9"
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
WEBP_HEADER = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8


def write_image(path: Path, header: bytes, size: int, complete: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray((header + b"\x00" * max(0, size - len(header)))[:size])
    if complete:
        if header.startswith(b"\x89PNG"):
            body = b"IDAT" + b"\x00" * (size - 8 - 12 - 12)
            chunk = (len(body) - 4).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")
            data = bytearray(b"\x89PNG\r\n\x1a\n" + chunk + chk.PNG_IEND)
        elif header.startswith(b"RIFF"):
            data[4:8] = (size - 8).to_bytes(4, "little")
        elif header.startswith(b"\xff\xd8"):
            data[-2:] = b"\xff\xd9"
    path.write_bytes(bytes(data))


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

    def test_fullwidth_version_brackets_in_link(self):
        items = chk.parse_checklist("- [x] [三月七（巡猎）](三月七（巡猎）.jpg)\n")
        self.assertEqual(items[0]["name"], "三月七（巡猎）")
        self.assertEqual(items[0]["image_path"], "三月七（巡猎）.jpg")

    def test_angle_bracket_link_path(self):
        items = chk.parse_checklist("- [x] [三月七（巡猎）](<三月七（巡猎）.jpg>)\n")
        self.assertEqual(items[0]["name"], "三月七（巡猎）")
        self.assertEqual(items[0]["image_path"], "三月七（巡猎）.jpg")

    def test_angle_bracket_link_with_spaces(self):
        items = chk.parse_checklist("- [x] [角色 名](<角色 名.jpg>)\n")
        self.assertEqual(items[0]["image_path"], "角色 名.jpg")

    def test_work_alias_line_is_not_a_checklist_item(self):
        items = chk.parse_checklist("作品别名：星铁、崩坏星穹铁道\n\n- [ ] 姬子\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "姬子")
        self.assertEqual(chk.parse_work_aliases("作品别名：星铁、崩坏星穹铁道\n"), ["星铁", "崩坏星穹铁道"])
        self.assertIsNone(chk.parse_work_aliases("- [ ] 姬子\n"))


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

    def test_truncated_images_fail(self):
        write_image(self.work / "a.png", PNG_HEADER, chk.SIZE_MIN, complete=False)
        webp = bytearray(WEBP_HEADER + b"\x00" * (chk.SIZE_MIN - len(WEBP_HEADER)))
        webp[4:8] = (chk.SIZE_MIN + 1000).to_bytes(4, "little")
        (self.work / "b.webp").write_bytes(bytes(webp))
        write_image(self.work / "c.jpg", JPEG_HEADER[:3], chk.SIZE_MIN, complete=False)
        write_checklist(self.work, "- [x] [甲](a.png)\n- [x] [乙](b.webp)\n- [x] [丙](c.jpg)\n")
        errors, _warnings, _count = chk.check_work(self.work)
        self.assertEqual(sum("不完整" in error for error in errors), 3)

    def test_complete_png_and_webp_pass(self):
        write_image(self.work / "a.png", PNG_HEADER, chk.SIZE_MIN)
        write_image(self.work / "b.webp", WEBP_HEADER, chk.SIZE_MIN)
        write_checklist(self.work, "- [x] [甲](a.png)\n- [x] [乙](b.webp)\n")
        errors, _warnings, _count = chk.check_work(self.work)
        self.assertEqual(errors, [])

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
    def test_genshin_work_folder_passes(self):
        work = ROOT / "images" / "原神"
        if not (work / "00-清单.md").is_file():
            self.skipTest("原神作品目录不在当前工作区")
        errors, _warnings, count = chk.check_work(work)
        self.assertEqual(errors, [])
        self.assertGreater(count, 0)

    def test_all_repo_work_folders_pass(self):
        works = chk.find_work_dirs(ROOT)
        if not works:
            self.skipTest("当前工作区没有作品目录")
        for work in works:
            errors, _warnings, _count = chk.check_work(work)
            self.assertEqual(errors, [], work.name)


class CliTests(unittest.TestCase):
    def test_missing_directory_returns_2(self):
        self.assertEqual(chk.main(["/tmp/does-not-exist-check-work"]), 2)




class AliasParseTests(unittest.TestCase):
    def test_fullwidth_colon_and_enumeration_comma(self):
        items = chk.parse_checklist("- [x] [雷电将军](雷电将军.jpg) 别名：雷电影、影\n")
        self.assertEqual(items[0]["name"], "雷电将军")
        self.assertEqual(items[0]["aliases"], ["雷电影", "影"])

    def test_halfwidth_colon_and_other_separators(self):
        items = chk.parse_checklist("- [x] [钟离](钟离.jpg) 别名:岩王爷,摩拉克斯，往生者\n")
        self.assertEqual(items[0]["aliases"], ["岩王爷", "摩拉克斯", "往生者"])

    def test_slash_separators(self):
        items = chk.parse_checklist("- [x] [温迪](温迪.jpg) 别名：巴巴托斯/风神／诗人\n")
        self.assertEqual(items[0]["aliases"], ["巴巴托斯", "风神", "诗人"])

    def test_plain_item_can_have_aliases(self):
        items = chk.parse_checklist("- [ ] 待收集角色 别名：别称\n")
        self.assertEqual(items[0]["name"], "待收集角色")
        self.assertEqual(items[0]["aliases"], ["别称"])
        self.assertIsNone(items[0]["image_path"])


class MatchNameTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {"name": "温迪", "aliases": [], "image_path": "温迪.jpg"},
            {"name": "雷电将军", "aliases": ["雷电影", "影"], "image_path": "雷电将军.jpg"},
            {"name": "纳西妲", "aliases": [], "image_path": "纳西妲.jpg"},
            {"name": "芙宁娜", "aliases": [], "image_path": "芙宁娜.webp"},
            {"name": "芙卡洛斯", "aliases": [], "image_path": "芙卡洛斯.webp"},
        ]
        self.work = "原神"

    def test_normalize_work_wrappers_and_separators(self):
        expected = chk.normalize_name("温迪", "原神")
        for query in (
            "温迪",
            "  温迪  ",
            "原神·温迪",
            "原神-温迪",
            "温迪（原神）",
            "温迪(原神)",
            "【原神】温迪",
            "原神/温迪",
        ):
            self.assertEqual(chk.normalize_name(query, "原神"), expected, query)

    def test_fullwidth_parentheses_and_spaces(self):
        self.assertEqual(
            chk.normalize_name("温　迪（原神）", "原神"),
            chk.normalize_name("温迪", "原神"),
        )

    def test_match_link_text_and_filename_variants(self):
        for query in ("温迪", "原神·温迪", "温迪（原神）", "【原神】温迪"):
            entry, status = chk.match_name(query, self.entries, "原神")
            self.assertEqual(status, "match", query)
            self.assertEqual(entry["name"], "温迪")

    def test_match_aliases(self):
        for query in ("雷电影", "影", "原神·雷电影"):
            entry, status = chk.match_name(query, self.entries, "原神")
            self.assertEqual(status, "match", query)
            self.assertEqual(entry["name"], "雷电将军")

    def test_unique_contains_match(self):
        entry, status = chk.match_name("雷电", self.entries, "原神")
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "雷电将军")

    def test_ambiguous_contains_match(self):
        entry, status = chk.match_name("芙", self.entries, "原神")
        self.assertIsNone(entry)
        self.assertEqual(status, "ambiguous")

    def test_no_match(self):
        entry, status = chk.match_name("散兵", self.entries, "原神")
        self.assertIsNone(entry)
        self.assertEqual(status, "none")

    def test_ambiguous_exact_alias_collision(self):
        entries = [
            {"name": "角色甲", "aliases": ["同名"], "image_path": "角色甲.jpg"},
            {"name": "角色乙", "aliases": ["同名"], "image_path": "角色乙.jpg"},
        ]
        entry, status = chk.match_name("同名", entries, "原神")
        self.assertIsNone(entry)
        self.assertEqual(status, "ambiguous")


class AliasCollisionTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work = Path(self._temp.name) / "原神"
        self.work.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_alias_collision_is_warning(self):
        write_image(self.work / "角色甲.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_image(self.work / "角色乙.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_checklist(
            self.work,
            "- [x] [角色甲](角色甲.jpg) 别名：同名\n- [x] [角色乙](角色乙.jpg) 别名：同名\n",
        )
        errors, warnings, count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(count, 2)
        self.assertTrue(any("冲突" in warning for warning in warnings))


class VersionAndWorkAliasTests(unittest.TestCase):
    def setUp(self):
        self.work = "星穹铁道"
        self.entries = [
            {"name": "姬子", "aliases": [], "image_path": "姬子.jpg"},
            {"name": "三月七", "aliases": [], "image_path": "三月七.jpg"},
            {"name": "三月七（巡猎）", "aliases": [], "image_path": "三月七（巡猎）.jpg"},
        ]

    def _match(self, query, entries=None, work=None, work_aliases=None):
        return chk.match_name(
            query,
            entries if entries is not None else self.entries,
            work if work is not None else self.work,
            work_aliases,
        )

    def test_version_brackets_are_not_stripped(self):
        self.assertEqual(chk.normalize_name("三月七", self.work), "三月七")
        self.assertEqual(chk.normalize_name("三月七（巡猎）", self.work), "三月七巡猎")
        self.assertNotEqual(
            chk.normalize_name("三月七（巡猎）", self.work),
            chk.normalize_name("三月七", self.work),
        )

    def test_work_brackets_are_stripped_but_version_kept(self):
        self.assertEqual(
            chk.normalize_name("姬子（星穹铁道）", self.work),
            chk.normalize_name("姬子", self.work),
        )
        self.assertEqual(
            chk.normalize_name("三月七（巡猎）（星穹铁道）", self.work),
            chk.normalize_name("三月七（巡猎）", self.work),
        )

    def test_default_work_aliases_as_prefix_or_suffix(self):
        expected = chk.normalize_name("姬子", self.work)
        for query in (
            "姬子",
            "姬子（星穹铁道）",
            "姬子(星穹铁道)",
            "星穹铁道·姬子",
            "崩坏星穹铁道·姬子",
            "崩坏：星穹铁道·姬子",
            "【星铁】姬子",
            "星铁-姬子",
            "星铁/姬子",
        ):
            self.assertEqual(chk.normalize_name(query, self.work), expected, query)
            entry, status = self._match(query)
            self.assertEqual(status, "match", query)
            self.assertEqual(entry["name"], "姬子", query)

    def test_query_base_prefers_exact_over_variant(self):
        entry, status = self._match("三月七")
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "三月七")

    def test_query_fullwidth_variant(self):
        entry, status = self._match("三月七（巡猎）")
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "三月七（巡猎）")

    def test_query_halfwidth_variant(self):
        entry, status = self._match("三月七(巡猎)")
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "三月七（巡猎）")

    def test_query_dot_variant(self):
        entry, status = self._match("三月七·巡猎")
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "三月七（巡猎）")

    def test_query_reordered_variant(self):
        entry, status = self._match("巡猎三月七")
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "三月七（巡猎）")

    def test_only_variant_contains_match_if_unique(self):
        entries = [self.entries[2]]
        entry, status = self._match("三月七", entries)
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "三月七（巡猎）")

    def test_base_name_ambiguous_when_two_variants_only(self):
        entries = [
            {"name": "三月七（巡猎）", "aliases": [], "image_path": "三月七（巡猎）.jpg"},
            {"name": "三月七（同谐）", "aliases": [], "image_path": "三月七（同谐）.jpg"},
        ]
        entry, status = self._match("三月七", entries)
        self.assertIsNone(entry)
        self.assertEqual(status, "ambiguous")

    def test_honkai3_aliases(self):
        self.assertEqual(
            chk.normalize_name("芽衣（崩坏三）", "崩坏3"),
            chk.normalize_name("芽衣", "崩坏3"),
        )
        self.assertEqual(
            chk.normalize_name("崩3·芽衣", "崩坏3"),
            chk.normalize_name("芽衣", "崩坏3"),
        )

    def test_checklist_work_aliases_win_over_defaults(self):
        # Without 星铁 in the checklist line, that wrapper stays.
        aliases = ["自定义简称"]
        self.assertEqual(
            chk.normalize_name("【自定义简称】姬子", "星穹铁道", aliases),
            chk.normalize_name("姬子", "星穹铁道", aliases),
        )
        self.assertNotEqual(
            chk.normalize_name("【星铁】姬子", "星穹铁道", aliases),
            chk.normalize_name("姬子", "星穹铁道", aliases),
        )

    def test_folder_name_always_kept_as_work_alias(self):
        aliases = ["自定义简称"]
        self.assertEqual(
            chk.normalize_name("姬子（星穹铁道）", "星穹铁道", aliases),
            chk.normalize_name("姬子", "星穹铁道", aliases),
        )


class WorkAliasChecklistTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.work = Path(self._temp.name) / "星穹铁道"
        self.work.mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def test_checklist_work_alias_used_by_check_work_keys(self):
        write_image(self.work / "姬子.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_checklist(
            self.work,
            "作品别名：自定义简称\n\n- [x] [姬子](姬子.jpg)\n",
        )
        errors, warnings, count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(count, 1)
        items = chk.parse_checklist((self.work / "00-清单.md").read_text(encoding="utf-8"))
        aliases = chk.parse_work_aliases((self.work / "00-清单.md").read_text(encoding="utf-8"))
        self.assertEqual(aliases, ["自定义简称"])
        entry, status = chk.match_name("【自定义简称】姬子", items, "星穹铁道", aliases)
        self.assertEqual(status, "match")
        self.assertEqual(entry["name"], "姬子")

    def test_versioned_entries_are_not_alias_collisions(self):
        write_image(self.work / "三月七.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_image(self.work / "三月七（巡猎）.jpg", JPEG_HEADER, chk.SIZE_MIN)
        write_checklist(
            self.work,
            "- [x] [三月七](三月七.jpg)\n- [x] [三月七（巡猎）](三月七（巡猎）.jpg)\n",
        )
        errors, warnings, count = chk.check_work(self.work)
        self.assertEqual(errors, [])
        self.assertEqual(count, 2)
        self.assertEqual(warnings, [])




if __name__ == "__main__":
    unittest.main()
