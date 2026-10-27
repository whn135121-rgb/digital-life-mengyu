# 仓库约定（给协作者 / 给 AI agent）

这是**公开分享**的仓库，不是机器人运行目录。动手前先看这几条。

## 1. 先读什么

1. `README.md` —— 项目是什么、现在能做什么、不做什么。
2. `docs/01-系统设计.md` 的 §0（大方向与五条分支）—— 任何改动都要能对上其中一条，否则先改设计文档。
3. `docs/03-运行手册.md` §11（环境坑）—— 大部分「明明改了没生效」都记在那里。

## 2. 隐私红线（硬性）

**永远不要提交**：

- `qqbot/` 下的任何东西：数据库、日志、配置、表情包、人格备份
- 任何真实 QQ 号、面板密码、OneBot token、API key、Cookie
- 私聊记录（哪怕只截一句）
- 本机绝对路径（`C:\Users\<你>`、`D:\...`）

文档里统一用占位符：`OWNER_QQ`、`BOT_QQ`、`FRIEND_QQ`、`PANEL_PASSWORD`、`C:\path\to\workspace`。
提交前自查一遍：

```powershell
git diff --cached | Select-String -Pattern 'AstrBot@|SnowLuma@|sk-[A-Za-z0-9]{10,}|[0-9]{9,11}'
```

## 3. 代码边界

- `plugins/astrbot_plugin_life/` 是**唯一**随仓库分发的运行代码，MIT。
- `extras/life_state.py` 是纯函数，不引入 IO / 定时任务 / 网络。
- `patches/` 只记录对**第三方插件**的改动说明，不分发它们的源码（其中 `self_learning` 是 AGPL-3.0）。
- `scripts/` 只调本机面板 API，密码一律从环境变量 `ASTRBOT_PASSWORD` 读取。

## 4. 文档规矩

- 写数字必须标口径：**实测**（带日期、样本量）还是**外推/估算**。没标的一律视为估算。
- 「设计」与「现状」分两栏写；不要把没实现的东西写成已实现。
- 改了参数就同时改三处：正文、参数台账、附录里的差异表。
- 主文档升版本时在附录变更记录里写一行：版本、日期、改了什么、为什么。

## 5. 提交信息

```
<范围>: <一句话做了什么>
```

范围用 `life` / `docs` / `scripts` / `patches`。示例：
`life: state.json 加 self 栏，未设置保持 null`。
