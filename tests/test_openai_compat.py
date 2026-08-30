#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI 兼容适配层单元测试（BMO Chatbot / 任意 OpenAI 协议客户端接入）。

覆盖：
- extract_query_and_history：messages → 检索 query + 对话历史的提取规则
- format_sources_footer：来源 Markdown 附录渲染
- _openai_completion / _openai_chunk / _openai_stream：OpenAI 响应帧构造
- BMO 客户端解析契约端到端复刻（split('\\n') + finish_reason 守卫 + [DONE]）

需要导入 rag_api（依赖 fastapi/langchain/chromadb，请在项目 .venv 下运行）：
python3 -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rag_api as rag  # noqa: E402


class TestExtractQueryAndHistory(unittest.TestCase):
    def test_bmo_style_conversation(self):
        """BMO 典型载荷：system 人设 + 多轮对话 → 取末条 user 为 query。"""
        messages = [
            {"role": "system", "content": "You are BMO."},
            {"role": "user", "content": "SmartVault 是什么？"},
            {"role": "assistant", "content": "是一个自动归档项目。"},
            {"role": "user", "content": "  它支持哪些归档规则？ "},
        ]
        query, history = rag.extract_query_and_history(messages)
        self.assertEqual(query, "它支持哪些归档规则？")
        self.assertEqual([h["role"] for h in history], ["user", "assistant"])

    def test_no_user_message(self):
        self.assertEqual(rag.extract_query_and_history(
            [{"role": "system", "content": "hi"}]), ("", []))
        self.assertEqual(rag.extract_query_and_history([]), ("", []))

    def test_system_messages_excluded_from_history(self):
        _, history = rag.extract_query_and_history([
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "q1"},
            {"role": "system", "content": "inserted"},
            {"role": "user", "content": "q2"},
        ])
        self.assertEqual([h["role"] for h in history], ["user"])

    def test_history_capped_to_recent_turns(self):
        messages = [{"role": "system", "content": "s"}]
        for i in range(10):  # 10 轮 = 20 条，远超 _MAX_HISTORY_TURNS=6
            messages += [{"role": "user", "content": f"q{i}"},
                         {"role": "assistant", "content": f"a{i}"}]
        query, history = rag.extract_query_and_history(messages)
        self.assertEqual(query, "q9")  # query 取最后一条 user 消息
        self.assertEqual(len(history), rag._MAX_HISTORY_TURNS * 2)
        self.assertEqual(history[0]["content"], "q3")  # q9 之前的最近 6 轮（q3 起）

    def test_long_history_message_truncated(self):
        messages = [{"role": "user", "content": "x" * 3000},
                    {"role": "user", "content": "q"}]
        _, history = rag.extract_query_and_history(messages)
        self.assertEqual(len(history[0]["content"]), rag._MAX_HISTORY_CHARS)

    def test_non_string_content_yields_empty_query(self):
        # 多模态客户端可能送 content 为数组；最后一条 user 非字符串 → 400 路径
        query, _ = rag.extract_query_and_history(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        self.assertEqual(query, "")


class TestSourcesFooter(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(rag.format_sources_footer([]), "")

    def test_with_and_without_title(self):
        footer = rag.format_sources_footer([
            {"path": "Projects/SmartVault.md", "title": "SmartVault", "distance": 0.1},
            {"path": "Notes/misc.md", "title": "", "distance": 0.2},
        ])
        self.assertIn("- SmartVault（Projects/SmartVault.md）", footer)
        self.assertIn("- Notes/misc.md（Notes/misc.md）", footer)
        self.assertTrue(footer.startswith("\n\n---\n**参考来源**\n"))


class TestOpenAIResponseShapes(unittest.TestCase):
    def test_completion_shape(self):
        out = rag._openai_completion("回答内容", "smartvault-rag")
        self.assertEqual(out["object"], "chat.completion")
        self.assertEqual(out["model"], "smartvault-rag")
        self.assertEqual(out["choices"][0]["message"]["content"], "回答内容")
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")
        self.assertEqual(out["choices"][0]["message"]["role"], "assistant")

    def test_chunk_frames(self):
        frame = json.loads(rag._openai_chunk("m", {"content": "增量"}, None))
        self.assertEqual(frame["object"], "chat.completion.chunk")
        self.assertIsNone(frame["choices"][0]["finish_reason"])
        self.assertEqual(frame["choices"][0]["delta"]["content"], "增量")
        stop = json.loads(rag._openai_chunk("m", {}, "stop"))
        self.assertEqual(stop["choices"][0]["finish_reason"], "stop")
        self.assertEqual(stop["choices"][0]["delta"], {})

    def test_chunk_keeps_chinese_raw(self):
        """ensure_ascii=False：中文不得被转义成 \\uXXXX（BMO 侧直接展示原文）。"""
        self.assertIn("中文", rag._openai_chunk("m", {"content": "中文"}, None))


class TestBMOStreamContract(unittest.TestCase):
    """复刻 BMO Chatbot 的 SSE 解析（src/components/FetchModelResponse.ts）。"""

    @staticmethod
    def _bmo_parse(frames):
        message = ""
        for part in "".join(frames).split("\n"):
            if not part:
                continue
            if "data: [DONE]" in part:
                break
            parsed = json.loads(part.replace("data: ", "", 1))
            if parsed["choices"][0]["finish_reason"] != "stop":
                message += parsed["choices"][0]["delta"].get("content", "")
        return message

    def test_openai_stream_reassembles_exactly(self):
        text = "索引为空或未命中任何笔记，请先执行 POST /reindex 建立索引。"
        frames = list(rag._openai_stream(text, "smartvault-rag"))
        self.assertTrue(frames[-1].endswith("data: [DONE]\n\n"))
        self.assertEqual(self._bmo_parse(frames), text)

    def test_model_endpoint_payload(self):
        """GET /v1/models 响应需满足 fetchRESTAPIURLModels 的解析（data[].id）。"""
        payload = rag.openai_models()
        self.assertEqual(payload["data"][0]["id"], rag.OPENAI_COMPAT_MODEL)


class TestRequestTemperature(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(rag._request_temperature({"temperature": 0.7}), 0.7)
        self.assertEqual(rag._request_temperature({"temperature": 1}), 1.0)
        self.assertIsNone(rag._request_temperature({"temperature": "0.7"}))
        self.assertIsNone(rag._request_temperature({}))


if __name__ == "__main__":
    unittest.main()

