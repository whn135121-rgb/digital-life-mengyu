"""只读探查：五算子（O1/O2/O3/O5）在现有库上「能不能走通」。

不看代码能不能改，只看数据与结构能不能支撑：
- O1 抽取：有没有可聚类的组（同会话 / 语义相似）
- O2 合并：有没有真正重复到该合并的条目（文档级 & 原子级 Jaccard/包含度）
- O3 重标定：有没有「权重」这个量可缩放
- O5 冷热迁移：热库离 4800 条有多远，归档后还能不能召回

用法：python scripts/probe_consolidation.py [--db PATH]
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DB_DEFAULT = BASE / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"
INDEX_DEFAULT = BASE / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.index"


def _meta(raw):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _chars(text: str) -> list[str]:
    return [c for c in (text or "") if not c.isspace() and c not in "，。、！？；：「」“”,.!?;:"]


def _bigrams(text: str) -> set[str]:
    cs = _chars(text)
    return {cs[i] + cs[i + 1] for i in range(len(cs) - 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set, b: set) -> float:
    """a 有多少比例落在 b 里（取两个方向的最大值）。"""
    if not a or not b:
        return 0.0
    return max(len(a & b) / len(a), len(a & b) / len(b))


def load_docs(path: Path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    docs = []
    for row in conn.execute("SELECT id, text, metadata FROM documents ORDER BY id"):
        meta = _meta(row[2])
        docs.append({"id": row[0], "text": row[1] or "", "meta": meta})
    atoms = []
    if _has_atoms(conn):
        for row in conn.execute(
            "SELECT id, content, importance, confidence, status FROM memory_atoms"
        ):
            atoms.append(
                {"id": row[0], "text": row[1] or "", "importance": row[2], "confidence": row[3], "status": row[4]}
            )
    conn.close()
    return docs, atoms


def _has_atoms(conn) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_atoms'"
    ).fetchone()
    return row is not None


def index_vectors(path: Path):
    import faiss
    import numpy as np

    index = faiss.read_index(str(path))
    inner = faiss.downcast_index(index.index)
    vecs = inner.reconstruct_n(0, inner.ntotal).astype("float32")
    ids = faiss.vector_to_array(index.id_map).tolist()
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return {int(i): vecs[n] / norms[n] for n, i in enumerate(ids)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--index", default=str(INDEX_DEFAULT))
    args = ap.parse_args()

    db = Path(args.db)
    docs, atoms = load_docs(db)
    now = time.time()

    print(f"文档 {len(docs)} 条 / 原子 {len(atoms)} 条")

    # ---------- 现有结构 ----------
    status = Counter(str(d["meta"].get("status") or "active") for d in docs)
    print(f"文档状态分布：{dict(status)}")
    imps = [float(d["meta"].get("importance", 0.5) or 0.5) for d in docs]
    imps.sort()
    if imps:
        print(
            f"文档 importance：min {imps[0]:.2f} / p50 {imps[len(imps)//2]:.2f} / max {imps[-1]:.2f}"
        )
    ages = [(now - float(d["meta"].get("create_time") or 0)) / 86400 for d in docs]
    ages.sort()
    print(f"文档年龄（天）：min {ages[0]:.1f} / p50 {ages[len(ages)//2]:.1f} / max {ages[-1]:.1f}")

    # ---------- O1 候选与分组 ----------
    for max_imp, age_days in ((0.5, 7), (0.6, 7), (0.5, 3)):
        cutoff = now - age_days * 86400
        cand = [
            d
            for d in docs
            if str(d["meta"].get("status") or "active") == "active"
            and float(d["meta"].get("importance", 0.5) or 0.5) < max_imp
            and float(d["meta"].get("create_time") or 0) < cutoff
        ]
        by_session = defaultdict(list)
        for d in cand:
            sid = d["meta"].get("session_id")
            if sid:
                by_session[str(sid)].append(d)
        groups = [g for g in by_session.values() if len(g) >= 3]
        print(
            f"O1 候选（importance<{max_imp}, 龄>{age_days}d）：{len(cand)} 条，"
            f"按会话≥3 的组 {len(groups)} 个，涉及文档 {sum(len(g) for g in groups)} 条"
        )

    # ---------- O1 语义分组（用索引向量） ----------
    try:
        vecs = index_vectors(Path(args.index))
        cand = [
            d
            for d in docs
            if str(d["meta"].get("status") or "active") == "active"
            and float(d["meta"].get("importance", 0.5) or 0.5) < 0.6
            and float(d["meta"].get("create_time") or 0) < now - 7 * 86400
        ]
        have = [d["id"] for d in cand if d["id"] in vecs]
        pairs = 0
        for a, b in itertools.combinations(have, 2):
            sim = float(vecs[a] @ vecs[b])
            if sim >= 0.7:
                pairs += 1
        print(f"O1 语义候选 {len(have)} 条，相似度≥0.7 的对 {pairs} 个（决定能连成几组）")
    except Exception as exc:  # pragma: no cover - 缺 faiss 时跳过
        print(f"O1 语义分组跳过：{exc}")

    # ---------- O2 文档级重复 ----------
    active = [d for d in docs if str(d["meta"].get("status") or "active") == "active"]
    grams = {d["id"]: _bigrams(d["text"]) for d in active}
    dup_pairs = []
    for a, b in itertools.combinations(active, 2):
        ga, gb = grams[a["id"]], grams[b["id"]]
        j = jaccard(ga, gb)
        c = containment(ga, gb)
        if j >= 0.60 and c >= 0.78:
            dup_pairs.append((a["id"], b["id"], round(j, 3), round(c, 3)))
    print(f"O2 文档级（Jaccard≥0.60 且包含度≥0.78）：{len(dup_pairs)} 对 {dup_pairs[:5]}")
    dist = Counter()
    for a, b in itertools.combinations(active, 2):
        dist[round(jaccard(grams[a["id"]], grams[b["id"]]), 1)] += 1
    print(f"O2 文档级 Jaccard 分布：{dict(sorted(dist.items()))}")

    # ---------- O2 原子级重复 ----------
    if atoms:
        ag = {a["id"]: _bigrams(a["text"]) for a in atoms}
        atom_dups = []
        for a, b in itertools.combinations(atoms, 2):
            ga, gb = ag[a["id"]], ag[b["id"]]
            j = jaccard(ga, gb)
            if j >= 0.6:
                atom_dups.append((a["id"], b["id"], round(j, 3), round(containment(ga, gb), 3)))
        print(f"O2 原子级 Jaccard≥0.60：{len(atom_dups)} 对 {atom_dups[:5]}")
        imp = sorted(float(a["importance"] or 0.5) for a in atoms)
        conf = sorted(float(a["confidence"] or 0.5) for a in atoms)
        print(
            f"原子 importance p50 {imp[len(imp)//2]:.2f} max {imp[-1]:.2f}；"
            f"confidence p50 {conf[len(conf)//2]:.2f} max {conf[-1]:.2f}"
        )

    # ---------- O3 / O5 结构判定 ----------
    has_weight_field = any(
        key in d["meta"] for d in docs for key in ("weight", "w0", "base_weight", "region")
    )
    print(f"O3 文档里有没有 weight/region 字段：{has_weight_field}")
    hot = len(active)
    print(f"O5 热库 {hot} 条 / 硬顶 5000（{hot / 5000:.1%}），触发线 4800")
    print(f"O5 现有 status 取值：{dict(status)} —— 归档（archived）在检索里被硬过滤")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
