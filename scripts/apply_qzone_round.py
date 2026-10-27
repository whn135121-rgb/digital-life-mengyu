# -*- coding: utf-8 -*-
"""给梦屿加「可以自己发 QQ 空间」的人格段落 + 打开 qzone 插件的自动评论。

用法：
    python apply_qzone_round.py --dry-run
    python apply_qzone_round.py

改动：
1) personas 表里「梦屿」「default」两份 system_prompt：在 # 记忆 之前插入 # 说说 段落（幂等，靠 marker 判断）；
2) astrbot_plugin_qzone：comment_cron / comment_offset / like_when_comment（发说说仍是每天一条，不动）。
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

PERSONAS = ("梦屿", "default")
MARKER = "llm_publish_feed"

QZONE_SECTION = """# 说说
- 你有 QQ 空间的权限：想到什么可以自己发一条说说（llm_publish_feed，正文写进 text），也能看自己或别人的空间（llm_view_feed）。
- 一天最多发一条，别为了发而发；内容跟你私聊说话一个味儿，一到三句，不带「说说：」前缀，不写话题标签。
- 对方刚发来的图片如果你觉得合适，可以顺带一起发（默认会带上当前对话里的图）。
- 别在私聊里汇报「我刚发了条说说」，除非对方正好问起。

"""


def call(method: str, path: str, token: str | None = None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(req, data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def patch_persona(token: str, persona_id: str, dry_run: bool) -> str:
    detail = call("GET", f"/api/v1/personas/{urllib.parse.quote(persona_id)}", token)
    data = detail.get("data") or detail
    text = data.get("system_prompt") or ""
    head = "# 说说"
    if MARKER in text and head in text:
        start = text.find(head)
        nxt = text.find("\n# ", start + len(head))
        end = len(text) if nxt < 0 else nxt + 1
        new_text = text[:start] + QZONE_SECTION + text[end:]
        action = "替换"
    else:
        anchor = "\n# 记忆"
        idx = text.find(anchor)
        if idx >= 0:
            insert_at, tail = idx + 1, ""
        else:
            insert_at, tail = len(text), "\n"
        new_text = text[:insert_at] + QZONE_SECTION + tail + text[insert_at:]
        action = "插入"
    if dry_run:
        return f"{persona_id}: 将{action} # 说说 段（{len(text)} -> {len(new_text)}），未保存"
    call("PUT", f"/api/v1/personas/{urllib.parse.quote(persona_id)}", token, {"system_prompt": new_text})
    after = (call("GET", f"/api/v1/personas/{urllib.parse.quote(persona_id)}", token).get("data") or {}).get(
        "system_prompt"
    ) or ""
    ok = MARKER in after and len(after) == len(new_text)
    return f"{persona_id}: {action}{'成功' if ok else '异常'}（{len(text)} -> {len(after)} 字）"


def patch_qzone(token: str, dry_run: bool) -> list[str]:
    pid = "astrbot_plugin_qzone"
    cfg = call("GET", f"/api/v1/plugins/config?plugin_id={pid}", token)["data"]["config"]
    trig = cfg.setdefault("trigger", {})
    before = {k: trig.get(k) for k in ("publish_cron", "publish_offset", "comment_cron", "comment_offset", "like_when_comment")}
    trig["comment_cron"] = "0 21 * * *"
    trig["comment_offset"] = 90
    trig["like_when_comment"] = True
    lines = [f"{pid} 发说说保持每天一条：cron={trig.get('publish_cron')} offset={trig.get('publish_offset')}（不变）"]
    for k, v in before.items():
        lines.append(f"  {k}: {v!r} -> {trig.get(k)!r}")
    if dry_run:
        lines.append("  (dry-run，未保存)")
        return lines
    call("PUT", "/api/v1/plugins/config", token, {"plugin_id": pid, "config": cfg})
    after = call("GET", f"/api/v1/plugins/config?plugin_id={pid}", token)["data"]["config"]["trigger"]
    for k in ("publish_cron", "publish_offset", "comment_cron", "comment_offset", "like_when_comment"):
        lines.append(f"  校验 {k} = {after.get(k)!r} {'OK' if after.get(k) == trig.get(k) else '不一致'}")
    return lines


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = call("POST", "/api/v1/auth/login", body={"username": USERNAME, "password": PASSWORD})["data"]["token"]
    print("== 人格 ==")
    for persona_id in PERSONAS:
        print(" ", patch_persona(token, persona_id, dry_run))
    print("== qzone 插件 ==")
    for line in patch_qzone(token, dry_run):
        print(" ", line)


if __name__ == "__main__":
    main()
