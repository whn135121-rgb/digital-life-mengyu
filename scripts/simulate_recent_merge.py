"""模拟「最近记忆」与语义召回怎么分 k 个槽位：只读，不改线上配置。

背景：`memory_engine_write_ops.py:180 _merge_recent_memories()` 的逻辑是
    selected = 语义前 (k - recent_count) 条  +  最近 recent_count 条
所以 recent_memory_count 越大，留给语义召回的槽位越少。2026-10-27 之后新写入
61/62/63 三篇，k=6 + recent=3 时只剩 3 个语义槽，「我叫你什么」「45.76」标注的
58/59 就被挤出去了。

做法：对 15 个真实问题各取 k=30 的结果（顺序就是 final 顺序：语义在前、最近的在后），
把最近 N 篇（按 documents.updated_at 取）剔掉，得到纯语义排序，再按上面的公式
重放不同 (recent_count, k) 组合，看 15 问命中几个。

用法：python scripts\\simulate_recent_merge.py
"""

from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"
OUT_PATH = ROOT / "scripts" / "recent_merge_simulation.json"
BASE = "http://127.0.0.1:6185"
PLUGIN = "astrbot_plugin_livingmemory"

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

RECENT_SLOTS = (0, 1, 2, 3, 4)
K_SLOTS = (4, 5, 6, 7, 8, 10)


def _post(path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def login() -> str:
    return _post("/api/v1/auth/login", {"username": "astrbot", "password": os.environ.get("ASTRBOT_PASSWORD", "")})["data"]["token"]


def recent_ids(limit: int = 6) -> list[int]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id FROM documents ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


def simulate(semantic: list[int], recent: list[int], k: int, r: int) -> list[int]:
    """重放 _merge_recent_memories 的选择逻辑。"""
    r = min(max(0, r), k)
    selected = semantic[: max(0, k - r)]
    seen = set(selected)
    for doc_id in recent[:r]:
        if doc_id not in seen:
            selected.append(doc_id)
            seen.add(doc_id)
    for doc_id in semantic:
        if len(selected) >= k:
            break
        if doc_id not in seen:
            selected.append(doc_id)
            seen.add(doc_id)
    return selected[:k]


def main() -> int:
    token = login()
    recent_pool = recent_ids(6)
    print(f"最近文档（按 updated_at）：{recent_pool[:5]}")

    rows = []
    for query, expected in QUERIES:
        out = _post(f"/api/plug/{PLUGIN}/page/recall/test", {"query": query, "k": 30}, token)
        results = (out.get("data") or {}).get("results") or []
        ids = [int(r["memory_id"]) for r in results]
        chars = {int(r["memory_id"]): len(str(r.get("content") or "")) for r in results}
        grid = {}
        for k in K_SLOTS:
            grid[k] = {}
            for r in RECENT_SLOTS:
                recents = recent_pool[:r]
                semantic = [i for i in ids if i not in set(recents)]
                picked = simulate(semantic, recents, k, r)
                grid[k][str(r)] = {
                    "hit": bool(expected & set(picked)),
                    "chars": sum(chars.get(i, 0) for i in picked),
                }
        rows.append(
            {
                "query": query,
                "expected": sorted(expected),
                "ranking": ids[:10],
                "grid": grid,
            }
        )

    summary = {}
    for k in K_SLOTS:
        for r in RECENT_SLOTS:
            hits = sum(1 for row in rows if row["grid"][k][str(r)]["hit"])
            avg_chars = round(sum(row["grid"][k][str(r)]["chars"] for row in rows) / len(rows), 1)
            summary[f"k={k},recent={r}"] = {"hit": hits, "avg_chars": avg_chars}

    best = sorted(summary.items(), key=lambda kv: (-kv[1]["hit"], kv[1]["avg_chars"]))
    report = {
        "recent_pool": recent_pool,
        "summary": summary,
        "best": best[:6],
        "details": rows,
    }
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n组合（命中数 / 平均注入字数，预算 1200）：")
    for key, val in sorted(summary.items(), key=lambda kv: (int(kv[0].split(",")[1].split("=")[1]), int(kv[0].split(",")[0].split("=")[1]))):
        bar = "  <-- 当前" if key == "k=6,recent=3" else ""
        print(f"  {key:16s} {val['hit']:2d}/15  {val['avg_chars']:7.1f}{bar}")
    print(f"\n报告：{OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
