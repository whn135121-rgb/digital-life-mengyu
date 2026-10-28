"""把 recall_engine.top_k 6 → 7（并把 recent_memory_count 保持 2），改完复核 15 问。

依据：scripts\\apply_recent_count.py / tests 里的扫 k 结果 —— recent=2 时
k=6 命中 13/15，k=7 命中 14/15（平均 1114 字，仍在 1200 预算内）。

用法：python scripts\\apply_top_k.py [--apply]
"""

from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:6185"
PLUGIN = "astrbot_plugin_livingmemory"
TARGET_K = 7

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


def post(path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def check(token: str, k: int) -> tuple[int, float, list[str]]:
    hits, chars, misses = 0, [], []
    for query, expected in QUERIES:
        out = post(f"/api/plug/{PLUGIN}/page/recall/test", {"query": query, "k": k}, token)
        results = (out.get("data") or {}).get("results") or []
        ids = {int(r["memory_id"]) for r in results}
        chars.append(sum(len(str(r.get("content") or "")) for r in results))
        if expected & ids:
            hits += 1
        else:
            misses.append(query)
    return hits, round(sum(chars) / len(chars), 1), misses


def main() -> int:
    apply = "--apply" in sys.argv
    token = post("/api/v1/auth/login", {"username": "astrbot", "password": os.environ.get("ASTRBOT_PASSWORD", "")})["data"]["token"]
    print("改前 k=6：", check(token, 6))
    if not apply:
        print("（预演：加 --apply 才改）")
        return 0

    post(
        f"/api/plug/{PLUGIN}/page/config/update",
        {"values": {"recall_engine.top_k": TARGET_K}},
        token,
    )
    post(f"/api/v1/plugins/{PLUGIN}/reload", {}, token)
    time.sleep(2.0)
    for k in (TARGET_K,):
        print(f"改后 k={k}：", check(token, k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
