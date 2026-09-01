# -*- coding: utf-8 -*-
"""v1.10.0 RapidOCR 输入归一化测试：宽图 LANCZOS 降采样（默认 800px 甜点），窄图/关闭透传。

不加载真实模型——直接测 _rapidocr_normalize/_rapidocr_max_width 的变换行为，并经
FakeEngine 验证 extract_image（图像附件路径）与 extract_pdf（扫描页路径）两端接入；
真实识别效果由 E2E 手工验证。
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_daemon as sv  # noqa: E402


class FakeEngine:
    """记录输入的假 RapidOCR 引擎（不校验类型——由各测试自行断言）。"""

    def __init__(self, texts=("归一化文本一", "归一化文本二")):
        self.calls = []
        self._texts = texts

    def __call__(self, img):
        self.calls.append(img)
        return SimpleNamespace(txts=self._texts)


def make_png(path: Path, width: int, height: int, color=(255, 255, 255)) -> Path:
    from PIL import Image
    Image.new("RGB", (width, height), color).save(path)
    return path


def png_bytes(width: int, height: int, color=(255, 255, 255)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _ndarray(img):
    import numpy as np
    assert isinstance(img, np.ndarray), f"应为 ndarray，实为 {type(img)}"
    return img


class TestRapidOCRMaxWidth(unittest.TestCase):
    """配置读取与容错：默认 800；<=0 关闭；非法值回落默认。"""

    def test_default(self):
        self.assertEqual(sv._rapidocr_max_width({}), 800)
        self.assertEqual(sv._rapidocr_max_width(None), 800)
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": 800}), 800)

    def test_custom_and_disable(self):
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": 600}), 600)
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": "1000"}), 1000)
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": 0}), 0)
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": -5}), 0)


class TestRapidOCRNormalize(unittest.TestCase):
    """_rapidocr_normalize 变换行为：降采样/透传/BGR/宽高比/容错。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.dir = d

    def test_wide_path_downscaled_to_800(self):
        import numpy as np
        p = make_png(self.dir / "宽图.png", 1200, 300)
        arr = _ndarray(sv._rapidocr_normalize(str(p), 800))
        self.assertEqual(arr.shape, (200, 800, 3))       # 1200x300 → 800x200
        self.assertEqual(arr.dtype, np.uint8)

    def test_narrow_path_passthrough(self):
        p = make_png(self.dir / "窄图.png", 60, 30)
        self.assertEqual(sv._rapidocr_normalize(str(p), 800), str(p))  # 无需降采样：透传

    def test_disabled_passthrough(self):
        p = make_png(self.dir / "宽图.png", 1200, 300)
        self.assertEqual(sv._rapidocr_normalize(str(p), 0), str(p))    # 0 = 关闭

    def test_custom_width_and_aspect(self):
        p = make_png(self.dir / "比例图.png", 1600, 800)
        arr = _ndarray(sv._rapidocr_normalize(str(p), 1000))
        self.assertEqual(arr.shape, (500, 1000, 3))      # 宽高比保持 2:1

    def test_rgb_to_bgr(self):
        """纯红 RGB(255,0,0) → ndarray 应为 BGR：B 通道低、R 通道高。"""
        p = make_png(self.dir / "红图.png", 900, 100, color=(255, 0, 0))
        arr = _ndarray(sv._rapidocr_normalize(str(p), 800))
        self.assertEqual(arr[..., 0].max(), 0)           # B
        self.assertEqual(arr[..., 2].min(), 255)         # R

    def test_bytes_wide_downscaled(self):
        """PDF 扫描页路径：PNG 字节流同样归一化（842pt 页 @200dpi ≈ 2339px 宽）。"""
        arr = _ndarray(sv._rapidocr_normalize(png_bytes(2339, 1653), 800))
        self.assertEqual(arr.shape, (565, 800, 3))       # 1653×800/2339 ≈ 565

    def test_bytes_small_passthrough(self):
        data = png_bytes(60, 30)
        self.assertIs(sv._rapidocr_normalize(data, 800), data)

    def test_corrupt_input_passthrough(self):
        """解码失败不抛异常——透传原输入交引擎按原逻辑报错。"""
        bad = b"not-an-image-at-all"
        self.assertIs(sv._rapidocr_normalize(bad, 800), bad)


class TestNormalizeIntegration(unittest.TestCase):
    """经 FakeEngine 验证两端接入：extract_image（图像附件）与 extract_pdf（扫描页）。"""

    def setUp(self):
        self._saved = dict(sv._RAPIDOCR_STATE)
        sv._RAPIDOCR_STATE.clear()
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.dir = d

    def tearDown(self):
        sv._RAPIDOCR_STATE.clear()
        sv._RAPIDOCR_STATE.update(self._saved)

    def _engine(self):
        eng = FakeEngine()
        sv._RAPIDOCR_STATE["engine"] = eng
        return eng

    def _scan_pdf(self, name="扫描笔记.pdf") -> Path:
        import fitz
        img_png = make_png(self.dir / "__scan.png", 842, 595)
        pdf = self.dir / name
        doc = fitz.open()
        doc.new_page().insert_image(doc[-1].rect, filename=str(img_png))
        doc.save(str(pdf))
        doc.close()
        img_png.unlink()
        return pdf

    def test_extract_image_rapidocr_mode_wide_normalized(self):
        """图像附件 rapidocr 模式：宽图降采样为 800px ndarray 后入引擎。"""
        eng = self._engine()
        png = make_png(self.dir / "手写便签.png", 1600, 400)
        cfg = {"vision": {}, "ocr": {"image_engine": "rapidocr"},
               "limits": {"attachment_max_chars": 1000}}
        kind, text = sv.extract_image(png, cfg)
        self.assertEqual(len(eng.calls), 1)
        arr = _ndarray(eng.calls[0])
        self.assertEqual(arr.shape, (200, 800, 3))
        self.assertEqual(kind, "图像 OCR（RapidOCR）")
        self.assertIn("归一化文本一", text)

    def test_extract_image_auto_fallback_wide_normalized(self):
        """auto 兜底链：Vision 无结果时 RapidOCR 同样吃归一化输入。"""
        eng = self._engine()
        png = make_png(self.dir / "潦草手写.png", 1200, 300)
        cfg = {"vision": {}, "ocr": {}, "limits": {"attachment_max_chars": 1000}}
        with mock.patch.object(sv, "_vision_image", return_value=""):
            kind, text = sv.extract_image(png, cfg)
        self.assertEqual(kind, "图像 OCR（Vision→RapidOCR 兜底）")
        self.assertEqual(_ndarray(eng.calls[0]).shape[1], 800)

    def test_extract_image_normalization_disabled(self):
        """rapidocr_max_width=0：宽图原样透传路径字符串。"""
        eng = self._engine()
        png = make_png(self.dir / "宽图关闭归一化.png", 1600, 400)
        cfg = {"vision": {}, "ocr": {"image_engine": "rapidocr", "rapidocr_max_width": 0},
               "limits": {"attachment_max_chars": 1000}}
        sv.extract_image(png, cfg)
        self.assertEqual(eng.calls, [str(png)])

    def test_extract_pdf_scan_page_normalized(self):
        """PDF 扫描页：842x595pt 页 @200dpi 渲染 2339px → 归一化 800px ndarray。"""
        eng = self._engine()
        pdf = self._scan_pdf()
        kind, text = sv.extract_pdf(pdf, {})
        self.assertEqual(len(eng.calls), 1)
        self.assertEqual(_ndarray(eng.calls[0]).shape[1], 800)
        self.assertIn("手写 1 页", kind)
        self.assertIn("[第 1 页｜手写OCR]", text)

    def test_extract_pdf_normalization_disabled(self):
        """关闭归一化：扫描页按渲染 PNG 字节流原样透传（v1.8.0 行为）。"""
        eng = self._engine()
        pdf = self._scan_pdf("关闭归一化.pdf")
        sv.extract_pdf(pdf, {"rapidocr_max_width": 0})
        self.assertEqual(len(eng.calls), 1)
        self.assertIsInstance(eng.calls[0], (bytes, bytearray))
        self.assertEqual(bytes(eng.calls[0][:4]), b"\x89PNG")


if __name__ == "__main__":
    unittest.main()


    def test_invalid_falls_back(self):
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": "abc"}), 800)
        self.assertEqual(sv._rapidocr_max_width({"rapidocr_max_width": None}), 800)
