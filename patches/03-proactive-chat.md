# 03 · `proactive_chat`：主动消息也带「此刻」

文件：`qqbot/data/plugins/astrbot_plugin_proactive_chat/core/llm_adapter.py`

## 问题

主动消息的提示词里有 `{{current_time}}` 和 `{{unanswered_count}}`，**没有生活状态**。结果「主动关心」只是定时器到了就发一句，时间上对，内容上没有来源：她既不知道自己在哪、在干什么，也不知道现在是不是该困了。

## 改动

**`:41`–`:57`** 加一个惰性加载器，按路径找 `qqbot/data/life/life_state.py`，用 `importlib` 加载成独立模块，调 `render()`：

```python
_LIFE_PATH = Path(__file__).resolve().parents[3] / "life" / "life_state.py"
...
def _life_line() -> str:
    return _LIFE_MODULE.render()
```

**`:558`–`:563`** 在 `_build_content()` 组装完提示词后，把这一行放到**最前面**：

```python
life = _life_line()
if life:
    # [dsh 本地补丁 2026-10-26] 主动消息也要知道自己此刻在哪、在干什么，
    # 否则「主动关心」只能是定时器到了就发一句，没有生活来源。
    content = f"{life}\n\n{content}"
```

## 为什么放最前面

主动消息本来就是短句，模型对开头的注意力最高；后面才是聊天流水和任务要求。避免「提示词很长、当前状态被埋在中间」。

## 与 `ai_delay` 的关系

同一个作息表（`extras/life_state.py`）被两个插件调用：`ai_delay` 用于对话注入，`proactive_chat` 用于主动消息。**作息表只有一份**，改作息只改那个文件，两边同时生效——这是刻意的，否则她会「对话时说在上课、主动消息说在宿舍」。

## 失败模式

- 文件不存在 → `_LIFE_LINE_ENABLED` 之类开关置空，注入跳过，不报错。
- 加载失败 → 只打 debug，主动消息照常发（宁可少一行状态，也不要因为一行状态发不出去）。
