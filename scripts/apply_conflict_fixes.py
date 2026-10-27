"""按冲突报告里的 a/b/c 三项修配置。

a) meme_manager  generation.trigger.scope: chat_and_plugin_llm -> only_chat_llm
b) ai_delay      min_delay/max_delay -> 0/0, merge_window 6 -> 4
c) anysearch     禁用插件（api_key 为空，三个工具全废）

用法：
    python apply_conflict_fixes.py            # 执行
    python apply_conflict_fixes.py --dry-run  # 只看差异
"""

from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:6185/api/v1"
USER = "astrbot"
PASSWORD = os.environ.get("ASTRBOT_PASSWORD", "")
DRY = "--dry-run" in sys.argv


def call(method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def login() -> str:
    status, payload = call("POST", "/auth/login", {"username": USER, "password": PASSWORD})
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"登录失败: {status} {payload}")
    return payload["data"]["token"]


def get_plugin_config(plugin_id: str, token: str) -> dict:
    status, payload = call("GET", f"/plugins/config?plugin_id={plugin_id}", token=token)
    if status != 200:
        raise SystemExit(f"读取 {plugin_id} 配置失败: {status} {payload}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and "config" in data:
        return data["config"]
    if isinstance(data, dict):
        return data
    raise SystemExit(f"{plugin_id} 配置结构无法识别: {payload}")


def put_plugin_config(plugin_id: str, config: dict, token: str):
    return call(
        "PUT",
        "/plugins/config",
        {"plugin_id": plugin_id, "config": config},
        token=token,
    )


def show(title: str, pairs: list[tuple[str, object, object]]) -> bool:
    changed = False
    print(f"\n[{title}]")
    for path, before, after in pairs:
        mark = "->" if before != after else "=="
        print(f"  {path:32} {before!r:28} {mark} {after!r}")
        changed = changed or before != after
    return changed


def main() -> None:
    token = login()
    print("登录成功" + ("（dry-run，不写盘）" if DRY else ""))

    # a) meme_manager
    meme = get_plugin_config("meme_manager", token)
    gen = meme.setdefault("generation", {})
    trigger = gen.setdefault("trigger", {})
    old_scope = trigger.get("scope")
    trigger["scope"] = "only_chat_llm"
    changed_a = show("a) meme_manager", [("generation.trigger.scope", old_scope, "only_chat_llm")])
    if changed_a and not DRY:
        print("  PUT:", put_plugin_config("meme_manager", meme, token)[0])

    # b) ai_delay
    delay = get_plugin_config("astrbot_plugin_ai_delay", token)
    old = (delay.get("min_delay"), delay.get("max_delay"), delay.get("merge_window"))
    delay["min_delay"] = 0
    delay["max_delay"] = 0
    delay["merge_window"] = 4
    changed_b = show(
        "b) ai_delay",
        [
            ("min_delay", old[0], 0),
            ("max_delay", old[1], 0),
            ("merge_window", old[2], 4),
        ],
    )
    if changed_b and not DRY:
        print("  PUT:", put_plugin_config("astrbot_plugin_ai_delay", delay, token)[0])

    # c) 禁用 anysearch
    print("\n[c) anysearch]")
    status, payload = call("GET", "/plugins", token=token)
    state = None
    if status == 200 and isinstance(payload, dict):
        items = payload.get("data") or []
        if isinstance(items, dict):
            items = items.get("plugins") or []
        for item in items:
            if isinstance(item, dict) and "anysearch" in str(item.get("name") or item.get("id") or ""):
                state = item.get("activated", item.get("enabled"))
                print(f"  当前 activated={state}  ({item.get('name')})")
    if DRY:
        print("  PATCH: 跳过（dry-run）")
    else:
        code, body = call(
            "PATCH",
            "/plugins/enabled",
            {"plugin_id": "astrbot_plugin_anysearch", "enabled": False},
            token=token,
        )
        print("  PATCH:", code, str(body)[:200])

    # 回读校验
    if not DRY:
        print("\n[回读校验]")
        meme2 = get_plugin_config("meme_manager", token)
        print("  meme scope =", meme2.get("generation", {}).get("trigger", {}).get("scope"))
        d2 = get_plugin_config("astrbot_plugin_ai_delay", token)
        print("  ai_delay   =", {k: d2.get(k) for k in ("min_delay", "max_delay", "merge_window", "recall_guard", "stale_guard", "split_bubbles")})

        status, tools = call("GET", "/tools", token=token)
        if status == 200:
            names = [
                t.get("name")
                for t in (tools.get("data") or [])
                if isinstance(t, dict)
            ]
            print("  工具清单   =", names)
            print("  anysearch 工具是否还在 =", any(str(n).startswith("anysearch") for n in names))


if __name__ == "__main__":
    main()
