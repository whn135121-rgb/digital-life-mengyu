"""关系边类型派生（v4 §2.11 的 temporal / co / tag）：只读现有图数据，派生新边。

背景：现有 1855 条边只有 `describes`（实体→事实）与 `mentioned_in`（人物→事实），
时间维与共现维完全没有。这里补三种**结构边**，不改任何检索路径：

- `temporal`：同一会话里按 `create_time` 相邻的两条事实节点，间隔 ≤ 6 小时连一条，
  权重 exp(-间隔/6h)，让「同一周」「那天前后还发生了什么」可查；
- `co`：共享 ≥2 个主题节点的事实对，权重随共享主题数上升，让「同话题」可查；
- `tag`：共同描述 ≥3 条事实的主题节点对，让主题聚类可查。

`ground`（事实→证据）不在这里做：证据已经在 `memory_atoms.metadata.evidence` 与
`/lmem evidence` 里，图里没有「文档节点」，硬造节点收益为负。

用法：
    python scripts\\derive_edges.py                 # 只算不写（默认）
    python scripts\\derive_edges.py --apply         # 备份后写入
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"

TEMPORAL_WINDOW_SECONDS = 6 * 3600
TEMPORAL_MAX_PAIRS = 400
CO_MIN_SHARED = 2
CO_PER_FACT = 3
CO_MAX_EDGES = 400
TAG_MIN_SHARED = 3
TAG_MAX_EDGES = 120


def temporal_weight(gap_seconds: float) -> float:
    """间隔越近权重越高，1 小时以上开始明显掉。"""
    gap = max(0.0, float(gap_seconds))
    return round(max(0.10, min(0.95, math.exp(-gap / float(TEMPORAL_WINDOW_SECONDS)))), 3)


def co_weight(shared_topics: int) -> float:
    """共享 2 个主题是入门线（0.40），每多一个 +0.15。"""
    return round(min(0.90, 0.40 + 0.15 * max(0, int(shared_topics) - 2)), 3)


def tag_weight(shared_facts: int) -> float:
    return round(min(0.85, 0.30 + 0.10 * max(0, int(shared_facts) - 2)), 3)


def edge_key(kind: str, left: int, right: int) -> str:
    a, b = sorted((int(left), int(right)))
    return f"{kind}|{a}|{b}"


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def plan_temporal(facts: list[dict]) -> list[dict]:
    """同会话按时间排序后的相邻事实对。"""
    by_session: dict[str, list[dict]] = {}
    for fact in facts:
        by_session.setdefault(str(fact.get("session_id") or ""), []).append(fact)

    edges: list[dict] = []
    seen: set[str] = set()
    for session, items in by_session.items():
        ordered = sorted(items, key=lambda f: (_ts(f.get("create_time")), int(f["node_id"])))
        for prev, cur in zip(ordered, ordered[1:]):
            gap = _ts(cur.get("create_time")) - _ts(prev.get("create_time"))
            if gap < 0 or gap > TEMPORAL_WINDOW_SECONDS:
                continue
            key = edge_key("temporal", prev["node_id"], cur["node_id"])
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "edge_key": key,
                    "source_node_id": int(prev["node_id"]),
                    "target_node_id": int(cur["node_id"]),
                    "relation_type": "temporal",
                    "source_memory_id": int(cur.get("memory_id") or prev.get("memory_id") or 0),
                    "weight": temporal_weight(gap),
                    "confidence": 0.55,
                    "metadata": {"gap_seconds": round(gap, 1), "session": session},
                }
            )
            if len(edges) >= TEMPORAL_MAX_PAIRS:
                return edges
    return edges


def plan_co(facts: list[dict], describes: list[tuple[int, int]], node_types: dict[int, str] | None = None) -> list[dict]:
    """共享 ≥2 个主题的事实对；每个事实最多连 3 条，避免稠密。"""
    types = node_types or {}
    topics_of: dict[int, set[int]] = {}
    for entity_id, fact_id in describes:
        if types and types.get(int(entity_id)) not in (None, "topic"):
            continue
        topics_of.setdefault(int(fact_id), set()).add(int(entity_id))
    fact_ids = [int(f["node_id"]) for f in facts]

    edges: list[dict] = []
    seen: set[str] = set()
    degree: dict[int, int] = {}
    memory_of = {int(f["node_id"]): int(f.get("memory_id") or 0) for f in facts}

    for i, left in enumerate(fact_ids):
        left_topics = topics_of.get(left)
        if not left_topics:
            continue
        for right in fact_ids[i + 1 :]:
            shared = left_topics & topics_of.get(right, set())
            if len(shared) < CO_MIN_SHARED:
                continue
            key = edge_key("co", left, right)
            if key in seen:
                continue
            if degree.get(left, 0) >= CO_PER_FACT or degree.get(right, 0) >= CO_PER_FACT:
                continue
            seen.add(key)
            degree[left] = degree.get(left, 0) + 1
            degree[right] = degree.get(right, 0) + 1
            edges.append(
                {
                    "edge_key": key,
                    "source_node_id": left,
                    "target_node_id": right,
                    "relation_type": "co",
                    "source_memory_id": memory_of.get(right) or memory_of.get(left) or 0,
                    "weight": co_weight(len(shared)),
                    "confidence": 0.5,
                    "metadata": {"shared_topics": len(shared)},
                }
            )
            if len(edges) >= CO_MAX_EDGES:
                return edges
    return edges


def plan_tag(facts: list[dict], describes: list[tuple[int, int]], node_types: dict[int, str] | None = None) -> list[dict]:
    """共同描述 ≥3 条事实的主题对。"""
    types = node_types or {}
    facts_of: dict[int, set[int]] = {}
    for entity_id, fact_id in describes:
        if types and types.get(int(entity_id)) not in (None, "topic"):
            continue
        facts_of.setdefault(int(entity_id), set()).add(int(fact_id))
    topic_ids = sorted(facts_of)
    memory_of = {int(f["node_id"]): int(f.get("memory_id") or 0) for f in facts}

    edges: list[dict] = []
    seen: set[str] = set()
    for i, left in enumerate(topic_ids):
        for right in topic_ids[i + 1 :]:
            shared = facts_of[left] & facts_of.get(right, set())
            if len(shared) < TAG_MIN_SHARED:
                continue
            key = edge_key("tag", left, right)
            if key in seen:
                continue
            seen.add(key)
            memory_id = memory_of.get(next(iter(shared)), 0)
            edges.append(
                {
                    "edge_key": key,
                    "source_node_id": left,
                    "target_node_id": right,
                    "relation_type": "tag",
                    "source_memory_id": memory_id,
                    "weight": tag_weight(len(shared)),
                    "confidence": 0.45,
                    "metadata": {"shared_facts": len(shared)},
                }
            )
            if len(edges) >= TAG_MAX_EDGES:
                return edges
    return edges


def load_graph(conn: sqlite3.Connection) -> tuple[list[dict], list[tuple[int, int]], set[str], dict[int, str]]:
    facts: list[dict] = []
    node_types: dict[int, str] = {
        int(nid): str(ntype) for nid, ntype in conn.execute("SELECT id, node_type FROM graph_nodes")
    }
    rows = conn.execute(
        "SELECT source_memory_id, session_id, content, metadata FROM graph_entries WHERE entry_type = 'fact'"
    ).fetchall()
    for memory_id, session_id, content, metadata in rows:
        try:
            meta = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            meta = {}
        text = str(content or "")
        if text.startswith("Atom: "):
            text = text[len("Atom: ") :]
        node_key = f"fact:{text}"
        facts.append(
            {
                "node_id": 0,
                "node_key": node_key,
                "memory_id": int(memory_id or 0),
                "session_id": session_id,
                "create_time": meta.get("create_time", 0),
            }
        )

    key_to_id = {
        str(key): int(nid)
        for nid, key in conn.execute("SELECT id, node_key FROM graph_nodes WHERE node_type = 'fact'")
    }
    resolved: list[dict] = []
    for fact in facts:
        nid = key_to_id.get(fact["node_key"])
        if nid:
            fact["node_id"] = nid
            resolved.append(fact)
    facts = resolved

    describes: list[tuple[int, int]] = []
    for source, target in conn.execute(
        "SELECT source_node_id, target_node_id FROM graph_edges WHERE relation_type = 'describes'"
    ):
        describes.append((int(source), int(target)))

    existing = {row[0] for row in conn.execute("SELECT edge_key FROM graph_edges")}
    return facts, describes, existing, node_types



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default="", help="另指定数据库（默认线上库）")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    conn = sqlite3.connect(db_path)
    try:
        facts, describes, existing, node_types = load_graph(conn)
        print(f"事实节点 {len(facts)}，describes 边 {len(describes)}，现有边 {len(existing)}")
        candidates = (
            plan_temporal(facts)
            + plan_co(facts, describes, node_types)
            + plan_tag(facts, describes, node_types)
        )
        fresh = [c for c in candidates if c["edge_key"] not in existing]
        by_type: dict[str, int] = {}
        for item in fresh:
            by_type[item["relation_type"]] = by_type.get(item["relation_type"], 0) + 1
        print(f"待写入 {len(fresh)} 条：{by_type}")

        if not args.apply:
            for item in fresh[:5]:
                print("  ", item["relation_type"], item["edge_key"], item["weight"])
            return 0

        backup = db_path.with_name(db_path.name + ".bak-edges-20261028")
        if not backup.exists():
            import shutil

            shutil.copy2(db_path, backup)
            print(f"已备份 {backup.name}")

        now = datetime.now().astimezone().isoformat()
        inserted = 0
        for item in fresh:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO graph_edges(
                    edge_key, source_node_id, target_node_id, relation_type,
                    source_memory_id, weight, confidence, status, metadata,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["edge_key"],
                    item["source_node_id"],
                    item["target_node_id"],
                    item["relation_type"],
                    item["source_memory_id"],
                    item["weight"],
                    item["confidence"],
                    "active",
                    json.dumps(item["metadata"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
        print(f"已写入 {inserted} 条")
        print("边类型分布：", conn.execute("SELECT relation_type, COUNT(*) FROM graph_edges GROUP BY 1").fetchall())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
