#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartVault / 智能仓库 —— 模块 A：自动化摄入与归档守护进程 (Ingest & Archive Daemon)
================================================================================
职责：
  1. 读取外部 config.json，注册多个 Obsidian Vault，用 watchdog 同时监听每个
     Vault 内部统一名称的「待处理笔记」收件箱目录。
  2. 实时捕获拖入的 Markdown 草稿及附件，按扩展名路由多模态解析：
       图像   (.png/.jpg/.jpeg/.heic...)   -> ocrmac（macOS Vision / Neural Engine）
       音视频 (.mp3/.m4a/.wav/.mp4/.mov..) -> mlx-whisper（Metal）/ openai-whisper（备选）
       PDF    (.pdf)                       -> PyMuPDF(fitz)
       Office (.docx/.xlsx/.pptx)          -> python-docx / openpyxl / python-pptx
       iWork  (.pages/.numbers/.key)       -> 解剖 Zip 包/包目录 -> QuickLook/Preview.pdf -> fitz
  3. 动态扫描仓库目录树（一/二级）+ 读取 ai_context.md 人设与历史索引，注入 Prompt，
     呼叫本地 LM Studio 生成 Strict JSON（全字段纯简体中文约束）。
  4. 生成 YAML 属性 + 排版正文，移动附件与笔记至目标目录，清理草稿，
     反向追加 ai_context.md 历史索引，最后通过 obsidian:// URI 唤醒 Obsidian。

隐私承诺：全流程仅访问 localhost（LM Studio / 系统框架），零外部网络请求。

用法：
  python ingest_daemon.py --config config.json      # 前台常驻守护
  python ingest_daemon.py --check                   # 环境自检
  python ingest_daemon.py --scan                    # 一次性处理收件箱积压草稿后退出
  python ingest_daemon.py --once 草稿.md --vault 工作事务   # 调试单篇草稿
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

# ---- 可选第三方依赖（容错导入，保证纯逻辑可在最小环境运行与自检）----
try:  # watchdog：仅常驻监听模式需要
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

try:  # requests：仅呼叫 LM Studio 时需要
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

APP_NAME = "SmartVault"
LOG = logging.getLogger("smartvault.ingest")
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"

# ------------------------------------------------------------------ 扩展名路由表
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif", ".bmp", ".gif", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma", ".aiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
IWORK_EXTS = {".pages", ".numbers", ".key"}
OFFICE_KIND_MAP = {".docx": "Word 文档", ".xlsx": "Excel 表格", ".pptx": "PPT 演示文稿"}
ATTACHMENT_EXTS = IMAGE_EXTS | MEDIA_EXTS | {".pdf"} | IWORK_EXTS | set(OFFICE_KIND_MAP) | {".txt"}

HIDDEN_DIR_NAMES = {".obsidian", ".trash", ".git", ".stfolder", ".smartvault"}

# LM Studio 结构化输出 JSON Schema（strict）
NOTE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_folder": {"type": "string", "description": "目标目录，相对仓库根，如“一级目录”或“一级目录/二级目录”"},
        "new_filename": {"type": "string", "description": "新文件名，不含 .md 扩展名与路径分隔符"},
        "summary": {"type": "string", "description": "80~120 字摘要，必须严格取材于草稿原文，禁止出现原文没有的数字或事实"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
        "optimized_content": {"type": "string", "description": "排版后的完整 Markdown 正文"},
    },
    "required": ["target_folder", "new_filename", "summary", "tags", "optimized_content"],
    "additionalProperties": False,
}

# ------------------------------------------------------------------ 默认配置
CONFIG_DEFAULTS: Dict[str, Any] = {
    "lm_studio": {
        "base_url": "http://localhost:1234/v1",
        "chat_model": "qwen2.5-7b-instruct",
        "temperature": 0.3,
        "max_tokens": 4096,
        "timeout_seconds": 300,
        "structured_output": True,
    },
    "inbox_folder_name": "待处理笔记",
    "context_file": "ai_context.md",
    "ai_context_max_chars": 6000,
    "tree_depth": 2,
    "obsidian": {"wake_enabled": True},
    "vision": {"language_preference": ["zh-Hans", "en-US"]},
    "whisper": {"backend": "auto", "mlx_model": "mlx-community/whisper-large-v3-turbo",
                "openai_model": "small", "language": "zh"},
    "processing": {"debounce_seconds": 8, "quiet_seconds": 3, "attachment_wait_timeout": 30,
                   "attachments_subfolder": "附件", "allow_new_folder": True, "max_folder_depth": 2,
                   "fallback_folder": "未分类", "content_rewrite": False, "rewrite_max_chars": 6000},
    "limits": {"raw_note_max_chars": 30000, "attachment_max_chars": 12000},
    "rag": {"enabled": True, "embedding_model_path": "models/bge-small-zh-v1.5",
            "embedding_device": "mps", "chroma_dir": "data/chroma", "collection_name": "smartvault",
            "chunk_size": 500, "chunk_overlap": 80, "top_k": 4, "rescan_seconds": 300,
            "exclude_folders": [".obsidian", ".trash", "待处理笔记"]},
    "api": {"host": "127.0.0.1", "port": 8788},
    "vaults": [],
    "log_dir": "logs",
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置：override 覆盖 base（仅 dict 递归，list/标量直接替换）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> Dict[str, Any]:
    """读取 config.json 并与默认值合并；相对路径一律相对 config.json 所在目录解析。"""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = json.load(f)
    cfg = _deep_merge(CONFIG_DEFAULTS, user_cfg or {})
    cfg["_config_dir"] = str(path.parent)
    cfg["log_dir_abs"] = str((path.parent / cfg.get("log_dir", "logs")).resolve())
    return cfg


def setup_logging(cfg: Dict[str, Any], level: int = logging.INFO) -> None:
    log_dir = Path(cfg["log_dir_abs"])
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    LOG.setLevel(level)
    LOG.handlers.clear()
    fh = RotatingFileHandler(log_dir / "ingest_daemon.log", maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    LOG.addHandler(fh)
    LOG.addHandler(sh)
    LOG.propagate = False


# ================================================================== Vault 注册
@dataclass
class Vault:
    """一个被监听的 Obsidian 仓库。"""
    name: str          # Obsidian 中的仓库名（用于 URI 唤醒与展示）
    root: Path         # 仓库根目录
    inbox: Path        # 收件箱目录（待处理笔记）
    context_file: Path  # 仓库根下的 ai_context.md


def build_vaults(cfg: Dict[str, Any], strict: bool = False) -> List[Vault]:
    """根据 config.json 的 vaults 列表构建 Vault 对象；收件箱目录不存在则自动创建。"""
    inbox_name = cfg["inbox_folder_name"]
    ctx_name = cfg["context_file"]
    vaults: List[Vault] = []
    for item in cfg.get("vaults", []):
        root = Path(str(item["path"])).expanduser()
        name = str(item.get("name") or root.name)
        if not root.is_dir():
            msg = f"Vault 目录不存在，已跳过：{name} -> {root}"
            if strict:
                raise FileNotFoundError(msg)
            LOG.error(msg)
            continue
        inbox = root / inbox_name
        inbox.mkdir(parents=True, exist_ok=True)
        vaults.append(Vault(name=name, root=root, inbox=inbox, context_file=root / ctx_name))
    if not vaults:
        raise RuntimeError("config.json 中没有可用的 Vault（vaults 列表为空或路径全部无效）")
    return vaults


# ================================================================== 名称净化
_FILENAME_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|#\^\[\]]')


def sanitize_component(text: str) -> str:
    """净化单个路径片段/文件名：去非法字符、折叠空白、去首尾空白与点号。"""
    text = _FILENAME_ILLEGAL_RE.sub("", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text


def sanitize_filename(name: str, fallback: str = "未命名笔记", max_len: int = 80) -> str:
    """净化 LLM 生成的文件名（不含扩展名），保证跨平台安全。"""
    out = sanitize_component(name)
    return out[:max_len].strip(" .") or fallback


def sanitize_folder_parts(folder: str) -> List[str]:
    """把 LLM 生成的 target_folder 拆成安全的相对路径片段列表，拒绝绝对路径与穿越。"""
    parts = re.split(r"[/\\]+", str(folder or ""))
    parts = [sanitize_component(p) for p in parts]
    return [p for p in parts if p not in ("", ".", "..")]


def sanitize_tags(tags: List[str]) -> List[str]:
    out: List[str] = []
    for t in tags or []:
        t = re.sub(r"^#+", "", str(t).strip()).strip()
        t = re.sub(r"\s+", "-", t)
        if t and len(t) <= 24 and t not in out:
            out.append(t)
    return out[:5]


# ================================================================== 附件引用提取
# [[wikilink]] / ![[wikilink embed]]，兼容别名（|alias）与标题锚点（#section）
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
# [text](path) / ![alt](path)，group(1)=方括号部分，group(2)=路径
MD_LINK_RE = re.compile(r"(\[[^\[\]]*\])\(\s*([^()\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# HTML 内嵌资源标签（<img>/<audio>/<video>/<source>/<embed> 的 src 属性），
# Kindle/HTML 转 Markdown 的产物常用此语法引用附件；双引号/单引号/裸值均兼容
HTML_SRC_RE = re.compile(
    r"""<(?:img|audio|video|source|embed)\b[^>]*?\bsrc\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)


def find_attachment_refs(md_text: str) -> List[str]:
    """提取草稿中引用的附件文件名（按出现顺序去重）。

    - 支持 ![[文件.png]]、[[文件.pdf|别名]] 与 standard [文本](路径) / ![](路径) 语法
    - 支持 HTML 内嵌标签 <img src="路径">（Kindle/HTML 转 Markdown 产物常用）
    - 自动忽略：URL（http/obsidian:// 等带 scheme 的链接）、data: 内嵌资源、
      .md 笔记链接、无扩展名双链
    """
    names: List[str] = []

    def _add(target: str) -> None:
        target = target.strip()
        if not target:
            return
        ext = Path(target).suffix.lower()
        if ext not in ATTACHMENT_EXTS:  # .md 笔记链接与裸双链不算附件
            return
        if target not in names:
            names.append(target)

    for m in WIKILINK_RE.finditer(md_text or ""):
        target = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        _add(target)
    for m in MD_LINK_RE.finditer(md_text or ""):
        raw = m.group(2).strip()
        if _URL_SCHEME_RE.match(raw) or raw.startswith("#"):
            continue  # 跳过 http(s)、obsidian://、mailto: 等外部链接
        _add(unquote(Path(raw).name))
    for m in HTML_SRC_RE.finditer(md_text or ""):
        raw = m.group(1).strip("\"'").strip()
        if not raw or _URL_SCHEME_RE.match(raw) or raw.startswith("#"):
            continue  # 跳过 http(s)、data:image/... 内嵌资源等非收件箱附件
        _add(unquote(Path(raw).name))
    return names


def resolve_attachment(inbox: Path, ref_name: str) -> Optional[Path]:
    """在收件箱内定位附件文件：精确名 -> 无扩展名补全 -> 递归大小写不敏感匹配。"""
    ref_name = ref_name.strip()
    candidates = [ref_name]
    if not Path(ref_name).suffix:  # 用户写 ![[截图]] 但文件实际是 截图.png
        candidates += [Path(ref_name).stem + ext for ext in sorted(ATTACHMENT_EXTS)]
    for c in candidates:
        q = inbox / c
        if q.is_file():
            return q
    want = {c.lower() for c in candidates}
    for f in inbox.rglob("*"):
        try:
            if f.is_file() and not f.name.startswith(".") and f.name.lower() in want:
                return f
        except OSError:
            continue
    return None


# ================================================================== 多模态解析器
def _clip_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n……（内容超长，已截断）"


def ocr_image(path: Path, vision_cfg: Dict[str, Any]) -> str:
    """图像 OCR：macOS Vision 框架（ocrmac），由 M4 Neural Engine 零延迟执行。"""
    from ocrmac import ocrmac
    langs = vision_cfg.get("language_preference", ["zh-Hans", "en-US"])
    annotations = ocrmac.OCR(str(path), language_preference=langs).recognize()
    lines = []
    for item in annotations:
        lines.append(item[0] if isinstance(item, (tuple, list)) else str(item))
    text = "\n".join(x.strip() for x in lines if x and x.strip())
    return text or "（Vision OCR 未识别到文字，可能为纯图形图片）"


_OPENAI_WHISPER_MODEL: Dict[str, Any] = {}


def _transcribe_mlx(path: Path, wcfg: Dict[str, Any]) -> str:
    """MLX whisper：Apple Metal GPU 加速转写（M4 Pro 推荐）。"""
    import mlx_whisper
    result = mlx_whisper.transcribe(
        str(path),
        path_or_hf_repo=wcfg.get("mlx_model", "mlx-community/whisper-large-v3-turbo"),
        language=wcfg.get("language") or None,
    )
    return str(result.get("text", "")).strip()


def _transcribe_openai(path: Path, wcfg: Dict[str, Any]) -> str:
    """openai-whisper CPU 后端（需要 ffmpeg）。"""
    import whisper
    model_name = wcfg.get("openai_model", "small")
    if model_name not in _OPENAI_WHISPER_MODEL:
        LOG.info("加载 openai-whisper 模型：%s", model_name)
        _OPENAI_WHISPER_MODEL[model_name] = whisper.load_model(model_name, device="cpu")
    result = _OPENAI_WHISPER_MODEL[model_name].transcribe(
        str(path), language=wcfg.get("language") or None
    )
    return str(result.get("text", "")).strip()


def transcribe_media(path: Path, wcfg: Dict[str, Any]) -> str:
    """音视频转写：优先 MLX(Metal) 后端，失败自动回退 openai-whisper(CPU)。"""
    backend = str(wcfg.get("backend", "auto")).lower()
    errors: List[str] = []
    if backend in ("auto", "mlx"):
        try:
            return _transcribe_mlx(path, wcfg)
        except ImportError:
            if backend == "mlx":
                raise RuntimeError("未安装 mlx-whisper（pip install mlx-whisper）")
            errors.append("mlx-whisper 不可用")
        except Exception as e:  # noqa: BLE001 —— 单后端失败回退另一个，不中断流水线
            LOG.exception("MLX whisper 转写失败：%s", path.name)
            errors.append(f"mlx 异常: {e}")
    if backend in ("auto", "openai"):
        try:
            return _transcribe_openai(path, wcfg)
        except ImportError:
            errors.append("未安装 openai-whisper")
        except Exception as e:  # noqa: BLE001
            LOG.exception("openai-whisper 转写失败：%s", path.name)
            errors.append(f"openai 异常: {e}")
    raise RuntimeError("语音转写失败（" + "；".join(errors) + "），请安装 mlx-whisper 或 openai-whisper")


def extract_pdf(path: Path) -> str:
    """PDF 全文提取：PyMuPDF(fitz)，零弹窗静默读取。"""
    import fitz
    pages: List[str] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, 1):
            t = page.get_text("text").strip()
            if t:
                pages.append(f"[第 {i} 页]\n{t}")
    return "\n\n".join(pages) or "（PDF 中未提取到文本，可能为纯扫描件）"


def extract_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts: List[str] = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts) or "（Word 文档为空）"


def extract_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts: List[str] = []
    for ws in wb.worksheets:
        parts.append(f"[工作表：{ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts) or "（Excel 表格为空）"


def extract_pptx(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts: List[str] = []
    for idx, slide in enumerate(prs.slides, 1):
        chunks: List[str] = [f"[幻灯片 {idx}]"]
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = shape.text_frame.text.strip()
                if t:
                    chunks.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        chunks.append(" | ".join(cells))
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                chunks.append(f"[备注] {notes}")
        except Exception:  # noqa: BLE001 —— 无备注页属正常
            pass
        parts.append("\n".join(chunks))
    return "\n\n".join(parts) or "（PPT 为空）"


def extract_iwork(path: Path) -> str:
    """Apple iWork：解剖 Zip 包/包目录 -> QuickLook/Preview.pdf -> fitz 提取预览文本。"""
    import fitz
    preview: Optional[bytes] = None
    if path.is_dir():  # Finder 中的“文件”在文件系统层可能是包目录
        q = path / "QuickLook" / "Preview.pdf"
        if q.is_file():
            preview = q.read_bytes()
    else:
        try:
            with zipfile.ZipFile(str(path)) as z:
                if "QuickLook/Preview.pdf" in z.namelist():
                    preview = z.read("QuickLook/Preview.pdf")
        except zipfile.BadZipFile:
            preview = None
    if not preview:
        return "（iWork 文件内未找到 QuickLook/Preview.pdf，建议在 Pages/Numbers/Keynote 中导出 PDF 后再拖入）"
    with fitz.open(stream=preview, filetype="pdf") as doc:
        pages = [p.get_text("text").strip() for p in doc]
    text = "\n\n".join(t for t in pages if t)
    return text or "（Preview.pdf 中未提取到文本）"


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def dispatch_attachment(path: Path, cfg: Dict[str, Any]) -> Tuple[str, str]:
    """按扩展名路由解析器，返回 (类型标签, 解析文本)；任何失败都不会中断流水线。"""
    ext = path.suffix.lower()
    try:
        started = time.time()
        if ext in IMAGE_EXTS:
            kind, text = "图像 OCR（Vision）", ocr_image(path, cfg["vision"])
        elif ext in MEDIA_EXTS:
            kind, text = "音视频转录（whisper）", transcribe_media(path, cfg["whisper"])
        elif ext == ".pdf":
            kind, text = "PDF 文本（PyMuPDF）", extract_pdf(path)
        elif ext == ".docx":
            kind, text = OFFICE_KIND_MAP[ext], extract_docx(path)
        elif ext == ".xlsx":
            kind, text = OFFICE_KIND_MAP[ext], extract_xlsx(path)
        elif ext == ".pptx":
            kind, text = OFFICE_KIND_MAP[ext], extract_pptx(path)
        elif ext in IWORK_EXTS:
            kind, text = "iWork 预览（QuickLook）", extract_iwork(path)
        else:  # .txt
            kind, text = "纯文本附件", read_text_file(path)
        LOG.info("附件解析完成 [%s] %s（%.1fs，%d 字符）", kind, path.name,
                 time.time() - started, len(text))
        return kind, _clip_text(text, int(cfg["limits"]["attachment_max_chars"]))
    except Exception as e:  # noqa: BLE001
        LOG.exception("附件解析失败：%s", path.name)
        return "解析失败", f"（附件 {path.name} 解析失败：{e}）"


# ================================================================== 目录树与上下文
def scan_tree(root: Path, depth: int = 2, exclude_names: frozenset = frozenset(),
              max_dirs: int = 200) -> str:
    """扫描仓库一/二级目录树，输出带树形符号的文本（供 LLM 选择归档位置）。"""
    lines: List[str] = [f"{root.name}/"]
    count = 0

    def rec(d: Path, level: int, prefix: str) -> None:
        nonlocal count
        if count >= max_dirs:
            return
        try:
            entries = sorted(
                (c for c in d.iterdir()
                 if c.is_dir() and not c.name.startswith(".")
                 and c.name not in HIDDEN_DIR_NAMES and c.name not in exclude_names),
                key=lambda x: x.name,
            )
        except OSError:
            return
        for i, c in enumerate(entries):
            if count >= max_dirs:
                lines.append(prefix + "……（目录过多已截断）")
                return
            last = i == len(entries) - 1
            lines.append(prefix + ("└── " if last else "├── ") + c.name + "/")
            count += 1
            if level < depth:
                rec(c, level + 1, prefix + ("    " if last else "│   "))

    rec(root, 1, "")
    return "\n".join(lines)


def load_ai_context(path: Path, max_chars: int) -> str:
    """读取 ai_context.md；超长时保留头部（规则区）+ 尾部（最近索引），中段省略。"""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    head_keep = max(1, int(max_chars * 0.35))
    tail_keep = max(1, max_chars - head_keep)
    return (text[:head_keep] + "\n……（中间历史索引已省略）……\n" + text[len(text) - tail_keep:])


def _drop_stale_ctx_entries(path: Path, stem: str) -> int:
    """移除 ai_context.md 中文件名（alias）与 stem 相同的「历史归档索引」条目。

    场景：误归档笔记移回收件箱重新归档时，指向旧目录的条目会随 Prompt
    注入形成「历史一致性」锚定（SYSTEM_PROMPT 规则 5），使 LLM 沿用旧
    目录、纠错失效；append-only 也会堆积重复条目。仅匹配标准生成行
    `- 文件：[[…|stem]]`（人工改写的非标准条目保守不动）；无移除不写盘；
    读写失败返回 0（追加逻辑照常执行）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    parts = re.split(r"(?=^## )", text, flags=re.M)
    pat = re.compile(rf"^- 文件：\[\[.*\|{re.escape(stem)}\]\]\s*$", re.M)
    kept = [p for p in parts if not pat.search(p)]
    if len(kept) == len(parts):
        return 0
    try:
        path.write_text("".join(kept), encoding="utf-8")
    except OSError:
        return 0
    return len(parts) - len(kept)


def append_ai_context(vault: Vault, meta: Dict[str, Any], final_md: Path) -> None:
    """归档成功后向 ai_context.md 追加历史索引条目（文件不存在则带模板创建）。

    重归档自愈（v1.6.1）：追加前先移除同文件名的旧条目，索引始终只保留
    该笔记的最新归档位置。
    """
    path = vault.context_file
    rel = final_md.relative_to(vault.root)
    link = rel.as_posix()[:-3] if rel.as_posix().lower().endswith(".md") else rel.as_posix()
    tags_line = " ".join(f"#{t}" for t in meta.get("tags", []))
    entry = (
        f"\n## {datetime.now():%Y-%m-%d %H:%M}｜SmartVault 归档\n"
        f"- 文件：[[{link}|{final_md.stem}]]\n"
        f"- 目录：{meta.get('target_folder', '')}\n"
        f"- 摘要：{meta.get('summary', '')}\n"
        f"- 标签：{tags_line}\n"
    )
    if not path.exists():
        path.write_text(
            "# ai_context\n\n"
            "> 本文件由 SmartVault 维护：上半部为「AI 处理规则」（可人工编辑，"
            "守护进程每次都会注入给模型），下半部为「历史归档索引」（自动追加）。\n\n"
            "## AI 处理规则\n\n"
            "（示例）优先使用现有目录；标签使用小词而非长句；摘要聚焦事实。\n\n"
            "## 历史归档索引\n",
            encoding="utf-8",
        )
    else:
        removed = _drop_stale_ctx_entries(path, final_md.stem)
        if removed:
            LOG.info("ai_context 重归档去重：移除 %d 条旧条目（%s）", removed, final_md.stem)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


# ================================================================== LM Studio 客户端
def apply_thinking_switch(messages: List[Dict[str, str]], enabled: bool) -> List[Dict[str, str]]:
    """Qwen3 混合思考模型的软开关：enabled=False 时在最后一条 user 消息末尾追加 /no_think。

    Qwen3 官方 chat template 识别该标记后注入空 <think> 块，模型跳过思考直接作答
    （实测 qwen3-14b 同请求 16.2s -> 1.25s）。LM Studio 的 /v1 接口不支持
    chat_template_kwargs / enable_thinking 请求参数（实测均无效），软开关是唯一通道。
    返回新列表（浅拷贝逐条 dict），不修改调用方传入的消息。
    """
    if enabled:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m.get("role") == "user":
            m["content"] = f"{str(m.get('content') or '')}\n\n/no_think"
            break
    return out


class LLMClient:
    """LM Studio（OpenAI 兼容 /v1）客户端：优先 JSON Schema 结构化输出。"""

    def __init__(self, cfg: Dict[str, Any]):
        lm = cfg["lm_studio"]
        self.base_url = str(lm["base_url"]).rstrip("/")
        self.model = lm["chat_model"]
        # 采样参数对齐 Qwen3 官方 thinking 模式推荐值（temp 0.6 / top_p 0.95 /
        # top_k 20）；此前只发 temperature，top_p/top_k 落到 LM Studio 默认值
        # （1.0 / 40），属非官方组合。thinking=False 时经 /no_think 软开关跳过思考。
        self.temperature = float(lm.get("temperature", 0.6))
        self.top_p = float(lm.get("top_p", 0.95))
        self.top_k = int(lm.get("top_k", 20))
        self.thinking = bool(lm.get("thinking", True))
        self.max_tokens = int(lm.get("max_tokens", 4096))
        self.timeout = int(lm.get("timeout_seconds", 300))
        self.structured = bool(lm.get("structured_output", True))
        if requests is None:
            raise RuntimeError("缺少依赖 requests，请先 pip install requests")

    def chat(self, messages: List[Dict[str, str]],
             json_schema: Optional[Dict[str, Any]] = None,
             temperature: Optional[float] = None) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": apply_thinking_switch(messages, self.thinking),
            "temperature": self.temperature if temperature is None else temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if json_schema and self.structured:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "smartvault_note", "strict": True, "schema": json_schema},
            }
        url = f"{self.base_url}/chat/completions"
        resp = requests.post(url, json=payload, timeout=self.timeout)
        if resp.status_code == 400:
            body = resp.text[:400]
            try:
                err = resp.json().get("error")
                if isinstance(err, dict) and err.get("message"):
                    body = str(err["message"])
                elif err:
                    body = str(err)
            except Exception:  # noqa: BLE001
                pass
            if "exceeds the available context size" in body or (
                "context" in body.lower() and "tokens" in body.lower()
            ):
                # 上下文超限：与 response_format 无关，重发同样失败，直接给出可操作指引
                raise RuntimeError(
                    f"输入超过 LM Studio 上下文窗口（模型 {self.model}）：{body[:200]}。"
                    f"请以更大 context length 重载模型（如 lms load {self.model} -c 32768），"
                    "或拆分超长草稿；草稿已保留在收件箱。"
                )
            if "response_format" in payload:
                LOG.warning("LM Studio 不支持 response_format，回退纯提示词模式（原因：%s）", body[:120])
                payload.pop("response_format")
                resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return str(data["choices"][0]["message"]["content"] or "")

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/models", timeout=5)
            return r.ok
        except Exception:  # noqa: BLE001
            return False


# ================================================================== Prompt 与 JSON 解析
SYSTEM_PROMPT = """你是「SmartVault 智能仓库」的资深知识管理图书管理员，负责把用户拖入收件箱的 Obsidian 草稿整理归档。

【输出契约（最高优先级）】
1. 你的回复必须是一个合法 JSON 对象本身：以 { 开始、以 } 结束；禁止 markdown 代码围栏、禁止任何解释性文字或前后缀。
2. JSON 字段固定为：
   - "target_folder": 字符串，笔记应归入的目标目录（相对仓库根目录，形如“一级目录”或“一级目录/二级目录”）。
   - "new_filename": 字符串，新文件名（不含 .md 扩展名，不含路径分隔符）。
   - "summary": 字符串，80~120 字的内容摘要；摘要中的事实与数字必须严格取材于草稿原文，严禁出现原文没有的数字、比例或结论。
   - "tags": 字符串数组，恰好 3~5 个主题标签，不带 # 号，不含空格。
   - "optimized_content": 字符串，整理排版后的完整 Markdown 正文；仅当系统未声明“原文保留模式”时才需要填写，声明后必须为空字符串 ""。

【整理规则】
1. target_folder 必须优先从用户给出的“仓库目录树”中选择已有目录；目录树中确无合适目录时，须依据笔记主题**新建简洁的一级目录**（2~6 个字，如“开发环境”“AI 工具”“网络工具”），让分类体系随归档自然生长，同主题笔记后续复用同一目录；最多二级深度；严禁使用“待处理笔记”、仓库根目录，以及“未分类”“笔记”“文档”“其他”等无信息量的目录名。
2. optimized_content 遵守 Obsidian Markdown 规范：文件内不要重复一级标题（标题由文件名承担），用二级/三级标题分节，善用列表与引用；正文中的所有事实、数字、百分比、指标、人名、结论必须逐字来自草稿原文或附件转录，严禁编造原文不存在的任何数字、比例或事实；整理仅限标题层级、列表化与删除冗余空白，禁止缩写、扩写或补充原文没有的内容；草稿为对话/问答体时必须保持原有问答结构与措辞，禁止重组为摘要式笔记。
3. 为正文涉及的关键概念、人物、书名、项目、技术名词添加 [[双链]]；目录树中的已有目录名可优先作为双链目标，以便沉淀知识网络。
4. 附件的解析文本必须融入正文：以“> [!quote]- 附件：文件名”折叠引用块或独立小节呈现，冗长转录可提炼要点但不得丢失信息。
5. 若提供了 ai_context.md 内容，必须严格遵守其中的「AI 处理规则」，并与「历史归档索引」中已有标签体系、双链风格保持一致。
6. 全部输出内容（target_folder、new_filename、summary、tags、optimized_content）一律使用简体中文；专有名词、代码、命令、英文缩写、文件名与扩展名除外。
7. 清除草稿痕迹：删除“待处理”“测试”等临时字样与冗余空白，输出即终稿。
8. 超短草稿（正文不足约 200 字符，多为链接收藏、账号信息、碎片备忘）语义信号弱：必须依据笔记的实际用途与关键实体（链接指向的站点/工具、邮箱、账号、备忘主题）判断归类，不得凭个别词语的弱关联塞入已有目录，确无贴切目录时新建；这些关键信息须如实写入 summary 与 tags。"""


def build_user_prompt(vault_name: str, draft_name: str, raw_md: str,
                      attach_blocks: List[str], tree_text: str, ctx_text: str,
                      raw_max_chars: int, keep_original_content: bool = False) -> str:
    parts = [
        f"【当前仓库：{vault_name}｜目录树（优先归类到已有目录）】",
        tree_text or "（目录树为空）",
        "【ai_context.md（仓库规则与历史索引）】",
        ctx_text or "（文件不存在，可自行判断）",
        f"【草稿原文（原始文件名：{draft_name}）】",
        _clip_text(raw_md, raw_max_chars),
    ]
    if attach_blocks:
        parts.append("【附件解析文本】\n" + "\n\n".join(attach_blocks))
    else:
        parts.append("【附件解析文本】\n（本篇草稿没有附件）")
    if keep_original_content:
        parts.append(
            "【本篇为原文保留模式（最高优先级，覆盖前述一切规则）】\n"
            f"本篇草稿共 {len(raw_md)} 字符，正文将由系统原样保留草稿原文，"
            "你绝对禁止对正文做任何摘要、压缩、改写或重排。因此：\n"
            "- optimized_content 字段必须返回空字符串 \"\"；\n"
            "- 你只需认真完成 target_folder、new_filename、summary、tags 四个字段，"
            "其中 summary 也必须严格取材于原文，禁止出现原文没有的数字、比例或事实。"
        )
    parts.append("请依据以上信息，直接输出符合契约的 JSON 对象。")
    return "\n\n".join(parts)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n", "", text)
        text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def parse_llm_json(raw: str, fallback_filename: str = "未命名笔记") -> Dict[str, Any]:
    """鲁棒解析 LLM 输出为笔记元数据；容忍围栏、前后缀噪声与类型偏差。"""
    text = _strip_code_fence(raw or "")
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError(f"LLM 未返回 JSON 对象：{raw[:200]!r}")
    obj = json.loads(text[s:e + 1])

    folder = str(obj.get("target_folder") or "").strip()
    fname = str(obj.get("new_filename") or "").strip()
    summary = str(obj.get("summary") or "").strip()
    tags = obj.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,，;；\s]+", tags) if t]
    content = _strip_code_fence(str(obj.get("optimized_content") or ""))

    return {
        "target_folder": folder,
        "new_filename": fname or fallback_filename,
        "summary": summary or "（模型未生成摘要）",
        "tags": sanitize_tags([str(t) for t in tags]),
        # 空 optimized_content 是合法值（长文保守模式），由 run_pipeline 回退为原文
        "optimized_content": content,
    }


# ================================================================== 落盘与归档
def _yaml_str(text: str) -> str:
    """YAML 双引号字符串安全转义（借用 JSON 转义规则）。"""
    return json.dumps(str(text), ensure_ascii=False)[1:-1]


def build_final_markdown(meta: Dict[str, Any], now: datetime) -> str:
    tags = meta.get("tags") or []
    tags_lines = "\n".join(f"  - {t}" for t in tags) or "  - 未分类"
    front = (
        "---\n"
        f"title: \"{_yaml_str(meta['new_filename'])}\"\n"
        f"date: {now:%Y-%m-%d %H:%M:%S}\n"
        f"summary: \"{_yaml_str(meta['summary'])}\"\n"
        "tags:\n"
        f"{tags_lines}\n"
        "source: SmartVault 自动归档\n"
        "---\n\n"
    )
    body = meta["optimized_content"].strip()
    if not body.endswith("\n"):
        body += "\n"
    return front + body


def choose_target_dir(vault: Vault, folder_str: str, proc: Dict[str, Any],
                      inbox_name: str) -> Path:
    """校验并确定目标目录：已有目录优先，允许受控新建，异常一律回退未分类。"""
    allow_new = bool(proc.get("allow_new_folder", True))
    max_depth = int(proc.get("max_folder_depth", 2))
    fallback = sanitize_component(proc.get("fallback_folder", "未分类")) or "未分类"
    parts = sanitize_folder_parts(folder_str)
    # LLM 常把仓库名一并输出（如“智能笔记/BMO/Profiles”），剥离后再判深度，避免误回退未分类
    while parts and parts[0] == vault.name:
        parts = parts[1:]
    if parts and inbox_name not in parts:
        cand = vault.root.joinpath(*parts)
        try:
            cand.relative_to(vault.root)
        except ValueError:
            cand = None  # 越界（不应发生，双保险）
        if cand is not None and cand != vault.root:
            if cand.is_dir():
                return cand
            if allow_new and len(parts) <= max_depth:
                cand.mkdir(parents=True, exist_ok=True)  # 深度已受限，可安全创建整条链路
                LOG.info("新建目标目录：%s", cand)
                return cand
    fb = vault.root / fallback
    fb.mkdir(parents=True, exist_ok=True)
    return fb


def prune_empty_dirs(inbox: Path) -> int:
    """清理收件箱内的空目录（自底向上，供归档后与启动补扫时调用）。

    归档移走附件后常残留空的 ``附件/`` 等目录。仅当目录为空、或只含
    Finder 元数据（.DS_Store）时才删除；收件箱根目录本身永不删除；
    含任何真实文件（含非 .DS_Store 的隐藏文件）的目录一律保留，
    因此不会误删正在等待附件的待处理草稿所在目录。返回删除的目录数。
    """
    try:
        dirs = [p for p in inbox.rglob("*") if p.is_dir()]
    except OSError:
        return 0
    removed = 0
    # 深度优先：先删空的子目录，父目录才可能随之变空
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name != ".DS_Store":
                break
        else:
            for e in entries:
                try:
                    e.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
                removed += 1
            except OSError:
                pass
    return removed


def unique_path(p: Path) -> Path:
    """目标已存在时按 Obsidian 风格追加序号，避免覆盖。"""
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem} {i}{p.suffix}")
        if not cand.exists():
            return cand
    return p.with_name(f"{p.stem} {int(time.time())}{p.suffix}")


def backup_draft(vault_root: Path, md_path: Path, keep: int = 100) -> Optional[Path]:
    """删除草稿前把原文备份到 vault 内 .smartvault/backup/，保留最近 keep 份。

    归档成功即删草稿是不可逆操作；备份兜底 LLM 整理失真或误归档的恢复需求。
    .smartvault 在 HIDDEN_DIR_NAMES 中（目录树扫描排除），也应配置进 RAG exclude_folders。
    """
    try:
        bdir = vault_root / ".smartvault" / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = unique_path(bdir / f"{stamp}_{md_path.name}")
        shutil.copy2(str(md_path), str(dest))
        olds = sorted([p for p in bdir.iterdir() if p.is_file()],
                      key=lambda p: p.name, reverse=True)
        for p in olds[keep:]:
            try:
                p.unlink()
            except OSError:
                pass
        return dest
    except Exception:  # noqa: BLE001 —— 备份失败不阻断归档，但必须留痕
        LOG.exception("草稿备份失败（继续归档）：%s", md_path)
        return None


def rewrite_links(content: str, moved_map: Dict[str, str], subfolder: str) -> str:
    """附件改名/移入子目录后，把正文中引用改写为新相对路径。

    - wikilink（[[x.png]]）Obsidian 全局按名解析，仅当附件被重命名时改写目标名；
    - standard link（[t](x.png)）与 HTML src（<img src="x.png">）是相对路径语法，
      统一改写为 子目录/新名（引号风格保持原样）。
    """
    if not moved_map:
        return content

    def rep_wikilink(m: "re.Match[str]") -> str:
        inner = m.group(1)
        target, _, alias = inner.partition("|")
        base, _, anchor = target.partition("#")
        if base.strip() in moved_map:
            new = moved_map[base.strip()]
            t = new + (f"#{anchor}" if anchor else "")
            return "[[" + t + (f"|{alias}" if alias else "") + "]]"
        return m.group(0)

    content = WIKILINK_RE.sub(rep_wikilink, content)

    def rep_mdlink(m: "re.Match[str]") -> str:
        raw = m.group(2)
        if _URL_SCHEME_RE.match(raw) or raw.startswith("#"):
            return m.group(0)
        name = Path(unquote(raw)).name
        if name in moved_map:
            new = moved_map[name]
            rel = f"{subfolder}/{new}" if subfolder else new
            return f"{m.group(1)}({rel})"
        return m.group(0)

    content = MD_LINK_RE.sub(rep_mdlink, content)

    def rep_htmlsrc(m: "re.Match[str]") -> str:
        quoted = m.group(1)
        q = quoted[0] if quoted[:1] in ('"', "'") else ""
        raw = quoted.strip("\"'").strip()
        if not raw or _URL_SCHEME_RE.match(raw) or raw.startswith("#"):
            return m.group(0)  # 外部 URL / data: 内嵌资源 / 锚点不动
        name = Path(unquote(raw)).name
        if name in moved_map:
            new = moved_map[name]
            rel = f"{subfolder}/{new}" if subfolder else new
            # 按 group(1) 区间精确重组，避免 alt="同值" 时误伤前序属性
            s, e = m.start(1) - m.start(0), m.end(1) - m.start(0)
            return m.group(0)[:s] + q + rel + q + m.group(0)[e:]
        return m.group(0)

    return HTML_SRC_RE.sub(rep_htmlsrc, content)


def wake_obsidian(vault_name: str, rel_path: str) -> None:
    """通过 obsidian:// URI 唤醒 Obsidian 并打开刚归档的笔记。"""
    uri = f"obsidian://open?vault={quote(vault_name, safe='')}&file={quote(rel_path, safe='')}"
    try:
        subprocess.Popen(["open", uri])
        LOG.info("已唤醒 Obsidian：%s", uri)
    except Exception as e:  # noqa: BLE001
        LOG.warning("唤醒 Obsidian 失败（不影响归档结果）：%s", e)


def _wait_file_stable(path: Path, checks: int = 10, interval: float = 1.0) -> bool:
    """等待附件文件大小稳定（大文件拖入时 Finder 是渐进拷贝）。"""
    last = -1
    for _ in range(checks):
        try:
            cur = path.stat().st_size
        except OSError:
            return False
        if cur == last and cur > 0:
            return True
        last = cur
        time.sleep(interval)
    return last > 0


def build_preserved_content(raw_md: str, attach_blocks: List[str]) -> str:
    """原文保留模式的正文组装：草稿原文逐字保留；附件转录以折叠引用块附加文末。

    附件转录是 OCR/Whisper 的机器产物而非用户原文：必须与原文明确区隔、可折叠、
    标注「以原附件为准」——既保证转录内容可被 RAG 检索，又避免转录误差污染原文。
    """
    if not attach_blocks:
        return raw_md
    lines = [raw_md.rstrip("\n"), "",
             "## 附：附件转录（机器自动生成，仅供检索，若有出入以原附件为准）", ""]
    for block in attach_blocks:
        header, _, text = block.partition("\n")
        lines.append(f"> [!quote]- {header}")
        body_lines = (text or "（空转录）").splitlines() or ["（空转录）"]
        lines.extend(f"> {ln}".rstrip() for ln in body_lines)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ================================================================== 归档流水线
def run_pipeline(cfg: Dict[str, Any], client: LLMClient, vault: Vault,
                 md_path: Path) -> Optional[Path]:
    """单篇草稿的完整处理链：读取 -> 等附件 -> 多模态解析 -> 上下文注入 ->
    LLM 提炼 -> 校验净化 -> 移动落盘 -> 更新索引 -> 唤醒 Obsidian。"""
    started = time.time()
    proc = cfg["processing"]
    inbox_name = cfg["inbox_folder_name"]
    raw = md_path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    LOG.info("开始处理草稿：%s（%d 字符）", md_path.name, len(raw))

    # 1) 等待引用的附件到齐（拖入多文件时附件可能晚于 md 到达）
    refs = find_attachment_refs(raw)
    deadline = time.time() + float(proc.get("attachment_wait_timeout", 30))
    while time.time() < deadline:
        missing = [r for r in refs if resolve_attachment(vault.inbox, r) is None]
        if not missing:
            break
        LOG.info("等待附件就绪：%s（剩余 %.0fs）", missing, deadline - time.time())
        time.sleep(2)

    # 2) 多模态解析附件
    attach_blocks: List[str] = []
    attachments: List[Tuple[str, Path]] = []  # (引用名, 实际路径)
    for ref in refs:
        p = resolve_attachment(vault.inbox, ref)
        if p is None:
            LOG.warning("附件未找到，保留原引用：%s", ref)
            attach_blocks.append(f"◆ 附件「{ref}」｜未找到\n（该附件未出现在收件箱中）")
            continue
        if not _wait_file_stable(p, checks=6, interval=1.0):
            LOG.warning("附件大小持续变化，按当前状态尽力解析：%s", p.name)
        kind, text = dispatch_attachment(p, cfg)
        attachments.append((ref, p))
        attach_blocks.append(f"◆ 附件「{p.name}」｜{kind}\n{text}")

    # 3) 组装动态上下文（原文保留模式：默认所有草稿正文逐字保留，LLM 只产元数据，
    #    杜绝任何改写导致的丢内容与幻觉——见 v1.3.1 事故复盘；短文 AI 润色为可选开关）
    tree_text = scan_tree(vault.root, depth=int(cfg.get("tree_depth", 2)),
                          exclude_names=frozenset({inbox_name}))
    ctx_text = load_ai_context(vault.context_file, int(cfg["ai_context_max_chars"]))
    rewrite_enabled = bool(proc.get("content_rewrite", False))   # 默认 False：正文永不改写
    rewrite_max = int(proc.get("rewrite_max_chars", 6000))
    keep_original = (not rewrite_enabled) or len(raw) > rewrite_max
    user_prompt = build_user_prompt(vault.name, md_path.name, raw, attach_blocks,
                                    tree_text, ctx_text,
                                    int(cfg["limits"]["raw_note_max_chars"]),
                                    keep_original_content=keep_original)

    # 4) LLM 提炼（Strict JSON Schema 优先，失败自动回退纯提示词）
    raw_llm = client.chat([{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": user_prompt}],
                          json_schema=NOTE_JSON_SCHEMA)
    meta = parse_llm_json(raw_llm, fallback_filename=md_path.stem)
    if keep_original:
        # 原文保留模式：正文逐字保留草稿原文（附件转录折叠附加于文末），无视 LLM 返回
        meta["optimized_content"] = build_preserved_content(raw, attach_blocks)
        LOG.info("原文保留模式（%d 字符，附件转录 %d 份）：正文保留原文，LLM 仅提供元数据",
                 len(raw), len(attach_blocks))
    else:
        # 短文模式：LLM 万一未返回正文也回退原文，任何情况下不允许内容丢失
        meta["optimized_content"] = meta["optimized_content"].strip() or raw
    LOG.info("LLM 提炼完成：目录=%s 文件名=%s 标签=%s", meta["target_folder"],
             meta["new_filename"], meta["tags"])

    # 5) 校验与净化
    folder = choose_target_dir(vault, meta["target_folder"], proc, inbox_name)
    now = datetime.now()
    final_md = unique_path(folder / (sanitize_filename(meta["new_filename"]) + ".md"))
    subfolder = str(proc.get("attachments_subfolder", "") or "").strip("/")

    # 6) 移动附件（含改名去重），并改写正文中的引用
    moved_map: Dict[str, str] = {}
    attach_dir = final_md.parent / subfolder if subfolder else final_md.parent
    if attachments:  # 无附件不预创建目录，避免遗留空 附件/ 目录
        attach_dir.mkdir(parents=True, exist_ok=True)
    for _, src in attachments:
        try:
            dest = unique_path(attach_dir / src.name)
            shutil.move(str(src), str(dest))
            moved_map[src.name] = dest.name
        except Exception:  # noqa: BLE001
            LOG.exception("附件移动失败（保留原处）：%s", src.name)
    meta["optimized_content"] = rewrite_links(meta["optimized_content"], moved_map, subfolder)

    # 7) 写入终稿、备份并清理草稿
    final_md.write_text(build_final_markdown(meta, now), encoding="utf-8")
    if (backup := backup_draft(vault.root, md_path)) is not None:
        LOG.info("草稿已备份：%s", backup.name)
    try:
        md_path.unlink()
    except OSError:
        LOG.exception("草稿清理失败（不影响归档）：%s", md_path)
    if (pruned := prune_empty_dirs(vault.inbox)) > 0:
        LOG.info("收件箱空目录清理：删除 %d 个（如归档后残留的空 附件/ 目录）", pruned)

    # 8) 反向写入 ai_context.md 历史索引 + 唤醒 Obsidian
    append_ai_context(vault, meta, final_md)
    rel = final_md.relative_to(vault.root).as_posix()
    if cfg.get("obsidian", {}).get("wake_enabled", True):
        wake_obsidian(vault.name, rel)

    LOG.info("归档完成：%s -> %s（耗时 %.1fs）", md_path.name, rel, time.time() - started)
    return final_md


# ================================================================== 监听与调度
class VaultState:
    """单个 Vault 的监听状态：防抖队列 + 处理工作线程。"""

    def __init__(self, cfg: Dict[str, Any], client: LLMClient, vault: Vault):
        self.cfg = cfg
        self.client = client
        self.vault = vault
        proc = cfg["processing"]
        self.debounce = float(proc.get("debounce_seconds", 8))
        self.quiet = float(proc.get("quiet_seconds", 3))
        self.pending: Dict[Path, Dict[str, Any]] = {}   # md -> {size, ready_at}
        self.lock = threading.Lock()
        self.last_event = time.time()
        self.queue: "Queue[Optional[Path]]" = Queue()
        self.stop_flag = threading.Event()
        self.worker = threading.Thread(target=self._worker, name=f"worker-{vault.name}", daemon=True)
        self.scheduler = threading.Thread(target=self._scheduler, name=f"sched-{vault.name}", daemon=True)

    # ---- 事件入口（由 watchdog handler 调用）----
    def on_event(self, path: Optional[Path], is_delete: bool) -> None:
        self.last_event = time.time()
        if path is None or path.name.startswith(".") or path.suffix.lower() != ".md":
            return
        with self.lock:
            if is_delete:
                self.pending.pop(path, None)
                return
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self.pending[path] = {"size": size, "ready_at": time.time() + self.debounce}

    def seed_inbox(self) -> None:
        """启动时把收件箱中已存在的草稿纳入待处理（崩溃恢复）。"""
        for f in sorted(self.vault.inbox.rglob("*.md")):
            if f.name.startswith("."):
                continue
            self.on_event(f, is_delete=False)
        prune_empty_dirs(self.vault.inbox)

    # ---- 调度线程：等收件箱安静 + 文件大小稳定后入队 ----
    def _scheduler(self) -> None:
        while not self.stop_flag.is_set():
            time.sleep(1.0)
            now = time.time()
            quiet = now - self.last_event >= self.quiet
            with self.lock:
                items = list(self.pending.items())
            for md, rec in items:
                try:
                    if not md.exists():
                        with self.lock:
                            self.pending.pop(md, None)
                        continue
                    size = md.stat().st_size
                    if size != rec["size"]:  # 仍在写入，重置计时
                        rec["size"] = size
                        rec["ready_at"] = now + self.debounce
                        continue
                    if quiet and now >= rec["ready_at"]:
                        with self.lock:
                            self.pending.pop(md, None)
                        LOG.info("[%s] 草稿就绪，入队：%s", self.vault.name, md.name)
                        self.queue.put(md)
                except FileNotFoundError:
                    with self.lock:
                        self.pending.pop(md, None)
                except Exception:  # noqa: BLE001
                    LOG.exception("调度异常：%s", md)

    # ---- 工作线程：串行处理，避免同仓库并发写 ai_context.md ----
    def _worker(self) -> None:
        while not self.stop_flag.is_set():
            try:
                md = self.queue.get(timeout=1.0)
            except Empty:
                continue
            if md is None:
                break
            try:
                run_pipeline(self.cfg, self.client, self.vault, md)
            except Exception:  # noqa: BLE001 —— 失败保留草稿原处，便于人工重试
                LOG.exception("处理失败（草稿保留原处）：%s", md)
            finally:
                self.queue.task_done()

    def start(self) -> None:
        self.worker.start()
        self.scheduler.start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.queue.put(None)


def make_event_handler(state: VaultState):
    """构造 watchdog 事件处理器：只关心收件箱内的 .md 增删改。"""
    if Observer is None:
        raise RuntimeError("缺少依赖 watchdog，请先 pip install watchdog")

    class InboxHandler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
        def on_any_event(self, event) -> None:  # noqa: ANN001
            try:
                raw = getattr(event, "dest_path", None) or event.src_path
                if not raw:
                    return
                state.on_event(Path(raw), is_delete=(event.event_type == "deleted"))
            except Exception:  # noqa: BLE001
                LOG.exception("事件处理异常")

    return InboxHandler()


# ================================================================== 常驻守护
def run_daemon(cfg: Dict[str, Any]) -> None:
    vaults = build_vaults(cfg)
    client = LLMClient(cfg)
    if not client.ping():
        LOG.warning("LM Studio 未响应（%s），守护进程继续启动，处理时将自动重试",
                    cfg["lm_studio"]["base_url"])
    states = [VaultState(cfg, client, v) for v in vaults]
    observers = []
    for st in states:
        obs = Observer(timeout=1.0)
        obs.schedule(make_event_handler(st), str(st.vault.inbox), recursive=True)
        obs.start()
        observers.append(obs)
        st.seed_inbox()
        st.start()
        LOG.info("已监听 Vault [%s] 收件箱：%s", st.vault.name, st.vault.inbox)

    stop = threading.Event()

    def _sig(_sig_num, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    LOG.info("SmartVault 摄入守护进程运行中（监听 %d 个 Vault），Ctrl+C 退出", len(states))
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        LOG.info("正在停止……")
        for st in states:
            st.stop()
        for obs in observers:
            obs.stop()
        for obs in observers:
            obs.join(timeout=5)
        for st in states:
            st.scheduler.join(timeout=5)
            st.worker.join(timeout=5)
        LOG.info("SmartVault 摄入守护进程已退出")


# ================================================================== 自检 / 批处理 / CLI
def run_check(cfg: Dict[str, Any]) -> int:
    """环境自检：逐项探测配置、依赖、LM Studio 与模型可用性。"""
    print(f"\n=== {APP_NAME} 环境自检 ===")
    ok_all = True

    def probe(label: str, fn) -> None:  # noqa: ANN001
        nonlocal ok_all
        try:
            msg = fn()
            print(f"  [✓] {label}" + (f"：{msg}" if msg else ""))
        except Exception as e:  # noqa: BLE001
            ok_all = False
            print(f"  [✗] {label}：{e}")

    probe("config.json 解析", lambda: f"{len(cfg.get('vaults', []))} 个 Vault")
    for item in cfg.get("vaults", []):
        p = Path(str(item["path"])).expanduser()
        probe(f"Vault 可达：{item.get('name', p.name)}",
              lambda p=p: (p.is_dir() or (_ for _ in ()).throw(FileNotFoundError(p))))
    probe("LM Studio 连通", lambda: (requests is not None and requests.get(
        cfg["lm_studio"]["base_url"].rstrip("/") + "/models", timeout=4).ok)
        or (_ for _ in ()).throw(RuntimeError(f"{cfg['lm_studio']['base_url']} 未响应")))
    for mod, name in [("ocrmac", "图像 OCR（ocrmac）"), ("fitz", "PDF（PyMuPDF）"),
                      ("docx", "Word（python-docx）"), ("openpyxl", "Excel（openpyxl）"),
                      ("pptx", "PPT（python-pptx）")]:
        probe(name, lambda m=mod: __import__(m) and "")
    probe("whisper 后端", lambda: "")
    for backend in ("mlx_whisper", "whisper"):
        try:
            __import__(backend)
            print(f"      - 可用：{backend}")
        except ImportError:
            print(f"      - 未安装：{backend}")
    emb_abs = (Path(cfg["_config_dir"]) / cfg["rag"].get("embedding_model_path", "")).resolve()
    probe("本地嵌入模型目录", lambda: (emb_abs / "config.json").is_file()
          or (_ for _ in ()).throw(FileNotFoundError(f"{emb_abs}（需手动下载）")))
    print("=== 自检结束 ===\n")
    return 0 if ok_all else 1


def run_scan(cfg: Dict[str, Any]) -> int:
    """一次性处理所有收件箱中的积压草稿（处理完退出，适合手动批处理）。"""
    vaults = build_vaults(cfg)
    client = LLMClient(cfg)
    total = done = 0
    for vault in vaults:
        drafts = [f for f in sorted(vault.inbox.rglob("*.md")) if not f.name.startswith(".")]
        if not drafts:
            continue
        LOG.info("[%s] 扫描到 %d 篇积压草稿", vault.name, len(drafts))
        for f in drafts:
            total += 1
            try:
                run_pipeline(cfg, client, vault, f)
                done += 1
            except Exception:  # noqa: BLE001
                LOG.exception("处理失败（草稿保留原处）：%s", f)
    LOG.info("批处理完成：%d/%d 成功", done, total)
    return 0 if done == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartVault 摄入与归档守护进程")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.json 路径")
    parser.add_argument("--check", action="store_true", help="环境自检后退出")
    parser.add_argument("--scan", action="store_true", help="处理积压草稿后退出")
    parser.add_argument("--once", metavar="DRAFT_MD", help="调试：处理单篇草稿")
    parser.add_argument("--vault", metavar="NAME", help="配合 --once 指定 Vault 名称")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg, getattr(logging, args.log_level.upper(), logging.INFO))
    if args.check:
        sys.exit(run_check(cfg))
    if args.scan:
        sys.exit(run_scan(cfg))
    if args.once:
        vaults = {v.name: v for v in build_vaults(cfg)}
        key = args.vault or ""
        if key not in vaults:
            raise SystemExit(f"--vault 必须是以下之一：{', '.join(vaults)}")
        draft = Path(args.once).expanduser()
        if not draft.is_absolute():
            draft = vaults[key].inbox / draft
        if not draft.is_file():
            raise SystemExit(f"草稿不存在：{draft}")
        sys.exit(0 if run_pipeline(cfg, LLMClient(cfg), vaults[key], draft) else 1)
    run_daemon(cfg)


if __name__ == "__main__":
    main()










