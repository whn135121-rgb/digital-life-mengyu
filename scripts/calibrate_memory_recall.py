"""LivingMemory 召回校准：会临时改线上配置，finally 恢复 baseline（recent=3, min=0）。

只在维护窗口手动运行；结果写 scripts/recall_calibration.json。
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
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

VARIANTS = [
    ("baseline", 3, 0.0),
    ("recent1_min0", 1, 0.0),
    ("recent1_min035", 1, 0.35),
    ("recent1_min045", 1, 0.45),
    ("recent2_min035", 2, 0.35),
]


def req(url, payload=None, headers=None, timeout=90):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def recall(h, query, k):
    started = time.perf_counter()
    out = req(
        f"{BASE}/api/plug/{PLUGIN}/page/recall/test",
        {"query": query, "k": k},
        h,
    )
    elapsed = (time.perf_counter() - started) * 1000
    return (out.get("data") or {}).get("results") or [], elapsed


def apply_config(h, recent, min_sim):
    req(
        f"{BASE}/api/plug/{PLUGIN}/page/config/update",
        {"values": {"recall_engine.recent_memory_count": recent, "recall_engine.min_similarity_for_retrieval": min_sim}},
        h,
    )
    req(f"{BASE}/api/v1/plugins/{PLUGIN}/reload", {}, h)
    time.sleep(1.5)


def main():
    login = req(f"{BASE}/api/auth/login", {"username": "astrbot", "password": os.environ.get("ASTRBOT_PASSWORD", "")})
    h = {"Authorization": f"Bearer {login['data']['token']}"}
    report = {}
    try:
        for name, recent, min_sim in VARIANTS:
            apply_config(h, recent, min_sim)
            rows = []
            for k in (5, 6, 8):
                for query, expected in QUERIES:
                    results, elapsed = recall(h, query, k)
                    ids = [r.get("memory_id") for r in results]
                    vecs = [r.get("score_breakdown", {}).get("document_vector_score") for r in results]
                    low = sum(1 for v in vecs if isinstance(v, (int, float)) and v < 0.35)
                    rows.append({
                        "k": k,
                        "query": query,
                        "hit": bool(set(ids) & expected),
                        "ids": ids,
                        "vec": vecs,
                        "low": low,
                        "chars": sum(len(r.get("content") or "") for r in results),
                        "ms": round(elapsed, 1),
                        "n": len(results),
                    })
            # cache probe: first and immediate second call
            q = QUERIES[0][0]
            _, first_ms = recall(h, q, 6)
            _, second_ms = recall(h, q, 6)
            summary = {}
            for k in (5, 6, 8):
                subset = [r for r in rows if r["k"] == k]
                summary[k] = {
                    "hit": sum(r["hit"] for r in subset),
                    "avg_chars": round(sum(r["chars"] for r in subset) / len(subset)),
                    "avg_ms": round(sum(r["ms"] for r in subset) / len(subset), 1),
                    "avg_n": round(sum(r["n"] for r in subset) / len(subset), 2),
                    "low_vec_total": sum(r["low"] for r in subset),
                }
            report[name] = {
                "recent": recent,
                "min_sim": min_sim,
                "summary": summary,
                "cache_first_ms": round(first_ms, 1),
                "cache_second_ms": round(second_ms, 1),
            }
            print(name, json.dumps(report[name], ensure_ascii=False))
    finally:
        apply_config(h, 3, 0.0)
        print("restored baseline recent=3 min_sim=0.0")
    with open("scripts/recall_calibration.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("WROTE scripts/recall_calibration.json")


if __name__ == "__main__":
    main()
