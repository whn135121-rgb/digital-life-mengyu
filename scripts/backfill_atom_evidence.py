"""给已有的记忆原子补「证据链」（谁在什么时候说的）。

2026-10-28 S3 落地的一部分：写入侧已经会在 `metadata.evidence` 里记出处，但
此前入库的 290 条原子没有。它们本身带 `session_id` 和 `created_at`，够推断
出「会话 + 时间」，所以这里直接按行补齐，不需要回读原始对话。

用法：
    python scripts\\backfill_atom_evidence.py            # 直接写库（自动备份）
    python scripts\\backfill_atom_evidence.py --dry-run  # 只看会改多少条

只动 `memory_atoms.metadata` 里的 `evidence` 字段，不动 content / 时间 / 衰减。
已经有 evidence 的行跳过，可以反复跑。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"
EVIDENCE_PY = (
    ROOT
    / "qqbot"
    / "data"
    / "plugins"
    / "astrbot_plugin_livingmemory"
    / "core"
    / "utils"
    / "evidence.py"
)


def _load_evidence_module():
    """按文件路径加载 evidence.py —— 直接 import core.* 会拉起插件重依赖。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("dsh_lm_evidence", EVIDENCE_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_EVIDENCE = _load_evidence_module()
build_evidence = _EVIDENCE.build_evidence
ensure_evidence = _EVIDENCE.ensure_evidence


def plan_backfill(rows: list[tuple[Any, ...]]) -> list[tuple[int, str]]:
    """纯函数：吃 (atom_id, session_id, created_at, event_time, metadata) 行，吐要写的 (id, json)。"""
    updates: list[tuple[int, str]] = []
    for atom_id, session_id, created_at, event_time, metadata_raw in rows:
        try:
            metadata = json.loads(metadata_raw) if metadata_raw else {}
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        merged = ensure_evidence(
            metadata,
            session_id=session_id,
            said_at=created_at,
            event_at=event_time,
            source="backfill",
        )
        if merged is None:
            continue
        updates.append((int(atom_id), json.dumps(merged, ensure_ascii=False)))
    return updates


def get_meta_path(db: Path) -> Path:
    return db.with_suffix(db.suffix + ".bak-evidence-20261028")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}")
        return 2

    if not args.dry_run:
        backup = get_meta_path(db_path)
        if not backup.exists():
            shutil.copy2(db_path, backup)
            print(f"已备份：{backup.name}")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, session_id, created_at, event_time, metadata FROM memory_atoms"
        ).fetchall()
        updates = plan_backfill(rows)
        print(f"原子总数 {len(rows)}，需要补证据 {len(updates)}")
        if args.dry_run or not updates:
            if updates:
                print(f"示例：{updates[0]}")
            return 0
        conn.executemany(
            "UPDATE memory_atoms SET metadata = ? WHERE id = ?",
            [(payload, atom_id) for atom_id, payload in updates],
        )
        conn.commit()
        done = conn.execute(
            "SELECT COUNT(*) FROM memory_atoms WHERE metadata LIKE '%\"evidence\"%'"
        ).fetchone()[0]
        print(f"已写入 {len(updates)} 条，现有证据原子 {done}/{len(rows)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
