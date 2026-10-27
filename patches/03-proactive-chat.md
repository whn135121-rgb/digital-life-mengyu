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

---

## 追加补丁（2026-10-27）：让 `life` 的判断真正影响发送

**问题**：`life` 插件已经把「睡着 / 想说话 / 平静」写进 `state.json`，但主动消息链路完全不读它——定时器到点就发。而且 proactive 走 `provider.text_chat()` 直调，**不经过 `on_llm_request`**，所以插件那边的注入对它无效，`_life_line()` 是它唯一的状态来源。

**文件 1**：`qqbot/data/plugins/astrbot_plugin_proactive_chat/core/chat_flow.py`

```python
_LIFE_STATE = Path(__file__).resolve().parents[3] / "life" / "state.json"

def _life_gate() -> tuple[bool, str]:
    """读 life 的判断；读不到就放行，绝不挡链路。"""
    try:
        decision = (json.loads(_LIFE_STATE.read_text(encoding="utf-8")).get("decision") or {})
    except Exception:
        return True, "life_state_unreadable"
    if decision.get("action") == "sleepy":
        return False, "life_sleepy"
    return True, "allowed"
```

在 `_is_chat_allowed()` 的 `quiet_hours` 判断之后：

```python
life_ok, life_reason = _life_gate()
if not life_ok:
    return False, life_reason
```

`check_and_chat()` 的阻断日志补一个 `life_sleepy` 分支（「她这会儿在睡」）。

**文件 2**：同目录 `llm_adapter.py`

新增 `_life_state_line()`：从 `state.json` 读 `fact.schedule`，拼「（你此刻：周二 15:39，图书馆，在上课或者在自习，脑子转得慢；水杯空了懒得去接。…」；`decision.action == "opener"` 时追加「你隔了一阵没人说话，这会儿正想找他。」；`self` 有值时追加「你自己记过：…」。`_life_line()` 改成**先试它、失败再回落到作息表**。

## 为什么是「拦」而不是「推」

`social ≥ 阈值` 之后更该做的是**提前发**，但那要改调度（§16.3 S2 的活）。这一版只做无风险的一半：**她睡着的时候不要替她开口**。判断与发送仍然分属两个插件——判断归 `life`，发送归 `proactive_chat`，同一条资源只有一个权威。

## 失败模式

- `state.json` 缺失或坏 → `life_state_unreadable`，放行（宁可多发一条，也不要因为读不到状态而不说话）。
- `life` 插件被禁用 → 同上，自动放行。
