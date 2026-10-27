# -*- coding: utf-8 -*-
"""把 AstrBot 内置的 default 人格还原（去掉我之前插进去的联网搜索规则和 # 说说 段）。

用法：
    python revert_default_persona.py --dry-run
    python revert_default_persona.py

只动 default，不动「梦屿」。
"""
from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:6185"
USERNAME = "astrbot"
PASSWORD = os.environ.get("ASTRBOT_PASSWORD", "")
PERSONA_ID = "default"
ORIGINAL_LEN = 1708  # 改动前 AstrBot 内置 default 的长度


def call(method: str, path: str, token: str | None = None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(req, data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def strip_section(text: str, marker: str, head: str) -> tuple[str, bool]:
    """删除以 head 开头、到下一个 '\\n# ' 之前的一整段。"""
    if marker not in text or head not in text:
        return text, False
    start = text.find(head)
    nxt = text.find("\n# ", start + len(head))
    end = len(text) if nxt < 0 else nxt + 1
    return text[:start] + text[end:], True


def strip_search_rules(text: str) -> tuple[str, bool]:
    """删除联网搜索那 3 条 bullet（以 anysearch_search 为标记）。"""
    if "anysearch_search" not in text:
        return text, False
    lines = text.splitlines(keepends=True)
    keep, dropped = [], 0
    for line in lines:
        if dropped < 3 and line.startswith("- 手里有联网搜索工具") or (
            dropped and dropped < 3 and line.startswith(("- 一次搜不到就换个说法再搜", "- 别跟人念工具名"))
        ):
            dropped += 1
            continue
        keep.append(line)
    return "".join(keep), dropped > 0


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = call("POST", "/api/v1/auth/login", body={"username": USERNAME, "password": PASSWORD})["data"]["token"]
    detail = call("GET", f"/api/v1/personas/{urllib.parse.quote(PERSONA_ID)}", token)
    data = detail.get("data") or detail
    text = data.get("system_prompt") or ""
    print(f"当前 default: {len(text)} 字，含 anysearch={'anysearch_search' in text}，含说说段={'# 说说' in text}")

    out, a = strip_section(text, "llm_publish_feed", "# 说说")
    out, b = strip_search_rules(out)
    out = out.replace("\n\n\n", "\n\n")
    print(f"删 # 说说 段: {a}；删联网搜索 3 条: {b}；结果 {len(out)} 字（原始 {ORIGINAL_LEN}）")
    if len(out) != ORIGINAL_LEN:
        print("⚠️ 长度与原始值不一致，先不写回。差异片段：")
        for i, line in enumerate(out.splitlines(), 1):
            if "anysearch" in line or "llm_publish" in line:
                print("  残留:", i, line)
        return
    if dry_run:
        print("(dry-run，未保存)")
        return
    call("PUT", f"/api/v1/personas/{urllib.parse.quote(PERSONA_ID)}", token, {"system_prompt": out})
    after = (call("GET", f"/api/v1/personas/{urllib.parse.quote(PERSONA_ID)}", token).get("data") or {}).get(
        "system_prompt"
    ) or ""
    print(f"已写回：{len(after)} 字，残留标记={('anysearch_search' in after) or ('llm_publish_feed' in after)}")


if __name__ == "__main__":
    main()
