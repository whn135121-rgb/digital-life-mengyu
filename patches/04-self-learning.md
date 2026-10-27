# 04 · `self_learning`：表达模式入库前过滤

文件：`qqbot/data/plugins/astrbot_plugin_self_learning/services/analysis/expression_pattern_learner.py`
（本地版 748 → 788 行，纯新增 + 两处闸门）

## 起因

`expression_patterns` 表里堆了 425 条「情境 → 她的说法」。抽查发现三类垃圾：

1. **低价值样本**：「嗯嗯」「好的」「在吗」这种对答，学不到任何风格；
2. **纯标点**：`…` / `。。。。` 也被当成一句话；
3. **同一句重复入库**：160 组完全重复（键是 `(群, 人格, 情境, 表达)`，但早期没做约束）。

还有一个口径错误要先说明：这张表学的是**「用户说了什么 → 她怎么回」的正配对**，不是「学她自己的话」。之前按后者判断过一次，结论是错的。

## 改动

**常量（`:28`–`:30` 附近）**

```python
_MIN_SITUATION_CHARS = 5
_MIN_EXPRESSION_CHARS = 4
_LOW_VALUE_SITUATIONS = { ... }        # 36 个语气词/寒暄，如「嗯嗯」「在吗」「好的」
_PUNCT_ONLY = re.compile(r"^[\s\W_]+$", re.UNICODE)
```

**新增判定（`:450` 附近）** `_is_learnable_pair(situation, expression) -> bool`：

- 任一侧短于下限 → 否
- 任一侧是纯标点 → 否
- 情境去标点后落在 `_LOW_VALUE_SITUATIONS` → 否

**两处闸门**

- `_extract_few_shot_pairs`（`:428`）：原来只判 `len < 3`，换成 `if not self._is_learnable_pair(...): continue`。
- `_save_expression_patterns`（`:507`–`:511`）：入库循环前再判一次（防别的路径绕进来），并计数 `skipped_low_value`；
- 日志（`:570`）：`保存了 N 个表达模式到数据库（群组: …），过滤低价值 M 条`。

## 配置

无需新增配置项，阈值是模块常量。想放宽/收紧直接改常量——但改之前先看 §「清库」里的分布，别把正常样本一起砍掉。

## 已验证

从补丁后的文件里 AST 抽出常量和函数独立执行：

| 输入 | 期望 | 结果 |
| --- | --- | --- |
| 情境「嗯嗯好」/ 表达「是的是的」 | 拒绝 | ✅ |
| 情境「好的」/ 表达「行吧」 | 拒绝 | ✅ |
| 情境「…」/ 表达「。。。。」 | 拒绝 | ✅ |
| 情境「为什么不可以叫我主人啊」/ 表达「叫主人多生分啊，我又不是宠物」 | 保留 | ✅ |
| 情境「今天上课好累啊」/ 表达「那就早点睡，别熬了」 | 保留 | ✅ |

## 清库（一次性，不是补丁的一部分）

补丁只挡新数据，历史脏数据要单独清。做法（改库前先备份，且要注意 `qqbot` 子树需要更高权限才能写 SQLite）：

1. `persona` 三桶合并为统一的「梦屿」；
2. 删 `length(trim(situation)) < 5` 与 `length(trim(expression)) < 4`；
3. 删黑名单情境；
4. 按 `(group_id, persona_id, situation, expression)` 去重，保留 `min(id)`。

实测 **425 → 197**（删 228），剩下的分布：主号 181、好友 12、两个历史群 4。

## 升级注意

这是四个补丁里唯一**动到 AGPL-3.0 代码**的：本仓库只写说明、不分发源码。升级后按上面的函数名重新加，别整文件覆盖。
