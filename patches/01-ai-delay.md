# 01 · `ai_delay`：延迟按字数、生活状态注入、正在输入、错字自补

文件：`qqbot/data/plugins/astrbot_plugin_ai_delay/main.py`（原 640 行左右，改后含新增函数）
行号基准：2026-10-27 的 v1.2.1 安装版。

---

## 1. 延迟按字数（改的是核心手感）

**原来**（`on_llm_resp` 里）：

```python
delay = random.uniform(tmin, tmax)
await asyncio.sleep(delay)
```

固定 1–3 秒，跟回复长短无关：回一个字和回两百个字花一样的时间。

**现在**：新增 `_calc_delay(text)`（约 :82），公式为

```
delay = uniform(min_delay, min(max_delay, min_delay + len(text) * delay_per_char))
delay *= uniform(1 - delay_jitter, 1 + delay_jitter)
```

实测（min=1 / max=9 / per_char=0.05 / jitter=0.25）：1 字 1.25s、10 字 1.39s、33 字 2.62s、200 字 8.77s。
**原因**：打字时间应该近似线性于字数；封顶 9 秒是为了不让人等成掉线。

新增配置（`_conf_schema.json` + `astrbot_plugin_ai_delay_config.json`）：
`delay_per_char=0.05`、`delay_jitter=0.25`，并把 `min_delay/max_delay` 从 1/3 调到 1/9。

---

## 2. 生活状态注入（`inject_life_state`，约 :332）

在 `@filter.on_llm_request()` 里，把 `qqbot/data/life/life_state.py` 算出的**一行**追加到 system prompt 末尾：

```
（你此刻：周二 09:30，教室，在上课，有点困。）
```

**原因**：她不联网、不看表，光靠 prompt 里写「你在上大学」不会知道现在几点、在不在上课。这一行给了她可陈述的当下。
**约束**：只加一行，不重复人格内容；作息表是纯函数（`extras/life_state.py`），没有 IO 也没有定时任务，不会拖慢请求。

---

## 3. 正在输入 + 长回复回执

- `_set_typing(event, on)`（约 :450）：`event.bot.call_action("set_input_status", user_id=..., event_type=0/1)`，开的时候起一个每 4 秒续一次、最多 12 次的心跳任务。
  - SnowLuma 的 `set_input_status` 在 `index.mjs` 里，参数 `user_id` / `event_type`，QQ 端显示「对方正在输入…」。
- `head_start`（约 :387）：对方消息 ≥ `head_start_min_chars`（20 字）时，延迟 `head_start_after`（4.5 秒）先发一句「我看看。」「等下啊，我看看。」
  - 关键：真的回信到达时要 `_cancel_head_start()`，否则先说「我看看」结果正文早发了。
- 只在 `hasattr(event.bot, "call_action")` 时执行，失败只 debug 不报错——别的平台适配器不会因此炸。

新增配置：`typing_indicator=true`、`head_start=true`、`head_start_after=4.5`、`head_start_min_chars=20`。

---

## 4. 错字自补（`_apply_typo` 约 :491，`after_sent` 约 :619）

**第一版是错的，记在这里免得再踩**：最初把更正挂在 1.5–2.8 秒定时器上、且把整句重发。结果正文被 `segmented_reply` 又推后 1.8–3.5 秒，更正反而先到；用户看到的就是「同一条消息说了两遍」。

**现在的做法**：

1. `_apply_typo(text)` 返回 `(打错整句, 短纠错句)`：随机对调一处相邻汉字；纠错句按标点切成短句，超过 14 字就退化成「错点前后 3–4 字」的窗口。
2. `on_decorating_result`（约 :538）命中时只把**短句**存进 `self._pending_fix[session]`，不发。
3. 新增 `@filter.after_message_sent()`（约 :619）：等这一轮所有分段都发完，再延迟 1.2–2.4 秒补发那条短句（「打错了，那我就陪你耗着呗」）。

新增配置：`typo_probability=0`、`typo_delay_min=1.2`、`typo_delay_max=2.4`。

**2026-10-27 晚修正**：用户反馈「前面错字、后面打错了」太刻意。代码保留，但 `typo_probability` 现网与默认都改为 `0`，插件不再强行制造错字；正文里要不要打错、要不要补一句，由梦屿自己写。`typo_delay_*` 只在有人手动调高概率时才生效。

**为什么当初值得做**：90 条消息里 0 个错字、0 次自我更正，是「太完美」的主要来源。但机械补句会被一眼看穿，所以最终选择关掉插件注入，而不是继续加规则。

---

## 接线顺序（重打补丁时按这个顺序）

```
merge_burst_messages(:226) → _set_typing(on) → [LLM]
  → on_llm_request: inject_life_state(:332) / head_start(:387)
  → on_llm_response: on_llm_resp(:404) 算延迟 + sleep → _set_typing(off)
  → on_decorating_result(:538) 拆气泡 / 记错字
  → after_message_sent(:619) 补发更正
```
