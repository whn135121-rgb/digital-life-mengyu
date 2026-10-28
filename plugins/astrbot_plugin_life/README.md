# astrbot_plugin_life

梦屿的生活状态与驱力。**只做状态，不做对话**：不调模型、不发消息、不调度别的任务。

v0.3.0 起它同时是**对话内唯一的注入点**——`astrbot_plugin_ai_delay` 里那段临时注入已关（兜底开关 `inject_life_state=false`），避免两行状态打架。

v0.6.0（2026-10-27）补上**居民版的可观察状态**：`availability`（open/focus/rest/sleep）、严格只读的 `life_state`、今天的时间线、以及 `proactive_chat` 的收口。只加状态与读口，不改聊天行为、不新建调度任务。

v0.7.1（2026-10-27）补上**睡着时的边界**：普通私聊被高优先级闸门拦下（不回复、不进模型），带 `/` 的指令仍放行；所有者发 `/急事 <一句话>` 可在每日配额内叫醒她，事件写 journal 审计。另加 `/life diary [YYYY-MM-DD]`，按天把 journal 事实汇成日记摘要，不编、不写聊天摘要；21 点后由 tick 自动落盘当天日记，并补昨天漏掉的。仍不调模型、不发消息。

v0.8.0（2026-10-27，S2-A）换掉插件内 asyncio 常驻循环：**单 APScheduler + SQLAlchemyJobStore（`life/jobs.db`）**，`life.tick` 与 `sched.heartbeat` 持久化，并把 `proactive_chat`、`livingmemory`、`self_learning` 的周期任务收进同一 scheduler；心跳带 `job_state`，启动自检能发现任务缺失/心跳过期/单任务停摆并重建/告警；连续失败 3 次暂停该任务。插件热重载不销毁 scheduler，进程重启后从 jobs.db 恢复。

v0.9.1（2026-10-27，S2-B）补上**跨会话连续性**：`life/params.json` 四类参数版本化/快照/回滚；`life/handover.json` 每 25 轮成功回复压三句，新会话开头注入一次；启动时若上次心跳超过 90 秒，从 `data_v4.db` 只读补最近 30 条私聊进交接备忘，**不自动回复**。仍不调模型、不发消息。

v0.9.2（2026-10-27）把睡眠分成**浅睡/深睡**：刚入睡 15 分钟浅睡，白名单私聊可直接叫醒；之后深睡，普通私聊拦截、只有 `/急事` 能叫醒；被叫醒后重新进入浅睡，第一次 5 分钟，之后每次减半、最低 1 分钟；浅睡中再说话只顺延窗口，不重复计数。窗口持久化在 `life/sleep.json`，被叫醒的那轮注入「刚被叫醒、还在浅睡」的标记，不改 availability、不切 open。

v0.9.3（2026-10-28，S1-C）补上**主动消息的可解释性**与**第二天跟进**：新增 `life/proactive_stats.json` 与 `trigger_stats.py`，proactive_chat 每次发送成功后按 `state/timer/manual` 记账；`/life stats` 看最近 7 天状态触发占比（目标 ≥33.3%，manual 不计分母）。新增 `store.render_day_brief()`，每个会话每天第一次请求把昨天最后 4 段作息压成「（昨天你：…）」注入，最多 180 字；只读 journal，不读聊天、不编事实。仍不调模型、不发消息。

## 它做什么

- **独占写入** `qqbot/data/life/`：`state.json`、`heartbeat.json`、`jobs.db`、`scheduler-alerts.log`、`journal/YYYY-MM-DD.jsonl`
- **单 scheduler 持久化**：`life.tick`（30s）与 `sched.heartbeat`（60s）存在 `jobs.db`；其他插件经 `scheduler_client.py` 的 `SharedScheduler` 接入同一 scheduler；目前共 9 个任务；插件热重载复用，进程重启恢复
- **参数版本化**（S2-B）：`params.json` 分 structure/tuned/drift/behavior 四组，每次修改前快照到 `params-history/`，`/life params` 可看版本、`set`、`rollback`
- **会话交接**（S2-B）：每 25 轮成功回复把最近对话压成三句写 `handover.json`，新会话只注入一次；确定性抽取，不额外调模型
- **消息补漏**（S2-B）：启动时若上次心跳 >90 秒，只读 `data_v4.db` 补最近 30 条私聊进交接，不自动回复；>24 小时不补
- **每小时幂等推进**：幂等键 `(date, hour)`，重启重放不叠加
- **每 60 秒心跳**：J3 判据（重启 2 分钟内任务恢复、心跳在跳）就看这个文件；带 `job_state` 看每个任务最近一次是否 ok
- **作息事实快照**：读 `data/life/life_state.py` 的 `state()`，把「此刻在哪、在做什么、什么感觉」写进 `state.json` 的 `fact.schedule`。作息表是纯函数，改课表改那个文件即可
- **四驱力**：social / curiosity / play / rest，低通 `v_t = 0.7·v_{t-1} + 0.3·v_target`，每 60 秒落一次盘
- **阈值判断**：`decision` 写进 `state.json`，动作只有四类——`opener`（想找他）/ `sleepy`（困了）/ `decline`（她自己说了先别找）/ `none`（静默时段、刚说过话、social 还低等，理由在 `reason` 里）
- **注入**：`on_llm_request` 在系统提示末尾追加一行（≤130 字，已有则不重复追加）。**只给事实、不给结论**（v4.0 §4.5）：时间、地点、在做什么、感觉、多久没说话、她自己填过的字段；不再写「这会儿正想找他」这类替她下的判断，情绪与语气交回模型
- **工具 `life_state`（严格只读）**：她可以被问到「现在在干嘛」「今天干嘛了」，读到 availability、事实、她自填的两栏和今天的时间线；不写 `state.json`、不创建目录、不重启任务
- **工具 `life_update`（自写）**：`field` ∈ activity/location/mood/note，`value` 为空表示清回未设置；只写 `self`，不碰事实
- **工具 `life_decline`（拒绝出口）**：她可以在真的不想聊时说「到几点之前先别找我」（0.5–24 小时，默认 2），写进 `state.decline.until`；`decide()` 立刻转 `decline`，`proactive_chat` 的闸门据此不发、也不提前挪时间。**到期自动失效**，不需要谁去清；`/life clear` 可提前结束
- **管理员命令 `/life`**：`/life` 看状态，`/life today` 看今天的时间线，`/life files` 看文件位置与作息模块加载情况，`/life stats` 看最近 7 天主动消息触发来源与状态触发占比，`/life why` 看四驱力的当前值/目标值、闲置分钟、判断与理由、安静窗口（调阈值时用这个），`/life decline` / `/life clear` 手动进出安静窗口

## 状态分两栏

| 栏位 | 谁写 | 内容 | 缺省 |
| --- | --- | --- | --- |
| `fact` | 系统 | 时间、时段、此刻（地点/在做什么/心情/细节）、推进次数、心跳 | 作息模块可用时永远有值 |
| `self` | 她自己 | activity / location / mood / note | `null` = **未设置**，不替她编 |

「未设置就是未设置」——不冒认、不推测。这来自 digital-life 的 `state-board.mjs` 原则。

## 能不能聊天（P0.1）

`state.json` 顶层新增四个字段，全部由已有事实**派生**，不另存第二份状态：

| 字段 | 含义 |
| --- | --- |
| `availability` | `open` / `focus` / `rest` / `sleep` |
| `activity` | 当前活动文案，如「在上课」 |
| `availability_since` | 当前状态从什么时候开始 |
| `next_transition` | 下一次状态变化的时间 |

派生优先级：**自己说的先别找 → 静默/睡着 → 作息忙 → 普通休息 → 可聊**。`life_state`、`/life`、`/life files` 都能读；`proactive_chat` 只在 `open` 时允许主动发，`focus/rest/sleep` 直接不发、不调模型，日志写明 `life_focus` / `life_rest` / `life_sleep`。

## 今天的时间线（P1-A）

`journal/YYYY-MM-DD.jsonl` 里的 `kind="fact"` 按 `slot_key` 去重后，变成「07:20 食堂 吃早饭 / 08:00 教室 上课」这样的时间线。`life_state` 和 `/life today` 读到的是它；没有记录就明说「还没有作息记录」，不编。

连续跑满 24 小时后，起床、吃饭、上课/工作、休息、睡觉都会在 journal 里留下事实行。当前没有单独的「睡觉」行；睡觉由作息段的 `doing` 与 `availability=sleep` 体现。

## 驱力怎么算

目标值由作息段和闲置时长决定，然后向目标低通靠近：

| 驱力 | 目标 |
| --- | --- |
| `social` | `min(0.95, 0.15 + 闲置分钟/75)`；睡觉压 0.20、上课压 0.72（v0.3.2：原来是 0.55，永远够不到 0.7 阈值，等于上课时段不可能想说话）、静默时段压 0.25 |
| `rest` | 睡觉 0.95；21 点后 0.55；其余 0.30 |
| `curiosity` | 睡觉或静默 0.30；其余 0.55 |
| `play` | 睡觉或上课 0.30；其余 0.50 |

`social ≥ social_threshold`（默认 0.7）**且**闲置 ≥ `idle_trigger_minutes`（默认 45）时才判 `opener`。`recognition` / `achievement` 还没做——它们要有关系和成就数据，属于 S4。

**判断不等于发送**：判断只写 `state.json`，注入只给事实；真正发不发由 `proactive_chat` 决定。同一条资源一个权威。

## 拒绝出口（v0.4.0）

她有权说「这会儿先别找我」，而且这句话要**真的生效**：

| 环节 | 行为 |
| --- | --- |
| 她调用 `life_decline(hours=2, reason=...)` | 写 `state.decline = {until, reason, at}`，时长夹在 0.5–24 小时 |
| `decide()` | 窗口内一律返回 `action="decline"`（优先级最高，连「困了」都不覆盖它） |
| 注入那一行 | 加一条事实：「你自己说过 19:00 之前先别找人说话。」——是事实，不是结论 |
| `proactive_chat._life_gate()` | 返回 `(False, "life_decline")`：不发、不提前挪时间，日志写明原因 |
| 到期 | `until` 过了自动失效，`decide()` 回到正常出口，无需任何人清理 |

## 睡眠闸门与 `/急事`（P1-B，v0.7.0；v0.9.2 浅睡/深睡）

`availability=sleep` 时，普通私聊由高优先级事件处理器处理：**浅睡**放行并叫醒，**深睡**静默拦截；不回复、不进模型、不触发主动消息。拦截只记一行 `kind="sleep_block"` 到当天 journal（用户 ID、字数、深浅层，不存正文）。

窗口规则（所有者 2026-10-27 确认）：

| 阶段 | 时长 | 普通私聊 | `/急事` |
| --- | --- | --- | --- |
| 刚入睡 | 15 分钟 | 白名单内直接叫醒 | 可用，进入下一档 |
| 深睡 | 浅睡窗口结束后 | 静默拦截 | 可用，进入下一档 |
| 第一次醒后 | 5 分钟 | 白名单内直接接话；再说话只顺延 | 可用，进入下一档 |
| 之后每次醒 | 5 / 2.5 / 1.25 / 1 分钟，最低 1 分钟 | 同上 | 同上 |

- 浅睡可叫醒的 QQ 由 `sleep_wake_uids` 控制，默认所有者 `OWNER_QQ` 和朋友 `FRIEND_QQ`；留空表示不额外限制（仍受 AstrBot 私聊白名单约束）。
- 被叫醒后**仍算睡着**，只注入「刚被叫醒、还在浅睡」；回复完继续浅睡，不切 `open`。
- 浅睡中再发消息只把当前窗口从“现在”往后顺延，不增加 `wake_count`；只有从深睡被叫醒才进入下一档、窗口减半。
- 窗口存 `life/sleep.json`，按 `availability_since` 识别睡眠周期；新的一觉从 0 开始，不继承上一夜计数。
- `/急事` 仍只允许所有者，每日默认 3 次；浅睡、深睡都可用，并会进入下一档浅睡。

| 输入 | 睡着时行为 |
| --- | --- |
| 浅睡 + 白名单普通文字 | 放行、叫醒、注入浅睡标记；`kind="sleep_wake"` 审计 |
| 深睡 + 普通文字 / 图片 / 文件 | 静默拦截，`event.stop_event()` |
| 带 `/` 的指令（`/life`、`/reset`、`/急事`…） | 不拦，交给原命令/处理器 |
| `/急事 <一句话>`（仅所有者） | 每日默认 3 次；记账后放行给正常对话链，注入「这是急事」+「刚被叫醒」 |
| `/急事` 空参数 | 回用法，不计数 |
| 非所有者发 `/急事` | 回「只有所有者可以用」，不计数 |
| 配额用完 | 回「今天名额用完了，真的很急请直接打电话」，不调模型 |

`/急事` **不是注册命令**（不占 `/api/v1/commands` 的 25 条），而是睡眠闸门里识别的文本；配额按当天 journal 的 `kind="urgent"` 行计数，跨天自动归零。`/life files` 会显示 `今日急事：n/3`。

## 日记（v0.7.1）

`/life diary` 读 `journal/YYYY-MM-DD.jsonl`，把当天 `fact`（按 `slot_key` 去重）和 `self`（她最后自填的心情/活动等）汇成一段「我今天做了什么」。也可以指定日期：`/life diary 2026-10-26`，只读那天，不会串到别的日期。

边界：

- **不是聊天摘要**：不输出对话轮数、判断次数；那些留在 journal 里给审计，不塞进日记。
- 没有记录就直说「今天没有记录」，不编行程。
- 只读；读缺失日期不会创建文件或目录。
- 自动落盘：`tick` 在 21:00 后把当天写成 `journal/YYYY-MM-DD.diary.md`；每次 tick 也会给昨天补写（已有文件不覆盖，没有 journal 就不写）。落盘失败只记 warning，不影响心跳。

## 状态触发占比与第二天跟进（S1-C，v0.9.3）

### 触发来源统计

`life/proactive_stats.json` 按本地日期分桶，只记主动消息**为什么发**：

- `state`：`life` 判断 `opener` 后，由 `proactive` 的 `_pull_in_session()` 把下一次提前（`next_trigger_source="state"`）
- `timer`：普通 20–180 分钟倒计时（每次 `_schedule_next_chat_and_save()` 写 `timer`）
- `manual`：WebUI「立即触发」，不计入占比分母

`proactive_chat` 在消息**送达后**才调用 `trigger_stats.record()`；生成失败、被新消息丢弃、未送达都不记账。`/life stats` 返回最近 7 天的 state/timer/manual、状态触发占比与统计文件路径；目标 ≥33.3%，低于目标时先看是不是 opener 被 availability 收口或 pending 压住，不要直接改倒计时。

### 昨日摘要

`store.render_day_brief(day, max_chars)` 只取当天 journal 最后 4 段 `fact`，格式「HH:MM 地点，在做什么」，默认 ≤180 字，不读聊天、不生成新事实；没有记录返回空串。

`inject_state` 在每个会话每天第一次请求时注入「（昨天你：…。今天如果自然，可以提一句；不必硬提。）」，同一会话同一天只注入一次；已有「（昨天你：」时不重复。配置 `day_brief_enabled`、`day_brief_max_chars`（最小 60）。这是**注入**，不是主动消息：她可以选择不提，不强制。

## 跨会话连续性（S2-B，v0.9.1）

### 参数版本化

`life/params.json` 把参数分四组：`structure`（结构常量）、`tuned`（自动调参）、`drift`（漂移量）、`behavior`（行为参数）。每次 `set` 前把当前文件快照到 `params-history/vN.json`，只保留最近 20 份；`rollback N` 先把当前存成新版本，再恢复到第 N 版，可回退。

| 命令 | 行为 |
| --- | --- |
| `/life params` | 显示当前 version 与四组参数 |
| `/life params set behavior.urgent_daily_limit 3` | 写入并生成新 version；值按 JSON 解析，失败则当字符串 |
| `/life params rollback 2` | 回滚到 v2（当前版本先入历史） |

### 会话交接

`life/handover.json` 按 `unified_msg_origin` 分会话保存：`turns`（累计成功回复）、`since`、`summary`（最多 3 句）、`catchup`（补漏三句）、`injected`（是否已注入）。每轮 `on_llm_response` 累计，满 `handover_every` 轮把最近对话压成 3 句；摘要为确定性抽取（谁说了什么），不额外调模型。

- 新会话第一次 `on_llm_request` 注入一次「（接上次对话的备忘：…）」，同一进程同一会话不重复。
- `/life handover` 看所有会话；`/life handover clear` 清空。
- 与 `llm_compress` 分工：压缩防溢出，交接防「换会话就失忆」。

### 消息补漏

启动时读 `heartbeat.json` 的 mtime；若距现在 > `catchup_gap_seconds`（默认 90 秒）且 < 24 小时，说明上次不是正常关停。此时只读 `data/data_v4.db` 的 `platform_message_history`，取该 QQ 最近 30 条私聊，压成 3 句写进 `handover.json` 的 `catchup`，**不自动回复**；>24 小时不补，避免重启翻旧账。

## 调度器（S2-A，v0.8.0）

- 持久化：APScheduler `AsyncIOScheduler` + `SQLAlchemyJobStore`，文件 `qqbot\data\life\jobs.db`。
- jobstore 里只存模块级 `run_job(name)`；真正回调放内存注册表，避免把插件实例/bound method 腌进数据库。
- 任务（9 个）：`life.tick`（`tick_seconds`，默认 30s）调 `_tick_once()`；`sched.heartbeat`（`heartbeat_seconds`，默认 60s）写 `heartbeat.json`；`proactive.opener_pullin`（120s）、proactive 会话任务（date）、`livingmemory.decay`（00:05 cron）、`livingmemory.atom_maintenance`（按 `maintenance_interval_hours`）、`self_learning.forgetting`（7 天 interval）、`self_learning.knowledge_update`（每小时）、`self_learning.recommend_refresh`（每 30 分钟）。
- 心跳字段：`ts/loop/jobs/last_job/job_state/pid/version/interval` + `slot/advance_count/availability`。
- 启动自检：检查 running、任务数/缺失（自动重建）、心跳 >180s、单任务超过 `stale_after`（默认 300s）、paused；告警写 `scheduler-alerts.log`。
- 失败：单个任务连续 3 次异常后 `pause_job` 并告警；不阻塞其他任务。
- 插件热重载：`terminate()` 不销毁 scheduler/jobs.db；新实例只补缺失任务、更新回调。
- 注意：改 `scheduler_runtime.py` 后，插件热重载不会重新 import 已缓存的模块；要让运行时改动生效，需重启 AstrBot 进程（jobs.db 会保留）。

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
| `sleep_gate_enabled` | true | 睡着时是否启用睡眠闸门；带 `/` 的指令不受影响 |
| `urgent_daily_limit` | 3 | 所有者每天可用几次 `/急事`，至少 1 |
| `sleep_light_seconds` | 900 | 刚入睡的浅睡窗口（秒），默认 15 分钟 |
| `sleep_wake_seconds` | 300 | 第一次醒后的浅睡窗口（秒），默认 5 分钟；之后每次减半 |
| `sleep_wake_min_seconds` | 60 | 浅睡窗口下限（秒），最低 1 分钟 |
| `sleep_wake_uids` | `OWNER_QQ,FRIEND_QQ` | 浅睡时可直接叫醒的 QQ，逗号分隔；留空不额外限制 |
| `handover_enabled` | true | 是否做每 25 轮三句交接 |
| `handover_every` | 25 | 成功回复多少轮生成一次交接，最小 5 |
| `catchup_enabled` | true | 启动时是否补漏 |
| `catchup_gap_seconds` | 90 | 上次心跳超过多少秒算掉线，最小 30 |
| `owner_qq` | `OWNER_QQ` | 补漏只读这个 QQ 的私聊 |
| `platform_id` | `qq-snowluma` | 补漏读哪个平台的消息库 |
| `day_brief_enabled` | true | 是否在每个会话每天首次请求注入昨日摘要 |
| `day_brief_max_chars` | 180 | 昨日摘要字数上限，最小 60；超出截断 |

## 已验证

- store 层 14 项单测：schema 自愈、旧文件升版、幂等刷新、坏 JSON 改名 `.json.bad` 重建、注入行在未设置时不编造、清空回到 null、journal 记 fact/self
- 插件层 9 项：注入追加一次、重复请求不重复追加、`inject_enabled=false` 时不改 prompt、`life_update` 别名/超长截断/空值清空/非法字段拒绝
- 驱力 16 项：6 小时模拟里单步变化 ≤0.075（阈值是 0.3）、social 随闲置单调上升并收敛到 0.95、`decide` 四个出口各命中一次、上课/睡觉的目标值被压低、注入前判断后记账（第一句带「想找他」、记完账闲置归零）
- 去结论化回归 13 项：注入行不再含「正想找他 / 真实的一天」、含「距上次说话已 45 分钟」这类事实、`opener` 与 `none` 的注入文本完全一致、短闲置不写闲置句、坏 JSON 时回落不抛错
- 拒绝出口 18 项：默认不在窗口、设置后 `decide` 立刻转 `decline`、30 分钟后仍 `decline`（不被驱力更新冲掉）、3 小时后自动失效、`clear_decline` 立即结束、0.1→0.5 与 99→24 的时长夹取、注入行含安静事实且不含结论句、旧 `state.json` 自动补 `decline`、闸门四态（decline/sleepy/none/文件缺失 fail-open）
- P0.1 只读/边界 11 项：`life_state` 前后 `state.json` 哈希不变、缺失 state 时不创建目录/文件、decline/quiet/focus 的 `next_transition` 分别取对、落盘值与派生值一致
- P1-A 时间线 7 项：journal fact 去重、limit、空记录不编、live 写入可见
- P1-A 主动消息闸门 7 项：sleep/focus/rest/decline 均拦截，open 放行，旧 state 与文件缺失 fail-open
- P1-B 睡眠闸门 14 项：深睡拦普通消息、浅睡白名单直接叫醒、浅睡二次消息只顺延、浅睡非白名单仍拦、深睡/浅睡 `/急事` 都进入下一档、醒着放行、醒着 `/急事` 不占配额、`/急事` 记账放行并带 `life_urgent`、配额用完拒、非所有者拒、带 `/` 指令不拦、空参数给用法
- 睡眠深浅窗口 17 项：初始 15 分钟浅睡、15 分钟后深睡、醒后 5/2.5/1.25/1 分钟递减、最低 1 分钟、浅睡再说话只顺延不计数、`sleep.json` 持久化、新周期重置、非睡眠返回 awake
- P1-B 日记 14 项：日期标题、fact 去重后段数、self 最后值、不输出聊天摘要、边界计数、按天隔离、空日诚实、缺日不建文件、写盘成功、写盘内容、已有文件不覆盖、缺日不写、命令可读、坏日期给格式
- S2-A 调度器：`tests\_t_scheduler.py` 覆盖注册/执行、删任务自检重建、shutdown 后同 jobs.db 恢复、心跳过期补写、删 jobs.db 重建告警
- S2-B 参数：`tests\_t_params.py` 覆盖默认四组、set 生成新版本、历史快照、rollback、坏 JSON 回落
- S2-B 交接：`tests\_t_handover.py` 覆盖 25 轮触发、三句上限、注入一次、`render_for_prompt` 带 catchup、clear
- S2-B 补漏：`tests\_t_catchup.py` 覆盖只读查询、角色解析、三句压缩、写 handover、`render_for_prompt(root, session)` 签名；10/10
- S1-C 触发统计：`tests\_t_trigger_stats.py` 覆盖三来源、manual 不进分母、按天窗口、60 天 prune、未知来源归 timer；10/10
- S1-C 昨日摘要：`tests\_t_life_next_day.py` 覆盖只取最后 4 段、不生成日记文件、max_chars 截断、缺日返回空；7/7
- 全量：2026-10-28 00:19 `tests\run-all.cmd` 17 个文件全过（含新增两份 S1-C 测试）
- 线上：2026-10-27 23:46 热重载 v0.9.2，`Plugin astrbot_plugin_life (v0.9.2)`，`[Scheduler] 自检通过：9 个任务，心跳 0s`；`sleep_gate` 仍在 handler 列表；深睡状态下 webchat 普通消息被拦（不是 QQ 白名单，属于预期边界）
- 线上：22:30:56 重启后 v0.8.0，`[Scheduler] 自检通过：9 个任务`，`jobs.db` 保留 9 个任务，PID 31424；`heartbeat.json` 22:30:58 起更新、`jobs=9`、`job_state` 9 项；日志有 `智能化提升服务启动成功`、`opener 提前触发巡检已接入统一 scheduler`
- 线上：webchat 一次真实请求触发 `[Life] 首次注入状态：（你此刻：周二 13:25，宿舍，午睡，迷迷糊糊；手机还亮着。…）`，模型据此答「刚才在午睡呢，手机还亮着呢」

## 已知边界

- S2-A 已迁 `life.tick`/`sched.heartbeat`、proactive opener/会话任务、`livingmemory.decay`、`livingmemory.atom_maintenance`、`self_learning.forgetting`、`self_learning.knowledge_update`、`self_learning.recommend_refresh`；self_learning 其余采集/情绪循环、meme/token 记账仍各自为政
- S2-B 的交接摘要是确定性抽取，不是模型总结；补漏只在启动时按心跳间隔触发，不监听 OneBot 重连事件；真实换会话/断网补漏尚未手工验收
- S1-C 触发统计只记**成功送达**的主动消息；日志按本地日期分桶，保留 60 天；manual 不计分母，所以 `state+timer=0` 时占比显示 0.0%（不是 100%）
- S1-C 昨日摘要是进程内每会话每天一次：热重载/进程重启后的同一天会再注入一次（同一天不重复的集合在内存里，不落盘）
- 浅睡/深睡已有单测与 webchat 拦截验证；真实 QQ 的白名单浅睡叫醒、深睡 `/急事`、醒后窗口递减尚未手工验收
- 改 `scheduler_runtime.py` 后插件热重载会复用旧模块，需重启 AstrBot 才生效
- `recognition` / `achievement` 还没做（要有关系值与成就记录）
- 不主动发消息（那是 `proactive_chat` 的事），不判断「该不该说」
- 时区按进程本地时间（北京），不做多时区
- 判断只在对话来临时与 60 秒 tick 时更新；没有对话的时段靠 tick 累积
