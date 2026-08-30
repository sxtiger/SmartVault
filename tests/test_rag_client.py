#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LMStudioClient.stream_chat SSE 解码的单元测试（回归：Latin-1 乱码 bug）。

需要导入 rag_api（依赖 fastapi/langchain/chromadb，请在项目 .venv 下运行）：
python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rag_api as rag  # noqa: E402


class _FakeStreamResp:
    """模拟 requests 流式响应：iter_lines() 产出原始 bytes（未解码）。

    encoding 复刻 LM Studio 场景：text/event-stream 无 charset 头时
    requests 会把 encoding 定为 ISO-8859-1（正是 v1.2.0 乱码的根源）。
    """

    def __init__(self, lines):
        self._lines = lines
        self.encoding = "ISO-8859-1"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        return iter(self._lines)


def _frame(delta) -> bytes:
    payload = json.dumps({"choices": [{"delta": {"content": delta}}]}, ensure_ascii=False)
    return b"data: " + payload.encode("utf-8")


class StreamChatTests(unittest.TestCase):

    def _client(self) -> rag.LMStudioClient:
        return rag.LMStudioClient({"lm_studio": {
            "base_url": "http://localhost:1234/v1", "chat_model": "qwen3-14b",
            "temperature": 0.3, "max_tokens": 4096, "timeout_seconds": 5}})

    def test_utf8_deltas_not_mojibake(self):
        """中文增量必须按 UTF-8 解码（回归：曾被 requests 按 ISO-8859-1 解成乱码）。"""
        lines = [_frame("笔记中未找到"), _frame("相关内容。"), b"data: [DONE]"]
        client = self._client()
        with mock.patch.object(rag.requests, "post", return_value=_FakeStreamResp(lines)):
            out = "".join(client.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, "笔记中未找到相关内容。")

    def test_bad_frames_and_role_frames_skipped(self):
        """注释行 / 坏 JSON / 无 content 的 role 帧跳过；[DONE] 后不再产出。"""
        role_frame = b"data: " + json.dumps(
            {"choices": [{"delta": {"role": "assistant"}}]}).encode("utf-8")
        lines = [b": keep-alive", b"data: {bad json", role_frame,
                 _frame("好"), b"data: [DONE]", _frame("不该出现")]
        client = self._client()
        with mock.patch.object(rag.requests, "post", return_value=_FakeStreamResp(lines)):
            out = "".join(client.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(out, "好")


class ChatPayloadSamplingTests(unittest.TestCase):
    """v1.6.2 Qwen3 调优：问答侧默认非 thinking + 官方非 thinking 采样参数。"""

    def _client(self, rag_extra=None, lm_extra=None) -> rag.LMStudioClient:
        lm = {"base_url": "http://localhost:1234/v1", "chat_model": "qwen3-14b",
              "temperature": 0.6, "max_tokens": 4096, "timeout_seconds": 5}
        lm.update(lm_extra or {})
        return rag.LMStudioClient({"lm_studio": lm, "rag": dict(rag_extra or {})})

    def test_defaults_follow_qwen3_non_thinking_recommendation(self):
        """无任何 chat_* 覆盖时：temp 0.7 / top_p 0.8 / top_k 20 / thinking 关。"""
        client = self._client()
        self.assertAlmostEqual(client.temperature, 0.7)
        self.assertAlmostEqual(client.top_p, 0.8)
        self.assertEqual(client.top_k, 20)
        self.assertFalse(client.thinking)

    def test_chat_payload_appends_no_think_and_sampling(self):
        """阻塞式 chat：payload 带 top_p/top_k，末条 user 追加 /no_think，原消息不动。"""
        ok = mock.Mock()
        ok.json.return_value = {"choices": [{"message": {"content": "答"}}]}
        client = self._client()
        msgs = [{"role": "user", "content": "问题"}]
        with mock.patch.object(rag.requests, "post", return_value=ok) as post:
            self.assertEqual(client.chat(msgs), "答")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["top_p"], 0.8)
        self.assertEqual(body["top_k"], 20)
        self.assertTrue(body["messages"][-1]["content"].endswith("/no_think"))
        self.assertEqual(msgs[-1]["content"], "问题")

    def test_chat_overrides_and_thinking_enabled(self):
        """rag.chat_* 覆盖生效；chat_thinking=true 时不追加软开关标记。"""
        ok = mock.Mock()
        ok.json.return_value = {"choices": [{"message": {"content": "答"}}]}
        client = self._client(rag_extra={"chat_temperature": 0.5,
                                         "chat_top_p": 0.9, "chat_thinking": True})
        with mock.patch.object(rag.requests, "post", return_value=ok) as post:
            client.chat([{"role": "user", "content": "问题"}])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["temperature"], 0.5)
        self.assertEqual(body["top_p"], 0.9)
        self.assertNotIn("/no_think", body["messages"][-1]["content"])


class PruneAiContextTests(unittest.TestCase):
    """prune_ai_context_entries：删除笔记后 ai_context.md 失效归档条目的自动剔除。"""

    def _vault(self, name="测试库"):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        return root, {"context_file": "ai_context.md",
                      "vaults": [{"name": name, "path": str(root)}]}

    def _write_ctx(self, root, entries):
        (root / "未分类").mkdir(parents=True, exist_ok=True)
        (root / "ai_context.md").write_text(
            "# ai_context\n\n## AI 处理规则\n\n规则。\n\n## 历史归档索引\n"
            + "".join(entries), encoding="utf-8")

    @staticmethod
    def _entry(ts, link, alias):
        """复刻 append_ai_context 的条目格式。"""
        return (f"\n## {ts}｜SmartVault 归档\n"
                f"- 文件：[[{link}|{alias}]]\n"
                f"- 目录：未分类\n- 摘要：s\n- 标签：#t\n")

    def test_dead_removed_alive_kept(self):
        """死链条目整段剔除，活条目与固定头部完整保留。"""
        root, cfg = self._vault()
        (root / "未分类").mkdir(parents=True)
        (root / "未分类" / "活笔记.md").write_text("x", encoding="utf-8")
        self._write_ctx(root, [
            self._entry("2026-08-30 10:00", "未分类/活笔记", "活笔记"),
            self._entry("2026-08-30 11:00", "未分类/死笔记", "死笔记"),
        ])
        out = rag.prune_ai_context_entries(cfg)
        self.assertEqual(out, {"测试库": 1})
        text = (root / "ai_context.md").read_text(encoding="utf-8")
        self.assertIn("[[未分类/活笔记|活笔记]]", text)
        self.assertIn("## 历史归档索引", text)
        self.assertNotIn("死笔记", text)

    def test_md_suffix_and_subdir_links(self):
        """带 .md 后缀与嵌套子目录的链接均可正确判定存活性。"""
        root, cfg = self._vault()
        (root / "A" / "B").mkdir(parents=True)
        (root / "A" / "B" / "深笔记.md").write_text("x", encoding="utf-8")
        self._write_ctx(root, [
            self._entry("2026-08-30 10:00", "A/B/深笔记.md", "深笔记"),
            self._entry("2026-08-30 11:00", "A/B/没了.md", "没了"),
        ])
        out = rag.prune_ai_context_entries(cfg)
        self.assertEqual(out, {"测试库": 1})
        text = (root / "ai_context.md").read_text(encoding="utf-8")
        self.assertIn("深笔记", text)
        self.assertNotIn("没了", text)

    def test_all_alive_no_write(self):
        """全部条目有效时不写盘（mtime 不变），返回空。"""
        root, cfg = self._vault()
        (root / "未分类").mkdir(parents=True)
        (root / "未分类" / "活.md").write_text("x", encoding="utf-8")
        self._write_ctx(root, [self._entry("2026-08-30 10:00", "未分类/活", "活")])
        p = root / "ai_context.md"
        mtime, before = p.stat().st_mtime_ns, p.read_text(encoding="utf-8")
        self.assertEqual(rag.prune_ai_context_entries(cfg), {})
        self.assertEqual(p.stat().st_mtime_ns, mtime)
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_no_context_file_or_nonstandard_entry(self):
        """ai_context.md 不存在 → 返回 {}；条目被人工改写（无标准文件行）→ 保守不动。"""
        root, cfg = self._vault()
        self.assertEqual(rag.prune_ai_context_entries(cfg), {})
        (root / "ai_context.md").write_text(
            "# ai_context\n\n## 历史归档索引\n\n"
            "## 2026-08-30 10:00｜SmartVault 归档\n"
            "（人工改写过的条目，无标准文件行）\n", encoding="utf-8")
        self.assertEqual(rag.prune_ai_context_entries(cfg), {})
        self.assertIn("人工改写", (root / "ai_context.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
