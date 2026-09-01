# -*- coding: utf-8 -*-
"""v1.11.0 PDF 扫描页 VLM 主引擎测试：Qwen2.5-VL（LM Studio）逐字转写 + RapidOCR 兜底。

不请求真实 LM Studio——mock ingest_daemon.requests 的 get/post，配合
_RAPIDOCR_STATE 注入 fake 引擎，验证：VLM 优先、失败/空结果降级 RapidOCR、
模型 id 解析（全路径 ≡ 目录短名）、请求体与防漏行指令、kind 标签与
OCR 页数上限对双引擎统一计数。真实识别效果由 E2E 手工验证。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_daemon as sv  # noqa: E402


class FakeEngine:
    """记录调用的假 RapidOCR 引擎（宽图应归一化为不超过 800px 的 ndarray）。"""

    def __init__(self, texts=("识别的手写文本一", "识别的手写文本二")):
        self.calls = []
        self._texts = texts

    def __call__(self, img):
        import numpy as np
        if isinstance(img, (bytes, bytearray)):
            assert bytes(img[:4]) == b"\x89PNG", "关闭归一化时应透传 PNG 字节流"
        else:
            assert isinstance(img, np.ndarray) and img.ndim == 3 and img.shape[1] <= 800, \
                "宽图应归一化为不超过 rapidocr_max_width 的 ndarray"
        self.calls.append(img)
        return SimpleNamespace(txts=self._texts)


def make_pdf(path: Path, image_pages=1) -> Path:
    """生成无文本层的扫描页 PDF（插入涂鸦图片模拟手写）。"""
    import fitz
    from PIL import Image, ImageDraw

    doc = fitz.open()
    for n in range(image_pages):
        png = path.parent / f"__scan_tmp_{n}.png"
        img = Image.new("RGB", (842, 595), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([100, 100, 700, 400], outline="black", width=3)
        img.save(png)
        doc.new_page().insert_image(doc[-1].rect, filename=str(png))
        png.unlink()
    doc.save(str(path))
    doc.close()
    return path


def fake_models_resp(ids):
    return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                           json=lambda: {"data": [{"id": i} for i in ids]})


def fake_chat_resp(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


class _VlmTestBase(unittest.TestCase):
    """公共脚手架：保存/恢复 requests、_VLM_STATE、_RAPIDOCR_STATE。"""

    def setUp(self):
        self._saved_req = sv.requests
        self._saved_vlm = dict(sv._VLM_STATE)
        self._saved_ocr = dict(sv._RAPIDOCR_STATE)
        sv._VLM_STATE.clear()
        sv._RAPIDOCR_STATE.clear()
        sv.requests = MagicMock()
        sv.requests.get.return_value = fake_models_resp(["qwen2.5-vl-7b-instruct"])

    def tearDown(self):
        sv.requests = self._saved_req
        sv._VLM_STATE.clear()
        sv._VLM_STATE.update(self._saved_vlm)
        sv._RAPIDOCR_STATE.clear()
        sv._RAPIDOCR_STATE.update(self._saved_ocr)

    def _engine(self, **kw):
        eng = FakeEngine(**kw)
        sv._RAPIDOCR_STATE["engine"] = eng
        return eng

    def _tmp(self, name="t.pdf"):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d / name


class TestVlmResolveModel(_VlmTestBase):
    """模型 id 解析：配置写全路径，LM Studio 以目录短名加载时等价匹配。"""

    def test_full_path_matches_short_id(self):
        sv.requests.get.return_value = fake_models_resp(
            ["qwen3-14b", "qwen2.5-vl-7b-instruct", "bge-small-embed"])
        got = sv._vlm_resolve_model({"vlm_model": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"})
        self.assertEqual(got, "qwen2.5-vl-7b-instruct")

    def test_exact_match_wins(self):
        full = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
        sv.requests.get.return_value = fake_models_resp([full])
        self.assertEqual(sv._vlm_resolve_model({"vlm_model": full}), full)

    def test_not_found_returns_configured_for_jit(self):
        """列表里没有同款 → 原样返回配置值（LM Studio 按全路径 JIT 拉起）。"""
        sv.requests.get.return_value = fake_models_resp(["qwen3-14b"])
        got = sv._vlm_resolve_model({"vlm_model": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"})
        self.assertEqual(got, "mlx-community/Qwen2.5-VL-7B-Instruct-4bit")

    def test_service_down_returns_configured(self):
        sv.requests.get.side_effect = ConnectionError("connection refused")
        got = sv._vlm_resolve_model({"vlm_model": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"})
        self.assertEqual(got, "mlx-community/Qwen2.5-VL-7B-Instruct-4bit")

    def test_resolve_result_cached_per_process(self):
        sv._vlm_resolve_model({"vlm_model": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"})
        sv.requests.get.side_effect = ConnectionError("第二次不应再请求")
        got = sv._vlm_resolve_model({"vlm_model": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"})
        self.assertEqual(got, "qwen2.5-vl-7b-instruct")


class TestVlmOcrPng(_VlmTestBase):
    """_vlm_ocr_png 纯函数行为（mock requests，不加载真实模型）。"""

    def test_chat_payload_and_fence_strip(self):
        """请求体带 base64 图片 + 防漏行转写指令；输出剥离偶发代码围栏。"""
        sv.requests.post.return_value = fake_chat_resp("```\nVLM 转写结果\n```")
        out = sv._vlm_ocr_png(b"\x89PNGfake",
                              {"vlm_model": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"})
        self.assertEqual(out, "VLM 转写结果")
        url = sv.requests.post.call_args[0][0]
        self.assertTrue(url.endswith("/chat/completions"))
        payload = sv.requests.post.call_args[1]["json"]
        self.assertEqual(payload["temperature"], 0)               # 确定性输出
        self.assertEqual(payload["model"], "qwen2.5-vl-7b-instruct")
        prompt = payload["messages"][0]["content"][1]["text"]
        self.assertNotIn("/no_think", prompt)                     # 转写指令不得掺思考开关
        self.assertIn("逐字转写", prompt)
        img_part = payload["messages"][0]["content"][0]
        self.assertEqual(img_part["type"], "image_url")
        self.assertTrue(img_part["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_http_error_raises_for_fallback(self):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "model not found"
        sv.requests.post.return_value = resp
        with self.assertRaises(RuntimeError):
            sv._vlm_ocr_png(b"x", {})

    def test_empty_content_returns_empty(self):
        sv.requests.post.return_value = fake_chat_resp("")
        self.assertEqual(sv._vlm_ocr_png(b"x", {}), "")

    def test_timeout_config_read_with_tolerance(self):
        """vlm_timeout 非法值回落 300（容错读取）。"""
        sv.requests.post.return_value = fake_chat_resp("文本")
        sv._vlm_ocr_png(b"x", {"vlm_timeout": "not-a-number"})
        self.assertEqual(sv.requests.post.call_args[1]["timeout"], 300)


class TestExtractPdfVlmEngine(_VlmTestBase):
    """extract_pdf engine=vlm：VLM 优先转写，失败/空结果 RapidOCR 兜底。"""

    def test_vlm_success_skips_rapidocr(self):
        pdf = make_pdf(self._tmp(), image_pages=2)
        eng = self._engine()
        sv.requests.post.return_value = fake_chat_resp("VLM 转写文本")
        kind, text = sv.extract_pdf(pdf, {"engine": "vlm"})
        self.assertEqual(eng.calls, [])                          # VLM 成功不触发 RapidOCR
        self.assertIn("VLM", kind)
        self.assertNotIn("RapidOCR", kind)
        self.assertIn("手写 2 页", kind)
        self.assertEqual(text.count("VLM 转写文本"), 2)
        self.assertIn("[第 1 页｜手写OCR]", text)
        self.assertEqual(sv.requests.post.call_count, 2)         # 每个扫描页各转写一次

    def test_vlm_failure_falls_back_to_rapidocr(self):
        pdf = make_pdf(self._tmp(), image_pages=1)
        eng = self._engine()
        sv.requests.post.side_effect = ConnectionError("LM Studio 未启动")
        kind, text = sv.extract_pdf(pdf, {"engine": "vlm"})
        self.assertEqual(len(eng.calls), 1)                      # 降级兜底生效
        self.assertIn("VLM→RapidOCR", kind)
        self.assertIn("识别的手写文本一", text)

    def test_vlm_empty_result_falls_back_to_rapidocr(self):
        pdf = make_pdf(self._tmp(), image_pages=1)
        eng = self._engine()
        sv.requests.post.return_value = fake_chat_resp("")       # VLM 空结果同样兜底
        kind, text = sv.extract_pdf(pdf, {"engine": "vlm"})
        self.assertEqual(len(eng.calls), 1)
        self.assertIn("VLM→RapidOCR", kind)

    def test_both_engines_empty_reports_honestly(self):
        """VLM 空 + RapidOCR 空：如实占位「未识别到文字」，kind 标 VLM→RapidOCR。"""
        pdf = make_pdf(self._tmp(), image_pages=1)
        self._engine(texts=())
        sv.requests.post.return_value = fake_chat_resp("")
        kind, text = sv.extract_pdf(pdf, {"engine": "vlm"})
        self.assertIn("VLM→RapidOCR", kind)
        self.assertIn("未识别到文字", text)

    def test_ocr_page_limit_counts_vlm_pages(self):
        """pdf_max_ocr_pages 对 VLM 转写页同样生效（两引擎统一计数）。"""
        pdf = make_pdf(self._tmp(), image_pages=3)
        eng = self._engine()
        sv.requests.post.return_value = fake_chat_resp("VLM 文本")
        kind, text = sv.extract_pdf(pdf, {"engine": "vlm", "pdf_max_ocr_pages": 2})
        self.assertEqual(sv.requests.post.call_count, 2)
        self.assertEqual(eng.calls, [])
        self.assertIn("手写 2 页", kind)
        self.assertIn("超出 OCR 页数上限", text)

    def test_engine_rapidocr_never_calls_vlm(self):
        pdf = make_pdf(self._tmp(), image_pages=1)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {"engine": "rapidocr"})
        sv.requests.post.assert_not_called()
        self.assertEqual(len(eng.calls), 1)
        self.assertIn("RapidOCR", kind)
        self.assertNotIn("VLM", kind)

    def test_default_engine_is_vlm(self):
        """v1.11.0 起 ocr.engine 默认 vlm：不传配置的扫描页走 VLM 转写。"""
        pdf = make_pdf(self._tmp(), image_pages=1)
        self._engine()
        sv.requests.post.return_value = fake_chat_resp("默认引擎文本")
        kind, text = sv.extract_pdf(pdf, {})
        self.assertIn("VLM", kind)
        self.assertIn("默认引擎文本", text)


if __name__ == "__main__":
    unittest.main()

