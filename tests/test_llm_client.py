#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLMClient.chat 对 400 错误分流的单元测试（上下文超限 vs response_format 不支持）。

与 test_recent_errors.py 一样需要导入 ingest_daemon（依赖 requests，请在项目
.venv 下运行）：python3 -m unittest discover -s tests -v
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


CTX_ERR = {"error": {"message": "request (12194 tokens) exceeds the available context size (8192 tokens), try increasing it"}}
RF_ERR = {"error": {"message": "response_format is not supported"}}


class LLMClientChatTests(unittest.TestCase):
    """requests.post 全程 mock，不触碰真实 LM Studio。"""

    def _client(self) -> sv.LLMClient:
        return sv.LLMClient({"lm_studio": {
            "base_url": "http://localhost:1234/v1", "chat_model": "qwen3-14b",
            "temperature": 0.3, "max_tokens": 4096, "timeout_seconds": 5,
            "structured_output": True}})

    def test_context_overflow_raises_with_guidance_no_retry(self):
        """上下文超限 400：抛带指引的 RuntimeError，且不做无效的 response_format 回退重发。"""
        client = self._client()
        with mock.patch.object(sv.requests, "post", return_value=_resp(400, CTX_ERR)) as post:
            with self.assertRaises(RuntimeError) as cm:
                client.chat([{"role": "user", "content": "hi"}],
                            json_schema={"type": "object"})
        self.assertIn("上下文", str(cm.exception))
        self.assertIn("lms load", str(cm.exception))
        self.assertEqual(post.call_count, 1)

    def test_response_format_unsupported_falls_back(self):
        """真·response_format 不支持 400：去掉 response_format 重发一次并成功。"""
        client = self._client()
        ok = _resp(200, {"choices": [{"message": {"content": "{\"a\":1}"}}]})
        with mock.patch.object(sv.requests, "post",
                               side_effect=[_resp(400, RF_ERR), ok]) as post:
            out = client.chat([{"role": "user", "content": "hi"}],
                              json_schema={"type": "object"})
        self.assertEqual(out, "{\"a\":1}")
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])

    def test_ok_returns_content(self):
        """200 正常返回首选项内容。"""
        client = self._client()
        ok = _resp(200, {"choices": [{"message": {"content": "hello"}}]})
        with mock.patch.object(sv.requests, "post", return_value=ok) as post:
            self.assertEqual(client.chat([{"role": "user", "content": "hi"}]), "hello")
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
