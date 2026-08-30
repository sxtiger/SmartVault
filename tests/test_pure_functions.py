#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SmartVault 纯函数单元测试（不依赖任何第三方库，随时可跑）。

运行：python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ingest_daemon as sv  # noqa: E402


class TestSanitize(unittest.TestCase):
    def test_sanitize_filename_removes_illegal_chars(self):
        self.assertEqual(sv.sanitize_filename('报告/2026:最终*版?"测试"'), "报告2026最终版测试")

    def test_sanitize_filename_fallback(self):
        self.assertEqual(sv.sanitize_filename('???'), "未命名笔记")

    def test_sanitize_folder_parts_blocks_traversal(self):
        parts = sv.sanitize_folder_parts("../etc/passwd")
        self.assertEqual(parts, ["etc", "passwd"])
        self.assertNotIn("..", parts)

    def test_sanitize_tags(self):
        self.assertEqual(sv.sanitize_tags(["#智能 仓库", "RAG", "RAG", ""]),
                         ["智能-仓库", "RAG"])


class TestAttachmentRefs(unittest.TestCase):
    def test_wikilink_embed_with_alias(self):
        md = "![[截图 2026-08-30.png|300]] 以及 [[会议纪要]]"
        self.assertEqual(sv.find_attachment_refs(md), ["截图 2026-08-30.png"])

    def test_standard_md_link_and_url_skip(self):
        md = "![logo](./files/报告%20v2.pdf) [官网](https://example.com/a.png) [锚点](#sec)"
        self.assertEqual(sv.find_attachment_refs(md), ["报告 v2.pdf"])

    def test_md_note_links_ignored(self):
        self.assertEqual(sv.find_attachment_refs("[[旧笔记.md]] [[另一篇]]"), [])

    def test_resolve_attachment_no_ext(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d)
            (inbox / "白板.png").write_bytes(b"x")
            found = sv.resolve_attachment(inbox, "白板")
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "白板.png")


class TestParseLLMJson(unittest.TestCase):
    def test_fenced_json(self):
        raw = ('```json\n{"target_folder":"工作/会议","new_filename":"周会纪要",\n'
               '"summary":"s","tags":["a","b","c"],"optimized_content":"## 内容"}\n```')
        meta = sv.parse_llm_json(raw)
        self.assertEqual(meta["target_folder"], "工作/会议")
        self.assertEqual(meta["new_filename"], "周会纪要")
        self.assertEqual(meta["tags"], ["a", "b", "c"])

    def test_tags_string_coerced(self):
        raw = ('{"target_folder":"x","new_filename":"n","summary":"s",'
               '"tags":"a, b，c","optimized_content":"c"}')
        self.assertEqual(sv.parse_llm_json(raw)["tags"], ["a", "b", "c"])

    def test_noise_prefix(self):
        raw = ('好的，以下是结果：{"target_folder":"x","new_filename":"n","summary":"s",'
               '"tags":[],"optimized_content":"c"} 完毕')
        self.assertEqual(sv.parse_llm_json(raw)["new_filename"], "n")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            sv.parse_llm_json("完全不是 JSON")


class TestMarkdownBuild(unittest.TestCase):
    def test_frontmatter_escapes_quotes(self):
        meta = {"new_filename": '含"引号"的标题', "summary": '他说："好"',
                "tags": ["测试"], "optimized_content": "正文"}
        out = sv.build_final_markdown(meta, sv.datetime(2026, 8, 30, 12, 0, 0))
        self.assertTrue(out.startswith("---\n"))
        self.assertIn('title: "含\\"引号\\"的标题"', out)
        self.assertIn("  - 测试", out)
        self.assertIn("source: SmartVault 自动归档", out)
        self.assertTrue(out.rstrip().endswith("正文"))


class TestTreeAndContext(unittest.TestCase):
    def test_scan_tree_excludes_hidden_and_inbox(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".obsidian").mkdir()
            (root / "待处理笔记").mkdir()
            (root / "会议记录").mkdir()
            (root / "会议记录" / "周会").mkdir()
            tree = sv.scan_tree(root, depth=2, exclude_names=frozenset({"待处理笔记"}))
            self.assertIn("会议记录/", tree)
            self.assertIn("周会/", tree)
            self.assertNotIn(".obsidian", tree)
            self.assertNotIn("待处理笔记", tree)

    def test_load_ai_context_head_tail_split(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ai_context.md"
            body = "".join(f"{i:04d}行内容\n" for i in range(500))
            p.write_text("# 规则头\n" + body + "# 尾部索引\n", encoding="utf-8")
            clipped = sv.load_ai_context(p, 500)
            self.assertLessEqual(len(clipped), 500 + 40)
            self.assertIn("# 规则头", clipped)
            self.assertIn("# 尾部索引", clipped)
            self.assertIn("中间历史索引已省略", clipped)

    def test_load_ai_context_missing(self):
        self.assertEqual(sv.load_ai_context(Path("/nonexistent/x.md"), 100), "")


class TestChooseTargetDir(unittest.TestCase):
    def _vault(self, root: Path) -> sv.Vault:
        return sv.Vault(name="测试", root=root, inbox=root / "待处理笔记",
                        context_file=root / "ai_context.md")

    PROC = {"allow_new_folder": True, "max_folder_depth": 2, "fallback_folder": "未分类"}

    def test_existing_folder_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "会议记录").mkdir()
            got = sv.choose_target_dir(self._vault(root), "会议记录", self.PROC, "待处理笔记")
            self.assertEqual(got, root / "会议记录")

    def test_new_folder_created_within_depth(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            got = sv.choose_target_dir(self._vault(root), "读书笔记/哲学", self.PROC, "待处理笔记")
            self.assertTrue(got.is_dir())
            self.assertEqual(got.name, "哲学")

    def test_too_deep_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            got = sv.choose_target_dir(self._vault(root), "a/b/c/d", self.PROC, "待处理笔记")
            self.assertEqual(got.name, "未分类")

    def test_inbox_forbidden(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "待处理笔记").mkdir()
            got = sv.choose_target_dir(self._vault(root), "待处理笔记", self.PROC, "待处理笔记")
            self.assertEqual(got.name, "未分类")


class TestRewriteLinks(unittest.TestCase):
    def test_wikilink_rename_and_mdlink_subfolder(self):
        content = "![[白板.png|400]] 和 [文档](白板.png) 及 [外部](https://a.com/b.png)"
        out = sv.rewrite_links(content, {"白板.png": "白板 2.png"}, "attachments")
        self.assertIn("[[白板 2.png|400]]", out)
        self.assertIn("[文档](attachments/白板 2.png)", out)
        self.assertIn("https://a.com/b.png", out)


class TestUniquePath(unittest.TestCase):
    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "笔记.md"
            p.write_text("x", encoding="utf-8")
            self.assertEqual(sv.unique_path(p).name, "笔记 2.md")


if __name__ == "__main__":
    unittest.main()

