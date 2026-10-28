"""astrbot_plugin_life —— 梦屿的「生活状态」与「驱力」。

职责：
1. 独占 `qqbot/data/life/` 的写入：state.json / heartbeat.json / journal/ / jobs.db。
2. 单 APScheduler（SQLAlchemyJobStore）持久化任务；每小时幂等推进一次（重启不重放）。
3. 每 60 秒写心跳，带每个任务的最近运行状态（S2 的 J3 判据靠它）。
4. 把作息模块算出的「此刻」落成事实快照（fact.schedule）。
5. 维护四驱力（social/curiosity/play/rest）与其一阶滤波，按阈值给出「此刻该不该开口」的判断。
6. 唯一注入点：`on_llm_request` 在系统提示末尾追加一行现状（可关）。
7. 暴露 `life_state`（只读）/ `life_update`（她自己写）/ `/life`（管理员看）。
8. 睡着时拦普通私聊；`/急事` 由所有者按每日配额叫醒，放行给正常对话链。

刻意不做：不调用模型、不负责发主动消息（发送仍归 proactive_chat，本插件只判断
「她想不想说」，判断写在 state.json 的 decision 里，谁发谁看）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.triggers.interval import IntervalTrigger
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .store import (
    DECLINE_MAX_HOURS,
    DECLINE_MIN_HOURS,
    DRIVES,
    SELF_FIELDS,
    SELF_LABELS,
    LifeStore,
)

PLUGIN_NAME = "astrbot_plugin_life"
VERSION = "0.10.1"
LOG = "[Life]"

# qqbot/data/plugins/astrbot_plugin_life/main.py -> qqbot/data
DATA_ROOT = Path(__file__).resolve().parents[2]


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default if value is None else bool(value)


def _quiet_window(text: str) -> tuple[int, int] | None:
    """把 "23-8" 解析成 (23, 8)；跨零点允许。"""
    raw = (text or "").replace("：", ":").strip()
    if not raw or "-" not in raw:
        return None
    left, _, right = raw.partition("-")
    try:
        start, end = int(left), int(right)
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 24):
        return None
    return start, end


@register(PLUGIN_NAME, "dsh", "梦屿的生活状态与驱力：状态文件、小时推进、心跳、四驱力、注入、只读/自写工具", VERSION)
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
        self.drive_update_seconds = max(10, _as_int(config.get("drive_update_seconds", 60), 60))
        self.social_threshold = min(1.0, max(0.0, _as_float(config.get("social_threshold", 0.7), 0.7)))
        self.idle_trigger_minutes = max(1.0, _as_float(config.get("idle_trigger_minutes", 45), 45.0))
        self.quiet_window = _quiet_window(str(config.get("quiet_hours", "23-8")))
        self.sleep_gate_enabled = _as_bool(config.get("sleep_gate_enabled", True), True)
        self.sleep_light_seconds = max(60, _as_int(config.get("sleep_light_seconds", 900), 900))
        self.sleep_wake_seconds = max(60, _as_int(config.get("sleep_wake_seconds", 300), 300))
        self.sleep_wake_min_seconds = max(30, _as_int(config.get("sleep_wake_min_seconds", 60), 60))
        raw_uids = str(config.get("sleep_wake_uids", "OWNER_QQ,FRIEND_QQ") or "")
        self.sleep_wake_uids = {
            item.strip() for item in raw_uids.replace("，", ",").split(",") if item.strip()
        }
        self.urgent_daily_limit = max(1, _as_int(config.get("urgent_daily_limit", 3), 3))
        self.handover_enabled = _as_bool(config.get("handover_enabled", True), True)
        self.handover_every = max(5, _as_int(config.get("handover_every", 25), 25))
        self.catchup_enabled = _as_bool(config.get("catchup_enabled", True), True)
        self.catchup_gap_seconds = max(30, _as_int(config.get("catchup_gap_seconds", 90), 90))
        self.catchup_owner_qq = str(config.get("owner_qq", "OWNER_QQ") or "").strip()
        self.catchup_platform_id = str(config.get("platform_id", "qq-snowluma") or "").strip()
        self.day_brief_enabled = _as_bool(config.get("day_brief_enabled", True), True)
        self.day_brief_max_chars = max(60, _as_int(config.get("day_brief_max_chars", 180), 180))
        # [dsh 本地补丁 2026-10-28] S5：世界目录（每天三件「自己的事」）
        self.world_enabled = _as_bool(config.get("world_enabled", True), True)
        self.world_hour = min(23, max(0, _as_int(config.get("world_hour", 21), 21)))
        self.world_brief_max_chars = max(20, _as_int(config.get("world_brief_max_chars", 60), 60))
        self.params = None
        self.handover = None
        self.catchup = None
        self.sleep_depth = None
        self.sleep_inbox = None
        self.trigger_stats = None
        self.world = None
        try:
            self.params = self._load_life_module("mengyu_life_params", "params.py")
            self.handover = self._load_life_module("mengyu_life_handover", "handover.py")
            self.catchup = self._load_life_module("mengyu_life_catchup", "catchup.py")
            self.sleep_depth = self._load_life_module("mengyu_life_sleep", "sleep_depth.py")
            self.sleep_inbox = self._load_life_module("mengyu_life_sleep_inbox", "sleep_inbox.py")
            self.trigger_stats = self._load_life_module("mengyu_life_trigger_stats", "trigger_stats.py")
            self.world = self._load_life_module("mengyu_life_world", "world.py")
            if self.params:
                self.params.load(self.root)
        except Exception as exc:
            logger.warning(f"{LOG} 参数/交接模块加载失败：{exc!r}")
        try:
            age = time.time() - self.store.heartbeat_path.stat().st_mtime
            self._boot_hb_age = max(0.0, age)
        except OSError:
            self._boot_hb_age = 0.0
        self._handover_injected: set[str] = set()
        self._day_brief_injected: set[str] = set()
        self._world_injected: set[str] = set()
        self._module = None
        self._module_error: str | None = None
        self._injected_once = False
        self._last_beat = 0.0
        self._last_drive = 0.0
        self._last_action: str | None = None
        self._started = False
        self.scheduler_runtime = None
        self._scheduler_started = False
        self._ensure_task()
        self._catchup_if_needed()

    # ------------------------------------------------------------ 生命周期

    def _load_life_module(self, name: str, filename: str):
        """从 data/life/ 加载纯函数模块；模块名固定，文件没改就复用缓存。

        [dsh 本地补丁 2026-10-28] 这些模块不在 data/plugins/ 下，AstrBot 热重载
        不会清它们的 sys.modules 缓存；改完 world.py 这类文件后不重启进程就还是
        旧代码。这里按文件 mtime 判断：文件变过就重新 exec，没变才用缓存
        （scheduler_runtime 这类持有进程内状态的模块，文件不动就不会被重装）。
        """
        path = self.root / filename
        if not path.exists():
            raise FileNotFoundError(f"缺少生活模块：{path}")
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = sys.modules.get(name)
        if cached is not None and getattr(cached, "__dsh_mtime__", None) == mtime:
            return cached
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载生活模块：{path}")
        module = importlib.util.module_from_spec(spec)
        module.__dsh_mtime__ = mtime
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def _load_scheduler_runtime(self):
        """从 data/life/ 加载调度器模块，模块名固定，便于 jobstore 恢复。"""
        return self._load_life_module("mengyu_scheduler_runtime", "scheduler_runtime.py")

    def _catchup_if_needed(self) -> None:
        """启动时若上次心跳已过期，补最近 30 条私聊进交接备忘；不回复。"""
        if not getattr(self, "catchup_enabled", False):
            return
        if not getattr(self, "catchup", None) or not getattr(self, "handover", None):
            return
        age = float(getattr(self, "_boot_hb_age", 0.0) or 0.0)
        if age < self.catchup_gap_seconds:
            return
        if age > 86400:
            logger.info(f"{LOG} 补漏跳过：上次心跳已 {age / 3600:.1f} 小时")
            return
        if not self.catchup_owner_qq or not self.catchup_platform_id:
            return
        try:
            messages = self.catchup.load_recent(
                DATA_ROOT / "data_v4.db",
                self.catchup_platform_id,
                self.catchup_owner_qq,
                since=datetime.now() - timedelta(hours=24),
                limit=30,
            )
            lines = self.catchup.summarize(messages)
            if not lines:
                logger.info(f"{LOG} 补漏：掉线 {age:.0f} 秒，最近没有可补的私聊")
                return
            session = f"{self.catchup_platform_id}:FriendMessage:{self.catchup_owner_qq}"
            self.catchup.apply(self.handover, self.root, session, lines)
            logger.info(
                f"{LOG} 补漏：掉线 {age:.0f} 秒，补 {len(messages)} 条，写入 {len(lines)} 句到交接"
            )
        except Exception as exc:
            logger.warning(f"{LOG} 补漏失败：{exc!r}")

    def _ensure_task(self) -> None:
        """启动或复用全局 scheduler；插件热重载时只更新回调，不重造任务。"""
        if self._scheduler_started:
            return
        try:
            runtime = self._load_scheduler_runtime()
        except Exception:
            logger.exception(f"{LOG} 调度器模块加载失败，生活状态将不再推进")
            return
        self.scheduler_runtime = runtime
        runtime.register("life.tick", self._scheduler_tick)
        runtime.register("scheduler.heartbeat_extra", self._heartbeat_extra)
        jobs = [
            {
                "id": "life.tick",
                "name": "生活状态推进",
                "callback": "life.tick",
                "trigger": IntervalTrigger(seconds=self.tick_seconds),
            },
            {
                "id": "sched.heartbeat",
                "name": "调度器心跳",
                "callback": "sched.heartbeat",
                "trigger": IntervalTrigger(seconds=self.store.heartbeat_seconds),
            },
        ]
        try:
            runtime.ensure_scheduler(
                self.root,
                jobs,
                heartbeat_seconds=self.store.heartbeat_seconds,
                version=VERSION,
            )
            self._scheduler_started = True
            self._started = True
            logger.info(
                f"{LOG} 已启动 v{VERSION}（S2 scheduler）：数据目录 {self.root}，"
                f"心跳 {self.store.heartbeat_seconds}s，检查间隔 {self.tick_seconds}s，"
                f"注入 {'开' if self.inject_enabled else '关'}"
            )
            self.store.ensure()
            self._boot_report()
        except Exception:
            logger.exception(f"{LOG} 调度器启动失败")

    def _scheduler_tick(self) -> None:
        """scheduler 的 life.tick 回调；异常交给 runtime 计数。"""
        self._tick_once()

    def _heartbeat_extra(self) -> dict:
        try:
            state = self.store.load_state(readonly=True)
        except Exception:
            state = {}
        return {
            "slot": state.get("last_advance"),
            "advance_count": state.get("advance_count", 0),
            "availability": state.get("availability"),
            "plugin": PLUGIN_NAME,
            "version": VERSION,
        }

    def _boot_report(self) -> None:
        result = self.store.advance(journal=self.journal_enabled)
        self._refresh_fact()
        self._refresh_availability()
        state = self.store.load_state()
        self._beat(state)
        if result["advanced"]:
            logger.info(f"{LOG} 启动补推进：{result['slot']}（第 {result['count']} 次）")
        else:
            logger.info(f"{LOG} 启动：{result['slot']} 已推进过，跳过（第 {result['count']} 次）")

    def _beat(self, state: dict | None = None) -> None:
        state = state or self.store.load_state()
        extra = {
            "slot": state.get("last_advance"),
            "advance_count": state.get("advance_count", 0),
            "plugin": PLUGIN_NAME,
            "version": VERSION,
        }
        runtime = getattr(self, "scheduler_runtime", None)
        if runtime is not None:
            try:
                runtime.beat(extra, heartbeat_seconds=self.store.heartbeat_seconds)
                self._last_beat = time.time()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG} 调度器心跳失败，回退到 store.beat：{exc!r}")
        self.store.beat(extra=extra)
        self._last_beat = time.time()

    def _tick_once(self) -> None:
        # 心跳由 scheduler 的 sched.heartbeat 任务单独负责，这里只推进状态。
        result = self.store.advance(journal=self.journal_enabled)
        if result["advanced"]:
            self._refresh_fact()
            logger.info(f"{LOG} 推进到 {result['slot']}（第 {result['count']} 次）")
        else:
            self._refresh_fact()
        self._update_drives()
        self._refresh_availability()
        self._maybe_diary()
        self._maybe_world()

    def _maybe_diary(self) -> None:
        """21 点后落盘当天日记；顺手补昨天漏掉的。失败只记日志，不影响心跳。"""
        now = datetime.now()
        try:
            if now.hour >= 21:
                self.store.write_diary(now.strftime("%Y-%m-%d"))
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            self.store.write_diary(yesterday)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG} 日记落盘失败：{exc!r}")

    # [dsh 本地补丁 2026-10-28] S5：世界目录（每天三件「自己的事」落盘 + 次日注入）
    def _maybe_world(self, now: datetime | None = None) -> None:
        """21 点后生成当天三件产物；顺手补昨天漏掉的。失败只记日志。

        Args:
            now: 判定用的当前时间，仅测试注入；None 表示取系统时间。
        """
        if not getattr(self, "world_enabled", False) or not getattr(self, "world", None):
            return
        now = now or datetime.now()
        try:
            if now.hour >= self.world_hour:
                self._generate_world(now.strftime("%Y-%m-%d"))
            self._generate_world((now - timedelta(days=1)).strftime("%Y-%m-%d"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG} 世界目录生成失败：{exc!r}")

    def _generate_world(self, day: str, force: bool = False) -> dict:
        """按当天 journal 事实生成三件产物；没有 journal 的那天不动。"""
        journal = self.store.journal_dir / f"{day}.jsonl"
        if not journal.exists():
            return {}
        digest = self.store.day_digest(day)
        facts = digest.get("facts") or []
        drives = self.store.drives() if day == datetime.now().strftime("%Y-%m-%d") else {}
        result = self.world.generate(self.root, day, facts=facts, drives=drives, force=force)
        if result.get("created"):
            logger.info(f"{LOG} 世界目录已生成：{day}（{len(result.get('items') or [])} 件）")
        return result

    def _world_brief(self, day: str) -> str:
        if not getattr(self, "world_enabled", False) or not getattr(self, "world", None):
            return ""
        try:
            return self.world.prompt_line(
                self.root, day, max_chars=self.world_brief_max_chars
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG} 读取世界目录失败：{exc!r}")
            return ""

    async def terminate(self) -> None:
        # 插件热重载不销毁 scheduler：任务留在 jobs.db，回调会在新实例里重新注册。
        logger.info(f"{LOG} 插件已卸载；S2 scheduler 与 jobs.db 保留")

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

    def _refresh_fact(self, persist: bool = True) -> dict | None:
        """刷新 fact.schedule；persist=False 时只返回快照，不落盘。"""
        data = self._state_now()
        if data is None:
            return None
        if persist:
            self.store.refresh_schedule(data, journal=self.journal_enabled)
        return data

    def _upcoming(self) -> str:
        module = self._schedule()
        if module is None or not hasattr(module, "upcoming"):
            return ""
        try:
            return str(module.upcoming() or "")
        except Exception:
            return ""

    # ------------------------------------------------------------ 驱力

    def _state_now(self) -> dict | None:
        module = self._schedule()
        if module is None:
            return None
        try:
            return module.state()
        except Exception as exc:
            logger.warning(f"{LOG} 计算作息状态失败：{exc!r}")
            return None

    def _in_quiet(self, now: datetime) -> bool:
        if not self.quiet_window:
            return False
        start, end = self.quiet_window
        if start == end:
            return False
        if start < end:
            return start <= now.hour < end
        return now.hour >= start or now.hour < end

    def _quiet_end(self, now: datetime) -> str | None:
        """静默时段的结束时刻；不在静默里就返回 None。"""
        if not self.quiet_window:
            return None
        start, end = self.quiet_window
        if start == end:
            return None
        if start < end:
            if start <= now.hour < end:
                end_dt = now.replace(hour=end, minute=0, second=0, microsecond=0)
                return end_dt.isoformat()
            return None
        if now.hour >= start:
            end_dt = (now + timedelta(days=1)).replace(
                hour=end, minute=0, second=0, microsecond=0
            )
            return end_dt.isoformat()
        if now.hour < end:
            end_dt = now.replace(hour=end, minute=0, second=0, microsecond=0)
            return end_dt.isoformat()
        return None

    def _refresh_availability(self) -> None:
        """统一按当前时间/静默边界刷新 availability。"""
        now = datetime.now()
        self.store.refresh_availability(
            dt=now,
            journal=self.journal_enabled,
            quiet=self._in_quiet(now),
            quiet_end=self._quiet_end(now),
        )

    def _peek_state(self) -> tuple[datetime, dict]:
        """只读地算出「此刻」的 state；给闸门和 life_state 用，不落盘。"""
        now = datetime.now()
        data = self._refresh_fact(persist=False)
        state = self.store.load_state(readonly=True)
        if data is not None:
            state = self.store.apply_schedule(state, data, now)
        derived = self.store.derive_availability(
            state,
            now,
            quiet=self._in_quiet(now),
            quiet_end=self._quiet_end(now),
        )
        state = self.store.apply_availability(state, derived, now)
        return now, state

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        """取原始/唤醒前缀剥离后的文本，优先有内容的那个。"""
        for value in (
            getattr(event, "message_str", ""),
            getattr(getattr(event, "message_obj", None), "message_str", ""),
        ):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _urgent_text(self, event: AstrMessageEvent) -> str | None:
        """识别 /急事；返回参数文本，不是急事返回 None。"""
        for value in (
            getattr(event, "message_str", ""),
            getattr(getattr(event, "message_obj", None), "message_str", ""),
        ):
            text = str(value or "").strip()
            if not text:
                continue
            for prefix in ("/急事", "急事"):
                if text == prefix:
                    return ""
                if text.startswith(prefix + " "):
                    return text[len(prefix) :].strip()
        return None

    @staticmethod
    def _is_command(event: AstrMessageEvent) -> bool:
        """带 / 的指令不归睡眠闸门管，避免把 /life、/reset 一起吞掉。"""
        for value in (
            getattr(event, "message_str", ""),
            getattr(getattr(event, "message_obj", None), "message_str", ""),
        ):
            if str(value or "").strip().startswith("/"):
                return True
        return False

    def _drive_targets(self, now: datetime, sched: dict | None) -> dict:
        """把「现在几点、她在干什么、多久没说话」折算成四驱力的目标值。

        social 随闲置分钟线性上升（45 分钟 ≈ 0.75，跨过 0.7 阈值），睡觉/上课/静默
        时段分别压到 0.2 / 0.72 / 0.25；rest 由作息段直接给；不引入随机量。

        上课时段的上限是 0.72 而不是 0.55：0.55 会让目标永远够不到 0.7 阈值，
        一下午都在上课就等于一下午不可能「想说话」。0.72 仍压得比平时低，
        但因为低通每拍只走 30%，从 0.55 爬到 0.7 要约 6 拍（≈6 分钟），
        实际效果是「课上久了才会冒出想找他的念头」，不是一进教室就开口。
        """
        idle = self.store.idle_minutes(now)
        doing = str((sched or {}).get("doing") or "")
        asleep = "睡" in doing
        in_class = ("课" in doing) and not asleep
        quiet = self._in_quiet(now)

        social = min(0.95, 0.15 + idle / 75.0)
        if asleep:
            social = min(social, 0.20)
        elif in_class:
            social = min(social, 0.72)
        if quiet:
            social = min(social, 0.25)

        if asleep:
            rest = 0.95
        elif now.hour >= 21 or now.hour < 8:
            rest = 0.55
        else:
            rest = 0.30

        # [dsh 本地补丁 2026-10-28] S1：recognition 随「多久没人回应」上升；
        # achievement 随「今天还没留下东西」上升。两个都只看事实，不参与 decide()。
        recognition = min(0.90, 0.20 + idle / 120.0)
        if asleep:
            recognition = min(recognition, 0.20)
        elif quiet:
            recognition = min(recognition, 0.25)
        elif in_class:
            recognition = min(recognition, 0.60)

        today_done = self._today_world_done(now)
        achievement = 0.30 if today_done else 0.62
        if asleep:
            achievement = min(achievement, 0.15)
        elif quiet:
            achievement = min(achievement, 0.30)
        elif in_class:
            achievement = min(achievement, 0.45)

        return {
            "social": round(social, 3),
            "curiosity": 0.30 if (asleep or quiet) else 0.55,
            "play": 0.30 if (asleep or in_class) else 0.50,
            "rest": round(rest, 3),
            "recognition": round(recognition, 3),
            "achievement": round(achievement, 3),
            "inputs": {
                "idle_minutes": idle,
                "doing": doing,
                "asleep": asleep,
                "in_class": in_class,
                "quiet": quiet,
                "today_done": today_done,
            },
        }

    def _today_world_done(self, now: datetime) -> bool:
        """今天是否已经有「自己的事」落盘（S5 的世界目录）。失败一律当没做成。"""
        world = getattr(self, "world", None)
        if world is None:
            return False
        try:
            return world.meta_path(self.root, now.strftime("%Y-%m-%d")).exists()
        except Exception:
            return False

    def _update_drives(
        self, force: bool = False, sched: dict | None = None, decide_now: bool = True
    ) -> dict | None:
        """按 drive_update_seconds 落一次驱力与判断；失败只记日志，不影响对话。"""
        now_mono = time.time()
        if not force and now_mono - self._last_drive < self.drive_update_seconds:
            return None
        self._last_drive = now_mono
        try:
            current = datetime.now()
            snapshot = sched if sched is not None else self._state_now()
            targets = self._drive_targets(current, snapshot)
            self.store.update_drives(targets, journal=self.journal_enabled)
            if not decide_now:
                self._refresh_availability()
                return None
            decision = self.store.decide(
                social_threshold=self.social_threshold,
                idle_trigger_minutes=self.idle_trigger_minutes,
                quiet=bool(targets["inputs"].get("quiet")),
                asleep=bool(targets["inputs"].get("asleep")),
                dt=current,
            )
            if decision.get("action") != self._last_action:
                self._last_action = decision.get("action")
                logger.info(
                    f"{LOG} 判断 → {decision.get('action')}（{decision.get('reason')}，"
                    f"{decision.get('drive')}={decision.get('value')}，"
                    f"闲置 {decision.get('idle_minutes')} 分钟）"
                )
            self._refresh_availability()
            return decision
        except Exception as exc:
            logger.warning(f"{LOG} 更新驱力失败：{exc!r}")
            return None

    # ------------------------------------------------------------ 睡眠闸门

    def _sleep_data(self, state: dict, now: datetime | None = None) -> dict:
        """读浅睡/深睡窗口；模块缺失时按深睡处理（普通私聊仍拦，/急事 仍可用）。"""
        now = now or datetime.now()
        module = getattr(self, "sleep_depth", None)
        if module is None:
            return {"phase": "deep", "wake_count": 0, "window_seconds": 0}
        try:
            return module.load(
                self.root,
                state,
                now,
                self.sleep_light_seconds,
                self.sleep_wake_seconds,
                self.sleep_wake_min_seconds,
            )
        except Exception as exc:
            logger.warning(f"{LOG} 读取睡眠深度失败，按深睡处理：{exc!r}")
            return {"phase": "deep", "wake_count": 0, "window_seconds": 0}

    def _wake_sleep(self, state: dict, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        module = getattr(self, "sleep_depth", None)
        if module is None:
            return {"phase": "light", "wake_count": 0, "window_seconds": 0}
        try:
            return module.wake(
                self.root,
                state,
                now,
                self.sleep_light_seconds,
                self.sleep_wake_seconds,
                self.sleep_wake_min_seconds,
            )
        except Exception as exc:
            logger.warning(f"{LOG} 唤醒窗口写入失败：{exc!r}")
            return {"phase": "light", "wake_count": 0, "window_seconds": 0}

    def _touch_sleep(self, state: dict, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        module = getattr(self, "sleep_depth", None)
        if module is None:
            return {"phase": "light", "wake_count": 0, "window_seconds": 0}
        try:
            return module.touch(
                self.root,
                state,
                now,
                self.sleep_light_seconds,
                self.sleep_wake_seconds,
                self.sleep_wake_min_seconds,
            )
        except Exception as exc:
            logger.warning(f"{LOG} 浅睡窗口顺延失败：{exc!r}")
            return {"phase": "light", "wake_count": 0, "window_seconds": 0}

    def _sleep_uid_allowed(self, user_id: str) -> bool:
        allowed = getattr(self, "sleep_wake_uids", set())
        return not allowed or str(user_id) in allowed

    def _remember_sleep_message(
        self, event: AstrMessageEvent, user_id: str, text: str, phase: str
    ) -> None:
        """深睡拦截时把正文暂存，醒来后的第一轮请求再交给她。"""
        module = getattr(self, "sleep_inbox", None)
        if module is None:
            return
        try:
            session = str(getattr(event, "unified_msg_origin", "") or "")
            module.add(
                self.root,
                session=session,
                user_id=user_id,
                text=text,
                phase=phase,
            )
        except Exception as exc:
            logger.warning(f"{LOG} 暂存睡眠消息失败：{exc!r}")

    def _block_sleep(self, event: AstrMessageEvent, user_id: str, text: str, phase: str) -> None:
        self._remember_sleep_message(event, user_id, text, phase)
        try:
            if self.journal_enabled:
                self.store.journal_event(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "sleep_block",
                        "user_id": user_id,
                        "chars": len(text),
                        "phase": phase,
                    }
                )
        except Exception as exc:
            logger.warning(f"{LOG} 记录睡眠拦截失败：{exc!r}")
        logger.info(f"{LOG} 睡眠闸门拦截 {user_id}（{phase}，{len(text)} 字），已暂存待醒来")
        event.stop_event()

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE, priority=100000
    )
    async def sleep_gate(self, event: AstrMessageEvent):
        """睡着时：浅睡普通私聊可直接叫醒；深睡只放 /急事。"""
        if not self.sleep_gate_enabled:
            return
        urgent = self._urgent_text(event)
        if urgent is None and self._is_command(event):
            return
        try:
            now, state = self._peek_state()
        except Exception as exc:
            logger.warning(f"{LOG} 睡眠闸门读状态失败，放行：{exc!r}")
            return
        now = now or datetime.now()
        if state.get("availability") != "sleep":
            return
        sleep = self._sleep_data(state, now)
        phase = str(sleep.get("phase") or "deep")
        user_id = str(event.get_sender_id() or "")
        text = self._message_text(event)
        if urgent is not None:
            before = phase
            async for result in self._handle_urgent(event, urgent):
                yield result
            if event.get_extra("life_urgent"):
                self._wake_sleep(state, now)
                event.set_extra("life_wake_light", True)
                try:
                    if self.journal_enabled:
                        self.store.journal_event(
                            {
                                "ts": now.isoformat(timespec="seconds"),
                                "kind": "sleep_wake",
                                "source": "urgent",
                                "from": before,
                                "user_id": user_id,
                            }
                        )
                except Exception as exc:
                    logger.warning(f"{LOG} 记录急事唤醒失败：{exc!r}")
                logger.info(f"{LOG} 急事唤醒：{user_id}（{before} → 浅睡）")
            return
        if phase == "light":
            if not self._sleep_uid_allowed(user_id):
                self._block_sleep(event, user_id, text, phase)
                return
            woke = int(sleep.get("wake_count") or 0) <= 0
            if woke:
                self._wake_sleep(state, now)
            else:
                self._touch_sleep(state, now)
            event.set_extra("life_wake_light", True)
            try:
                if self.journal_enabled:
                    self.store.journal_event(
                        {
                            "ts": now.isoformat(timespec="seconds"),
                            "kind": "sleep_wake" if woke else "sleep_touch",
                            "source": "message",
                            "from": "light",
                            "user_id": user_id,
                            "chars": len(text),
                        }
                    )
            except Exception as exc:
                logger.warning(f"{LOG} 记录浅睡唤醒失败：{exc!r}")
            logger.info(
                f"{LOG} {'浅睡唤醒' if woke else '浅睡顺延'}：{user_id}（{len(text)} 字）"
            )
            return
        self._block_sleep(event, user_id, text, phase)

    async def _handle_urgent(self, event: AstrMessageEvent, text: str):
        """处理 /急事：鉴权、配额、审计，放行给正常对话链。"""
        if not event.is_admin():
            yield event.plain_result("只有所有者可以用 /急事 叫醒她。")
            event.stop_event()
            return
        content = (text or "").strip()
        if not content:
            yield event.plain_result(
                "用法：/急事 一句话（例如 /急事 我发烧了，家里药在哪）"
            )
            event.stop_event()
            return
        try:
            used = self.store.urgent_count()
        except Exception:
            used = 0
        limit = self.urgent_daily_limit
        if used >= limit:
            yield event.plain_result(
                f"今天的 /急事 名额已经用完了（{used}/{limit}）。"
                "如果真的很急，请直接打电话。"
            )
            event.stop_event()
            return
        try:
            _, state = self._peek_state()
        except Exception:
            state = {}
        try:
            self.store.journal_event(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "urgent",
                    "user_id": str(event.get_sender_id() or ""),
                    "text": content[:200],
                    "availability": state.get("availability"),
                    "reason": state.get("availability_reason"),
                }
            )
        except Exception as exc:
            logger.warning(f"{LOG} 记录急事失败：{exc!r}")
        event.set_extra("life_urgent", True)
        logger.info(
            f"{LOG} 急事放行：{event.get_sender_id()} 第 {used + 1}/{limit} 次，"
            f"{len(content)} 字"
        )

    # ------------------------------------------------------------ 注入

    @filter.on_llm_request()
    async def inject_state(self, event: AstrMessageEvent, req):
        """把一行现状拼到系统提示末尾（唯一注入点，ai_delay 的旧注入已关）。

        顺序有讲究：先按「这条消息到达之前」的闲置时长判断一次（此时还可能是
        opener，会带一句「隔了一阵没人说话」），生成状态行，然后才记账并更新驱力。
        """
        self._ensure_task()
        line = ""
        if self.inject_enabled:
            try:
                current = datetime.now()
                sched = self._state_now()
                targets = self._drive_targets(current, sched)
                self.store.decide(
                    social_threshold=self.social_threshold,
                    idle_trigger_minutes=self.idle_trigger_minutes,
                    quiet=bool(targets["inputs"].get("quiet")),
                    asleep=bool(targets["inputs"].get("asleep")),
                    dt=current,
                )
                line = self.store.render_line()
                if line and event.get_extra("life_urgent"):
                    line += (
                        "\n（这是急事：他用 /急事 把你叫醒了，先回应他，"
                        "不要拿「在睡」推脱。）"
                    )
                if line and event.get_extra("life_wake_light"):
                    line += (
                        "\n（他刚把你从浅睡里叫醒；你还在浅睡，可以回应，"
                        "不用装作完全清醒。回完继续睡。）"
                    )
            except Exception as exc:
                logger.warning(f"{LOG} 生成状态行失败：{exc!r}")
        try:
            self.store.touch_talk(journal=self.journal_enabled)
        except Exception as exc:
            logger.warning(f"{LOG} 记录对话时间失败：{exc!r}")
        self._update_drives(force=True, decide_now=False)

        session = str(getattr(event, "unified_msg_origin", "") or "")
        handover_line = ""
        injected = getattr(self, "_handover_injected", set())
        if (
            getattr(self, "handover_enabled", False)
            and getattr(self, "handover", None)
            and session
            and session not in injected
        ):
            try:
                handover_line = self.handover.render_for_prompt(self.root, session)
                if handover_line:
                    injected.add(session)
            except Exception as exc:
                logger.warning(f"{LOG} 读取会话交接失败：{exc!r}")

        day_brief_line = ""
        day_seen = getattr(self, "_day_brief_injected", set())
        if (
            getattr(self, "day_brief_enabled", False)
            and session
        ):
            today_text = datetime.now().strftime("%Y-%m-%d")
            brief_key = f"{session}:{today_text}"
            if brief_key not in day_seen:
                try:
                    yesterday_text = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                    brief = self.store.render_day_brief(
                        yesterday_text, max_chars=self.day_brief_max_chars
                    )
                    if brief:
                        day_brief_line = (
                            f"（昨天你：{brief}。今天如果自然，可以提一句；不必硬提。）"
                        )
                        day_seen.add(brief_key)
                except Exception as exc:
                    logger.warning(f"{LOG} 读取昨日摘要失败：{exc!r}")

        world_line = ""
        world_seen = getattr(self, "_world_injected", set())
        if getattr(self, "world_enabled", False) and session:
            today_text = datetime.now().strftime("%Y-%m-%d")
            world_key = f"{session}:world:{today_text}"
            if world_key not in world_seen:
                world_line = self._world_brief(
                    (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                )
                if world_line:
                    world_seen.add(world_key)

        pending_line = ""
        pending_ids: list[str] = []
        if getattr(self, "sleep_inbox", None) and session:
            try:
                _, pending_state = self._peek_state()
                awake = pending_state.get("availability") != "sleep"
                if event.get_extra("life_urgent") or event.get_extra("life_wake_light"):
                    awake = True
                if awake:
                    pending_line, pending_ids = self.sleep_inbox.render(
                        self.root,
                        session=session,
                        user_id=str(event.get_sender_id() or ""),
                    )
                    if pending_ids:
                        event.set_extra("life_pending_ids", pending_ids)
            except Exception as exc:
                logger.warning(f"{LOG} 读取睡眠暂存失败：{exc!r}")

        base = getattr(req, "system_prompt", "") or ""
        if "（你此刻：" in base:
            line = ""
        if "（接上次对话的备忘：" in base:
            handover_line = ""
        if "（昨天你：" in base:
            day_brief_line = ""
        if "（昨天你自己做的：" in base:
            world_line = ""
        if "（你睡着时收到的消息：" in base:
            pending_line = ""
        extra = "\n\n".join(
            part
            for part in (line, handover_line, day_brief_line, world_line, pending_line)
            if part
        )
        if not extra:
            return
        try:
            req.system_prompt = f"{base}\n\n{extra}".strip()
            if not self._injected_once:
                self._injected_once = True
                logger.info(f"{LOG} 首次注入状态：{extra}")
        except Exception as exc:
            logger.warning(f"{LOG} 注入状态失败：{exc!r}")

    @filter.on_llm_response()
    async def capture_handover(self, event: AstrMessageEvent, resp):
        """每轮成功回复后累计交接窗口；满 N 轮生成三句摘要。"""
        text = str(getattr(resp, "completion_text", "") or "").strip()
        pending_ids = event.get_extra("life_pending_ids") or []
        if text and pending_ids and getattr(self, "sleep_inbox", None):
            try:
                removed = self.sleep_inbox.clear(self.root, list(pending_ids))
                if removed:
                    logger.info(f"{LOG} 睡眠暂存已交付：{removed} 条")
            except Exception as exc:
                logger.warning(f"{LOG} 清理睡眠暂存失败：{exc!r}")
        if not self.handover_enabled or not self.handover:
            return
        user = self._message_text(event)
        session = str(getattr(event, "unified_msg_origin", "") or "")
        if len(user) < 2 or len(text) < 2 or not session:
            return
        try:
            summary = self.handover.record(
                self.root,
                session,
                user,
                text,
                every=self.handover_every,
            )
            if summary:
                logger.info(f"{LOG} 会话交接已更新：{session}（{len(summary)} 句）")
        except Exception as exc:
            logger.warning(f"{LOG} 记录会话交接失败：{exc!r}")

    # ------------------------------------------------------------ 工具

    @filter.llm_tool(name="life_state")
    async def life_state(self, event: AstrMessageEvent):
        """读取当前生活状态（只读）。返回两栏：系统给的（时间、此刻在哪在做什么、
        availability 能否聊天、推进次数、心跳）和她自己填的（在做什么、在哪、心情、备注）。
        未设置的字段就是「未设置」，不要替她编。
        当你要回答「你现在在干嘛 / 在哪儿 / 心情怎么样 / 现在方便吗」，或需要确认时间与自身状态时调用。
        这个工具不能改状态，只能读。

        Returns:
            一段自然语言文字。
        """
        _, state = self._peek_state()
        parts = [
            self.store.render(state=state),
            self.store.render_today(limit=5),
        ]
        if state.get("availability") == "sleep":
            try:
                sleep = self._sleep_data(state)
                phase = "浅睡" if sleep.get("phase") == "light" else "深睡"
                until = str(sleep.get("light_until") or "")
                parts.append(
                    f"睡眠深度：{phase}（这一觉已醒 {int(sleep.get('wake_count') or 0)} 次"
                    + (f"，浅睡窗口到 {until}" if sleep.get("phase") == "light" else "")
                    + "）"
                )
            except Exception as exc:
                logger.warning(f"{LOG} 读取睡眠深度失败：{exc!r}")
        upcoming = self._upcoming()
        if upcoming:
            parts.append(f"接下来：{upcoming}")
        return "\n".join(parts)

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
        self._refresh_availability()
        if text:
            return f"已记下：{SELF_LABELS[name]}＝{text}"
        return f"已清空：{SELF_LABELS[name]}"

    @filter.llm_tool(name="life_decline")
    async def life_decline(
        self, event: AstrMessageEvent, hours: float = 2, reason: str = ""
    ):
        """当她自己这会儿不想说话、想安静待着时调用：告诉系统「到某个时间之前先别找我」。
        只在她真的不想聊时用，不要拿它当撒娇或者玩笑。到点自动失效，也可以随时手动结束。

        Args:
            hours(number): 安静多久，0.5 到 24 小时，默认 2。
            reason(string): 一句理由，可不填，不超过 40 字。
        Returns:
            一段确认文字。
        """
        self._ensure_task()
        try:
            span = float(hours or 0)
        except (TypeError, ValueError):
            span = 2.0
        span = min(DECLINE_MAX_HOURS, max(DECLINE_MIN_HOURS, span))
        decline = self.store.set_decline(span, reason or "")
        self._update_drives(force=True)
        until = str(decline.get("until") or "")[:16].replace("T", " ")
        return f"记下了：{until} 之前不主动找你，也不会提前挪时间。"

    # ------------------------------------------------------------ 命令

    @filter.command("life")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def life_cmd(self, event: AstrMessageEvent):
        """查看生活状态文件（管理员）：/life 状态，/life files 文件位置，/life stats 触发占比，/life world 世界目录"""
        if not event.is_admin():
            yield event.plain_result("权限不足：仅 AstrBot 管理员可以查看。")
            return
        raw = str(event.message_str or "").strip()
        arg = raw.lower()
        if arg in {"params", "参数"} or arg.startswith("params ") or arg.startswith("参数 "):
            if not self.params:
                yield event.plain_result("参数模块未加载。").use_t2i(False)
                return
            parts = raw.split()
            try:
                if len(parts) >= 3 and parts[1].lower() in {"rollback", "回滚"}:
                    data = self.params.rollback(self.root, int(parts[2]))
                    yield event.plain_result(
                        f"已回滚到 v{parts[2]}，当前 v{data['version']}。\n" + self.params.render(self.root)
                    ).use_t2i(False)
                    return
                if len(parts) >= 4 and parts[1].lower() in {"set", "设置"}:
                    group, _, key = parts[2].partition(".")
                    if not group or not key:
                        yield event.plain_result("格式：/life params set behavior.urgent_daily_limit 3").use_t2i(False)
                        return
                    value_text = " ".join(parts[3:])
                    try:
                        value = json.loads(value_text)
                    except Exception:
                        value = value_text
                    data = self.params.set_value(self.root, group, key, value, note="manual via /life params")
                    yield event.plain_result(
                        f"已写入 {group}.{key}，当前 v{data['version']}。\n" + self.params.render(self.root)
                    ).use_t2i(False)
                    return
            except Exception as exc:
                yield event.plain_result(f"参数操作失败：{exc}").use_t2i(False)
                return
            yield event.plain_result(self.params.render(self.root)).use_t2i(False)
            return
        if arg in {"handover", "交接"} or arg.startswith("handover "):
            if not self.handover:
                yield event.plain_result("交接模块未加载。").use_t2i(False)
                return
            if arg in {"handover clear", "交接 清空"}:
                self.handover.clear(self.root)
                self._handover_injected.clear()
                yield event.plain_result("已清空所有会话交接。").use_t2i(False)
                return
            yield event.plain_result(self.handover.render(self.root)).use_t2i(False)
            return
        if arg in {"decline", "安静", "别找我"}:
            decline = self.store.set_decline(2.0, "手动")
            self._update_drives(force=True)
            yield event.plain_result(
                f"已进入安静状态，到 {decline.get('until')} 为止（/life clear 取消）。"
            ).use_t2i(False)
            return
        if arg in {"clear", "reset", "取消", "恢复"}:
            self.store.clear_decline()
            self._update_drives(force=True)
            yield event.plain_result("已取消安静状态。").use_t2i(False)
            return
        if arg in {"diary", "日记"} or arg.startswith("diary ") or arg.startswith("日记 "):
            parts = arg.split()
            day = parts[1] if len(parts) > 1 else None
            if day:
                try:
                    datetime.strptime(day, "%Y-%m-%d")
                except ValueError:
                    yield event.plain_result(
                        "日期要写成 YYYY-MM-DD，例如 /life diary 2026-10-27。"
                    ).use_t2i(False)
                    return
            yield event.plain_result(self.store.render_diary(day)).use_t2i(False)
            return
        if arg in {"today", "今天", "一天"}:
            lines = [self.store.render_today()]
            upcoming = self._upcoming()
            if upcoming:
                lines.append(f"接下来：{upcoming}")
            yield event.plain_result("\n".join(lines)).use_t2i(False)
            return
        if arg in {"stats", "统计", "触发统计"}:
            if not self.trigger_stats:
                yield event.plain_result("触发统计模块未加载。").use_t2i(False)
                return
            stats = self.trigger_stats.summary(self.root, days=7)
            ratio = stats["ratio"] * 100
            yield event.plain_result(
                "最近 7 天主动消息触发来源：\n"
                f"状态触发 state：{stats['state']} 次\n"
                f"定时器 timer：{stats['timer']} 次\n"
                f"手动 manual：{stats['manual']} 次（不计入占比）\n"
                f"状态触发占比：{ratio:.1f}%（目标 ≥33.3%）\n"
                f"统计文件：{self.root / 'proactive_stats.json'}"
            ).use_t2i(False)
            return
        if arg in {"files", "path", "文件"}:
            state = self.store.load_state()
            lines = [
                f"数据目录：{self.root}",
                f"state.json：{'存在' if self.store.state_path.exists() else '缺失'}",
                f"heartbeat.json：{'存在' if self.store.heartbeat_path.exists() else '缺失'}",
                f"journal：{self.store.journal_dir}",
                f"params.json：{'存在' if (self.root / 'params.json').exists() else '缺失'}",
                f"handover.json：{'存在' if (self.root / 'handover.json').exists() else '缺失'}",
                f"proactive_stats.json：{'存在' if (self.root / 'proactive_stats.json').exists() else '缺失'}",
                f"sleep_inbox.json：{'存在' if (self.root / 'sleep_inbox.json').exists() else '缺失'}",
                f"world：{'已启用' if getattr(self, 'world_enabled', False) else '已关'}"
                f"（{len(self.world.summary(self.root).get('days', [])) if getattr(self, 'world', None) else 0} 天有产物）",
                f"boot_id：{state.get('boot_id')}",
                f"推进次数：{state.get('advance_count')}，最近 {state.get('last_advance')}",
                f"availability：{state.get('availability')}（{state.get('availability_reason')}）",
                f"今日急事：{self.store.urgent_count()}/{self.urgent_daily_limit}",
                f"作息模块：{self.schedule_file}（{'已加载' if self._module else '未加载'}）",
            ]
            yield event.plain_result("\n".join(lines)).use_t2i(False)
            return
        if arg in {"world", "世界"} or arg.startswith("world ") or arg.startswith("世界 "):
            if not getattr(self, "world", None):
                yield event.plain_result("世界目录模块未加载。").use_t2i(False)
                return
            parts = raw.split()
            force = len(parts) > 1 and parts[1].lower() in {"make", "做", "生成"}
            day_arg = parts[2] if force and len(parts) > 2 else (parts[1] if len(parts) > 1 and not force else None)
            day = day_arg or datetime.now().strftime("%Y-%m-%d")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                yield event.plain_result(
                    "日期要写成 YYYY-MM-DD，例如 /life world 2026-10-27。"
                ).use_t2i(False)
                return
            try:
                result = self._generate_world(day, force=force)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"生成失败：{exc}").use_t2i(False)
                return
            if not result:
                yield event.plain_result(f"{day} 没有 journal，先不生成。").use_t2i(False)
                return
            head = "已重新生成" if force and result.get("created") else "已生成"
            text = self.world.render_index(self.root, day)
            if result.get("created"):
                text = f"{head} {day} 的 {len(result.get('items') or [])} 件。\n" + text
            yield event.plain_result(text).use_t2i(False)
            return
        if arg in {"why", "reason", "原因", "判断"}:
            state = self.store.load_state()
            now = datetime.now()
            sched = self._state_now()
            targets = self._drive_targets(now, sched)
            drives = self.store.drives()
            decision = state.get("decision") or {}
            rows = [
                f"此刻：{sched.get('weekday')} {sched.get('local')}，{sched.get('place')}，{sched.get('doing')}",
                f"availability：{state.get('availability')}（{state.get('availability_reason')}）",
                f"闲置：{self.store.idle_minutes():.1f} 分钟（阈值 {self.idle_trigger_minutes:g}）",
            ]
            for name in DRIVES:
                rows.append(
                    f"{name}: 当前 {drives.get(name, 0):.3f} / 目标 {targets.get(name, 0):.3f}"
                )
            rows.append(
                f"输入：上课 {targets['inputs'].get('in_class')}、睡觉 {targets['inputs'].get('asleep')}、静默 {targets['inputs'].get('quiet')}、今天做成 {targets['inputs'].get('today_done')}"
            )
            rows.append(
                f"判断：{decision.get('action')}（理由 {decision.get('reason')}，"
                f"{decision.get('drive')}={decision.get('value')}，{decision.get('at')}）"
            )
            declining, until, why = self.store.decline_active()
            rows.append(
                f"安静：{'到 ' + str(until) if declining else '无'}"
                f"{'（' + str(why) + '）' if declining and why else ''}"
            )
            rows.append(f"阈值：social ≥ {self.social_threshold:g} 且闲置 ≥ {self.idle_trigger_minutes:g} 分钟 → opener")
            yield event.plain_result("\n".join(rows)).use_t2i(False)
            return
        self._refresh_fact()
        yield event.plain_result(self.store.render()).use_t2i(False)
