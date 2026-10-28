# 05 · `livingmemory`：检索强化、注入预算、证据链

文件都在 `qqbot/data/plugins/astrbot_plugin_livingmemory/`（本地版 2.8.0，改动处均带 `# [dsh 本地补丁 2026-10-28]` 注释）。

## 起因

三件「已经攒了数据、但代码没用起来」的事：

1. `memory_atoms` 有 `reinforcement_count` / `last_reinforced_at`，全文搜索只有写入侧的 `run_manual_reinforcement()` 会碰，**召回命中从不强化**，线上 290 条原子全是 0。
2. 注入只受 `top_k` 约束，没有字数上限。长记忆一多就把上下文吃掉。
3. 「你怎么记得这个」答不出来——原子没有出处，`/lmem` 也没有查询入口。

## 改动一：O4 强化 + 反刍抑制

**新增** `core/retrieval/atom_reinforcer.py`（约 120 行）

- `AtomReinforcer(atom_store, config, clock=None)`，窗口 / 阈值 / 系数默认 `3600s / 3 次 / 0.85`，读 `recall_engine.rumination_*`（扁平键与带前缀两种写法都认），非法值回退默认；
- `penalty_factor(atom_id)`：同一原子 1 小时内第 3 次起返回 0.85，其余 1.0；
- `record(ids)` 记内存命中历史并剪窗口；`reinforce(ids)` 逐条 `await atom_store.reinforce(id)`，单条失败只 warning。

**改** `core/retrieval/atom_retriever.py`

- `__init__(atom_store, config, reinforcer=None)`；
- `search()` 里 `final_score *= reinforcer.penalty_factor(atom_id)`（`relevance_only=True` 时跳过）；
- 取完 top-k 后 `record(ids)` + `await reinforce(ids)`。

**改** `core/managers/memory_engine.py`：`initialize()` 里建 `self.atom_reinforcer`，传给 `AtomRetriever`。

**改** `core/plugin_initializer_finalize.py`：把 `rumination_window_seconds` / `rumination_hit_threshold` / `rumination_penalty` 拍成扁平键。

## 改动二：注入预算 1200 字

**新增** `core/utils/injection_budget.py`

- `memory_priority(m, now)` = `importance × e^(-age/30天)`；
- `apply_injection_budget(memories, budget_chars, ...)`：按优先级降序贪心装，**保留项恢复原检索顺序**，返回 `(保留列表, 截断条数)`；成本按 `len(content) + 60` 估；`budget <= 0` 不限；极小额至少留一条。

**改** `core/event_handler_modules/memory_recall.py`

- 读 `recall_engine.injection_budget_chars`（默认 1200）；
- `apply_injection_budget()` 先裁，再 `format_memories_for_injection(..., max_chars=预算)` 做硬上限；
- 注入后写日志：`记忆注入统计：条数=… 字数=… 截断=… 预算=…`。

## 改动三：证据链

**新增** `core/utils/evidence.py`：`build_evidence()`（说话人 / 会话 / 时间 / 来源）、`ensure_evidence()`、`format_evidence()`、`describe_atom()`、`speaker_from()`（参与者优先，否则从 `…:QQ号` 反推）。

**改** `core/processors/atom_classifier.py`：建 `MemoryAtom` 时写 `metadata={"evidence": ...}`（`metadata` 列本来就是 JSON，**不需要迁移**）。

**改** `core/command_handler.py` + `main.py`：新增 `handle_evidence()` 与 `/lmem evidence <原子ID>`（admin）。

存量回填：`scripts/backfill_atom_evidence.py`（按路径加载 `evidence.py`，`--dry-run` 预演，正式跑先 `copy2` 备份，只 `UPDATE metadata`），实测 290/290。

## 配置

`_conf_schema.json` 的 `recall_engine` 段新增 4 项（线上 `qqbot/data/config/astrbot_plugin_livingmemory_config.json` 同步写入）：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `injection_budget_chars` | 1200 | 单次注入字数上限，0 = 不限 |
| `rumination_window_seconds` | 3600 | 反刍抑制的统计窗口 |
| `rumination_hit_threshold` | 3 | 窗口内第几次起降权 |
| `rumination_penalty` | 0.85 | 降权系数 |

## 已验证

- `tests/_t_s3_memory.py` 32/32（预算裁剪、优先级、抑制系数、写回计数）；`tests/_t_s3_evidence.py` 42/42（含源码级接线断言）。
- 线上日志（2026-10-28 15:12 起真实对话）：`记忆注入统计：条数=5 字数=1200 截断=2 预算=1200`，预算确实在顶。
- `memory_atoms` 里出现 `reinforcement_count=11` 的原子，说明命中→强化链路通了（随后清零重计，脚本 `scripts/reset_reinforcement.py`）。

## 升级注意

插件市场更新是**整目录替换**，以上全部丢失。重打顺序：新增三个文件 → 四个改动文件按函数名重新插入 → `_conf_schema.json` 补四项 → 重载插件 → 看日志有没有 `记忆注入统计`。`injection_budget.py` / `atom_reinforcer.py` / `evidence.py` 是纯新增，直接整文件拷回即可。
