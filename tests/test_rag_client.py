#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LMStudioClient.stream_chat SSE 解码的单元测试（回归：Latin-1 乱码 bug）。

需要导入 rag_api（依赖 fastapi/langchain/chromadb，请在项目 .venv 下运行）：
python3 -m unittest discover -s tests -v
"""
import json
import sys
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


if __name__ == "__main__":
    unittest.main()
