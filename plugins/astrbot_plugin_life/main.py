"""astrbot_plugin_life —— 梦屿的「生活状态」骨架。

职责（只做六件事，别的都不做）：
1. 独占 `qqbot/data/life/` 的写入：state.json / heartbeat.json / journal/。
2. 每小时幂等推进一次（重启不重放）。
3. 每 60 秒写心跳（S2 的 J3 判据靠它）。
4. 把作息模块算出的「此刻」落成事实快照（fact.schedule），不再由别处临时计算。
5. 唯一注入点：`on_llm_request` 在系统提示末尾追加一行现状（可关）。
6. 暴露 `life_state`（只读）/ `life_update`（她自己写）/ `/life`（管理员看）。

刻意不做：不调用模型、不做意向判断、不调度别的任务。等 S2 的 jobstore 落地再谈。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .store import SELF_FIELDS, SELF_LABELS, LifeStore

PLUGIN_NAME = "astrbot_plugin_life"
VERSION = "0.2.0"
LOG = "[Life]"

# qqbot/data/plugins/astrbot_plugin_life/main.py -> qqbot/data
DATA_ROOT = Path(__file__).resolve().parents[2]


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@register(PLUGIN_NAME, "dsh", "梦屿的生活状态：状态文件、小时推进、心跳、注入、只读/自写工具", VERSION)
class LifePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = DATA_ROOT / "life"
        self.store = LifeStore(
            self.root,
            heartbeat_seconds=_as_int(config.get("heartbeat_seconds", 60), 60),
        )
        self.tick_seconds = max(5, _as_int(config.get("tick_seconds", 30), 30))
        self.journal_enabled = bool(config.get("journal_enabled", True))
        self.inject_enabled = bool(config.get("inject_enabled", True))
        self.schedule_file = str(config.get("schedule_file", "life_state.py") or "").strip()
        self._module = None
        self._module_error: str | None = None
        self._injected_once = False
        self._task: asyncio.Task | None = None
        self._last_beat = 0.0
        self._started = False
        self._ensure_task()

    # ------------------------------------------------------------ 生命周期

    def _ensure_task(self) -> None:
        """幂等地启动常驻任务；插件重载后旧任务可能已被取消。"""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"{LOG} 没有运行中的事件循环，暂不启动心跳任务")
            return
        self._task = loop.create_task(self._tick_loop())
        self._started = True
        logger.info(
            f"{LOG} 已启动 v{VERSION}：数据目录 {self.root}，心跳 {self.store.heartbeat_seconds}s，"
            f"检查间隔 {self.tick_seconds}s，注入 {'开' if self.inject_enabled else '关'}"
        )

    async def _tick_loop(self) -> None:
        try:
            self.store.ensure()
            self._boot_report()
            while True:
                await asyncio.sleep(self.tick_seconds)
                try:
                    self._tick_once()
                except Exception as exc:  # 单次失败不能打死常驻任务
                    logger.warning(f"{LOG} 本轮检查失败：{exc!r}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"{LOG} 心跳任务异常退出")

    def _boot_report(self) -> None:
        result = self.store.advance(journal=self.journal_enabled)
        self._refresh_fact()
        state = self.store.load_state()
        self._beat(state)
        if result["advanced"]:
            logger.info(f"{LOG} 启动补推进：{result['slot']}（第 {result['count']} 次）")
        else:
            logger.info(f"{LOG} 启动：{result['slot']} 已推进过，跳过（第 {result['count']} 次）")

    def _beat(self, state: dict | None = None) -> None:
        state = state or self.store.load_state()
        self.store.beat(
            extra={
                "slot": state.get("last_advance"),
                "advance_count": state.get("advance_count", 0),
                "plugin": PLUGIN_NAME,
                "version": VERSION,
            }
        )
        self._last_beat = time.time()

    def _tick_once(self) -> None:
        # 0.75 倍阈值而不是 1.0 倍：tick 有 30 秒粒度，卡 1.0 会退化成 90 秒一次
        due = max(5.0, self.store.heartbeat_seconds * 0.75)
        if time.time() - self._last_beat >= due:
            self._beat()
        result = self.store.advance(journal=self.journal_enabled)
        if result["advanced"]:
            self._refresh_fact()
            logger.info(f"{LOG} 推进到 {result['slot']}（第 {result['count']} 次）")
        else:
            self._refresh_fact()

    async def terminate(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info(f"{LOG} 已停止")

    # ------------------------------------------------------------ 作息事实

    def _schedule(self):
        """从 data/life/life_state.py 读作息；文件缺失或语法错就返回 None。"""
        if self._module is not None:
            return self._module
        if not self.schedule_file:
            return None
        path = self.root / self.schedule_file
        if not path.exists():
            if self._module_error != f"missing:{path}":
                self._module_error = f"missing:{path}"
                logger.warning(f"{LOG} 作息模块不存在，跳过事实刷新：{path}")
            return None
        try:
            spec = importlib.util.spec_from_file_location("dsh_life_schedule", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"无法加载 {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules["dsh_life_schedule"] = module
            spec.loader.exec_module(module)
            if not hasattr(module, "state"):
                raise RuntimeError(f"{path} 缺少 state() 函数")
            self._module = module
            self._module_error = None
            logger.info(f"{LOG} 作息模块已加载：{path}")
            return module
        except Exception as exc:
            if self._module_error != repr(exc):
                self._module_error = repr(exc)
                logger.warning(f"{LOG} 作息模块加载失败，跳过事实刷新：{exc!r}")
            return None

    def _refresh_fact(self) -> None:
        module = self._schedule()
        if module is None:
            return
        try:
            data = module.state()
        except Exception as exc:
            logger.warning(f"{LOG} 计算作息状态失败：{exc!r}")
            return
        self.store.refresh_schedule(data, journal=self.journal_enabled)

    # ------------------------------------------------------------ 注入

    @filter.on_llm_request()
    async def inject_state(self, event: AstrMessageEvent, req):
        """把一行现状拼到系统提示末尾（唯一注入点，ai_delay 的旧注入已关）。"""
        if not self.inject_enabled:
            return
        self._ensure_task()
        try:
            line = self.store.render_line()
        except Exception as exc:
            logger.warning(f"{LOG} 生成状态行失败：{exc!r}")
            return
        if not line:
            return
        base = getattr(req, "system_prompt", "") or ""
        if "（你此刻：" in base:
            return
        try:
            req.system_prompt = f"{base}\n\n{line}".strip()
            if not self._injected_once:
                self._injected_once = True
                logger.info(f"{LOG} 首次注入状态：{line}")
        except Exception as exc:
            logger.warning(f"{LOG} 注入状态失败：{exc!r}")

    # ------------------------------------------------------------ 工具

    @filter.llm_tool(name="life_state")
    async def life_state(self, event: AstrMessageEvent):
        """读取当前生活状态（只读）。返回两栏：系统给的（时间、此刻在哪在做什么、推进次数、心跳）
        和她自己填的（在做什么、在哪、心情、备注）。未设置的字段就是「未设置」，不要替她编。
        当你要回答「你现在在干嘛 / 在哪儿 / 心情怎么样」，或需要确认时间与自身状态时调用。
        这个工具不能改状态，只能读。

        Returns:
            一段自然语言文字。
        """
        self._ensure_task()
        self._refresh_fact()
        return self.store.render()

    @filter.llm_tool(name="life_update")
    async def life_update(self, event: AstrMessageEvent, field: str, value: str = ""):
        """记录她自己的当前状态（只写「她自己填的」那一栏）。只在真有必要时用：
        换了地方、刚做完一件想记住的事、心情明显变了。不要每轮都写，不要为了迎合对方编状态。
        传空字符串表示「这个字段不填了」，会清回未设置。

        Args:
            field(string): 要写的字段，只能是 activity（在做什么）/ location（在哪）/ mood（心情）/ note（备注）。
            value(string): 写成什么，一句话，不超过 30 字。传空字符串则清空该字段。
        """
        self._ensure_task()
        name = (field or "").strip().lower()
        aliases = {
            "在做什么": "activity",
            "活动": "activity",
            "在哪": "location",
            "地点": "location",
            "心情": "mood",
            "备注": "note",
        }
        name = aliases.get(name, name)
        if name not in SELF_FIELDS:
            allowed = "、".join(f"{k}（{SELF_LABELS[k]}）" for k in SELF_FIELDS)
            return f"字段不认：{field!r}。只能写 {allowed}。"
        text = (value or "").strip()
        if len(text) > 30:
            text = text[:30]
        self.store.set_self(name, text or None, source="model")
        if text:
            return f"已记下：{SELF_LABELS[name]}＝{text}"
        return f"已清空：{SELF_LABELS[name]}"

    # ------------------------------------------------------------ 命令

    @filter.command("life")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_cmd(self, event: AstrMessageEvent):
        """查看生活状态文件（管理员）：/life 状态，/life files 文件位置"""
        if not event.is_admin():
            yield event.plain_result("权限不足：仅 AstrBot 管理员可以查看。")
            return
        arg = str(event.message_str or "").strip().lower()
        if arg in {"files", "path", "文件"}:
            state = self.store.load_state()
            lines = [
                f"数据目录：{self.root}",
                f"state.json：{'存在' if self.store.state_path.exists() else '缺失'}",
                f"heartbeat.json：{'存在' if self.store.heartbeat_path.exists() else '缺失'}",
                f"journal：{self.store.journal_dir}",
                f"boot_id：{state.get('boot_id')}",
                f"推进次数：{state.get('advance_count')}，最近 {state.get('last_advance')}",
                f"作息模块：{self.schedule_file}（{'已加载' if self._module else '未加载'}）",
            ]
            yield event.plain_result("\n".join(lines)).use_t2i(False)
            return
        self._refresh_fact()
        yield event.plain_result(self.store.render()).use_t2i(False)
