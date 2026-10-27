# 02 · `meme_manager`：给情感选图加超时

文件：`qqbot/data/plugins/meme_manager/main.py`、`qqbot/data/plugins/meme_manager/mixins/event_handlers.py`

## 现象

每条回复发出前，`meme_manager` 会再调一次模型，判断「这条要不要配表情、配哪张」，这个调用是**同步阻塞**的：

- 日志实测每次 3–15 秒（均值 8.5 秒）；
- 用户看到的是「回复很慢」，而不是「她在想怎么回」；
- 更要命的是它跟 `ai_delay` 的延迟串行叠加，首条气泡最慢到过 19 秒。

## 改动

**`main.py`（约 :235）** 读配置并给默认值：

```python
("generation", "emotion", "llm", "timeout"),   # 新增配置项，默认 4.0
self.emotion_llm_timeout = max(0.5, float(emotion_llm_timeout))
```

**`mixins/event_handlers.py`（`_emotion_llm_generate`，约 :392）** 把调用包进 `asyncio.wait_for`（约 :411）：

```python
return await asyncio.wait_for(self.context.llm_generate(...), timeout=timeout)
```

`asyncio.TimeoutError` 时打一行 warning（`[meme_manager] 情感模型 4.0 秒未返回，本轮跳过表情选择`）并 `return None`。

## 为什么 `return None` 是安全的

三处调用点（约 :1245 / :1297 / :1385）对 `None` 都有兜底：拿不到判断就走原来的关键词/空结果路径，本轮不配表情而已，不会连回复一起丢。

## 效果与代价

- 选图最多拖 4 秒，超时就当没有表情（用户感知是「这条没配图」，不是「她卡住了」）。
- 代价：4 秒内没返回的那一次，表情包少发一张。比起让人等 15 秒，这个交换划算。

## 升级注意

配置键 `generation.emotion.llm.timeout` 已写进 `qqbot/data/config/meme_manager_config.json`；插件升级后如果配置被重写，先看键还在不在，再决定要不要重打代码补丁。
