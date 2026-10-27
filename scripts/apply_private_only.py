# -*- coding: utf-8 -*-
"""梦屿只服务私聊：给人格加"实时信息先搜再答"规则，并关掉群聊相关功能。

用法：
    python apply_private_only.py --dry-run
    python apply_private_only.py

改动（全部走面板 API，保存即生效，不需要重启 AstrBot）：
1) personas 表里「梦屿」和「default」两份人格的 system_prompt：在 # 事实和工具 段落追加联网搜索规则；
2) proactive_chat：group_settings.enable=false，group_settings.auto_trigger_settings.enable_auto_trigger=false；
3) ai_delay：group_owner_only=true（群里只处理所有者，其他人发的消息不合并也不延迟）；
4) anysearch：chunk_group_command_results=false（群聊长结果不再拆条刷屏）。
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

SEARCH_RULES = (
    "- 手里有联网搜索工具（anysearch_search / anysearch_extract / anysearch_batch_search）："
    "碰到新闻、版本号、发布、价格、天气、比赛结果、刚出的东西，或任何你自己没底的事实，"
    "先搜再答，不要凭记忆编。\n"
    "- 一次搜不到就换个说法再搜；标题不够就提取最相关那页的正文；两次都没有，就直说没查到。\n"
    "- 别跟人念工具名、别解释自己在查资料，把查到的结果揉进正常说话里。\n"
)
MARKER = "anysearch_search"

PLUGIN_CHANGES = {
    "astrbot_plugin_proactive_chat": {
        "group_settings.enable": False,
        "group_settings.auto_trigger_settings.enable_auto_trigger": False,
    },
    "astrbot_plugin_ai_delay": {
        "group_owner_only": True,
    },
    "astrbot_plugin_anysearch": {
        "chunk_group_command_results": False,
    },
}


def call(method: str, path: str, token: str | None = None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(req, data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def set_path(cfg: dict, dotted: str, value) -> tuple[object, object]:
    """按 a.b.c 写入嵌套字典，返回 (旧值, 新值)。"""
    keys = dotted.split(".")
    node = cfg
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise TypeError(f"{dotted} 的父级 {key} 不是字典")
    old = node.get(keys[-1])
    node[keys[-1]] = value
    return old, value


def get_path(cfg: dict, dotted: str):
    node = cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def patch_persona(token: str, persona_id: str, dry_run: bool) -> str:
    detail = call("GET", f"/api/v1/personas/{urllib.parse.quote(persona_id)}", token)
    data = detail.get("data") or detail
    text = data.get("system_prompt") or ""
    if MARKER in text:
        return f"{persona_id}: 已有规则，跳过（{len(text)} 字）"
    anchor = "# 事实和工具"
    idx = text.find(anchor)
    if idx < 0:
        return f"{persona_id}: 找不到「{anchor}」段落，跳过（{len(text)} 字）"
    nxt = text.find("\n# ", idx + len(anchor))
    insert_at = len(text) if nxt < 0 else nxt + 1
    new_text = text[:insert_at] + SEARCH_RULES + ("\n" if nxt >= 0 else "") + text[insert_at:]
    if dry_run:
        return f"{persona_id}: 将插入 {len(SEARCH_RULES)} 字（{len(text)} -> {len(new_text)}），未保存"
    call("PUT", f"/api/v1/personas/{urllib.parse.quote(persona_id)}", token,
         {"system_prompt": new_text})
    check = call("GET", f"/api/v1/personas/{urllib.parse.quote(persona_id)}", token)
    after = (check.get("data") or check).get("system_prompt") or ""
    ok = MARKER in after and len(after) == len(new_text)
    return f"{persona_id}: {'已保存' if ok else '保存异常'}（{len(text)} -> {len(after)} 字）"


def patch_plugin(token: str, plugin_id: str, changes: dict[str, object], dry_run: bool) -> list[str]:
    cfg = call("GET", f"/api/v1/plugins/config?plugin_id={plugin_id}", token)["data"]["config"]
    lines = []
    for dotted, value in changes.items():
        old, _ = set_path(cfg, dotted, value)
        lines.append(f"  {dotted}: {old!r} -> {value!r}")
    if dry_run:
        lines.append("  (dry-run，未保存)")
        return [f"{plugin_id}"] + lines
    call("PUT", "/api/v1/plugins/config", token, {"plugin_id": plugin_id, "config": cfg})
    after = call("GET", f"/api/v1/plugins/config?plugin_id={plugin_id}", token)["data"]["config"]
    for dotted, value in changes.items():
        got = get_path(after, dotted)
        status = "OK" if got == value else f"不一致(实际 {got!r})"
        lines.append(f"  校验 {dotted} = {got!r} {status}")
    return [f"{plugin_id}"] + lines


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = call("POST", "/api/v1/auth/login",
                 body={"username": USERNAME, "password": PASSWORD})["data"]["token"]

    print("== 人格 ==")
    for persona_id in PERSONAS:
        print(" ", patch_persona(token, persona_id, dry_run))

    print("== 插件 ==")
    for plugin_id, changes in PLUGIN_CHANGES.items():
        for line in patch_plugin(token, plugin_id, changes, dry_run):
            print(" ", line)


if __name__ == "__main__":
    main()
