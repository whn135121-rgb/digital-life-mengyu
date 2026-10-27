"""astrbot_plugin_life —— 梦屿的「生活状态」最小骨架。

职责（只做四件事，别的都不做）：
1. 独占 `qqbot/data/life/` 的写入：state.json / heartbeat.json / journal/。
2. 每小时幂等推进一次（重启不重放）。
3. 每 60 秒写心跳（S2 的 J3 判据靠它）。
4. 暴露只读工具 `life_state` 与只读命令 `/life`，让模型和她自己都能读到
   「系统给的」与「她自己填的」两栏，未设置的字段保持未设置。

刻意不做：不注入对话（注入仍由 ai_delay 负责，避免两个写入者）、
不调用模型、不做意向判断、不调度别的任务。等 S2 的 jobstore 落地再谈。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .store import LifeStore

PLUGIN_NAME = "astrbot_plugin_life"
LOG = "[Life]"

# qqbot/data/plugins/astrbot_plugin_life/main.py -> qqbot/data
DATA_ROOT = Path(__file__).resolve().parents[2]


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@register(PLUGIN_NAME, "dsh", "梦屿的生活状态：独占状态文件、每小时幂等推进、心跳、只读工具", "0.1.0")
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
            f"{LOG} 已启动：数据目录 {self.root}，心跳 {self.store.heartbeat_seconds}s，"
            f"检查间隔 {self.tick_seconds}s"
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
            logger.info(f"{LOG} 推进到 {result['slot']}（第 {result['count']} 次）")

    async def terminate(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info(f"{LOG} 已停止")

    # ------------------------------------------------------------ 只读工具

    @filter.llm_tool(name="life_state")
    async def life_state(self, event: AstrMessageEvent):
        """读取当前生活状态（只读）。返回两栏：系统给的（时间、时段、推进次数、心跳）和
        她自己填的（活动、地点、心情、备注）。未设置的字段就是「未设置」，不要替她编。
        当你要回答「你现在在干嘛 / 在哪儿 / 心情怎么样」，或需要确认时间与自身状态时调用。
        这个工具不能改状态，只能读。"""
        self._ensure_task()
        return self.store.render()

    # ------------------------------------------------------------ 只读命令

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
            ]
            yield event.plain_result("\n".join(lines)).use_t2i(False)
            return
        yield event.plain_result(self.store.render()).use_t2i(False)
