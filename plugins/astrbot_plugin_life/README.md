# astrbot_plugin_life

梦屿的生活状态。**只做状态，不做对话**：不调模型、不调度别的任务。

v0.2.0 起它同时是**对话内唯一的注入点**——`astrbot_plugin_ai_delay` 里那段临时注入已关（兜底开关 `inject_life_state=false`），避免两行状态打架。

## 它做什么

- **独占写入** `qqbot/data/life/`：`state.json`、`heartbeat.json`、`journal/YYYY-MM-DD.jsonl`
- **每小时幂等推进**：幂等键 `(date, hour)`，重启重放不叠加
- **每 60 秒心跳**：J3 判据（重启 2 分钟内任务恢复、心跳在跳）就看这个文件
- **作息事实快照**：读 `data/life/life_state.py` 的 `state()`，把「此刻在哪、在做什么、什么感觉」写进 `state.json` 的 `fact.schedule`。作息表是纯函数，改课表改那个文件即可
- **注入**：`on_llm_request` 在系统提示末尾追加一行（≤110 字，已有则不重复追加）
- **工具 `life_state`（只读）**：她可以被问到「现在在干嘛」，读到事实与她自填的两栏
- **工具 `life_update`（自写）**：`field` ∈ activity/location/mood/note，`value` 为空表示清回未设置；只写 `self`，不碰事实
- **管理员命令 `/life`**：`/life` 看状态，`/life files` 看文件位置与作息模块加载情况

## 状态分两栏

| 栏位 | 谁写 | 内容 | 缺省 |
| --- | --- | --- | --- |
| `fact` | 系统 | 时间、时段、此刻（地点/在做什么/心情/细节）、推进次数、心跳 | 作息模块可用时永远有值 |
| `self` | 她自己 | activity / location / mood / note | `null` = **未设置**，不替她编 |

「未设置就是未设置」——不冒认、不推测。这来自 digital-life 的 `state-board.mjs` 原则。

## 配置（`_conf_schema.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `tick_seconds` | 30 | 检查间隔；小于 5 会被夹到 5 |
| `heartbeat_seconds` | 60 | 心跳覆写间隔 |
| `journal_enabled` | true | 写不写每日流水 |
| `inject_enabled` | true | 是否在对话里注入那一行；关掉只保留工具与文件 |
| `schedule_file` | `life_state.py` | 相对 `data/life/` 的作息模块；留空则只有她自填那栏 |

## 已验证

- store 层 14 项单测：schema 自愈、旧文件升版、幂等刷新、坏 JSON 改名 `.json.bad` 重建、注入行在未设置时不编造、清空回到 null、journal 记 fact/self
- 插件层 9 项：注入追加一次、重复请求不重复追加、`inject_enabled=false` 时不改 prompt、`life_update` 别名/超长截断/空值清空/非法字段拒绝
- 线上：webchat 一次真实请求触发 `[Life] 首次注入状态：（你此刻：周二 13:25，宿舍，午睡，迷迷糊糊；手机还亮着。…）`，模型据此答「刚才在午睡呢，手机还亮着呢」

## 已知边界

- 没有驱力（drives）、没有意向机制——S1/S3 再定，不按旧 schema 硬套
- 不主动发消息（那是 proactive_chat 的事），不判断「该不该说」
- 时区按进程本地时间（北京），不做多时区
