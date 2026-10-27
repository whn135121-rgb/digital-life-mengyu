"""列出 AstrBot 当前注册的所有命令（面板 API /api/v1/commands）。

用法：python scripts\\list_commands.py [--json 输出路径]
"""

from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:6185"


def call(method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = call(
        "POST",
        "/api/v1/auth/login",
        {"username": "astrbot", "password": os.environ.get("ASTRBOT_PASSWORD", "")},
    )["data"]["token"]

    payload = call("GET", "/api/v1/commands", token=token)
    data = payload.get("data") or {}
    cmds = data.get("items", []) if isinstance(data, dict) else data

    rows = []
    for item in cmds:
        rows.append(
            {
                "name": item.get("effective_command") or item.get("original_command"),
                "aliases": item.get("aliases") or [],
                "description": item.get("description", ""),
                "plugin": item.get("plugin", ""),
                "plugin_display_name": item.get("plugin_display_name", ""),
                "permission": item.get("permission", ""),
                "enabled": item.get("enabled", True),
                "activated": item.get("plugin_activated", True),
                "is_group": item.get("is_group", False),
                "conflict": item.get("has_conflict", False),
                "sub_commands": item.get("sub_commands") or [],
                "type": item.get("type", ""),
            }
        )

    out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else "_commands.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)

    print(f"共 {len(rows)} 条命令 -> {out}")
    cur_plugin = None
    for row in sorted(rows, key=lambda r: (r["plugin"], r["name"] or "")):
        if row["plugin"] != cur_plugin:
            cur_plugin = row["plugin"]
            print(f"\n[{cur_plugin}]")
        alias = f"（别名 {', '.join(row['aliases'])}）" if row["aliases"] else ""
        perm = f" 权限:{row['permission']}" if row["permission"] else ""
        off = "" if row["enabled"] else "  [已禁用]"
        print(f"  {row['name']}{alias} - {row['description']}{perm}{off}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
