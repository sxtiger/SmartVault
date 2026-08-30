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


class LLMClientSamplingTests(unittest.TestCase):
    """v1.6.2 Qwen3 调优：采样参数显式下发 + thinking 软开关。"""

    def _client(self, **extra) -> sv.LLMClient:
        lm = {"base_url": "http://localhost:1234/v1", "chat_model": "qwen3-14b",
              "temperature": 0.6, "max_tokens": 4096, "timeout_seconds": 5,
              "structured_output": True}
        lm.update(extra)
        return sv.LLMClient({"lm_studio": lm})

    def test_payload_includes_sampling_params(self):
        """top_p / top_k 必须随请求显式下发，不再依赖 LM Studio 默认值（1.0/40）。"""
        ok = _resp(200, {"choices": [{"message": {"content": "{}"}}]})
        client = self._client(top_p=0.95, top_k=20, thinking=True)
        with mock.patch.object(sv.requests, "post", return_value=ok) as post:
            client.chat([{"role": "user", "content": "hi"}])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["top_p"], 0.95)
        self.assertEqual(body["top_k"], 20)
        self.assertNotIn("/no_think", body["messages"][-1]["content"])

    def test_thinking_off_appends_no_think_marker(self):
        """thinking=False：最后一条 user 消息末尾追加 /no_think，且原列表不被修改。"""
        ok = _resp(200, {"choices": [{"message": {"content": "{}"}}]})
        client = self._client(thinking=False)
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "正文"}]
        with mock.patch.object(sv.requests, "post", return_value=ok) as post:
            client.chat(msgs)
        sent = post.call_args.kwargs["json"]["messages"]
        self.assertTrue(sent[-1]["content"].endswith("/no_think"))
        self.assertEqual(sent[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], "正文")  # 原消息未被原地污染


if __name__ == "__main__":
    unittest.main()
