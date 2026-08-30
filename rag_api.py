#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartVault / 智能仓库 —— 模块 B：对话式知识查询服务 (FastAPI Local RAG)
================================================================================
职责：
  1. 索引管道：递归读取所有注册 Vault 下的 .md 文件，Markdown 友好分块
     （MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter），
     调用本地嵌入模型（BAAI/bge-small-zh-v1.5，MPS/Metal 加速）生成向量，
     存入本地文件型 ChromaDB，支持基于 mtime+md5 的增量更新与删除同步。
  2. 检索问答：POST /ask 接收 Query -> 向量检索 Top-K -> 组装 RAG Prompt
     -> 呼叫本地 LM Studio -> 以 SSE 流式或完整 JSON 返回（附参考笔记相对路径）。
  3. 辅助接口：/health（健康检查）、/status（索引统计）、/reindex（手动增量/全量重建）。

隐私承诺：
  - 进程启动即设置 HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE，禁用 HuggingFace 在线检查；
  - ChromaDB 关闭匿名遥测（anonymized_telemetry=False）；
  - 仅监听 127.0.0.1，唯一外部依赖是本机 LM Studio (localhost:1234)。

用法：
  python rag_api.py --config config.json                 # 默认 127.0.0.1:8788
  uvicorn rag_api:app --host 127.0.0.1 --port 8788       # 等效启动方式
  python scripts/build_index.py --rebuild                # 手动全量重建索引
"""
from __future__ import annotations

import os

# ---- 离线开关必须在导入 transformers/chromadb 之前生效（隐私硬约束）----
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import argparse
import hashlib
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Tuple

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

APP_NAME = "SmartVault-RAG"
LOG = logging.getLogger("smartvault.rag")

HIDDEN_DEFAULTS = {".obsidian", ".trash", ".git", ".stfolder", ".smartvault", ".DS_Store"}
_STATE_PATH = "data/index_state.json"  # 相对 config.json 目录

CONFIG_DEFAULTS: Dict[str, Any] = {
    "lm_studio": {"base_url": "http://localhost:1234/v1", "chat_model": "qwen2.5-7b-instruct",
                  "temperature": 0.3, "max_tokens": 4096, "timeout_seconds": 300},
    "inbox_folder_name": "待处理笔记",
    "context_file": "ai_context.md",
    "vaults": [],
    "log_dir": "logs",
    "rag": {"enabled": True, "embedding_model_path": "models/bge-small-zh-v1.5",
            "embedding_device": "mps", "chroma_dir": "data/chroma", "collection_name": "smartvault",
            "chunk_size": 500, "chunk_overlap": 80, "top_k": 4, "rescan_seconds": 300,
            "exclude_folders": [".obsidian", ".trash", "待处理笔记"]},
    "api": {"host": "127.0.0.1", "port": 8788},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> Dict[str, Any]:
    """读取 config.json 并合并默认值；rag 相关相对路径解析为绝对路径。"""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = json.load(f)
    cfg = _deep_merge(CONFIG_DEFAULTS, user_cfg or {})
    cfg_dir = path.parent
    rag = cfg["rag"]
    rag["embedding_model_path_abs"] = str((cfg_dir / rag["embedding_model_path"]).resolve())
    rag["chroma_dir_abs"] = str((cfg_dir / rag["chroma_dir"]).resolve())
    cfg["_config_dir"] = str(cfg_dir)
    cfg["log_dir_abs"] = str((cfg_dir / cfg.get("log_dir", "logs")).resolve())
    cfg["_state_path_abs"] = str((cfg_dir / _STATE_PATH).resolve())
    return cfg


def setup_logging(cfg: Dict[str, Any], level: int = logging.INFO) -> None:
    log_dir = Path(cfg["log_dir_abs"])
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger("smartvault")
    root.setLevel(level)
    root.handlers.clear()
    fh = RotatingFileHandler(log_dir / "rag_api.log", maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    root.propagate = False


# ================================================================== 嵌入模型
class BGEEmbeddings(HuggingFaceEmbeddings):
    """bge 中文系列模型的检索问句指令包装（官方推荐 query 前缀）。

    注意：HuggingFaceEmbeddings 是 pydantic v2 模型，类体内裸赋值的属性
    会被当成"未注解字段"而在类定义时抛错，因此必须用 ClassVar 注解。
    """

    QUERY_INSTRUCTION: ClassVar[str] = "为这个句子生成表示以用于检索相关文章："

    def embed_query(self, text: str) -> List[float]:
        return super().embed_query(self.QUERY_INSTRUCTION + text)


def _build_embeddings(cfg: Dict[str, Any]) -> BGEEmbeddings:
    rag = cfg["rag"]
    model_path = Path(rag["embedding_model_path_abs"])
    if not (model_path / "config.json").exists():
        raise FileNotFoundError(
            f"本地嵌入模型未找到：{model_path}\n"
            f"请先一次性下载（之后完全离线）：\n"
            f"  pip install \"huggingface_hub[cli]\"\n"
            f"  huggingface-cli download BAAI/bge-small-zh-v1.5 --local-dir {model_path}"
        )
    device = rag.get("embedding_device", "mps")

    def _make(dev: str) -> BGEEmbeddings:
        return BGEEmbeddings(
            model_name=str(model_path),
            model_kwargs={"device": dev},
            encode_kwargs={"normalize_embeddings": True},
        )

    try:
        return _make(device)
    except Exception as e:  # noqa: BLE001 —— mps 不可用时回退 CPU
        LOG.warning("嵌入模型在 %s 初始化失败（%s），回退 CPU", device, e)
        return _make("cpu")


# ================================================================== 工具函数
def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_FRONTMATTER_RE = re.compile(r"\A\uFEFF?---\s*\n(.*?)\n?\s*---\s*\n?", re.S)


def strip_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """剥离 YAML frontmatter，返回 (简单键值对, 正文)。"""
    m = _FRONTMATTER_RE.match(text or "")
    meta: Dict[str, str] = {}
    if not m:
        return meta, (text or "")
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, text[m.end():]


# ================================================================== 索引器
class VaultIndexer:
    """递归索引所有 Vault 的 .md 文件 -> Markdown 分块 -> 本地嵌入 -> ChromaDB。"""

    MAX_FILE_BYTES = 2 * 1024 * 1024  # 跳过超过 2MB 的异常笔记

    def __init__(self, cfg: Dict[str, Any]):
        import chromadb
        from chromadb.config import Settings

        self.cfg = cfg
        self.rag = cfg["rag"]
        self.lock = threading.RLock()
        self.syncing = False
        self.last_sync: Optional[str] = None

        chroma_dir = Path(self.rag["chroma_dir_abs"])
        chroma_dir.mkdir(parents=True, exist_ok=True)
        # 隐私：关闭 ChromaDB 匿名遥测，纯本地持久化
        self.client = chromadb.PersistentClient(
            path=str(chroma_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self.embeddings = _build_embeddings(cfg)
        self.store = self._make_store()
        self.header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
            strip_headers=False,  # 保留标题文本，利于检索语义
        )
        self.chunk_splitter = RecursiveCharacterTextSplitter(
            chunk_size=int(self.rag.get("chunk_size", 500)),
            chunk_overlap=int(self.rag.get("chunk_overlap", 80)),
            length_function=len,
        )
        self.state: Dict[str, Dict[str, Any]] = self._load_state()

    def _make_store(self) -> Chroma:
        return Chroma(
            collection_name=self.rag["collection_name"],
            embedding_function=self.embeddings,
            client=self.client,
            collection_metadata={"hnsw:space": "cosine"},
        )

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        p = Path(self.cfg["_state_path_abs"])
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                LOG.warning("索引状态文件损坏，已忽略：%s", p)
        return {}

    def _save_state(self) -> None:
        p = Path(self.cfg["_state_path_abs"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _exclude_names(self) -> set:
        return (set(HIDDEN_DEFAULTS) | {self.cfg["inbox_folder_name"]}
                | set(self.rag.get("exclude_folders", [])))

    def iter_md_files(self) -> List[Tuple[str, Path]]:
        """返回 [(文件键=仓库名/相对路径, 绝对路径), ...]，排除隐藏目录与收件箱。"""
        excludes = self._exclude_names()
        out: List[Tuple[str, Path]] = []
        for item in self.cfg.get("vaults", []):
            root = Path(str(item["path"])).expanduser()
            name = str(item.get("name") or root.name)
            if not root.is_dir():
                LOG.warning("RAG：Vault 不可达，跳过 -> %s", root)
                continue
            for f in sorted(root.rglob("*.md")):
                rel = f.relative_to(root)
                if any(part in excludes for part in rel.parts):
                    continue
                out.append((f"{name}/{rel.as_posix()}", f))
        return out

    # ---------------------------------------------------------------- 索引逻辑
    def split_documents(self, key: str, vault_name: str,
                        text: str, meta: Dict[str, str]) -> List[Document]:
        """Markdown 友好分块：先按标题切，再按长度递归切；携带来源与标题链元数据。"""
        if not text.strip():
            return []
        base_meta = {"source": key, "vault": vault_name,
                     "title": meta.get("title") or Path(key).stem}
        out: List[Document] = []
        for d in self.header_splitter.split_text(text):
            heading = " > ".join(str(v) for v in d.metadata.values() if v)
            for piece in self.chunk_splitter.split_text(d.page_content):
                if len(piece.strip()) < 20:  # 丢弃无意义碎屑
                    continue
                m = dict(base_meta)
                if heading:
                    m["heading"] = heading
                out.append(Document(page_content=piece, metadata=m))
        return out

    def sync(self, force: bool = False) -> Dict[str, int]:
        """增量同步：新增/修改 -> 重建该文件向量；删除 -> 移除向量。"""
        stats = {"scanned": 0, "added": 0, "updated": 0, "removed": 0}
        with self.lock:
            if self.syncing:
                LOG.info("已有索引任务进行中，跳过本次触发")
                return stats
            self.syncing = True
        try:
            files = self.iter_md_files()
            stats["scanned"] = len(files)
            current_keys = {k for k, _ in files}
            with self.lock:
                # 1) 处理已删除文件
                for key in list(self.state.keys()):
                    if key not in current_keys:
                        ids = self.state[key].get("ids", [])
                        if ids:
                            self.store.delete(ids=ids)
                        self.state.pop(key, None)
                        stats["removed"] += 1
                # 2) 处理新增/修改文件（mtime 短路 + md5 兜底）
                for key, path in files:
                    try:
                        st = path.stat()
                        if st.st_size > self.MAX_FILE_BYTES:
                            LOG.warning("文件过大，跳过索引：%s", key)
                            continue
                        mtime = st.st_mtime_ns
                        old = self.state.get(key)
                        if old and not force and old.get("mtime") == mtime:
                            continue
                        digest = _md5_file(path)
                        if old and not force and old.get("md5") == digest:
                            old["mtime"] = mtime
                            continue
                        text = path.read_text(encoding="utf-8", errors="replace")
                        fm, body = strip_frontmatter(text)
                        vault_name = key.split("/", 1)[0]
                        docs = self.split_documents(key, vault_name, body, fm)
                        ids = [hashlib.md5(f"{key}#{i}".encode()).hexdigest()
                               for i in range(len(docs))]
                        if old and old.get("ids"):
                            self.store.delete(ids=old["ids"])
                        if docs:
                            self.store.add_documents(docs, ids=ids)
                        self.state[key] = {"mtime": mtime, "md5": digest, "ids": ids,
                                           "chunks": len(docs),
                                           "indexed_at": datetime.now().isoformat(timespec="seconds")}
                        stats["updated" if old else "added"] += 1
                        LOG.info("索引更新：%s（%d 块）", key, len(docs))
                    except Exception:  # noqa: BLE001 —— 单文件失败不影响整体
                        LOG.exception("索引单文件失败：%s", key)
                self._save_state()
            self.last_sync = datetime.now().isoformat(timespec="seconds")
            LOG.info("增量同步完成：%s", stats)
            return stats
        finally:
            with self.lock:
                self.syncing = False

    def rebuild(self) -> Dict[str, int]:
        """全量重建：清空向量集合与状态后重新同步。"""
        with self.lock:
            try:
                self.client.delete_collection(self.rag["collection_name"])
                LOG.info("已删除旧向量集合：%s", self.rag["collection_name"])
            except Exception:  # noqa: BLE001 —— 集合本就不存在
                pass
            self.store = self._make_store()
            self.state = {}
            self._save_state()
        return self.sync(force=True)

    def search(self, query: str, k: int) -> List[Tuple[Document, float]]:
        """向量检索 Top-K，返回 (文档, 余弦距离)，距离越小越相关。"""
        with self.lock:
            return self.store.similarity_search_with_score(query, k=k)

    def count(self) -> int:
        with self.lock:
            try:
                return int(self.store._collection.count())
            except Exception:  # noqa: BLE001
                return -1


# ================================================================== 单例管理
_CFG: Optional[Dict[str, Any]] = None
_INDEXER: Optional[VaultIndexer] = None
_INDEXER_LOCK = threading.Lock()


def set_config(cfg: Dict[str, Any]) -> None:
    global _CFG
    _CFG = cfg


def get_indexer() -> VaultIndexer:
    """进程级懒加载单例：首次调用时初始化嵌入模型与 ChromaDB（较重）。"""
    global _INDEXER
    with _INDEXER_LOCK:
        if _INDEXER is None:
            if _CFG is None:
                raise RuntimeError("配置尚未初始化，请先调用 set_config()")
            _INDEXER = VaultIndexer(_CFG)
        return _INDEXER


# ================================================================== LM Studio 客户端
class LMStudioClient:
    """LM Studio（OpenAI 兼容 /v1）客户端，支持阻塞与 SSE 流式两种调用。"""

    def __init__(self, cfg: Dict[str, Any]):
        lm = cfg["lm_studio"]
        self.base_url = str(lm["base_url"]).rstrip("/")
        self.model = lm["chat_model"]
        self.temperature = float(lm.get("temperature", 0.3))
        self.max_tokens = int(lm.get("max_tokens", 4096))
        self.timeout = int(lm.get("timeout_seconds", 300))

    def _payload(self, messages: List[Dict[str, str]], stream: bool,
                 temperature: Optional[float]) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }

    def chat(self, messages: List[Dict[str, str]],
             temperature: Optional[float] = None) -> str:
        resp = requests.post(f"{self.base_url}/chat/completions",
                             json=self._payload(messages, False, temperature),
                             timeout=self.timeout)
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"] or "")

    def stream_chat(self, messages: List[Dict[str, str]],
                    temperature: Optional[float] = None) -> Iterator[str]:
        """生成器：逐段产出模型输出文本增量。

        编码陷阱：LM Studio 的 text/event-stream 响应头不带 charset，requests
        会按 RFC 默认 ISO-8859-1 解码（decode_unicode=True 将得到 ç¬è®° 式乱码），
        因此必须逐行取原始 bytes 并显式按 UTF-8 解码。
        """
        with requests.post(f"{self.base_url}/chat/completions",
                           json=self._payload(messages, True, temperature),
                           timeout=(10, self.timeout), stream=True) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content")
                except Exception:  # noqa: BLE001 —— 跳过坏帧
                    continue
                if delta:
                    yield delta

    def ping(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/models", timeout=4).ok
        except Exception:  # noqa: BLE001
            return False


# ================================================================== RAG Prompt
RAG_SYSTEM_PROMPT = (
    "你是 SmartVault 本地知识库助手。只能依据用户提供的笔记上下文回答问题；"
    "上下文中没有的信息必须坦诚说明“笔记中未找到相关内容”，严禁编造。"
    "回答使用简体中文，条理清晰，善用列表与标题，关键结论先行。"
)


def build_rag_user_prompt(query: str, chunks: List[Tuple[Document, float]]) -> str:
    blocks = []
    for i, (doc, dist) in enumerate(chunks, 1):
        src = doc.metadata.get("source", "未知来源")
        rel = max(0.0, 1.0 / (1.0 + float(dist)))  # 距离 -> 估算相关度
        blocks.append(f"【片段 {i}｜来源：{src}｜相关度：{rel:.2f}】\n{doc.page_content}")
    context = "\n\n".join(blocks)
    return (
        f"基于以下提供的本地笔记上下文：\n\n{context}\n\n"
        f"请回答用户的问题：{query}\n"
        f"请在回答末尾附上参考笔记的相对路径。"
    )


def unique_sources(chunks: List[Tuple[Document, float]]) -> List[Dict[str, Any]]:
    """汇总 Top-K 命中的去重来源（回答引用凭据）。"""
    seen: Dict[str, Dict[str, Any]] = {}
    for doc, dist in chunks:
        src = doc.metadata.get("source", "未知来源")
        if src not in seen:
            seen[src] = {"path": src, "title": doc.metadata.get("title", ""),
                         "distance": round(float(dist), 4)}
    return list(seen.values())


# ================================================================== FastAPI 应用
class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    top_k: int = Field(4, ge=1, le=10, description="检索片段数")
    stream: bool = Field(False, description="是否以 SSE 流式返回")
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)


class ReindexRequest(BaseModel):
    rebuild: bool = Field(False, description="true=全量重建，false=增量同步")


def _sse(event: str = "message", data: Any = None) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_SYNC_THREAD: Optional[threading.Thread] = None
_SYNC_STOP = threading.Event()


def _auto_sync_loop(stop_flag: threading.Event) -> None:
    """后台守护线程：启动即增量同步，此后按 rescan_seconds 周期增量同步。"""
    interval = int(_CFG["rag"].get("rescan_seconds", 300)) if _CFG else 300
    while not stop_flag.is_set():
        try:
            get_indexer().sync()
        except Exception:  # noqa: BLE001 —— 初始化失败时周期重试
            LOG.exception("后台索引同步失败，将按周期重试")
        stop_flag.wait(max(10, interval))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SYNC_THREAD
    if _CFG is None:  # 兼容 uvicorn 直接导入（未走 __main__）
        cfg_path = Path(os.environ.get("SMARTVAULT_CONFIG",
                                       Path(__file__).parent / "config.json"))
        set_config(load_config(cfg_path))
    setup_logging(_CFG)
    if _CFG["rag"].get("enabled", True):
        _SYNC_STOP.clear()
        _SYNC_THREAD = threading.Thread(target=_auto_sync_loop, args=(_SYNC_STOP,),
                                        name="rag-sync", daemon=True)
        _SYNC_THREAD.start()
        LOG.info("后台索引线程已启动")
    yield
    _SYNC_STOP.set()
    LOG.info("SmartVault RAG 服务已停止")


app = FastAPI(title="SmartVault 本地知识库", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 仅本机回环监听，供 Obsidian 插件/本地页面跨源调用
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> Dict[str, Any]:
    return {"app": APP_NAME, "endpoints": ["/ui", "/ask", "/health", "/status", "/reindex"],
            "docs": "/docs"}


@app.get("/ui", include_in_schema=False)
def ui():
    """浏览器聊天界面：static/chat.html 单页应用（离线零依赖，SSE 流式问答）。"""
    return FileResponse(Path(__file__).parent / "static" / "chat.html")


@app.get("/health")
def health() -> Dict[str, Any]:
    lm_ok = LMStudioClient(_CFG).ping()
    try:
        idx = get_indexer()
        emb_ok, chunks = True, idx.count()
    except Exception as e:  # noqa: BLE001
        emb_ok, chunks = False, str(e)
    return {"status": "ok" if (lm_ok and emb_ok) else "degraded",
            "lm_studio": lm_ok, "embeddings": emb_ok, "chunks": chunks}


@app.get("/status")
def status() -> Dict[str, Any]:
    try:
        idx = get_indexer()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"索引器未就绪：{e}")
    return {"files_indexed": len(idx.state), "chunks": idx.count(),
            "last_sync": idx.last_sync, "syncing": idx.syncing,
            "embedding_model": idx.rag["embedding_model_path_abs"],
            "lm_studio_model": _CFG["lm_studio"]["chat_model"]}


@app.post("/reindex")
def reindex(req: ReindexRequest) -> Dict[str, Any]:
    try:
        idx = get_indexer()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"索引器初始化失败：{e}")

    def _run() -> None:
        try:
            (idx.rebuild if req.rebuild else idx.sync)()
        except Exception:  # noqa: BLE001
            LOG.exception("手动重建索引失败")
    threading.Thread(target=_run, name="rag-manual-sync", daemon=True).start()
    return {"started": True, "mode": "rebuild" if req.rebuild else "incremental"}


@app.post("/ask")
def ask(req: AskRequest):
    try:
        idx = get_indexer()
        chunks = idx.search(req.query, k=req.top_k)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"检索失败：{e}")
    if not chunks:
        return {"answer": "索引为空或未命中任何笔记，请先执行 POST /reindex 建立索引。",
                "sources": []}
    sources = unique_sources(chunks)
    messages = [{"role": "system", "content": RAG_SYSTEM_PROMPT},
                {"role": "user", "content": build_rag_user_prompt(req.query, chunks)}]
    client = LMStudioClient(_CFG)

    if req.stream:
        def gen() -> Iterator[str]:
            yield _sse("sources", sources)
            try:
                for delta in client.stream_chat(messages, req.temperature):
                    yield _sse("message", {"delta": delta})
            except Exception as e:  # noqa: BLE001
                yield _sse("error", {"error": f"LM Studio 调用失败：{e}"})
            yield "event: done\ndata: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    try:
        answer = client.chat(messages, req.temperature)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"LM Studio 调用失败：{e}")
    return {"answer": answer, "sources": sources}


# ================================================================== 入口
def main() -> None:
    parser = argparse.ArgumentParser(description="SmartVault 本地 RAG 服务")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    parser.add_argument("--host", default=None, help="默认取 config.api.host")
    parser.add_argument("--port", type=int, default=None, help="默认取 config.api.port")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    os.environ["SMARTVAULT_CONFIG"] = str(Path(args.config).resolve())
    set_config(cfg)
    setup_logging(cfg)
    host = args.host or cfg["api"].get("host", "127.0.0.1")
    port = args.port or int(cfg["api"].get("port", 8788))
    LOG.info("SmartVault RAG 启动：http://%s:%d（LM Studio: %s）",
             host, port, cfg["lm_studio"]["base_url"])
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()





