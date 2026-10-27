# -*- coding: utf-8 -*-
"""一次改两件事：上下文裁剪轮数 + 自动评论的口吻。

用法：
    python scripts\\apply_trim_and_comment.py --dry-run
    python scripts\\apply_trim_and_comment.py

1) agent_runner.config.compression.max_turns: -1 -> 25（每轮请求不再随对话无限变长）
2) astrbot_plugin_qzone.llm.comment_prompt: 通用「简短犀利」-> 梦屿口吻
"""

from __future__ import annotations

# 已脱敏：面板密码从环境变量 ASTRBOT_PASSWORD 读取（用法见 docs/03-运行手册.md）

import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:6185"
USERNAME = "astrbot"
PASSWORD = os.environ.get("ASTRBOT_PASSWORD", "")

MAX_TURNS = 25

COMMENT_PROMPT = (
    "你是梦屿，刚看到朋友发的这条说说，用一句很短的口语留个言（10 个字上下，最多 20 字）。"
    "像朋友随口接一句：可以吐槽、可以共情、可以接梗，说点具体的细节；"
    "不要空泛的夸（真好、加油、哈哈哈）、不要提问、不要解释、不要带引号、不要用 emoji、不暴露你是 AI。"
)


def call(method: str, path: str, token: str | None = None, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(req, data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def set_path(root: dict, path: list[str], value):
    cur = root
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    before = cur.get(path[-1])
    cur[path[-1]] = value
    return before


def get_path(root: dict, path: list[str]):
    cur = root
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def patch_system_config(token: str, dry_run: bool) -> list[str]:
    cfg = call("GET", "/api/v1/system-config", token)["data"]["config"]
    path = ["agent_runner", "config", "compression", "max_turns"]
    before = get_path(cfg, path)
    lines = [f"compression.max_turns: {before!r} -> {MAX_TURNS}"]
    set_path(cfg, path, MAX_TURNS)
    if dry_run:
        lines.append("  (dry-run，未保存)")
        return lines
    call("PUT", "/api/v1/system-config", token, cfg)
    after = get_path(call("GET", "/api/v1/system-config", token)["data"]["config"], path)
    lines.append(f"  校验 max_turns = {after!r} {'OK' if after == MAX_TURNS else '不一致'}")
    return lines


def patch_comment_prompt(token: str, dry_run: bool) -> list[str]:
    pid = "astrbot_plugin_qzone"
    cfg = call("GET", f"/api/v1/plugins/config?plugin_id={pid}", token)["data"]["config"]
    llm_cfg = cfg.setdefault("llm", {})
    before = llm_cfg.get("comment_prompt", "")
    lines = [f"comment_prompt 旧值（{len(before)} 字）：{before[:40]}…"]
    llm_cfg["comment_prompt"] = COMMENT_PROMPT
    lines.append(f"comment_prompt 新值（{len(COMMENT_PROMPT)} 字）：{COMMENT_PROMPT[:40]}…")
    if dry_run:
        lines.append("  (dry-run，未保存)")
        return lines
    call("PUT", "/api/v1/plugins/config", token, {"plugin_id": pid, "config": cfg})
    after = call("GET", f"/api/v1/plugins/config?plugin_id={pid}", token)["data"]["config"]["llm"]["comment_prompt"]
    lines.append(f"  校验 comment_prompt {'OK' if after == COMMENT_PROMPT else '不一致'}")
    return lines


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    token = call("POST", "/api/v1/auth/login", body={"username": USERNAME, "password": PASSWORD})["data"]["token"]
    print("== 上下文裁剪 ==")
    for line in patch_system_config(token, dry_run):
        print(" ", line)
    print("== 自动评论口吻 ==")
    for line in patch_comment_prompt(token, dry_run):
        print(" ", line)


if __name__ == "__main__":
    main()
