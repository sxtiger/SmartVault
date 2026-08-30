#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3.1 归档保真性回归测试。

背景事故：19941 字符长草稿经 LLM「整理」后仅剩 2702 字节（94% 内容丢失），
且出现「准确率提升 17%」「接口覆盖率 75%」等原文不存在的编造数字。
本组测试锁定三类防线：
  1. 长文保守模式——build_user_prompt 注入「optimized_content 必须为空」指令；
  2. 空 optimized_content 是合法返回（长文模式），parse_llm_json 不再填占位符；
  3. choose_target_dir 剥离 LLM 误带的仓库名前缀；backup_draft 归档前备份草稿。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest_daemon as sv  # noqa: E402


class TestParseLlmJsonEmptyContent(unittest.TestCase):
    def test_empty_optimized_content_returns_empty_string(self):
        """长文保守模式下 LLM 返回空正文是合法值，不得替换为占位符。"""
        raw = '{"target_folder":"未分类","new_filename":"n","summary":"s",'
        raw += '"tags":["a","b","c"],"optimized_content":""}'
        meta = sv.parse_llm_json(raw)
        self.assertEqual(meta["optimized_content"], "")

    def test_missing_optimized_content_returns_empty_string(self):
        raw = '{"target_folder":"未分类","new_filename":"n","summary":"s","tags":["a"]}'
        meta = sv.parse_llm_json(raw)
        self.assertEqual(meta["optimized_content"], "")

    def test_normal_content_preserved_verbatim(self):
        body = "## 标题\n\n正文含数字 17% 与 6000 万。"
        obj = {"target_folder": "未分类", "new_filename": "n", "summary": "s",
               "tags": ["a", "b", "c"], "optimized_content": body}
        meta = sv.parse_llm_json(json.dumps(obj, ensure_ascii=False))
        self.assertEqual(meta["optimized_content"], body)


class TestBuildUserPromptConservativeMode(unittest.TestCase):
    def test_long_draft_injects_keep_original_directive(self):
        prompt = sv.build_user_prompt("智能笔记", "草稿.md", "x" * 100, [], "", "",
                                      30000, keep_original_content=True)
        self.assertIn("长文保守模式", prompt)
        self.assertIn("optimized_content", prompt)
        self.assertIn("空字符串", prompt)

    def test_short_draft_has_no_keep_original_directive(self):
        prompt = sv.build_user_prompt("智能笔记", "草稿.md", "短文", [], "", "",
                                      30000, keep_original_content=False)
        self.assertNotIn("长文保守模式", prompt)

    def test_length_reported_in_directive(self):
        text = "字" * 10000
        prompt = sv.build_user_prompt("智能笔记", "草稿.md", text, [], "", "",
                                      30000, keep_original_content=True)
        self.assertIn("10000 字符", prompt)


class TestChooseTargetDirVaultPrefix(unittest.TestCase):
    def _vault(self, root: Path) -> sv.Vault:
        return sv.Vault(name="智能笔记", root=root, inbox=root / "待处理笔记",
                        context_file=root / "ai_context.md")

    def _proc(self) -> dict:
        return {"allow_new_folder": True, "max_folder_depth": 2, "fallback_folder": "未分类"}

    def test_leading_vault_name_stripped(self):
        """LLM 输出“智能笔记/BMO/Profiles”（带仓库名，三级）应剥前缀后命中已有二级目录。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "BMO" / "Profiles").mkdir(parents=True)
            got = sv.choose_target_dir(self._vault(root), "智能笔记/BMO/Profiles",
                                       self._proc(), "待处理笔记")
            self.assertEqual(got, root / "BMO" / "Profiles")

    def test_plain_two_level_path_unaffected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "就业指导").mkdir()
            got = sv.choose_target_dir(self._vault(root), "就业指导",
                                       self._proc(), "待处理笔记")
            self.assertEqual(got, root / "就业指导")

    def test_overlong_path_still_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            got = sv.choose_target_dir(self._vault(root), "A/B/C/D",
                                       self._proc(), "待处理笔记")
            self.assertEqual(got, root / "未分类")


class TestBackupDraft(unittest.TestCase):
    def test_backup_created_and_content_intact(self):
        with tempfile.TemporaryDirectory() as d:
            root, inbox = Path(d), Path(d) / "待处理笔记"
            inbox.mkdir()
            md = inbox / "草稿.md"
            md.write_text("重要原文，不可丢失", encoding="utf-8")
            dest = sv.backup_draft(root, md)
            self.assertIsNotNone(dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(encoding="utf-8"), "重要原文，不可丢失")
            self.assertTrue(dest.parent == root / ".smartvault" / "backup")

    def test_backup_rotation_keeps_recent(self):
        with tempfile.TemporaryDirectory() as d:
            root, inbox = Path(d), Path(d) / "待处理笔记"
            inbox.mkdir()
            bdir = root / ".smartvault" / "backup"
            bdir.mkdir(parents=True)
            for i in range(3):
                (bdir / f"20260101_00000{i}_old.md").write_text(str(i), encoding="utf-8")
            md = inbox / "草稿.md"
            md.write_text("新草稿", encoding="utf-8")
            sv.backup_draft(root, md, keep=3)
            files = sorted(p.name for p in bdir.iterdir())
            self.assertEqual(len(files), 3)
            self.assertTrue(files[-1].endswith("草稿.md"))
            self.assertTrue(files[0].endswith("_old.md"))


if __name__ == "__main__":
    unittest.main()
