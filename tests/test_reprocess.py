#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.13.0 附件增补（Attachment Backfill）测试。

锁定四类行为：
  1. parse_transcribed_attachments 转录清单提取（增补幂等判据）；
  2. append_transcription_blocks 追加格式（无转录区时与 build_preserved_content
     逐字节一致、frontmatter/正文逐字不动、转录区后用户内容不打断）；
  3. resolve_archived_attachment 定位边界（笔记目录优先、全仓库兜底排除
     收件箱与隐藏目录、无扩展名补全）；
  4. run_reprocess / run_reprocess_all 增补行为（追加 / 幂等零副作用 / 附件
     找不到跳过不写占位块 / 收件箱草稿不归增补管线管，dispatch_attachment
     与 _wait_file_stable 全 mock，不打真实 OCR / LLM）。

需要导入 menu_bar_app（依赖 rumps/requests，请在项目 .venv 下运行）：
  .venv/bin/python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_daemon as sv  # noqa: E402
import menu_bar_app as m  # noqa: E402

SECTION_HEADER = "## 附：附件转录（机器自动生成，仅供检索，若有出入以原附件为准）"


def _test_cfg(vaults=None):
    """增补管线所需的最小配置（提炼关闭 → prepare_attachment_blocks 不触 LLM）。"""
    over = {"processing": {"attachment_digest_enabled": False}}
    if vaults is not None:
        over["vaults"] = vaults
    return sv._deep_merge(sv.CONFIG_DEFAULTS, over)


class TestParseTranscribed(unittest.TestCase):

    def test_standard_blocks(self):
        md = ("正文\n\n" + SECTION_HEADER + "\n\n"
              "> [!quote]- ◆ 附件「截图.png」｜图像 OCR\n> 文字A\n\n"
              "> [!quote]- ◆ 附件「会议录音.mp3」｜音视频转录（whisper）\n> 文字B\n")
        self.assertEqual(sv.parse_transcribed_attachments(md),
                         {"截图.png", "会议录音.mp3"})

    def test_no_section_returns_empty(self):
        self.assertEqual(sv.parse_transcribed_attachments("# 标题\n正文 ![[a.png]]"), set())

    def test_plain_body_reference_not_counted(self):
        """正文普通行里的「◆ 附件「…」」（无 > 引用前缀）不算已转录。"""
        md = "◆ 附件「a.png」｜图像 OCR\n正文行 ![[a.png]]\n"
        self.assertEqual(sv.parse_transcribed_attachments(md), set())

    def test_notfound_placeholder_counts_as_done(self):
        """历史「未找到」占位块也算已处理（增补只管从无到有，防占位块累积）。"""
        md = SECTION_HEADER + "\n\n> [!quote]- ◆ 附件「x.png」｜未找到\n> （该附件未出现在收件箱中）\n"
        self.assertEqual(sv.parse_transcribed_attachments(md), {"x.png"})


class TestAppendTranscription(unittest.TestCase):

    BLOCKS = ["◆ 附件「x.png」｜图像 OCR\n识别文字",
              "◆ 附件「y.mp3」｜音视频转录（whisper）\n转写文字"]

    def test_creates_section_identical_to_build_preserved_content(self):
        """无转录区时的创建格式与归档管线 build_preserved_content 逐字节一致。"""
        raw = "---\ntitle: t\n---\n\n# 标题\n\n正文 ![[x.png]]。"
        self.assertEqual(sv.append_transcription_blocks(raw, self.BLOCKS),
                         sv.build_preserved_content(raw, self.BLOCKS))

    def test_appends_to_existing_section_after_last_block(self):
        raw = "正文\n\n" + SECTION_HEADER + "\n\n> [!quote]- ◆ 附件「旧.png」｜图像 OCR\n> 旧文字\n"
        out = sv.append_transcription_blocks(raw, self.BLOCKS)
        self.assertIn("> [!quote]- ◆ 附件「旧.png」", out)
        self.assertIn("> [!quote]- ◆ 附件「x.png」｜图像 OCR\n> 识别文字", out)
        self.assertLess(out.index("旧.png"), out.index("x.png"))   # 新块在旧块之后
        self.assertTrue(out.startswith("正文\n"))                   # 正文逐字不动

    def test_inserts_before_trailing_user_content(self):
        """用户在转录区后手工加的内容不被打断：新块插在两者之间。"""
        raw = ("正文\n\n" + SECTION_HEADER + "\n\n"
               "> [!quote]- ◆ 附件「旧.png」｜图像 OCR\n> 旧文字\n\n"
               "## 用户后来手工加的章节\n手工内容\n")
        out = sv.append_transcription_blocks(raw, self.BLOCKS)
        self.assertLess(out.index("◆ 附件「x.png」"), out.index("用户后来手工加的章节"))
        self.assertTrue(out.endswith("手工内容\n"))
        self.assertIn("> [!quote]- ◆ 附件「x.png」｜图像 OCR\n> 识别文字", out)

    def test_empty_blocks_noop(self):
        raw = "# 标题\n"
        self.assertEqual(sv.append_transcription_blocks(raw, []), raw)

    def test_body_verbatim_and_single_section(self):
        raw = "# 标题\n\n正文\n\n\n"
        out = sv.append_transcription_blocks(raw, ["◆ 附件「a.png」｜图像 OCR\n文字"])
        self.assertIn("# 标题\n\n正文", out)                # 正文原样保留
        self.assertTrue(out.endswith("\n"))                 # 归一化单一结尾换行
        self.assertEqual(out.count("## 附：附件转录"), 1)   # 不重复建区


# === PART2 ===
class TestResolveArchivedAttachment(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.note_dir = self.root / "项目"
        (self.note_dir / "附件").mkdir(parents=True)
        (self.note_dir / "附件" / "图.png").write_bytes(b"png")
        self.note = self.note_dir / "笔记.md"
        self.note.write_text("# 笔记\n", encoding="utf-8")
        self.vault = sv.Vault(name="测试库", root=self.root,
                              inbox=self.root / "待处理笔记",
                              context_file=self.root / "ai_context.md")

    def tearDown(self):
        self._tmp.cleanup()

    def test_found_in_note_subfolder(self):
        p = sv.resolve_archived_attachment(self.vault, self.note, "图.png")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "图.png")

    def test_extension_completion(self):
        """引用写 ![[图]]（无扩展名）时按可解析扩展名补全。"""
        p = sv.resolve_archived_attachment(self.vault, self.note, "图")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "图.png")

    def test_vault_wide_fallback(self):
        (self.root / "资料" / "深").mkdir(parents=True)
        (self.root / "资料" / "深" / "别的.pdf").write_bytes(b"pdf")
        p = sv.resolve_archived_attachment(self.vault, self.note, "别的.pdf")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "别的.pdf")

    def test_inbox_same_name_not_matched(self):
        """收件箱里的同名文件属于待归档草稿，不算已归档笔记的附件。"""
        self.vault.inbox.mkdir(parents=True, exist_ok=True)
        (self.vault.inbox / "新图.png").write_bytes(b"png")
        self.assertIsNone(sv.resolve_archived_attachment(self.vault, self.note, "新图.png"))

    def test_hidden_dirs_not_matched(self):
        bdir = self.root / ".smartvault" / "backup"
        bdir.mkdir(parents=True)
        (bdir / "备份.png").write_bytes(b"png")
        self.assertIsNone(sv.resolve_archived_attachment(self.vault, self.note, "备份.png"))


class TestRunReprocess(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.note_dir = self.root / "项目"
        (self.note_dir / "附件").mkdir(parents=True)
        (self.note_dir / "附件" / "新增截图.png").write_bytes(b"png")
        self.note = self.note_dir / "会议.md"
        self.note.write_text(
            "---\ntitle: 会议\n---\n\n# 会议\n\n看这张图：![[新增截图.png]]\n",
            encoding="utf-8")
        self.vault = sv.Vault(name="测试库", root=self.root,
                              inbox=self.root / "待处理笔记",
                              context_file=self.root / "ai_context.md")
        self.cfg = _test_cfg()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        with mock.patch.object(sv, "dispatch_attachment",
                               return_value=("图像 OCR", "识别出的文字")) as dsp, \
             mock.patch.object(sv, "_wait_file_stable", return_value=True):
            stats = sv.run_reprocess(self.cfg, None, self.vault, self.note)
        return stats, dsp

    def test_appends_transcription_for_new_attachment(self):
        stats, dsp = self._run()
        self.assertEqual(stats["added"], 1)
        dsp.assert_called_once()
        out = self.note.read_text(encoding="utf-8")
        self.assertIn(SECTION_HEADER, out)
        self.assertIn("> [!quote]- ◆ 附件「新增截图.png」｜图像 OCR", out)
        self.assertIn("> 识别出的文字", out)
        # frontmatter 与正文逐字不动（只在其后追加转录区）
        self.assertTrue(out.startswith(
            "---\ntitle: 会议\n---\n\n# 会议\n\n看这张图：![[新增截图.png]]\n"))

    def test_idempotent_second_run(self):
        self._run()
        first = self.note.read_text(encoding="utf-8")
        stats, dsp = self._run()
        self.assertEqual((stats["added"], stats["already"]), (0, 1))
        dsp.assert_not_called()                                   # 不再触发解析
        self.assertEqual(self.note.read_text(encoding="utf-8"), first)

    def test_missing_attachment_skipped_no_placeholder(self):
        self.note.write_text("正文 ![[不存在的附件.png]]\n", encoding="utf-8")
        stats, dsp = self._run()
        self.assertEqual((stats["added"], stats["missing"]), (0, 1))
        self.assertEqual(self.note.read_text(encoding="utf-8"),
                         "正文 ![[不存在的附件.png]]\n")          # 不写占位块、原样保留

    def test_no_refs_noop(self):
        self.note.write_text("# 无附件笔记\n", encoding="utf-8")
        stats, dsp = self._run()
        self.assertEqual(stats["refs"], 0)
        dsp.assert_not_called()

    def test_subfolder_prefixed_ref_matches_transcription_name(self):
        """带子目录前缀的引用（![[附件/新增截图.png]]）按文件名比对幂等清单。"""
        self.note.write_text("图：![[附件/新增截图.png]]\n", encoding="utf-8")
        stats, dsp = self._run()
        self.assertEqual(stats["added"], 1)
        self.assertIn("◆ 附件「新增截图.png」",
                      self.note.read_text(encoding="utf-8"))
        stats2, dsp2 = self._run()
        self.assertEqual((stats2["added"], stats2["already"]), (0, 1))
        dsp2.assert_not_called()


# === PART3 ===
class TestRunReprocessAll(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "项目").mkdir(parents=True)
        self.cfg = _test_cfg(vaults=[{"name": "测试库", "path": str(self.root)}])

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_processes_only_untranscribed(self):
        # ① 待增补：引用附件但无转录区 → 触发解析
        (self.root / "项目" / "a.md").write_text("图：![[a.png]]\n", encoding="utf-8")
        (self.root / "项目" / "a.png").write_bytes(b"png")
        # ② 幂等快路径：引用且已转录 → 不解析不写盘
        (self.root / "项目" / "b.md").write_text(
            "图：![[b.png]]\n\n" + SECTION_HEADER + "\n\n"
            "> [!quote]- ◆ 附件「b.png」｜图像 OCR\n> 已转录\n", encoding="utf-8")
        (self.root / "项目" / "b.png").write_bytes(b"png")
        b_before = (self.root / "项目" / "b.md").read_text(encoding="utf-8")
        # ③ 收件箱草稿：归档管线的职责，增补不动
        inbox = self.root / "待处理笔记"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "c.md").write_text("图：![[c.png]]\n", encoding="utf-8")
        (inbox / "c.png").write_bytes(b"png")
        with mock.patch.object(sv, "dispatch_attachment",
                               return_value=("图像 OCR", "文字")) as dsp, \
             mock.patch.object(sv, "_wait_file_stable", return_value=True):
            rc = sv.run_reprocess_all(self.cfg, None)
        self.assertEqual(rc, 0)
        dsp.assert_called_once()                                  # 只有 a.md 触发解析
        self.assertIn("◆ 附件「a.png」",
                      (self.root / "项目" / "a.md").read_text(encoding="utf-8"))
        self.assertEqual((self.root / "项目" / "b.md").read_text(encoding="utf-8"), b_before)
        self.assertEqual((inbox / "c.md").read_text(encoding="utf-8"), "图：![[c.png]]\n")

    def test_no_candidates_no_parse(self):
        (self.root / "项目" / "无附件.md").write_text("# 纯文本\n", encoding="utf-8")
        with mock.patch.object(sv, "dispatch_attachment",
                               return_value=("图像 OCR", "文字")) as dsp:
            rc = sv.run_reprocess_all(self.cfg, None)
        self.assertEqual(rc, 0)
        dsp.assert_not_called()


class TestMenuBarReprocessCmd(unittest.TestCase):

    def test_build_cmd_shape(self):
        cmd = m._build_reprocess_cmd()
        self.assertEqual(cmd[0], m.PYTHON_BIN)                    # 与控制台同解释器
        self.assertEqual(Path(cmd[1]), m.PROJECT_DIR / "ingest_daemon.py")
        self.assertIn("--reprocess-all", cmd)
        self.assertIn(str(m.PROJECT_DIR / "config.json"), cmd)    # config 显式绝对路径


if __name__ == "__main__":
    unittest.main()
