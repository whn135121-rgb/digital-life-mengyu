#!/usr/bin/env python
"""按关键词检索 AstrBot 插件市场（约 2800 个插件）。

用法：
    python market_search.py 音乐 点歌
    python market_search.py --refresh 语录

缓存：_market_plugins.json，24 小时内复用；--refresh 强制重新拉取。
"""
from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:6185"
CACHE = Path(__file__).with_name("_market_plugins.json")
TTL = 24 * 3600


def api(path: str, payload: dict | None = None, token: str | None = None, method: str | None = None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def login() -> str:
    return api("/api/v1/auth/login", {"username": "astrbot", "password": os.environ.get("ASTRBOT_PASSWORD", "")})["data"]["token"]


def load_market(refresh: bool = False) -> dict:
    if not refresh and CACHE.exists() and time.time() - CACHE.stat().st_mtime < TTL:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    token = login()
    data = api("/api/v1/plugins/market", token=token)["data"]
    CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def main(argv: list[str]) -> int:
    refresh = "--refresh" in argv
    keywords = [a for a in argv if not a.startswith("--")]
    market = load_market(refresh)
    rows = [(k, v) for k, v in market.items() if k != "$meta"]
    print(f"市场共 {len(rows)} 个插件；缓存 {CACHE.name}")

    if not keywords:
        return 0

    def text_of(item):
        k, v = item
        return " ".join(
            str(x or "")
            for x in (k, v.get("name"), v.get("desc"), v.get("short_desc"), v.get("author"), v.get("tags"))
        ).lower()

    def score(item):
        k, v = item
        name = f"{k} {v.get('name') or ''}".lower()
        return sum(1 for kw in keywords if kw.lower() in name)

    hits = [(k, v) for k, v in rows if all(kw.lower() in text_of((k, v)) for kw in keywords)]
    if not hits:
        hits = [(k, v) for k, v in rows if any(kw.lower() in text_of((k, v)) for kw in keywords)]
    hits.sort(key=lambda it: (-score(it), it[0]))

    print(f"命中 {len(hits)} 个：\n")
    for k, v in hits[:25]:
        repo = v.get("repo") or v.get("home") or ""
        print(f"· {k}  v{v.get('version', '?')}")
        print(f"  {v.get('desc') or v.get('short_desc') or ''}")
        if repo:
            print(f"  {repo}")
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001
        print(f"检索失败：{type(exc).__name__}: {exc}")
        sys.exit(1)
