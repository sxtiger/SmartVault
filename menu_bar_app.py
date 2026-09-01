#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SmartVault 菜单栏控制台（macOS 状态栏常驻应用，基于 rumps + PyObjC）。

功能：
- 服务启停 / 重启 / 卸载（launchd 管理：摄入守护进程 + RAG 问答服务）
- 本控制台自身的“开机自启”开关（com.user.aibrain.menubar 登录项）
- 综合健康检查（LM Studio / 嵌入模型 / 索引状态 / API 就绪）
- 最近错误分析（增量监测各日志新增的 ERROR / Traceback / 启动失败，历史旧错误不重复告警）
- 在 Terminal.app 实时跟踪日志

启动方式（任选其一）：
1. 双击 SmartVaultMenuBar.app（由 scripts/build_menubar_app.sh 生成）
2. 终端：.venv/bin/python menu_bar_app.py
3. 菜单内开启“开机自启”后，由 launchd 在登录时自动拉起

状态栏图标：● 全部运行 ｜ ◐ 部分运行 ｜ ○ 全部停止 ｜ ⚠ 异常（崩溃循环 / RAG 未就绪）
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
import rumps

PROJECT_DIR = Path(__file__).resolve().parent
UID_NUM = os.getuid()
LAUNCH_DOMAIN = f"gui/{UID_NUM}"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PYTHON_BIN = sys.executable
LM_STUDIO_PORT = 1234


def _read_config() -> Dict[str, Any]:
    try:
        return json.loads((PROJECT_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


RAG_PORT = int(_read_config().get("api", {}).get("port", 8788))


def _resolve_log_dir() -> Path:
    """日志目录（与 ingest_daemon / rag_api 的 load_config 同一解析规则）。

    macOS 26（Tahoe）起 TCC 禁止 launchd 打开 ~/Documents 下的 stdout/stderr 日志，
    作业在 exec 前即以 78 EX_CONFIG 秒退且不落任何日志（v1.7.1），
    故默认迁至 ~/Library/Logs/SmartVault（不受 TCC 保护）。
    """
    raw = str(_read_config().get("log_dir", "~/Library/Logs/SmartVault"))
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_DIR / p


LOG_DIR = _resolve_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)  # 手动直跑时目录可能不存在（launchd 场景由安装脚本预建）


class ServiceSpec:
    """一个 launchd 服务的描述：模板 → 安装到 LaunchAgents → 状态查询。"""

    def __init__(self, label: str, title: str, log_name: Optional[str], template: str):
        self.label = label
        self.title = title
        self.log_name = log_name          # 主日志文件名（LOG_DIR 下），None 表示无
        self.template = PROJECT_DIR / "launchd" / template

    @property
    def plist(self) -> Path:
        return AGENTS_DIR / f"{self.label}.plist"

    def render(self) -> Path:
        """从 launchd/ 模板生成本机 plist（替换 @PROJECT_DIR@ / @PYTHON@ / @LOG_DIR@ 占位符）。"""
        text = self.template.read_text(encoding="utf-8")
        text = (text.replace("@PROJECT_DIR@", str(PROJECT_DIR))
                    .replace("@PYTHON@", PYTHON_BIN)
                    .replace("@LOG_DIR@", str(LOG_DIR)))
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text(text, encoding="utf-8")
        return self.plist


INGEST = ServiceSpec("com.user.aibrain", "摄入守护进程", "ingest_daemon.log",
                     "com.user.aibrain.plist")
RAG = ServiceSpec("com.user.aibrain.rag", "RAG 问答服务", "rag_api.log",
                  "com.user.aibrain.rag.plist")
MENUBAR = ServiceSpec("com.user.aibrain.menubar", "菜单栏控制台", None,
                      "com.user.aibrain.menubar.plist")

# ================================================================ launchd 封装
def _launchctl(*args: str, timeout: float = 15.0) -> Tuple[int, str]:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def svc_info(svc: ServiceSpec) -> Dict[str, Any]:
    """查询服务状态：{installed, pid, last_status}。

    installed=False 表示已 bootout（停止且不自启）；
    installed=True 且 pid=None 时，last_status 非零 = 崩溃循环，零 = 刚加载。
    """
    if _launchctl("print", f"{LAUNCH_DOMAIN}/{svc.label}")[0] != 0:
        return {"installed": False, "pid": None, "last_status": 0}
    _, out = _launchctl("list")
    pid, status = None, 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] == svc.label:
            pid = int(parts[0]) if parts[0].isdigit() else None
            try:
                status = int(parts[1])
            except ValueError:
                status = 0
            break
    return {"installed": True, "pid": pid, "last_status": status}


def svc_state(svc: ServiceSpec) -> str:
    """返回 running / starting / crashed / stopped。"""
    info = svc_info(svc)
    if not info["installed"]:
        return "stopped"
    if info["pid"]:
        return "running"
    return "crashed" if info["last_status"] != 0 else "starting"


def _wait(cond: Callable[[], bool], seconds: float = 10.0) -> bool:
    """轮询等待条件成立（launchctl bootout 是异步的，必须等作业真正消失）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.3)
    return cond()


def svc_start(svc: ServiceSpec) -> Tuple[bool, str]:
    """安装并启动（bootstrap）；已在正常运行则直接返回（幂等，不打断服务）。

    停止 / 崩溃循环 / 刚加载未起进程时：渲染 plist → bootout → 等待拆除
    → bootstrap×5 重试（复用 v1.0.1 竞态修复）。
    """
    if not svc.template.exists():
        return False, f"缺少模板：{svc.template}"
    if svc_state(svc) == "running":
        return True, "已在运行（未重启；如需应用新配置请用「重启」）"
    try:
        svc.render()
    except Exception as e:
        return False, f"生成 plist 失败：{e}"
    if svc_info(svc)["installed"]:
        _launchctl("bootout", f"{LAUNCH_DOMAIN}/{svc.label}")
        _wait(lambda: not svc_info(svc)["installed"])
    last = ""
    for _ in range(5):  # bootstrap 偶发 EIO(5)，重试
        rc, last = _launchctl("bootstrap", LAUNCH_DOMAIN, str(svc.plist))
        if rc == 0:
            return True, "已启动（KeepAlive 常驻，崩溃自动拉起）"
        time.sleep(1)
    return False, last or "launchctl bootstrap 失败（详见日志文件夹）"


def svc_stop(svc: ServiceSpec) -> Tuple[bool, str]:
    """停止 = bootout：进程退出、KeepAlive 失效，但 plist 保留，随时可再启动。"""
    if not svc_info(svc)["installed"]:
        return True, "本就未在运行"
    _launchctl("bootout", f"{LAUNCH_DOMAIN}/{svc.label}")
    gone = _wait(lambda: not svc_info(svc)["installed"])
    if gone:
        return True, "已停止（开机自启同步失效；再次“启动”即恢复）"
    return False, "bootout 超时，请稍后重试"


def svc_uninstall(svc: ServiceSpec) -> Tuple[bool, str]:
    """彻底卸载：停止 + 删除 ~/Library/LaunchAgents 下的 plist。"""
    ok, msg = svc_stop(svc)
    try:
        svc.plist.unlink(missing_ok=True)
    except OSError as e:
        return False, f"{msg}；删除 plist 失败：{e}"
    return True, f"{msg}；已移除 {svc.plist.name}"


# ================================================================ 诊断工具
def port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(path: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(f"http://127.0.0.1:{RAG_PORT}{path}", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def http_post_json(path: str, payload: Dict[str, Any],
                   timeout: float = 5.0) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """POST JSON 并返回 (是否成功, 响应体)；用于 /reindex 等触发型接口。"""
    try:
        r = requests.post(f"http://127.0.0.1:{RAG_PORT}{path}", json=payload, timeout=timeout)
        if r.status_code != 200:
            return False, None
        try:
            return True, r.json()
        except ValueError:
            return True, {}
    except Exception:
        return False, None


_ERROR_PAT = re.compile(
    r"\[ERROR\]|\[CRITICAL\]|Traceback|Bootstrap failed|ModuleNotFoundError"
    r"|FileNotFoundError|Address already in use|CUDA|core dump"
)


_ERR_STATE_PATH = LOG_DIR / ".menubar_err_state.json"


def recent_errors(limit: int = 8, consume: bool = True) -> List[str]:
    """增量扫描各日志「自上次检查以来新增」的错误行（连续重复自动去重）。

    日志是 append-only 且从不轮转，历史错误（如已修复版本的崩溃循环）会
    永久留在文件里——扫描“尾部 N 行”会把陈年旧账当作“最近错误”反复展示。
    改为按字节偏移增量读取（状态持久化到 LOG_DIR/.menubar_err_state.json）：
    - 首次运行（无状态文件）：全部从当前 EOF 起算（清零历史）
    - 新出现的日志文件：从头全量扫描（内容本来就是新的）
    - 文件被截断/轮转（size < 已记录偏移）：从头重读
    - 末尾不完整的行（正被写入）：留到下次
    consume=False 仅查看不更新状态（供综合健康检查调用，不抢走本菜单的新错误）。
    """
    try:
        first_run = not _ERR_STATE_PATH.exists()
        state: Dict[str, Any] = {}
        if not first_run:
            state = json.loads(_ERR_STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
                first_run = True  # 状态文件损坏：视为首跑清零
    except Exception:
        state = {}
        first_run = True
    hits: List[str] = []
    new_state: Dict[str, int] = {}
    for log in sorted(set(LOG_DIR.glob("*.log")) | set(LOG_DIR.glob("*.log.*"))):
        try:
            size = log.stat().st_size
        except OSError:
            continue
        if first_run:
            offset = size
        elif isinstance(state.get(log.name), int):
            offset = state[log.name]
        else:
            offset = 0
        if size < offset:  # 截断/轮转：从头重读
            offset = 0
        data = b""
        if size > offset:
            try:
                with log.open("rb") as f:
                    f.seek(offset)
                    data = f.read()
            except OSError:
                continue
        if data and not data.endswith(b"\n"):  # 末尾半行留到下次
            nl = data.rfind(b"\n")
            data = data[: nl + 1] if nl >= 0 else b""
        new_state[log.name] = offset + len(data)
        for line in data.decode("utf-8", errors="ignore").splitlines():
            if _ERROR_PAT.search(line):
                hits.append(f"[{log.name}] {line.strip()[:180]}")
    if consume:
        try:
            _ERR_STATE_PATH.write_text(json.dumps(new_state), encoding="utf-8")
        except OSError:
            pass
    dedup: List[str] = []
    for h in hits:
        tail = h.split("] ", 1)[-1]
        if not dedup or dedup[-1].split("] ", 1)[-1] != tail:
            dedup.append(h)
    return dedup[-limit:]


def tail_in_terminal(log_path: Path) -> None:
    """在 Terminal.app 新窗口执行 tail -f（首次会请求“自动化”权限，请允许）。"""
    cmd = f'tail -n 100 -f "{log_path}"'
    subprocess.Popen(
        ["osascript", "-e", f'tell application "Terminal" to do script {json.dumps(cmd)}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def open_in_finder(path: Path) -> None:
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ================================================================ 单例锁（防双开）
_LOCK_FILE = None


def _acquire_single_instance_lock() -> bool:
    import fcntl
    global _LOCK_FILE
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE = open(LOG_DIR / ".menubar.lock", "w")
    try:
        fcntl.flock(_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

# ================================================================ 菜单栏应用
_STATE_TEXT = {
    "running": "● 运行中",
    "starting": "… 启动中",
    "crashed": "⚠ 反复崩溃",
    "stopped": "○ 已停止",
}


class SmartVaultBar(rumps.App):
    def __init__(self):
        super().__init__(name="SmartVault", title="○", quit_button=None)
        self._menu_key: Optional[tuple] = None
        self.refresh(force=True)

    # ---------------- 状态刷新（仅状态变化时重建菜单，避免闪烁） ----------------
    def refresh(self, force: bool = False) -> None:
        ing, rag = svc_state(INGEST), svc_state(RAG)
        key = (ing, rag, port_open(RAG_PORT), svc_info(MENUBAR)["installed"])
        if force or key != self._menu_key:
            self._menu_key = key
            self.title = self._title_symbol(ing, rag)
            self._rebuild_menu(ing, rag)

    @staticmethod
    def _title_symbol(ing: str, rag: str) -> str:
        if ing == "crashed" or rag == "crashed":
            return "⚠"
        if ing == "running" and rag == "running":
            return "●" if port_open(RAG_PORT) else "⚠"  # 进程在但 API 未就绪（模型加载中）
        if ing == "stopped" and rag == "stopped":
            return "○"
        return "◐"

    # ---------------- 菜单构建 ----------------
    def _rebuild_menu(self, ing: str, rag: str) -> None:
        port_ok = port_open(RAG_PORT)
        header = f"摄入 {_STATE_TEXT[ing]} ｜ RAG {_STATE_TEXT[rag]}（:{RAG_PORT} {'✓' if port_ok else '…'}）"
        menu: List[Any] = [
            rumps.MenuItem(header),
            rumps.separator,
            rumps.MenuItem("▶ 启动全部服务", callback=self._start_all),
            rumps.MenuItem("⏹ 停止全部服务", callback=self._stop_all),
            rumps.MenuItem("🔄 重启全部服务", callback=self._restart_all),
            rumps.separator,
        ]
        menu.append(self._svc_submenu(INGEST, ing))
        menu.append(self._svc_submenu(RAG, rag))
        menu += [
            rumps.separator,
            rumps.MenuItem("🧹 清理已删笔记残留（同步索引）", callback=self._sync_index),
            rumps.MenuItem("♻️ 重建 RAG 索引（清空后重建）…", callback=self._rebuild_index),
            rumps.separator,
            rumps.MenuItem("🔍 综合健康检查…", callback=self._full_check),
            rumps.MenuItem("⚠️ 最近错误分析…", callback=self._show_errors),
            rumps.MenuItem("🛠 打开日志文件夹", callback=lambda _: open_in_finder(LOG_DIR)),
            rumps.separator,
        ]
        auto = rumps.MenuItem("🖥 开机自启：菜单栏控制台", callback=self._toggle_autostart)
        auto.state = svc_info(MENUBAR)["installed"]
        menu.append(auto)
        menu += [
            rumps.MenuItem("📁 打开项目文件夹", callback=lambda _: open_in_finder(PROJECT_DIR)),
            rumps.MenuItem("🧹 卸载全部 SmartVault 服务…", callback=self._uninstall_all),
            rumps.separator,
            rumps.MenuItem("退出菜单栏控制台", callback=self._quit),
        ]
        self.menu.clear()
        for item in menu:
            self.menu.add(item)

    def _svc_submenu(self, svc: ServiceSpec, state: str) -> rumps.MenuItem:
        title = f"{svc.title}（{_STATE_TEXT[state]}）"
        if state == "crashed":
            title += f"，退出码 {svc_info(svc)['last_status']}"
        sub = rumps.MenuItem(title)
        if state == "running":
            sub.add(rumps.MenuItem("⏹ 停止", callback=lambda _, s=svc: self._start_stop(s, stop=True)))
        else:
            sub.add(rumps.MenuItem("▶ 启动", callback=lambda _, s=svc: self._start_stop(s, stop=False)))
        sub.add(rumps.MenuItem("🔄 重启", callback=lambda _, s=svc: self._restart(s)))
        sub.add(rumps.MenuItem("🧹 卸载（停止并移除开机自启）", callback=lambda _, s=svc: self._uninstall(s)))
        if svc.log_name:
            sub.add(rumps.MenuItem("📜 实时日志（Terminal）", callback=lambda _, s=svc: self._tail_log(s)))
        return sub

    # ---------------- 回调 ----------------
    @staticmethod
    def _notify(title: str, ok: bool, msg: str) -> None:
        rumps.alert(title, ("✔ " if ok else "✘ ") + msg)

    def _start_stop(self, svc: ServiceSpec, stop: bool):
        ok, msg = svc_stop(svc) if stop else svc_start(svc)
        if not stop and svc is RAG and "已在运行" not in msg:
            msg += "\n（加载模型约需 10–30 秒，期间图标 ⚠ 属正常）"
        self._notify(("停止" if stop else "启动") + f" {svc.title}", ok, msg)
        self.refresh(force=True)

    def _restart(self, svc: ServiceSpec):
        svc_stop(svc)
        ok, msg = svc_start(svc)
        if svc is RAG:
            msg += "\n（加载模型约需 10–30 秒，期间图标 ⚠ 属正常）"
        self._notify(f"重启 {svc.title}", ok, msg)
        self.refresh(force=True)

    def _uninstall(self, svc: ServiceSpec):
        ok, msg = svc_uninstall(svc)
        self._notify(f"卸载 {svc.title}", ok, msg)
        self.refresh(force=True)

    def _start_all(self, _):
        msgs, changed = [], False
        for s in (INGEST, RAG):
            ok, msg = svc_start(s)
            changed = changed or "已在运行" not in msg
            msgs.append(f"{s.title}：{'✔' if ok else '✘'} {msg}")
        if changed:
            msgs.append("\n提示：RAG 加载模型约需 10–30 秒，期间图标显示 ⚠/◐ 属正常，就绪后自动变 ●。")
        rumps.alert("启动全部服务", "\n".join(msgs))
        self.refresh(force=True)

    def _stop_all(self, _):
        msgs = []
        for s in (INGEST, RAG):
            ok, msg = svc_stop(s)
            msgs.append(f"{s.title}：{'✔' if ok else '✘'} {msg}")
        rumps.alert("停止全部服务", "\n".join(msgs))
        self.refresh(force=True)

    def _restart_all(self, _):
        for s in (INGEST, RAG):
            svc_stop(s)
        msgs = []
        for s in (INGEST, RAG):
            ok, msg = svc_start(s)
            msgs.append(f"{s.title}：{'✔' if ok else '✘'} {msg}")
        msgs.append("\n提示：RAG 加载模型约需 10–30 秒，期间图标显示 ⚠/◐ 属正常，就绪后自动变 ●。")
        rumps.alert("重启全部服务", "\n".join(msgs))
        self.refresh(force=True)

    def _tail_log(self, svc: ServiceSpec):
        p = LOG_DIR / svc.log_name
        if p.exists():
            tail_in_terminal(p)
        else:
            rumps.alert("暂无日志", f"未找到 {p}")

    def _sync_index(self, _):
        """RAG 增量同步：移除已删笔记的向量块 + 清理 ai_context.md 失效条目。"""
        if not port_open(RAG_PORT):
            self._notify("清理已删笔记残留", False,
                         f"RAG 服务未在运行（端口 {RAG_PORT} 未监听），请先启动服务。")
            return
        ok, resp = http_post_json("/reindex", {"rebuild": False})
        if not ok or not (resp or {}).get("started"):
            self._notify("清理已删笔记残留", False,
                         "请求 /reindex 失败，请用「最近错误分析」或 RAG 实时日志排查。")
            return
        self._notify("清理已删笔记残留", True,
                     "增量同步已在后台执行：已删除笔记的向量块与 ai_context.md\n"
                     "失效归档条目将被移除，现存笔记不受影响。\n\n"
                     "（后台每 5 分钟也会自动同步一次；剔除明细见 RAG 服务日志）")

    def _rebuild_index(self, _):
        """RAG 全量重建：先清空整个向量库，再按当前仓库实际文件重建。"""
        if not port_open(RAG_PORT):
            self._notify("重建 RAG 索引", False,
                         f"RAG 服务未在运行（端口 {RAG_PORT} 未监听），请先启动服务。")
            return
        w = rumps.Window(
            title="重建 RAG 索引",
            message="将清空整个向量库，再按当前仓库内实际存在的笔记全量重建。\n\n"
                    "适用：清空/大批量删除测试笔记后让索引归零、\n"
                    "修改分块参数后重建。\n\n"
                    "现存笔记会重新索引；完成前问答可能不完整，\n"
                    "耗时取决于笔记量。确认请点 OK。",
            dimensions=(560, 280))
        if not w.run().clicked:
            return
        ok, resp = http_post_json("/reindex", {"rebuild": True})
        if not ok or not (resp or {}).get("started"):
            self._notify("重建 RAG 索引", False,
                         "请求 /reindex 失败，请用「最近错误分析」或 RAG 实时日志排查。")
            return
        self._notify("重建 RAG 索引", True,
                     "全量重建已启动：向量库已清空并开始按当前仓库重建。\n"
                     "若仓库已清空，完成后索引即为 0 条\n"
                     "（可用「综合健康检查」查看 chunks 数）。")

    def _am_launchd_instance(self) -> bool:
        """当前控制台进程是否由 launchd 作业 MENUBAR 运行（bootout 会终止自身）。"""
        info = svc_info(MENUBAR)
        return info["installed"] and info["pid"] == os.getpid()

    def _toggle_autostart(self, _):
        if svc_info(MENUBAR)["installed"]:
            if self._am_launchd_instance():
                w = rumps.Window(
                    title="关闭开机自启",
                    message="将移除登录项 com.user.aibrain.menubar。\n"
                            "注意：当前控制台正由该 launchd 项运行，关闭后控制台\n"
                            "将一并退出（菜单栏图标消失；终端手动启动的实例不受影响）。\n"
                            "确认请点 OK。",
                    dimensions=(540, 240))
                if not w.run().clicked:
                    return
            ok, msg = svc_uninstall(MENUBAR)
            if not self._am_launchd_instance():  # 手动实例：bootout 不影响自身，可弹结果
                self._notify("关闭开机自启", ok, msg)
        else:
            ok, msg = svc_start(MENUBAR)
            extra = "；已注册登录项，下次登录自动启动（当前实例继续运行）" if ok else ""
            self._notify("开启开机自启", ok, msg + extra)
        self.refresh(force=True)

    def _show_errors(self, _):
        errs = recent_errors()
        if not errs:
            rumps.alert("最近错误分析",
                        "✔ 自上次检查以来无新增 ERROR / Traceback / 启动失败。\n\n"
                        "（增量监测：历史日志中的旧错误不会重复告警；\n"
                        "如需回看完整历史，请用「打开日志文件夹」）")
            return
        body = "\n".join(errs) + "\n\n—— 完整上下文请用“实时日志（Terminal）”或“打开日志文件夹”查看。"
        rumps.Window(title=f"新增错误（{len(errs)} 条，旧→新）", message=body,
                     dimensions=(640, 420)).run()

    def _full_check(self, _):
        lines: List[str] = []

        def mark(ok: bool, text: str) -> None:
            lines.append(("✔ " if ok else "✘ ") + text)

        cfg = _read_config()
        mark(bool(cfg), "config.json 读取" + ("" if cfg else "（缺失或非法 JSON！）"))
        for v in cfg.get("vaults", []):
            mark(Path(str(v.get("path", ""))).is_dir(), f"Vault「{v.get('name')}」路径有效")
        emb = cfg.get("rag", {}).get("embedding_model_path", "models/bge-small-zh-v1.5")
        mark((PROJECT_DIR / emb / "config.json").is_file(), f"本地嵌入模型存在（{emb}）")
        mark(port_open(LM_STUDIO_PORT), f"LM Studio 端口 {LM_STUDIO_PORT} 可达（服务需已启动）")
        ing, rag = svc_info(INGEST), svc_info(RAG)
        mark(bool(ing["pid"]), f"摄入守护进程（PID {ing['pid'] or '—'}，上次退出码 {ing['last_status']}）")
        mark(bool(rag["pid"]), f"RAG 服务（PID {rag['pid'] or '—'}，上次退出码 {rag['last_status']}）")
        mark(port_open(RAG_PORT), f"RAG API 端口 {RAG_PORT} 已监听")
        h = http_json("/health")
        if h:
            mark(h.get("status") == "ok",
                 f"/health：lm_studio={h.get('lm_studio')}，embeddings={h.get('embeddings')}")
        else:
            mark(False, "/health 无响应（模型加载中或服务异常，稍后重试）")
        s = http_json("/status")
        if s:
            lines.append(f"ℹ 已索引 {s.get('files_indexed')} 篇笔记 / {s.get('chunks')} 个分块")
        errs = recent_errors(consume=False)
        mark(not errs, "自上次「最近错误分析」以来无新增错误" if not errs
             else f"发现 {len(errs)} 条新增错误（见“最近错误分析”）")
        rumps.Window(title="SmartVault 综合健康检查", message="\n".join(lines),
                     dimensions=(640, 460)).run()

    def _uninstall_all(self, _):
        w = rumps.Window(
            title="卸载全部 SmartVault 服务",
            message="将停止并移除以下 launchd 登录项：\n"
                    "  • 摄入守护进程（com.user.aibrain）\n"
                    "  • RAG 问答服务（com.user.aibrain.rag）\n"
                    "  • 菜单栏控制台开机自启（com.user.aibrain.menubar）\n\n"
                    "项目代码与数据不受影响，之后可随时用本菜单或 install_launchd.sh 重新安装。\n"
                    "确认请点 OK。",
            dimensions=(560, 300))
        if not w.run().clicked:
            return
        msgs = []
        for svc in (INGEST, RAG):
            ok, msg = svc_uninstall(svc)
            msgs.append(f"{svc.title}：{'✔' if ok else '✘'} {msg}")
        msgs.append("点 OK 后将移除控制台的开机自启；若本控制台由 launchd 运行会随之退出，"
                    "手动启动的实例则继续运行。")
        rumps.alert("卸载完成", "\n".join(msgs))
        # MENUBAR 放最后：若当前进程属于该作业，bootout 后本进程随之终止
        svc_uninstall(MENUBAR)
        self.refresh(force=True)

    def _quit(self, _):
        rumps.quit_application()

    # ---------------- 定时刷新 ----------------
    @rumps.timer(5)
    def _tick(self, _):
        self.refresh()

    # ---------------- 启动自诊断：状态项坐标 + 刘海遮挡检测 + 位置持久化 ----------------
    @rumps.timer(3)
    def _log_item_geometry(self, _):
        """启动后轮询状态项屏幕坐标（布局完成后记录）；若被刘海遮挡则告警。

        同时设置 autosaveName，使状态项位置跨重启持久化（launchd 每次重启进程
        PID 都会变化，ControlCenter 会将其视为全新 ephemeral 项重新布局，容易
        被排进刘海遮挡区 x 663..848；持久化后位置稳定）。
        """
        if getattr(self, "_geom_logged", False):
            return
        try:
            from AppKit import NSScreen
            from Foundation import NSUserDefaults
            item = self._nsapp.nsstatusitem
            if not getattr(self, "_autosave_set", False):
                self._autosave_set = True
                item.setAutosaveName_("SmartVaultMenuBarItem")
            btn = item.button()
            p = btn.accessibilityAttributeValue_("AXPosition").pointValue()
            sz = btn.accessibilityAttributeValue_("AXSize").sizeValue()
            if p.y <= 0:  # 布局未完成（占位坐标 x=0 y=-11），等待下个周期
                self._geom_tries = getattr(self, "_geom_tries", 0) + 1
                if self._geom_tries < 10:
                    return
                # 30 秒仍未布局：疑似被 macOS 收进溢出隐藏区
            self._geom_logged = True
            notch_txt = ""
            screen = NSScreen.mainScreen()
            left, right = screen.auxiliaryTopLeftArea(), screen.auxiliaryTopRightArea()
            if left is not None and right is not None:
                nl, nr = left.origin.x + left.size.width, right.origin.x
                if nl < nr:  # 存在刘海遮挡区
                    notch_txt = f"；刘海区 x {nl:.0f}..{nr:.0f}"
                    if p.x < nr and p.x + sz.width > nl:
                        notch_txt += " ⚠ 状态项被刘海遮挡！请按住 ⌘ 键把 ● 拖到时钟左侧可见区域（位置将被记住）"
            pref = NSUserDefaults.standardUserDefaults().floatForKey_(
                "NSStatusItem Preferred Position SmartVaultMenuBarItem")
            note = "" if p.y > 0 else " ⚠ 状态项始终未被布局（可能被收入菜单栏溢出区）"
            print(f"[SmartVault][诊断] 状态项 x={p.x:.0f} y={p.y:.0f} "
                  f"宽={sz.width:.0f} 高={sz.height:.0f} title={btn.title()!r}"
                  f" savedPref={pref:.0f}{notch_txt}{note}",
                  file=sys.stderr, flush=True)
        except Exception as exc:  # 诊断失败不影响主功能
            self._geom_logged = True
            print(f"[SmartVault][诊断] 状态项位置获取失败: {exc!r}", file=sys.stderr, flush=True)


def main() -> None:
    if not _acquire_single_instance_lock():
        rumps.alert("SmartVault 菜单栏已在运行",
                    "检测到另一个实例正在运行（多开无意义），本实例将退出。")
        sys.exit(0)
    SmartVaultBar().run()


if __name__ == "__main__":
    main()



