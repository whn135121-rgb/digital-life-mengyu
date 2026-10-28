"""分舱两级检索标定（v4 §2.10）：只测「值不值得接」，不改线上检索路径。

做法：
1. 从 FAISS 索引里取出 63 篇文档的向量（`reconstruct(doc_id)`，索引是 IndexIDMap，id 即 doc_id）；
2. 用 k-means 把文档聚成 8 个舱（每舱一个质心，确定性种子，可复现）；
3. 用线上 `/api/plug/.../page/recall/test` 对 15 个真实问题取 top-6，把这 6 篇的向量
   按相似度加权平均，当作「查询向量的代理」（插件没有暴露 query embedding 的接口）；
4. 用代理向量比 8 个质心，取最像的 2 舱，算候选集大小，看答案（标注文档）是否还在候选集里。

产出：scripts/cell_calibration.json + 控制台摘要。不写库、不改配置、不重启插件。

用法：
    python scripts\\calibrate_cells.py
"""

from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import faiss
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory"
INDEX_PATH = DATA / "livingmemory.index"
DB_PATH = DATA / "livingmemory.db"
OUT_PATH = ROOT / "scripts" / "cell_calibration.json"
BASE = "http://127.0.0.1:6185"
PLUGIN = "astrbot_plugin_livingmemory"
CELLS = 8
TOP_CELLS = 2

QUERIES = [
    ("我叫你什么", {59}),
    ("穿越成皇帝求速死的小说", {60}),
    ("余额还剩多少", {58}),
    ("橘猫坐键盘删文档", {57}),
    ("你答应过不乱改我", {58}),
    ("自己收录表情包", {56}),
    ("宿舍晚上打游戏", {50}),
    ("11点就困", {60}),
    ("星期日19点19分第一章", {60}),
    ("勤俭持家好男人", {59}),
    ("谁掏钱养你", {59}),
    ("天造地设的一对", {57}),
    ("45.76", {58}),
    ("我是不是有点活泼", {58}),
    ("群里有没有别人的记忆", {59}),
]


def _post(path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _login() -> str:
    out = _post("/api/v1/auth/login", {"username": "astrbot", "password": os.environ.get("ASTRBOT_PASSWORD", "")})
    return out["data"]["token"]


def load_index() -> tuple[np.ndarray, list[int]]:
    """取出向量与 doc_id。索引是 IndexIDMap(IndexFlatL2)，只有内层能 reconstruct_n。"""
    index = faiss.read_index(str(INDEX_PATH))
    inner = faiss.downcast_index(index.index) if hasattr(index, "index") else index
    ids = (
        faiss.vector_to_array(index.id_map).astype("int64").tolist()
        if hasattr(index, "id_map")
        else list(range(int(index.ntotal)))
    )
    vectors = inner.reconstruct_n(0, int(inner.ntotal)).astype("float32")
    if len(ids) != len(vectors):
        raise RuntimeError(f"id_map({len(ids)}) 与向量数({len(vectors)}) 不一致")
    return vectors, [int(i) for i in ids]


def normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def kmeans(vectors: np.ndarray, k: int = CELLS, iters: int = 40, seed: int = 20261028):
    """朴素 k-means（k-means++ 初始化），返回 (质心, 每点所属舱)。"""
    rng = np.random.default_rng(seed)
    n = len(vectors)
    k = min(k, n)
    centers = [vectors[int(rng.integers(n))]]
    dist2 = ((vectors - centers[0]) ** 2).sum(axis=1)
    for _ in range(1, k):
        probs = dist2 / dist2.sum() if dist2.sum() > 0 else np.full(n, 1.0 / n)
        idx = int(rng.choice(n, p=probs))
        centers.append(vectors[idx])
        dist2 = np.minimum(dist2, ((vectors - vectors[idx]) ** 2).sum(axis=1))
    centroids = np.stack(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        sims = normalize(vectors) @ normalize(centroids).T
        new_labels = sims.argmax(axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for c in range(k):
            members = vectors[labels == c]
            if len(members):
                centroids[c] = members.mean(axis=0)
    return centroids, labels


def doc_texts() -> dict[int, str]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, text FROM documents").fetchall()
    finally:
        conn.close()
    return {int(i): (t or "") for i, t in rows}


def main() -> int:
    started = time.perf_counter()
    vectors, ids = load_index()
    texts = doc_texts()
    missing = [i for i in ids if i not in texts]
    unit = normalize(vectors)
    centroids, labels = kmeans(unit)
    cell_members: dict[int, list[int]] = {}
    for doc_id, label in zip(ids, labels):
        cell_members.setdefault(int(label), []).append(int(doc_id))
    sizes = sorted(len(v) for v in cell_members.values())

    token = _login()
    routing_us: list[float] = []
    rows = []
    hit_baseline = 0
    hit_routed = 0
    candidate_sizes: list[int] = []

    for query, expected in QUERIES:
        out = _post(
            f"/api/plug/{PLUGIN}/page/recall/test",
            {"query": query, "k": 6},
            token,
        )
        results = (out.get("data") or {}).get("results") or []
        baseline_ids = [int(r["memory_id"]) for r in results]
        if expected & set(baseline_ids):
            hit_baseline += 1

        # 用 top-6 的向量按相似度加权，当作查询向量代理
        acc = np.zeros(vectors.shape[1], dtype="float32")
        total_w = 0.0
        for r in results:
            doc_id = int(r["memory_id"])
            if doc_id not in set(ids):
                continue
            weight = float(r.get("similarity_score") or 0.0)
            acc += unit[ids.index(doc_id)] * weight
            total_w += weight
        if total_w <= 0:
            rows.append({"query": query, "skipped": True})
            continue
        q = acc / total_w
        q = q / (np.linalg.norm(q) or 1.0)

        t0 = time.perf_counter()
        sims = normalize(centroids) @ q
        top = np.argsort(-sims)[:TOP_CELLS]
        routing_us.append((time.perf_counter() - t0) * 1e6)

        candidates = [d for c in top for d in cell_members.get(int(c), [])]
        candidate_sizes.append(len(candidates))
        routed_hit = bool(expected & set(candidates))
        if routed_hit:
            hit_routed += 1

        # 在候选集里按余弦取 top-6，看答案还在不在最终结果里
        cand_vectors = unit[[ids.index(d) for d in candidates]]
        order = np.argsort(-(cand_vectors @ q))[:6]
        routed_top = [candidates[int(i)] for i in order]
        rows.append(
            {
                "query": query,
                "expected": sorted(expected),
                "baseline_top6": baseline_ids,
                "baseline_hit": bool(expected & set(baseline_ids)),
                "top_cells": [int(c) for c in top],
                "candidate_size": len(candidates),
                "expected_in_candidates": routed_hit,
                "routed_top6": routed_top,
                "routed_top6_hit": bool(expected & set(routed_top)),
            }
        )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "index_path": str(INDEX_PATH),
        "docs": len(ids),
        "missing_text_docs": missing,
        "cells": CELLS,
        "top_cells": TOP_CELLS,
        "cell_sizes": sizes,
        "coverage": {f"cell_{k}": len(v) for k, v in sorted(cell_members.items())},
        "queries": len(QUERIES),
        "baseline_hit": hit_baseline,
        "expected_in_candidates": hit_routed,
        "avg_candidate_size": round(float(np.mean(candidate_sizes)), 1) if candidate_sizes else 0,
        "max_candidate_size": max(candidate_sizes) if candidate_sizes else 0,
        "routing_us_avg": round(float(np.mean(routing_us)), 1) if routing_us else 0,
        "p95_candidate_size": int(np.percentile(candidate_sizes, 95)) if candidate_sizes else 0,
        "details": rows,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"文档 {len(ids)}，8 舱大小 {sizes}")
    print(f"15 问：基线 top6 命中 {hit_baseline}/15，答案落在 2 舱候选集 {hit_routed}/15")
    print(
        f"候选集：平均 {report['avg_candidate_size']} 篇（最大 {report['max_candidate_size']}，"
        f"p95 {report['p95_candidate_size']}），全库 {len(ids)} 篇"
    )
    print(f"路由耗时：平均 {report['routing_us_avg']} µs（目标 < 1000 µs）")
    print(f"报告：{OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
