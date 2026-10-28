import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:6185"
USER = "astrbot"
PASSWORD = os.environ.get("ASTRBOT_PASSWORD", "")

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


def json_request(url, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    login = json_request(f"{BASE}/api/auth/login", {"username": USER, "password": PASSWORD})
    token = login["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    for k in (5, 6, 8):
        rows = []
        t0 = time.perf_counter()
        for query, expected in QUERIES:
            started = time.perf_counter()
            resp = json_request(
                f"{BASE}/api/plug/astrbot_plugin_livingmemory/page/recall/test",
                {"query": query, "k": k},
                headers,
            )
            elapsed = (time.perf_counter() - started) * 1000
            results = (resp.get("data") or {}).get("results") or []
            ids = [r.get("memory_id") for r in results]
            hit = bool(set(ids) & expected)
            low = sum(1 for r in results if float(r.get("score_breakdown", {}).get("document_vector_score", 1.0)) < 0.30)
            recent = sum(1 for r in results if float(r.get("metadata", {}).get("recent_memory", 0) or 0) >= 1.0)
            rows.append({
                "query": query,
                "hit": hit,
                "ids": ids,
                "scores": [r.get("similarity_score") for r in results],
                "vector": [r.get("score_breakdown", {}).get("document_vector_score") for r in results],
                "prompt_chars": sum(len(r.get("content") or "") for r in results),
                "low_vector": low,
                "recent": recent,
                "elapsed_ms": round(elapsed, 1),
            })
        total_ms = (time.perf_counter() - t0) * 1000
        print(f"\n=== k={k} ===")
        for row in rows:
            mark = "HIT " if row["hit"] else "MISS"
            print(f"{mark} {row['query'][:20]:20s} ids={row['ids']} scores={row['scores']} vec={row['vector']} low={row['low_vector']} recent={row['recent']} chars={row['prompt_chars']} {row['elapsed_ms']}ms")
        print(f"summary k={k}: hit={sum(r['hit'] for r in rows)}/{len(rows)} avg_chars={sum(r['prompt_chars'] for r in rows)/len(rows):.0f} avg_ms={sum(r['elapsed_ms'] for r in rows)/len(rows):.0f} total_s={total_ms/1000:.1f}")
    print("\nDONE")


if __name__ == "__main__":
    main()
