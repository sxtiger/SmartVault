# -*- coding: utf-8 -*-
"""v1.9.0 图像 OCR 引擎路由测试：ocr.image_engine = vision / rapidocr / auto / off。

不加载真实模型与 ocrmac——mock _vision_image、向 ingest_daemon._RAPIDOCR_STATE 注入
fake 引擎，验证分发逻辑与降级路径；真实识别效果由 E2E 手工验证。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_daemon as sv  # noqa: E402


class FakeEngine:
    """假 RapidOCR 引擎：接受路径输入，返回固定文本，记录调用。"""

    def __init__(self, texts=("手写内容一", "手写内容二")):
        self.calls = []
        self._texts = texts

    def __call__(self, img):
        self.calls.append(img)
        return SimpleNamespace(txts=self._texts)


def make_png(path: Path) -> Path:
    from PIL import Image
    Image.new("RGB", (60, 30), "white").save(path)
    return path


class TestExtractImageRouting(unittest.TestCase):
    """extract_image 三模式分发与 auto 兜底链。"""

    def setUp(self):
        self._saved = dict(sv._RAPIDOCR_STATE)
        sv._RAPIDOCR_STATE.clear()
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.png = make_png(d / "手写便签.png")

    def tearDown(self):
        sv._RAPIDOCR_STATE.clear()
        sv._RAPIDOCR_STATE.update(self._saved)

    def _engine(self, **kw):
        eng = FakeEngine(**kw)
        sv._RAPIDOCR_STATE["engine"] = eng
        return eng

    def _cfg(self, image_engine=None, ocr_extra=None):
        ocr = {} if image_engine is None else {"image_engine": image_engine}
        ocr.update(ocr_extra or {})
        return {"vision": {}, "ocr": ocr, "limits": {"attachment_max_chars": 1000}}

    def test_vision_mode_uses_vision_only(self):
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image", return_value="印刷体文本") as vis:
            kind, text = sv.extract_image(self.png, self._cfg("vision"))
        self.assertEqual(vis.call_count, 1)
        self.assertEqual(eng.calls, [])                    # RapidOCR 不参与
        self.assertEqual(kind, "图像 OCR（Vision）")
        self.assertEqual(text, "印刷体文本")

    def test_rapidocr_mode(self):
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image") as vis:
            kind, text = sv.extract_image(self.png, self._cfg("rapidocr"))
        self.assertEqual(vis.call_count, 0)
        self.assertEqual(eng.calls, [str(self.png)])
        self.assertEqual(kind, "图像 OCR（RapidOCR）")
        self.assertIn("手写内容一", text)

    def test_auto_vision_hit_skips_rapidocr(self):
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image", return_value="规整手写文本"):
            kind, text = sv.extract_image(self.png, self._cfg())   # 默认 auto
        self.assertEqual(eng.calls, [])
        self.assertEqual(kind, "图像 OCR（Vision）")
        self.assertEqual(text, "规整手写文本")

    def test_auto_vision_empty_falls_back_to_rapidocr(self):
        """潦草手写 Vision 识别为空 → RapidOCR 兜底（v1.9.0 核心场景）。"""
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image", return_value=""):
            kind, text = sv.extract_image(self.png, self._cfg())
        self.assertEqual(eng.calls, [str(self.png)])
        self.assertEqual(kind, "图像 OCR（Vision→RapidOCR 兜底）")
        self.assertIn("手写内容一", text)

    def test_auto_vision_error_falls_back(self):
        """Vision 抛异常（如 HEIC 解码失败）→ auto 模式交 RapidOCR 救场。"""
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image", side_effect=RuntimeError("解码失败")):
            kind, text = sv.extract_image(self.png, self._cfg())
        self.assertEqual(eng.calls, [str(self.png)])
        self.assertIn("手写内容一", text)

    def test_auto_both_empty_placeholder(self):
        eng = self._engine(texts=())
        with mock.patch.object(sv, "_vision_image", return_value=""):
            kind, text = sv.extract_image(self.png, self._cfg())
        self.assertEqual(eng.calls, [str(self.png)])
        self.assertIn("均未识别到文字", text)

    def test_rapidocr_unavailable_placeholder(self):
        """引擎不可用（未安装）→ 如实占位，绝不抛异常中断归档。"""
        sv._RAPIDOCR_STATE["engine"] = None
        sv._RAPIDOCR_STATE["error"] = "未安装 rapidocr（pip install rapidocr）"
        with mock.patch.object(sv, "_vision_image", return_value=""):
            kind, text = sv.extract_image(self.png, self._cfg())
        self.assertIn("未安装 rapidocr", text)
        self.assertIn("请打开原图片查看", text)

    def test_engine_off(self):
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image") as vis:
            kind, text = sv.extract_image(self.png, self._cfg("off"))
        self.assertEqual(vis.call_count, 0)
        self.assertEqual(eng.calls, [])
        self.assertIn("已关闭", text)

    def test_dispatch_routes_image_to_extract_image(self):
        """dispatch_attachment 的图像分支透传 ocr 配置并返回动态 kind。"""
        eng = self._engine()
        with mock.patch.object(sv, "_vision_image", return_value=""):
            kind, text = sv.dispatch_attachment(self.png, self._cfg())
        self.assertEqual(len(eng.calls), 1)
        self.assertIn("RapidOCR", kind)
        self.assertIn("手写内容一", text)


if __name__ == "__main__":
    unittest.main()
