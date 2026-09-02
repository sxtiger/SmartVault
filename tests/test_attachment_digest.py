#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.12.0 长附件分批提炼测试：Map-Reduce 要点提炼 + 保全/注入解耦。

mock client.chat / requests.post，不触真实 LM Studio（风格仿 test_llm_client.py）。
覆盖：_split_transcript_pages / _batch_pages / digest_attachment_text（含 Reduce 合并轮
与降级路径）/ LLMClient.chat 覆写参数 / prepare_attachment_blocks（短/长/关闭/失败）
/ 终稿速览块双块落盘顺序 / dispatch_attachment 不再裁剪（回归）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ingest_daemon as sv  # noqa: E402


def _resp(status_code, json_body=None, text=""):
    r = mock.Mock()
    r.status_code = status_code
    r.text = text or str(json_body)
    r.json.return_value = json_body if json_body is not None else {}
    return r


class FakeClient:
    """假 LLM 客户端，记录调用参数并返回预设结果。"""

    def __init__(self, responses=None, fail_indices=None):
        self.calls = []
        self._responses = responses or []
        self._fail_indices = set(fail_indices or [])
        self._idx = 0

    def chat(self, messages, json_schema=None, temperature=None,
             max_tokens=None, thinking=None):
        idx = self._idx
        self._idx += 1
        self.calls.append({"messages": messages, "max_tokens": max_tokens,
                           "temperature": temperature, "thinking": thinking})
        if idx in self._fail_indices:
            raise RuntimeError(f"模拟 LLM 失败（第 {idx} 次调用）")
        if idx < len(self._responses):
            return self._responses[idx]
        return f"默认要点{idx}"


def _cfg(digest_enabled=True, prompt_cap=12000, preserve_cap=100000,
         batch_chars=4000):
    return {
        "processing": {"attachment_digest_enabled": digest_enabled,
                       "attachment_digest_batch_chars": batch_chars},
        "limits": {"attachment_max_chars": preserve_cap,
                   "attachment_prompt_max_chars": prompt_cap},
    }


# ================================================================== _split_transcript_pages
class TestSplitTranscriptPages(unittest.TestCase):
    def test_split_by_page_markers(self):
        text = "[第 1 页]\n内容一\n\n[第 2 页]\n内容二\n\n[第 3 页]\n内容三"
        pages = sv._split_transcript_pages(text)
        self.assertEqual(len(pages), 3)
        self.assertTrue(pages[0].startswith("[第 1 页]"))
        self.assertTrue(pages[1].startswith("[第 2 页]"))
        self.assertTrue(pages[2].startswith("[第 3 页]"))

    def test_no_markers_returns_single_block(self):
        text = "一段没有页标记的文本"
        pages = sv._split_transcript_pages(text)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0], text)

    def test_handwritten_ocr_marker(self):
        """[第 N 页｜手写OCR] 后缀标记也应正确切分与提取页码。"""
        text = "[第 1 页｜手写OCR]\n手写内容\n\n[第 2 页｜手写OCR]\n更多手写"
        pages = sv._split_transcript_pages(text)
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages[0].startswith("[第 1 页｜手写OCR]"))
        self.assertEqual(sv._extract_page_num(pages[0]), 1)
        self.assertEqual(sv._extract_page_num(pages[1]), 2)

    def test_empty_input(self):
        self.assertEqual(sv._split_transcript_pages(""), [])


# ================================================================== _batch_pages
class TestBatchPages(unittest.TestCase):
    def test_greedy_merge_under_limit(self):
        """每页 ~1008 字（标记+1000x），batch_chars=4000 → 每批 3 页。"""
        pages = [f"[第 {i} 页]\n{'x' * 1000}" for i in range(1, 6)]
        batches = sv._batch_pages(pages, 4000)
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0][0], 1)   # 起始页码
        self.assertEqual(batches[0][1], 3)   # 终止页码
        self.assertEqual(batches[1][0], 4)
        self.assertEqual(batches[1][1], 5)

    def test_oversized_page_standalone(self):
        """超宽单页（5000 字 > batch_chars 4000）独立成批。"""
        pages = [f"[第 {i} 页]\n{'x' * 5000}" for i in range(1, 3)]
        batches = sv._batch_pages(pages, 4000)
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0][0], 1)
        self.assertEqual(batches[0][1], 1)
        self.assertEqual(batches[1][0], 2)
        self.assertEqual(batches[1][1], 2)

    def test_empty_input(self):
        self.assertEqual(sv._batch_pages([], 4000), [])


# ================================================================== digest_attachment_text
class TestDigestAttachmentText(unittest.TestCase):
    def test_digest_returns_joined_with_page_labels(self):
        """mock 返回要点 → 拼接含页码范围标注；LLM 以 thinking=False/max_tokens=1024 调用。"""
        text = "[第 1 页]\n内容一\n\n[第 2 页]\n内容二"
        client = FakeClient(responses=["要点一"])
        result = sv.digest_attachment_text(text, _cfg(), client)
        self.assertIsNotNone(result)
        self.assertIn("第 1-2 页", result)
        self.assertIn("要点一", result)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["thinking"], False)
        self.assertEqual(client.calls[0]["max_tokens"], 1024)
        self.assertEqual(client.calls[0]["temperature"], 0.3)

    def test_single_batch_failure_fallback_head_slice(self):
        """单批 LLM 失败 → 该批降级为头 300 字切片。"""
        text = "[第 1 页]\n内容一"
        client = FakeClient(fail_indices={0})
        result = sv.digest_attachment_text(text, _cfg(), client)
        self.assertIsNotNone(result)
        self.assertIn("第 1 页", result)
        self.assertIn("内容一", result)

    def test_total_failure_returns_none(self):
        """提炼关闭或空文本 → 返回 None。"""
        # 禁用 → None
        self.assertIsNone(sv.digest_attachment_text("text", _cfg(digest_enabled=False), FakeClient()))
        # 空文本 → None
        self.assertIsNone(sv.digest_attachment_text("", _cfg(), FakeClient()))

    def test_joined_exceeds_cap_triggers_merge_round(self):
        """拼接要点仍超注入上限 → 触发合并提炼轮。"""
        text = "[第 1 页]\n内容"
        long_digest = "x" * 200  # 拼接后 > prompt_cap=50
        merged = "合并要点"
        client = FakeClient(responses=[long_digest, merged])
        result = sv.digest_attachment_text(text, _cfg(prompt_cap=50), client)
        self.assertIsNotNone(result)
        self.assertEqual(result, merged)
        self.assertEqual(len(client.calls), 2)  # Map + Reduce

    def test_merge_failure_falls_back_to_head_clip(self):
        """合并提炼异常 → 降级头裁剪（不中断）。"""
        text = "[第 1 页]\n内容"
        long_digest = "x" * 200
        client = FakeClient(responses=[long_digest], fail_indices={1})
        result = sv.digest_attachment_text(text, _cfg(prompt_cap=50), client)
        self.assertIsNotNone(result)
        self.assertIn("第 1 页", result)


# ================================================================== LLMClient.chat 覆写参数
class TestLLMClientChatOverrides(unittest.TestCase):
    def _client(self, **extra) -> sv.LLMClient:
        lm = {"base_url": "http://localhost:1234/v1", "chat_model": "test",
              "temperature": 0.6, "max_tokens": 4096, "timeout_seconds": 5,
              "structured_output": True, "thinking": True}
        lm.update(extra)
        return sv.LLMClient({"lm_studio": lm})

    def test_overrides_enter_payload(self):
        """max_tokens/thinking/temperature 覆写值进入请求 payload。"""
        ok = _resp(200, {"choices": [{"message": {"content": "ok"}}]})
        client = self._client()
        with mock.patch.object(sv.requests, "post", return_value=ok) as post:
            client.chat([{"role": "user", "content": "hi"}],
                        max_tokens=1024, thinking=False, temperature=0.3)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["max_tokens"], 1024)
        self.assertIn("/no_think", body["messages"][-1]["content"])
        self.assertEqual(body["temperature"], 0.3)

    def test_no_overrides_uses_defaults(self):
        """不传覆写参数时使用实例默认值（向后兼容）。"""
        ok = _resp(200, {"choices": [{"message": {"content": "ok"}}]})
        client = self._client(max_tokens=4096, thinking=True)
        with mock.patch.object(sv.requests, "post", return_value=ok) as post:
            client.chat([{"role": "user", "content": "hi"}])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["max_tokens"], 4096)
        self.assertNotIn("/no_think", body["messages"][-1]["content"])


# ================================================================== prepare_attachment_blocks
class TestPrepareAttachmentBlocks(unittest.TestCase):
    def test_short_attachment_inject_equals_full(self):
        """短附件：注入块 == 保全块（行为不变）。"""
        cfg = _cfg()
        blocks, inject = sv.prepare_attachment_blocks("a.txt", "纯文本附件", "短文本", cfg, FakeClient())
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0], inject)
        self.assertIn("短文本", inject)

    def test_long_attachment_with_digest_label(self):
        """长附件带提炼标注：保全侧两块（速览+全文），注入侧为要点。"""
        text = "x" * 20000
        cfg = _cfg()
        client = FakeClient(responses=["提炼要点"])
        blocks, inject = sv.prepare_attachment_blocks("big.pdf", "PDF 文本", text, cfg, client)
        self.assertEqual(len(blocks), 2)
        self.assertIn("要点提炼", blocks[0])   # 速览块在前
        self.assertIn("PDF 文本", blocks[1])   # 全文块在后
        self.assertIn("要点提炼", inject)
        self.assertIn("提炼要点", inject)
        self.assertIn("全文见文末转录块", inject)

    def test_digest_disabled_falls_back_to_head_clip(self):
        """开关关闭 → 纯头裁剪，不调 LLM。"""
        text = "x" * 20000
        cfg = _cfg(digest_enabled=False)
        client = FakeClient()
        blocks, inject = sv.prepare_attachment_blocks("big.pdf", "PDF 文本", text, cfg, client)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(client.calls), 0)
        self.assertIn("截断", inject)


# ================================================================== 终稿速览块（§2.3）
class TestSpeedViewBlock(unittest.TestCase):
    def test_digested_returns_two_blocks_speed_first(self):
        """提炼过的附件保全侧返回两块且速览在前、全文在后。"""
        text = "x" * 20000
        blocks, _ = sv.prepare_attachment_blocks("big.pdf", "PDF 文本", text, _cfg(), FakeClient())
        self.assertEqual(len(blocks), 2)
        self.assertIn("要点提炼", blocks[0])
        self.assertIn("PDF 文本", blocks[1])

    def test_digest_failure_no_speed_block(self):
        """提炼失败/关闭 → 不产生速览块。"""
        text = "x" * 20000
        blocks, _ = sv.prepare_attachment_blocks("big.pdf", "PDF 文本", text,
                                                 _cfg(digest_enabled=False), FakeClient())
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("要点提炼", blocks[0])

    def test_short_attachment_no_speed_block(self):
        """小附件未触发提炼 → 不产生速览块。"""
        blocks, _ = sv.prepare_attachment_blocks("a.txt", "纯文本", "短文本", _cfg(), FakeClient())
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("要点提炼", blocks[0])

    def test_build_preserved_content_renders_two_independent_folded_blocks(self):
        """build_preserved_content 把两块渲染为两个独立折叠块且顺序正确。"""
        raw = "正文内容"
        blocks = [
            "◆ 附件「big.pdf」｜要点提炼（机器生成速览，若有出入以原附件为准）\n第 1 页：要点",
            "◆ 附件「big.pdf」｜PDF 文本\n全文内容",
        ]
        out = sv.build_preserved_content(raw, blocks)
        self.assertEqual(out.count("> [!quote]-"), 2)
        speed_pos = out.find("要点提炼")
        full_pos = out.find("PDF 文本")
        self.assertLess(speed_pos, full_pos)


# ================================================================== 回归：dispatch_attachment 不再裁剪
class TestDispatchAttachmentNoClip(unittest.TestCase):
    def test_long_text_not_clipped(self):
        """v1.12.0：dispatch_attachment 返回全文，不再 _clip_text 裁剪。"""
        cfg = {"limits": {"attachment_max_chars": 100000,
                          "attachment_prompt_max_chars": 12000},
               "ocr": {"engine": "off"}}
        long_text = "内" * 20000
        with mock.patch.object(sv, "extract_pdf", return_value=("PDF 文本", long_text)):
            kind, text = sv.dispatch_attachment(Path("test.pdf"), cfg)
        self.assertEqual(len(text), 20000)
        self.assertNotIn("截断", text)


if __name__ == "__main__":
    unittest.main()
