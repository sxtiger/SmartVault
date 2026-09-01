# -*- coding: utf-8 -*-
"""v1.8.0 PDF 双通道提取测试：文本层优先（PyMuPDF），扫描页 RapidOCR 兜底。

不加载真实 RapidOCR 模型——通过向 ingest_daemon._RAPIDOCR_STATE 注入 fake 引擎
验证分发逻辑、降级路径与页数上限；真实识别效果由 E2E 手工验证。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_daemon as sv  # noqa: E402


class FakeEngine:
    """记录调用的假 RapidOCR 引擎。

    v1.10.0 起宽图先归一化：默认（rapidocr_max_width=800）收到不超过该宽度的
    ndarray（RGB→BGR 后的 HWC 数组）；关闭归一化（rapidocr_max_width<=0）时
    收到渲染后的 PNG 字节流（透传原输入）。
    """

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


def make_pdf(path: Path, text_pages=(), image_pages=0) -> Path:
    """生成测试 PDF：text_pages 为文本层内容；image_pages 为无文本层的扫描页数。"""
    import fitz
    from PIL import Image, ImageDraw

    doc = fitz.open()
    for txt in text_pages:
        page = doc.new_page()
        page.insert_text((72, 96), txt, fontsize=16)
    for n in range(image_pages):
        png = path.parent / f"__scan_tmp_{n}.png"
        img = Image.new("RGB", (842, 595), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([100, 100, 700, 400], outline="black", width=3)  # 模拟手写内容涂鸦
        img.save(png)
        doc.new_page().insert_image(doc[-1].rect, filename=str(png))
        png.unlink()
    doc.save(str(path))
    doc.close()
    return path


class TestExtractPdfDualChannel(unittest.TestCase):
    """extract_pdf 双通道：文本层零成本直取；扫描页按页渲染 OCR。"""

    def setUp(self):
        self._saved = dict(sv._RAPIDOCR_STATE)
        sv._RAPIDOCR_STATE.clear()

    def tearDown(self):
        sv._RAPIDOCR_STATE.clear()
        sv._RAPIDOCR_STATE.update(self._saved)

    def _engine(self, **kw):
        eng = FakeEngine(**kw)
        sv._RAPIDOCR_STATE["engine"] = eng
        return eng

    def _tmp(self, name="t.pdf"):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d / name

    def test_text_layer_pdf_skips_ocr(self):
        pdf = make_pdf(self._tmp(), text_pages=["SmartVault ingest daemon pipeline test note."], image_pages=0)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {})
        self.assertEqual(eng.calls, [])                          # 纯文本层不触发 OCR
        self.assertEqual(kind, "PDF 文本（PyMuPDF）")
        self.assertIn("SmartVault", text)
        self.assertIn("[第 1 页]", text)
        self.assertNotIn("手写OCR", text)

    def test_chinese_text_layer_not_ocr(self):
        pdf = self._tmp()
        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((72, 96), "中文电子文档文本层测试内容一二三四五六七八九十", fontsize=16,
                                   fontname="china-s")
        doc.save(str(pdf))
        doc.close()
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {})
        self.assertEqual(eng.calls, [])
        self.assertIn("中文电子文档", text)

    def test_scanned_pages_use_rapidocr(self):
        pdf = make_pdf(self._tmp(), text_pages=(), image_pages=2)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {})
        self.assertEqual(len(eng.calls), 2)                       # 每个扫描页各渲染一次
        self.assertIn("RapidOCR", kind)
        self.assertIn("手写 2 页", kind)
        self.assertIn("[第 1 页｜手写OCR]", text)
        self.assertIn("[第 2 页｜手写OCR]", text)
        self.assertIn("识别的手写文本一", text)

    def test_mixed_pdf_text_pages_skip_ocr(self):
        pdf = make_pdf(self._tmp(), text_pages=["Mixed PDF page with enough text layer content."],
                       image_pages=1)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {})
        self.assertEqual(len(eng.calls), 1)                       # 只有扫描页走 OCR
        self.assertIn("Mixed PDF", text)
        self.assertIn("手写OCR", text)

    def test_short_text_layer_treated_as_scanned(self):
        """文本层字符数低于 pdf_min_text_chars 的页视为扫描页（水印页/坏导出页）。"""
        pdf = make_pdf(self._tmp(), text_pages=["short"], image_pages=0)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {"pdf_min_text_chars": 20})
        self.assertEqual(len(eng.calls), 1)
        self.assertIn("手写OCR", text)

    def test_ocr_page_limit(self):
        pdf = make_pdf(self._tmp(), text_pages=(), image_pages=3)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {"pdf_max_ocr_pages": 2})
        self.assertEqual(len(eng.calls), 2)                       # 上限生效
        self.assertIn("超出 OCR 页数上限", text)
        self.assertIn("已省略", text)

    def test_engine_off(self):
        pdf = make_pdf(self._tmp(), text_pages=(), image_pages=1)
        eng = self._engine()
        kind, text = sv.extract_pdf(pdf, {"engine": "off"})
        self.assertEqual(eng.calls, [])
        self.assertEqual(kind, "PDF 文本（PyMuPDF）")
        self.assertIn("OCR 已关闭", text)

    def test_engine_unavailable_degrades_gracefully(self):
        """引擎不可用（未安装/初始化失败）→ 占位说明，绝不抛异常中断归档。"""
        pdf = make_pdf(self._tmp(), text_pages=(), image_pages=2)
        sv._RAPIDOCR_STATE["engine"] = None
        sv._RAPIDOCR_STATE["error"] = "未安装 rapidocr（pip install rapidocr）"
        kind, text = sv.extract_pdf(pdf, {})
        self.assertEqual(kind, "PDF 文本（PyMuPDF）")             # 无 OCR 成功页
        self.assertEqual(text.count("未安装 rapidocr"), 2)         # 每个扫描页如实注明
        self.assertIn("请打开原文件查看", text)

    def test_dispatch_attachment_pdf_kind(self):
        """dispatch_attachment 的 .pdf 分支透传 ocr 配置并返回动态 kind。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pdf = make_pdf(Path(d) / "扫描笔记.pdf", text_pages=(), image_pages=1)
            eng = self._engine()
            cfg = {"vision": {}, "whisper": {}, "ocr": {"engine": "rapidocr"},
                   "limits": {"attachment_max_chars": 1000}}
            kind, text = sv.dispatch_attachment(pdf, cfg)
            self.assertEqual(len(eng.calls), 1)
            self.assertIn("RapidOCR", kind)
            self.assertIn("手写OCR", text)


if __name__ == "__main__":
    unittest.main()
