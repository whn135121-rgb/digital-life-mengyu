# astrbot_plugin_life

梦屿的生活状态与驱力。**只做状态，不做对话**：不调模型、不发消息、不调度别的任务。

v0.3.0 起它同时是**对话内唯一的注入点**——`astrbot_plugin_ai_delay` 里那段临时注入已关（兜底开关 `inject_life_state=false`），避免两行状态打架。

## 它做什么

- **独占写入** `qqbot/data/life/`：`state.json`、`heartbeat.json`、`journal/YYYY-MM-DD.jsonl`
- **每小时幂等推进**：幂等键 `(date, hour)`，重启重放不叠加
- **每 60 秒心跳**：J3 判据（重启 2 分钟内任务恢复、心跳在跳）就看这个文件
- **作息事实快照**：读 `data/life/life_state.py` 的 `state()`，把「此刻在哪、在做什么、什么感觉」写进 `state.json` 的 `fact.schedule`。作息表是纯函数，改课表改那个文件即可
- **四驱力**：social / curiosity / play / rest，低通 `v_t = 0.7·v_{t-1} + 0.3·v_target`，每 60 秒落一次盘
- **阈值判断**：`decision` 写进 `state.json`，只有六种出口——`opener`（想找他）/ `sleepy`（困了）/ `none`（静默时段）/ `none`（刚说过话）/ `none`（social 还低）
- **注入**：`on_llm_request` 在系统提示末尾追加一行（≤130 字，已有则不重复追加）。**只给事实、不给结论**（v4.0 §4.5）：时间、地点、在做什么、感觉、多久没说话、她自己填过的字段；不再写「这会儿正想找他」这类替她下的判断，情绪与语气交回模型
- **工具 `life_state`（只读）**：她可以被问到「现在在干嘛」，读到事实与她自填的两栏
- **工具 `life_update`（自写）**：`field` ∈ activity/location/mood/note，`value` 为空表示清回未设置；只写 `self`，不碰事实
- **管理员命令 `/life`**：`/life` 看状态，`/life files` 看文件位置与作息模块加载情况，`/life why` 看四驱力的当前值/目标值、闲置分钟、判断与理由（调阈值时用这个）

## 状态分两栏

| 栏位 | 谁写 | 内容 | 缺省 |
| --- | --- | --- | --- |
| `fact` | 系统 | 时间、时段、此刻（地点/在做什么/心情/细节）、推进次数、心跳 | 作息模块可用时永远有值 |
| `self` | 她自己 | activity / location / mood / note | `null` = **未设置**，不替她编 |

「未设置就是未设置」——不冒认、不推测。这来自 digital-life 的 `state-board.mjs` 原则。

## 驱力怎么算

目标值由作息段和闲置时长决定，然后向目标低通靠近：

| 驱力 | 目标 |
| --- | --- |
| `social` | `min(0.95, 0.15 + 闲置分钟/75)`；睡觉压 0.20、上课压 0.72（v0.3.2：原来是 0.55，永远够不到 0.7 阈值，等于上课时段不可能想说话）、静默时段压 0.25 |
| `rest` | 睡觉 0.95；21 点后 0.55；其余 0.30 |
| `curiosity` | 睡觉或静默 0.30；其余 0.55 |
| `play` | 睡觉或上课 0.30；其余 0.50 |

`social ≥ social_threshold`（默认 0.7）**且**闲置 ≥ `idle_trigger_minutes`（默认 45）时才判 `opener`。`recognition` / `achievement` 还没做——它们要有关系和成就数据，属于 S4。

**判断不等于发送**：判断只写 `state.json`、只影响注入那一句；真正发不发由 `proactive_chat` 决定。同一条资源一个权威。

## 配置（`_conf_schema.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `tick_seconds` | 30 | 检查间隔；小于 5 会被夹到 5 |
| `heartbeat_seconds` | 60 | 心跳覆写间隔 |
| `journal_enabled` | true | 写不写每日流水 |
| `inject_enabled` | true | 是否在对话里注入那一行；关掉只保留工具与文件 |
| `schedule_file` | `life_state.py` | 相对 `data/life/` 的作息模块；留空则只有她自填那栏 |
| `drive_update_seconds` | 60 | 驱力与判断的落盘间隔 |
| `social_threshold` | 0.7 | 想说话的阈值 |
| `idle_trigger_minutes` | 45 | 闲置多少分钟才允许「想找他」 |
| `quiet_window` | `23-8` | 静默时段：压低 social、不判 opener |

## 已验证

- store 层 14 项单测：schema 自愈、旧文件升版、幂等刷新、坏 JSON 改名 `.json.bad` 重建、注入行在未设置时不编造、清空回到 null、journal 记 fact/self
- 插件层 9 项：注入追加一次、重复请求不重复追加、`inject_enabled=false` 时不改 prompt、`life_update` 别名/超长截断/空值清空/非法字段拒绝
- 驱力 16 项：6 小时模拟里单步变化 ≤0.075（阈值是 0.3）、social 随闲置单调上升并收敛到 0.95、`decide` 四个出口各命中一次、上课/睡觉的目标值被压低、注入前判断后记账（第一句带「想找他」、记完账闲置归零）
- 去结论化回归 13 项：注入行不再含「正想找他 / 真实的一天」、含「距上次说话已 45 分钟」这类事实、`opener` 与 `none` 的注入文本完全一致、短闲置不写闲置句、坏 JSON 时回落不抛错
- 线上：webchat 一次真实请求触发 `[Life] 首次注入状态：（你此刻：周二 13:25，宿舍，午睡，迷迷糊糊；手机还亮着。…）`，模型据此答「刚才在午睡呢，手机还亮着呢」

## 已知边界

- `recognition` / `achievement` 还没做（要有关系值与成就记录）
- 不主动发消息（那是 `proactive_chat` 的事），不判断「该不该说」
- 时区按进程本地时间（北京），不做多时区
- 判断只在对话来临时与 60 秒 tick 时更新；没有对话的时段靠 tick 累积
