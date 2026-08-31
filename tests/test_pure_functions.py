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

    def test_html_img_tag_src_recognized(self):
        # v1.6.3：Kindle/HTML 转 Markdown 产物的 <img src="..."> 引用（带路径前缀取 basename）
        md = '<img src="附件/食谱-蒸海鲜什锦-1.png">\n<video src="演示.mp4"></video>'
        self.assertEqual(sv.find_attachment_refs(md), ["食谱-蒸海鲜什锦-1.png", "演示.mp4"])

    def test_html_src_variants(self):
        md = ("<img src='单引号.png'> <img src=裸值.jpg> <IMG SRC=\"大写.PDF\"> "
              '<img alt="说明" src="多属性.webp" width="300"> '
              '<img src="URL%E7%BC%96%E7%A0%81.png">')
        self.assertEqual(sv.find_attachment_refs(md),
                         ["单引号.png", "裸值.jpg", "大写.PDF", "多属性.webp", "URL编码.png"])

    def test_html_src_skip_external_and_data_uri(self):
        md = ('<img src="https://example.com/a.png"> <img src="data:image/png;base64,iVBOR"> '
              '<embed src="obsidian://open?v=x">')
        self.assertEqual(sv.find_attachment_refs(md), [])

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

    def test_html_src_subfolder_and_no_subfolder(self):
        # v1.6.3：HTML src 为相对路径语法，改写为 子目录/新名；引号风格保持原样
        content = '<img src="附件/白板.png"> <img src=\'面板.png\'>'
        out = sv.rewrite_links(content, {"白板.png": "白板 2.png"}, "附件")
        self.assertIn('<img src="附件/白板 2.png">', out)
        self.assertIn("<img src='面板.png'>", out)          # 未移动的附件不动
        out2 = sv.rewrite_links(content, {"白板.png": "白板 2.png"}, "")
        self.assertIn('<img src="白板 2.png">', out2)

    def test_html_src_alt_same_value_not_corrupted(self):
        # alt 与 src 同值时只改 src，前序属性不受影响
        content = '<img alt="白板.png" src="白板.png">'
        out = sv.rewrite_links(content, {"白板.png": "白板 2.png"}, "附件")
        self.assertEqual(out, '<img alt="白板.png" src="附件/白板 2.png">')

    def test_html_src_url_and_data_untouched(self):
        content = '<img src="https://a.com/b.png"><img src="data:image/png;base64,xx">'
        out = sv.rewrite_links(content, {"b.png": "c.png"}, "附件")
        self.assertEqual(out, content)


class TestUniquePath(unittest.TestCase):
    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "笔记.md"
            p.write_text("x", encoding="utf-8")
            self.assertEqual(sv.unique_path(p).name, "笔记 2.md")


if __name__ == "__main__":
    unittest.main()

