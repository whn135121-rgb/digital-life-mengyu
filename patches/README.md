# 第三方插件本地改动

这四个插件都从 AstrBot 插件市场安装，源码不随本仓库分发（体积 + 版权，其中 `self_learning` 是 AGPL-3.0）。这里只记录**改了哪个文件、哪一行、原值、新值、为什么、升级会不会被覆盖**。

## 总表

| 补丁 | 插件 | 文件 | 目的 |
| --- | --- | --- | --- |
| [`01-ai-delay.md`](01-ai-delay.md) | `astrbot_plugin_ai_delay` v1.2.1 | `main.py` | 延迟按字数、注入生活状态、正在输入、长回复回执、错字自补（默认关闭） |
| [`02-meme-manager.md`](02-meme-manager.md) | `meme_manager` v5.2.6 | `main.py`、`mixins/event_handlers.py` | 情感选图加超时，别把回复链路堵死 |
| [`03-proactive-chat.md`](03-proactive-chat.md) | `astrbot_plugin_proactive_chat` v1.2.6 | `core/llm_adapter.py` | 主动消息也带「此刻在哪、在干什么」 |
| [`04-self-learning.md`](04-self-learning.md) | `astrbot_plugin_self_learning` 4.4.2 | `services/analysis/expression_pattern_learner.py` | 表达模式入库前过滤低价值样本 |

## 三个共同前提

1. **行号会漂**。这里的行号对应 2026-10-27 那一次安装的版本；插件升级后先按函数名/锚点搜索，再按行号核对。
2. **升级即覆盖**。市场更新是整目录替换，所有改动全部丢失，必须按本文重打。重打后记得重启（或重载）插件，并看一眼日志里有没有 traceback。
3. **改配置不用改代码**。上面所有新参数都通过插件配置项暴露，改完调参数只需改 `qqbot/data/config/<plugin>_config.json` 或面板。
