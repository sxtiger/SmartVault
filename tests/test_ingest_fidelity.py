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
        self.assertIn("原文保留模式", prompt)
        self.assertIn("optimized_content", prompt)
        self.assertIn("空字符串", prompt)

    def test_short_draft_has_no_keep_original_directive(self):
        prompt = sv.build_user_prompt("智能笔记", "草稿.md", "短文", [], "", "",
                                      30000, keep_original_content=False)
        self.assertNotIn("原文保留模式", prompt)

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


class TestBuildPreservedContent(unittest.TestCase):
    """v1.4.0 原文保留模式的正文组装：原文逐字不动，附件转录折叠附加。"""

    def test_no_attachments_returns_raw_verbatim(self):
        raw = "# 原文\n\n正文一段，含数字 9527。\n"
        self.assertEqual(sv.build_preserved_content(raw, []), raw)

    def test_attachments_appended_as_folded_quote_blocks(self):
        raw = "正文内容"
        blocks = [
            "◆ 附件「a.png」｜图像 OCR\n第一行\n第二行",
            "◆ 附件「b.mp3」｜语音转录\n",
        ]
        out = sv.build_preserved_content(raw, blocks)
        self.assertTrue(out.startswith("正文内容"))                    # 原文未被改动
        self.assertIn("## 附：附件转录", out)                          # 明确区隔标注
        self.assertIn("以原附件为准", out)
        self.assertIn("> [!quote]- ◆ 附件「a.png」｜图像 OCR", out)   # 折叠引用块
        self.assertIn("> 第一行\n> 第二行", out)                       # 逐行引用
        self.assertIn("（空转录）", out)                               # 空转录占位

    def test_missing_attachment_block_still_appended(self):
        out = sv.build_preserved_content("x",
                                         ["◆ 附件「c.pdf」｜未找到\n（该附件未出现在收件箱中）"])
        self.assertIn("未找到", out)
        self.assertIn("> （该附件未出现在收件箱中）", out)


class TestPruneEmptyDirs(unittest.TestCase):
    """v1.6.0 收件箱空目录自动清理：归档移走附件后残留的空 附件/ 等目录应被删除。"""

    def _mk(self, d: str):
        inbox = Path(d)
        (inbox / "附件").mkdir()
        (inbox / "附件" / ".DS_Store").write_text("", encoding="utf-8")
        (inbox / "子" / "深层").mkdir(parents=True)          # 空的多层目录
        (inbox / "keep").mkdir()
        (inbox / "keep" / "草稿.md").write_text("x", encoding="utf-8")  # 占用目录不删
        return inbox

    def test_removes_empty_and_dsstore_dirs_keeps_occupied(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = self._mk(d)
            removed = sv.prune_empty_dirs(inbox)
            self.assertFalse((inbox / "附件").exists())       # 只含 .DS_Store → 删
            self.assertFalse((inbox / "子").exists())         # 空子目录 → 级联删
            self.assertTrue((inbox / "keep").exists())        # 含草稿 → 保留
            self.assertTrue(inbox.exists())                   # 收件箱根 → 永不删
            self.assertEqual(removed, 3)

    def test_hidden_file_blocks_removal(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d)
            (inbox / "配置").mkdir()
            (inbox / "配置" / ".gitkeep").write_text("", encoding="utf-8")
            self.assertEqual(sv.prune_empty_dirs(inbox), 0)
            self.assertTrue((inbox / "配置").exists())        # 非 .DS_Store 隐藏文件 → 保留

    def test_no_dirs_noop(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d)
            (inbox / "a.md").write_text("x", encoding="utf-8")
            self.assertEqual(sv.prune_empty_dirs(inbox), 0)
            self.assertTrue((inbox / "a.md").exists())


class TestAppendAiContext(unittest.TestCase):
    """v1.6.1 重归档自愈：同文件名旧条目应被移除，避免历史锚定与重复堆积。"""

    def _vault(self, d: str) -> sv.Vault:
        root = Path(d) / "库"
        (root / "网络工具").mkdir(parents=True)
        (root / "网络工具" / "claude_pro.md").write_text("x", encoding="utf-8")
        return sv.Vault(name="库", root=root, inbox=root / "待处理笔记",
                        context_file=root / "ai_context.md")

    @staticmethod
    def _meta(folder: str) -> dict:
        return {"target_folder": folder, "summary": "摘要", "tags": ["A", "B", "C"]}

    def test_rearchive_replaces_stale_entry(self):
        """旧条目（指向旧目录）被移除；新条目追加、无关条目保留、无重复堆积。"""
        with tempfile.TemporaryDirectory() as d:
            v = self._vault(d)
            v.context_file.write_text(
                "# ai_context\n\n## AI 处理规则\n\n规则。\n\n## 历史归档索引\n"
                "\n## 2026-08-30 21:15｜SmartVault 归档\n"
                "- 文件：[[Obsidian指南/claude_pro|claude_pro]]\n"
                "- 目录：智能笔记/Obsidian指南\n- 摘要：旧\n- 标签：#旧\n"
                "\n## 2026-08-30 21:16｜SmartVault 归档\n"
                "- 文件：[[开发环境/git安装指南|git安装指南]]\n"
                "- 目录：开发环境\n- 摘要：无关\n- 标签：#Git\n",
                encoding="utf-8")
            final_md = v.root / "网络工具" / "claude_pro.md"
            sv.append_ai_context(v, self._meta("网络工具"), final_md)
            text = v.context_file.read_text(encoding="utf-8")
            self.assertNotIn("Obsidian指南/claude_pro", text)         # 同 stem 旧条目 → 移除
            self.assertIn("[[网络工具/claude_pro|claude_pro]]", text)  # 新条目 → 追加
            self.assertIn("[[开发环境/git安装指南|git安装指南]]", text)  # 无关条目 → 保留
            self.assertEqual(text.count("claude_pro]]"), 1)           # 无重复堆积

    def test_first_archive_creates_template(self):
        """ai_context.md 不存在时带模板创建并追加首条。"""
        with tempfile.TemporaryDirectory() as d:
            v = self._vault(d)
            final_md = v.root / "网络工具" / "claude_pro.md"
            sv.append_ai_context(v, self._meta("网络工具"), final_md)
            text = v.context_file.read_text(encoding="utf-8")
            self.assertIn("## AI 处理规则", text)
            self.assertIn("[[网络工具/claude_pro|claude_pro]]", text)

    def test_prompt_locks_short_draft_rule(self):
        """SYSTEM_PROMPT 须含超短草稿防蹭目录规则（防后续误删回归）。"""
        self.assertIn("超短草稿", sv.SYSTEM_PROMPT)
        self.assertIn("弱关联", sv.SYSTEM_PROMPT)

    def test_prompt_locks_link_terms_contract(self):
        """v1.7.0：输出契约须含 link_terms，且严禁 LLM 自行改写正文双链。"""
        self.assertIn("link_terms", sv.SYSTEM_PROMPT)
        self.assertIn("link_terms", sv.NOTE_JSON_SCHEMA["required"])
        self.assertIn("严禁在 optimized_content 中自行添加、改写或删除 [[双链]]", sv.SYSTEM_PROMPT)

    def test_parse_llm_json_link_terms_tolerant(self):
        """link_terms 解析：缺失→[]、字符串→拆分、列表→截前 8 个。"""
        base = '{"target_folder":"A","new_filename":"B","summary":"s","tags":["t1","t2","t3"],"optimized_content":""'
        m1 = sv.parse_llm_json(base + "}")
        self.assertEqual(m1["link_terms"], [])
        m2 = sv.parse_llm_json(base + ',"link_terms":"Modbus, RS485；北鼎蒸锅"}')
        self.assertEqual(m2["link_terms"], ["Modbus", "RS485", "北鼎蒸锅"])
        m3 = sv.parse_llm_json(base + ',"link_terms":["a","b","c","d","e","f","g","h","i","j"]}')
        self.assertEqual(len(m3["link_terms"]), 8)


if __name__ == "__main__":
    unittest.main()
