#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""menu_bar_app.recent_errors 增量扫描的单元测试。

与 test_pure_functions.py 不同，本文件需要导入 menu_bar_app（依赖 rumps/requests，
请在项目 .venv 下运行）：python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import menu_bar_app as m  # noqa: E402


class RecentErrorsTests(unittest.TestCase):
    """logs 目录与错误状态文件均重定向到临时目录，互不污染真实日志。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name)
        self.state_path = self.log_dir / ".menubar_err_state.json"
        for name, target in (("LOG_DIR", self.log_dir),
                             ("_ERR_STATE_PATH", self.state_path)):
            patcher = mock.patch.object(m, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, text: str):
        (self.log_dir / name).write_text(text, encoding="utf-8")

    def _append(self, name: str, text: str):
        with (self.log_dir / name).open("a", encoding="utf-8") as f:
            f.write(text)

    def test_first_run_clean_slate(self):
        """首次运行（无状态文件）：历史错误清零，不报告。"""
        self._write("rag.stderr.log", "Traceback (most recent call last):\nboom\n")
        self.assertEqual(m.recent_errors(), [])

    def test_new_error_reported_once(self):
        """新增错误报一次；消费后不再报。"""
        self._write("a.log", "INFO: ok\n")
        self.assertEqual(m.recent_errors(), [])  # 建立基线
        self._append("a.log", "2026-08-30 17:00:00 [ERROR] boom\n")
        errs = m.recent_errors()
        self.assertEqual(len(errs), 1)
        self.assertIn("[a.log]", errs[0])
        self.assertIn("boom", errs[0])
        self.assertEqual(m.recent_errors(), [])  # 已消费

    def test_peek_does_not_consume(self):
        """consume=False 只看不消费（综合健康检查用）。"""
        self._write("a.log", "x\n")
        m.recent_errors()
        self._append("a.log", "Traceback (most recent call last):\n")
        self.assertEqual(len(m.recent_errors(consume=False)), 1)
        self.assertEqual(len(m.recent_errors(consume=False)), 1)  # 仍然在
        self.assertEqual(len(m.recent_errors()), 1)  # 消费
        self.assertEqual(m.recent_errors(), [])

    def test_partial_line_deferred(self):
        """末尾不完整的行（正被写入）留到下次。"""
        self._write("a.log", "x\n")
        m.recent_errors()
        with (self.log_dir / "a.log").open("a", encoding="utf-8") as f:
            f.write("[ERROR] half")  # 无换行符
        self.assertEqual(m.recent_errors(), [])
        self._append("a.log", " line\n")  # 补全
        errs = m.recent_errors()
        self.assertEqual(len(errs), 1)
        self.assertIn("half line", errs[0])

    def test_truncated_log_reread(self):
        """日志被截断（size < 已记录偏移）：从头重读。"""
        self._write("a.log", "x" * 200 + "\n")  # 201 字节
        m.recent_errors()  # 记录偏移 201
        self._write("a.log", "[ERROR] truncated\n")  # 19 字节 < 201 → 截断
        errs = m.recent_errors()
        self.assertEqual(len(errs), 1)
        self.assertIn("truncated", errs[0])

    def test_new_file_scanned_whole(self):
        """新出现的日志文件：全文扫描（内容本来就是新的）。"""
        m.recent_errors()  # 基线（此时 new.log 不存在）
        self._write("new.log", "[ERROR] fresh file\n")
        self.assertEqual(len(m.recent_errors()), 1)

    def test_consecutive_dedup(self):
        """连续重复的错误行自动去重。"""
        self._write("a.log", "x\n")
        m.recent_errors()
        self._append("a.log", "Traceback (most recent call last):\n"
                               "Traceback (most recent call last):\n")
        self.assertEqual(len(m.recent_errors()), 1)

    def test_state_persisted_to_disk(self):
        """消费后状态（字节偏移）落盘，跨实例重启依然生效。"""
        self._write("a.log", "x\n")
        m.recent_errors()
        self.assertEqual(json.loads(self.state_path.read_text()), {"a.log": 2})


if __name__ == "__main__":
    unittest.main()
