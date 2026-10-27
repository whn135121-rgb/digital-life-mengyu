# astrbot_plugin_life

梦屿的生活状态骨架。**只做状态，不做对话**：不调模型、不注入提示词（注入仍由 `astrbot_plugin_ai_delay` 的 `inject_life_state()` 负责，避免两个写入者）。

## 它做什么

- **独占写入** `qqbot/data/life/`：`state.json`、`heartbeat.json`、`journal/YYYY-MM-DD.jsonl`
- **每小时幂等推进**：幂等键 `(date, hour)`，重启重放不叠加
- **每 60 秒心跳**：J3 判据（重启 2 分钟内任务恢复、心跳在跳）就看这个文件
- **只读工具 `life_state`**：她可以被问到「现在在干嘛」，读到 facts 与 self 两栏
- **管理员命令 `/life`**：`/life` 看状态，`/life files` 看文件位置

## 状态分两栏（v0.1 的核心约束）

| 栏位 | 谁写 | 内容 | 缺省 |
| --- | --- | --- | --- |
| `facts` | 系统 | 时间、时段、推进次数、心跳、开机号 | 永远有值 |
| `self` | 她自己 | activity / location / mood / note | `null` = **未设置**，不替她编 |

「未设置就是未设置」——不冒认、不推测。这来自 digital-life 的 `state-board.mjs` 原则。

## 配置

`main.py` 顶部 `DEFAULTS`：`tick_seconds=30`、`heartbeat_seconds=60`、`journal_enabled=true`。
不带 `_conf_schema.json`，配置走 AstrBot 的默认合并。

## 已知边界

- 没有驱力（drives），没有意向机制——那些在 S1/S3 里再定，不按旧 schema 硬套
- 不注入、不主动发消息
- 时区固定按进程本地时间（北京）
