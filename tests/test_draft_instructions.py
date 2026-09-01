# -*- coding: utf-8 -*-
"""v1.9.0 草稿指令块测试：正文最前的 ```smartvault 围栏块作为作者归档要求注入 LLM。

锁定三类行为：
  1. parse_draft_instructions 只认「正文开头」的指令块，中段同名块不动、无块零副作用；
  2. parse_rewrite_directive「整理正文：是/否」是唯一确定性覆盖项（正文改写安全开关）；
  3. 指令注入 Prompt 高优先级段落，SYSTEM_PROMPT 含优先级规则（防后续误删回归）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_daemon as sv  # noqa: E402


BLOCK = "```smartvault\n归入「项目管理/会议纪要」\n文件名：2026项目启动会\n```\n"


class TestParseDraftInstructions(unittest.TestCase):

    def test_no_block_returns_raw_unchanged(self):
        raw = "# 标题\n\n正文内容。"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertEqual(ins, "")
        self.assertEqual(body, raw)

    def test_block_at_top_extracted_and_stripped(self):
        raw = BLOCK + "\n# 标题\n\n正文内容。"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertIn("归入「项目管理/会议纪要」", ins)
        self.assertIn("文件名：2026项目启动会", ins)
        self.assertEqual(body, "# 标题\n\n正文内容。")     # 指令块与其后空行不进正文
        self.assertNotIn("smartvault", body)

    def test_block_after_frontmatter(self):
        raw = "---\ntitle: t\n---\n\n" + BLOCK + "\n# 标题\n"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertTrue(ins)
        self.assertTrue(body.startswith("---\ntitle: t\n---\n"))
        self.assertIn("# 标题", body)

    def test_block_in_middle_not_extracted(self):
        """正文中段/标题之后的同名围栏块是普通内容，不提取、逐字不动。"""
        raw = "# 标题\n\n" + BLOCK + "\n正文。"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertEqual(ins, "")
        self.assertEqual(body, raw)

    def test_case_insensitive_fence_word(self):
        raw = "```SmartVault\n要求A\n```\n正文。"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertEqual(ins, "要求A")
        self.assertEqual(body, "正文。")

    def test_empty_block_not_treated_as_instructions(self):
        """空指令块（直接闭合）不视为指令，草稿原样保留。"""
        raw = "```smartvault\n```\n\n正文。"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertEqual(ins, "")
        self.assertEqual(body, raw)

    def test_leading_blank_lines_before_block_allowed(self):
        raw = "\n\n" + BLOCK + "正文。"
        ins, body = sv.parse_draft_instructions(raw)
        self.assertTrue(ins)
        self.assertEqual(body, "正文。")


class TestRewriteDirective(unittest.TestCase):

    def test_absent_returns_none(self):
        self.assertIsNone(sv.parse_rewrite_directive("归入某目录\n标签：测试"))
        self.assertIsNone(sv.parse_rewrite_directive(""))

    def test_yes_variants(self):
        for line in ("整理正文：是", "整理正文: yes", "润色正文 = true",
                     "content_rewrite：开", "重写正文: ON"):
            self.assertTrue(sv.parse_rewrite_directive(line), line)

    def test_no_variants(self):
        for line in ("整理正文：否", "content_rewrite: false", "重写正文=off", "整理正文: No"):
            self.assertFalse(sv.parse_rewrite_directive(line), line)


class TestPromptInjection(unittest.TestCase):

    def test_user_prompt_contains_instructions_section(self):
        prompt = sv.build_user_prompt("库", "草稿.md", "正文", [], "", "", 30000,
                                      instructions="归入「测试目录」")
        self.assertIn("作者对本篇草稿的特别要求", prompt)
        self.assertIn("归入「测试目录」", prompt)
        self.assertIn("草稿原文", prompt)

    def test_user_prompt_without_instructions(self):
        prompt = sv.build_user_prompt("库", "草稿.md", "正文", [], "", "", 30000)
        self.assertNotIn("作者对本篇草稿的特别要求", prompt)

    def test_instructions_position_before_draft_body(self):
        """指令段须出现在草稿原文之前（显眼位置，避免被后文稀释）。"""
        prompt = sv.build_user_prompt("库", "草稿.md", "正文", [], "", "", 30000,
                                      instructions="归入「测试目录」")
        self.assertLess(prompt.index("作者对本篇草稿的特别要求"),
                        prompt.index("【草稿原文"))

    def test_system_prompt_locks_author_requirement_rule(self):
        """SYSTEM_PROMPT 须含作者特别要求优先级规则（防后续误删回归）。"""
        self.assertIn("作者对本篇草稿的特别要求", sv.SYSTEM_PROMPT)
        self.assertIn("优先满足", sv.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
