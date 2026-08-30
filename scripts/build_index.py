#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动索引维护工具：增量同步或全量重建向量索引。

用法：
  python scripts/build_index.py            # 增量同步（默认）
  python scripts/build_index.py --rebuild  # 全量重建（清空后重建）
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from rag_api import VaultIndexer, load_config, setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartVault 向量索引维护")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.json"))
    parser.add_argument("--rebuild", action="store_true", help="全量重建（默认增量同步）")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg)
    indexer = VaultIndexer(cfg)
    stats = indexer.rebuild() if args.rebuild else indexer.sync()
    print(f"完成：{stats}｜当前索引文件数：{len(indexer.state)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
