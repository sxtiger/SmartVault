#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""log_dir 解析规则的单元测试（v1.7.1：日志默认迁至 ~/Library/Logs/SmartVault）。

覆盖 menu_bar_app._resolve_log_dir 与 ingest_daemon / rag_api 的 load_config：
绝对路径（含 ~ 前缀）expanduser 后直接使用；相对路径挂在 config.json 所在目录（向后兼容）。
请在项目 .venv 下运行：python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import menu_bar_app as m  # noqa: E402


class ResolveLogDirTests(unittest.TestCase):
    """menu_bar_app 的日志目录解析（读 config.json 的 log_dir）。"""

    def _with_cfg(self, cfg):
        patcher = mock.patch.object(m, "_read_config", lambda: cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_outside_tcc(self):
        """默认值必须落在 TCC 保护区之外（~/Library/Logs/SmartVault）。"""
        self._with_cfg({})
        self.assertEqual(m._resolve_log_dir(),
                         Path.home() / "Library" / "Logs" / "SmartVault")

    def test_explicit_tilde_expanded(self):
        """显式 ~ 路径：expanduser 展开为绝对路径。"""
        self._with_cfg({"log_dir": "~/Library/Logs/Custom"})
        self.assertEqual(m._resolve_log_dir(),
                         Path.home() / "Library" / "Logs" / "Custom")

    def test_relative_keeps_old_behavior(self):
        """相对路径：向后兼容，挂在项目根目录下。"""
        self._with_cfg({"log_dir": "logs"})
        self.assertEqual(m._resolve_log_dir(), m.PROJECT_DIR / "logs")


class DaemonLoadConfigLogDirTests(unittest.TestCase):
    """两个守护进程的 load_config：log_dir 解析规则一致。"""

    def _assert_log_dir_abs(self, module, cfg_text, expected):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(cfg_text, encoding="utf-8")
            self.assertEqual(module.load_config(cfg_path)["log_dir_abs"], expected)

    def test_ingest_daemon_tilde(self):
        import ingest_daemon
        self._assert_log_dir_abs(ingest_daemon, '{"log_dir": "~/Library/Logs/XYZ"}',
                                 str(Path.home() / "Library" / "Logs" / "XYZ"))

    def test_rag_api_tilde(self):
        import rag_api
        self._assert_log_dir_abs(rag_api, '{"log_dir": "~/Library/Logs/XYZ"}',
                                 str(Path.home() / "Library" / "Logs" / "XYZ"))

    def test_daemons_relative_backward_compat(self):
        """相对路径仍挂 config.json 所在目录（老配置无需迁移即可运行）。"""
        import ingest_daemon
        import rag_api
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text('{"log_dir": "logs"}', encoding="utf-8")
            expected = str((Path(td) / "logs").resolve())
            self.assertEqual(ingest_daemon.load_config(cfg_path)["log_dir_abs"], expected)
            self.assertEqual(rag_api.load_config(cfg_path)["log_dir_abs"], expected)


if __name__ == "__main__":
    unittest.main()
