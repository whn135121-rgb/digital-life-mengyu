"""生活状态的持久化层（只依赖标准库，可脱离 AstrBot 单测）。

数据目录：`qqbot/data/life/`
  state.json                事实快照 + 她的「自选」状态 + 推进记账
  heartbeat.json            心跳，每 60 秒覆写一次
  journal/YYYY-MM-DD.jsonl  当天流水（推进、事实刷新、自改状态各一行）

三条约束（来自《大肥鱼系统：技术术语与概念.md》v3.2 §0.3 S1）：
1. facts 由系统给，不可伪造；self 由她自己填，**未设置就保持 null**，不冒认。
2. 推进按小时幂等：同一 slot 只推进一次，重启后不会重放。
3. 写盘先写 `.tmp` 再 `os.replace`，不留半截文件；读坏文件先改名为 `.bad` 再重建。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = 3
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 四驱力（《大肥鱼系统：技术术语与概念.md》§4.1 的六维里先落四个：
# recognition / achievement 需要关系与成就数据，S4/S5 再说）
DRIVES = ("social", "curiosity", "play", "rest")
DRIVE_LABELS = {
    "social": "想说话",
    "curiosity": "好奇",
    "play": "想玩",
    "rest": "累",
}
DRIVE_DEFAULT = 0.4
DRIVE_LERP_KEEP = 0.7  # v_t = 0.7·v_{t-1} + 0.3·v_target
DRIVE_LERP_NEW = 0.3

# 她自己说「先别找我」的窗口：太短没意义，太长等于把主动权收走
DECLINE_MIN_HOURS = 0.5
DECLINE_MAX_HOURS = 24.0

SELF_FIELDS = ("activity", "location", "mood", "note")
SELF_LABELS = {
    "activity": "在做什么",
    "location": "在哪",
    "mood": "心情",
    "note": "备注",
}

# 居民版四状态：先只做「只读事实」，谁允许/禁止由后续闸门决定。
AVAILABILITY = ("open", "focus", "rest", "sleep")
AVAILABILITY_LABELS = {
    "open": "有空",
    "focus": "在做事",
    "rest": "想休息",
    "sleep": "睡了",
}
AVAILABILITY_REASON_LABELS = {
    "self_declared": "她说了先别找",
    "asleep": "作息里在睡",
    "quiet_hours": "静默时段",
    "schedule_busy": "作息里在忙",
    "resting": "作息里在休息",
    "tired": "驱力累了",
    "open": "有空",
    "unknown": "作息未知",
    "init": "初始化",
}

# 事实快照里只放「系统算得出的东西」，她自己的话一律进 self
FACT_FIELDS = ("slot", "slot_key", "place", "doing", "mood", "detail", "local", "weekday")
FACT_MAX = 120


def now_local() -> datetime:
    return datetime.now()


def slot_of(dt: datetime) -> str:
    """小时槽，形如 2026-10-27T13。"""
    return dt.strftime("%Y-%m-%dT%H")


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def human(dt: datetime) -> str:
    return f"{WEEKDAYS[dt.weekday()]} {dt.strftime('%H:%M')}"


def _empty_self() -> dict:
    data = {field: None for field in SELF_FIELDS}
    data["updated_at"] = None
    data["source"] = None
    return data


def _empty_fact() -> dict:
    return {"schedule": None, "updated_at": None}


def _empty_drives() -> dict:
    data = {name: DRIVE_DEFAULT for name in DRIVES}
    data["updated_at"] = None
    data["inputs"] = {}
    return data


def _empty_decision() -> dict:
    return {"action": "none", "reason": "init", "drive": None, "value": None, "at": None}


def _empty_decline() -> dict:
    """她自己说的「先别找我」：只在她主动声明时写入，到期自动失效。"""
    return {"until": None, "reason": None, "at": None}


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, float(value))), 4)


def _new_state(dt: datetime, boot_id: str) -> dict:
    return {
        "schema": SCHEMA,
        "boot_id": boot_id,
        "created_at": iso(dt),
        "last_advance": None,
        "last_advance_at": None,
        "advance_count": 0,
        "journal_today": 0,
        "last_talk_at": None,
        "talk_count": 0,
        "fact": _empty_fact(),
        "self": _empty_self(),
        "drives": _empty_drives(),
        "decision": _empty_decision(),
        "decline": _empty_decline(),
        # P0 居民版：availability 是系统推导的事实，activity 是当前活动，updated_at 由 save_state 维护。
        "availability": "open",
        "availability_reason": "init",
        "availability_since": None,
        "availability_updated_at": None,
        "next_transition": None,
        "activity": None,
        "updated_at": iso(dt),
    }


class LifeStore:
    def __init__(self, root: Path, heartbeat_seconds: int = 60) -> None:
        self.root = Path(root)
        self.heartbeat_seconds = max(10, int(heartbeat_seconds))
        self.state_path = self.root / "state.json"
        self.heartbeat_path = self.root / "heartbeat.json"
        self.journal_dir = self.root / "journal"
        self._boot_id = f"{int(time.time())}-{os.getpid()}"

    # ---------------------------------------------------------------- 基础

    @property
    def boot_id(self) -> str:
        return self._boot_id

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def _write_json(self, path: Path, payload: dict) -> None:
        self.ensure()
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- state

    def load_state(self, readonly: bool = False) -> dict:
        """读取并规范化 state；readonly=True 时绝不在磁盘上创建/改名/重建。"""
        if not readonly:
            self.ensure()
        if not self.state_path.exists():
            state = _new_state(now_local(), self._boot_id)
            if not readonly:
                self._write_json(self.state_path, state)
            return state
        try:
            state = self._read_json(self.state_path)
        except (OSError, ValueError):
            state = _new_state(now_local(), self._boot_id)
            if readonly:
                return state
            bad = self.state_path.with_suffix(".json.bad")
            try:
                os.replace(self.state_path, bad)
            except OSError:
                pass
            self._write_json(self.state_path, state)
            return state
        return self._normalize(state)

    def _normalize(self, state: dict) -> dict:
        base = _new_state(now_local(), self._boot_id)
        base.update({k: v for k, v in state.items() if k in base})
        base["schema"] = SCHEMA  # 旧版本文件读上来后顺手升到当前 schema

        fact = dict(_empty_fact())
        raw_fact = state.get("fact")
        if isinstance(raw_fact, dict):
            if isinstance(raw_fact.get("schedule"), dict):
                fact["schedule"] = {
                    k: raw_fact["schedule"].get(k) for k in FACT_FIELDS
                }
            fact["updated_at"] = raw_fact.get("updated_at")
        base["fact"] = fact

        self_block = dict(_empty_self())
        if isinstance(state.get("self"), dict):
            self_block.update(
                {k: v for k, v in state["self"].items() if k in self_block}
            )
        base["self"] = self_block

        drives = _empty_drives()
        if isinstance(state.get("drives"), dict):
            for name in DRIVES:
                if state["drives"].get(name) is not None:
                    try:
                        drives[name] = _clamp(state["drives"][name])
                    except (TypeError, ValueError):
                        pass
            drives["updated_at"] = state["drives"].get("updated_at")
            if isinstance(state["drives"].get("inputs"), dict):
                drives["inputs"] = state["drives"]["inputs"]
        base["drives"] = drives

        decision = _empty_decision()
        if isinstance(state.get("decision"), dict):
            decision.update(
                {k: v for k, v in state["decision"].items() if k in decision}
            )
        base["decision"] = decision

        decline = _empty_decline()
        if isinstance(state.get("decline"), dict):
            decline.update(
                {k: v for k, v in state["decline"].items() if k in decline}
            )
        base["decline"] = decline

        availability = state.get("availability")
        base["availability"] = availability if availability in AVAILABILITY else "open"
        base["availability_reason"] = (
            state.get("availability_reason")
            if isinstance(state.get("availability_reason"), str)
            else "init"
        )
        base["availability_since"] = state.get("availability_since")
        base["availability_updated_at"] = state.get("availability_updated_at")
        base["next_transition"] = state.get("next_transition")
        base["activity"] = (
            state.get("activity") if isinstance(state.get("activity"), str) else None
        )
        return base

    def save_state(self, state: dict) -> None:
        state["updated_at"] = iso(now_local())
        self._write_json(self.state_path, state)

    def _journal(self, payload: dict) -> None:
        self.ensure()
        day = now_local().strftime("%Y-%m-%d")
        path = self.journal_dir / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- 推进

    def advance(self, dt: datetime | None = None, journal: bool = True) -> dict:
        """按小时槽幂等推进。返回 {advanced, reason, slot, count}。"""
        dt = dt or now_local()
        slot = slot_of(dt)
        state = self.load_state()
        if state.get("last_advance") == slot:
            return {
                "advanced": False,
                "reason": "already_advanced",
                "slot": slot,
                "count": state.get("advance_count", 0),
            }
        state["last_advance"] = slot
        state["last_advance_at"] = iso(dt)
        state["advance_count"] = int(state.get("advance_count", 0)) + 1
        state["boot_id"] = self._boot_id
        self.save_state(state)
        if journal:
            self._journal(
                {
                    "ts": iso(dt),
                    "kind": "advance",
                    "slot": slot,
                    "count": state["advance_count"],
                }
            )
        return {
            "advanced": True,
            "reason": "slot_changed",
            "slot": slot,
            "count": state["advance_count"],
        }

    # ---------------------------------------------------------------- 事实

    def _schedule_snapshot(self, data: dict | None, dt: datetime | None = None) -> dict | None:
        """把作息模块的返回值裁成事实快照；纯函数，不读写 state。"""
        if not data:
            return None
        snapshot = {}
        for key in FACT_FIELDS:
            value = data.get(key)
            if value is None:
                continue
            text = str(value)
            snapshot[key] = text[:FACT_MAX]
        return snapshot or None

    def apply_schedule(
        self, state: dict, data: dict | None, dt: datetime | None = None
    ) -> dict:
        """在 state 副本上刷新 fact.schedule，不写盘；供只读工具使用。"""
        snapshot = self._schedule_snapshot(data, dt)
        if not snapshot:
            return state
        updated = dict(state)
        updated["fact"] = dict(state.get("fact") or {})
        updated["fact"]["schedule"] = snapshot
        updated["fact"]["updated_at"] = iso(dt or now_local())
        return updated

    def refresh_schedule(self, data: dict | None, journal: bool = True) -> dict | None:
        """把作息算出的「此刻」写成事实快照；data 为空表示作息模块不可用。"""
        snapshot = self._schedule_snapshot(data)
        if not snapshot:
            return None
        state = self.load_state()
        current = (state.get("fact") or {}).get("schedule") or {}
        same_slot = (
            current.get("slot_key") == snapshot.get("slot_key")
            and current.get("detail") == snapshot.get("detail")
        )
        # 同一时段内也要刷新 local，否则注入里会一直显示进这个时段的那一刻
        if same_slot and current.get("local") == snapshot.get("local"):
            return snapshot
        state["fact"]["schedule"] = snapshot
        state["fact"]["updated_at"] = iso(now_local())
        self.save_state(state)
        if journal and not same_slot:
            self._journal(
                {
                    "ts": iso(now_local()),
                    "kind": "fact",
                    "what": "schedule",
                    "slot_key": snapshot.get("slot_key"),
                    "place": snapshot.get("place"),
                    "doing": snapshot.get("doing"),
                }
            )
        return snapshot

    # ---------------------------------------------------------------- 心跳

    def beat(self, dt: datetime | None = None, extra: dict | None = None) -> dict:
        dt = dt or now_local()
        payload = {
            "ts": iso(dt),
            "ts_epoch": int(dt.timestamp()),
            "pid": os.getpid(),
            "boot_id": self._boot_id,
            "interval": self.heartbeat_seconds,
        }
        if extra:
            payload.update(extra)
        self._write_json(self.heartbeat_path, payload)
        return payload

    def heartbeat_is_fresh(self, tolerance: int = 0) -> bool:
        if not self.heartbeat_path.exists():
            return False
        try:
            beat = self._read_json(self.heartbeat_path)
            age = time.time() - float(beat.get("ts_epoch", 0))
        except (OSError, ValueError, TypeError):
            return False
        return age <= self.heartbeat_seconds * 2 + tolerance

    # ---------------------------------------------------------------- 自选状态

    def set_self(self, field: str, value: str | None, source: str = "model") -> dict:
        if field not in SELF_FIELDS:
            raise ValueError(f"unknown field: {field}")
        state = self.load_state()
        cleaned = None if value is None else str(value).strip()[:80] or None
        if state["self"].get(field) == cleaned:
            return state["self"]
        state["self"][field] = cleaned
        state["self"]["updated_at"] = iso(now_local())
        state["self"]["source"] = source
        self.save_state(state)
        self._journal(
            {
                "ts": iso(now_local()),
                "kind": "self",
                "field": field,
                "value": cleaned,
                "source": source,
            }
        )
        return state["self"]

    # ---------------------------------------------------------------- 驱力

    def idle_minutes(self, dt: datetime | None = None, state: dict | None = None) -> float:
        """距上次对话过去多少分钟；从没说过话就从建库时间算。"""
        dt = dt or now_local()
        state = state or self.load_state()
        last = state.get("last_talk_at") or state.get("created_at")
        try:
            last_dt = datetime.fromisoformat(str(last))
        except (TypeError, ValueError):
            return 0.0
        try:
            delta = (dt - last_dt).total_seconds()
        except TypeError:
            # 一边带时区一边不带（手改过 state.json 时会这样）：都按本地墙钟比较
            delta = (dt.replace(tzinfo=None) - last_dt.replace(tzinfo=None)).total_seconds()
        return round(max(0.0, delta / 60.0), 1)

    def touch_talk(self, dt: datetime | None = None, journal: bool = False) -> dict:
        """记一次对话（每轮请求调用一次），只改记账，不改驱力；驱力由下一拍向低目标收敛。"""
        dt = dt or now_local()
        state = self.load_state()
        state["last_talk_at"] = iso(dt)
        state["talk_count"] = int(state.get("talk_count", 0)) + 1
        self.save_state(state)
        if journal:
            self._journal(
                {"ts": iso(dt), "kind": "talk", "count": state["talk_count"]}
            )
        return {"last_talk_at": state["last_talk_at"], "talk_count": state["talk_count"]}

    def update_drives(
        self, targets: dict, dt: datetime | None = None, journal: bool = False
    ) -> dict:
        """一阶低通：v_t = 0.7·v_{t-1} + 0.3·v_target。单步最多挪 0.3×(1-当前值)。"""
        dt = dt or now_local()
        state = self.load_state()
        drives = state["drives"]
        merged = {}
        for name in DRIVES:
            current = float(drives.get(name, DRIVE_DEFAULT))
            target = targets.get(name)
            if target is None:
                merged[name] = _clamp(current)
                continue
            merged[name] = _clamp(
                DRIVE_LERP_KEEP * current + DRIVE_LERP_NEW * _clamp(target)
            )
        drives.update(merged)
        drives["updated_at"] = iso(dt)
        inputs = targets.get("inputs")
        drives["inputs"] = dict(inputs) if isinstance(inputs, dict) else {}
        self.save_state(state)
        if journal:
            self._journal(
                {"ts": iso(dt), "kind": "drives", **merged, "inputs": drives["inputs"]}
            )
        return drives

    def drives(self) -> dict:
        return self.load_state()["drives"]

    # ------------------------------------------------------- 拒绝出口

    def _decline_parts(
        self, state: dict | None, dt: datetime | None = None
    ) -> tuple[bool, str | None, str | None]:
        """从给定 state 判断「先别找我」，不额外读盘。"""
        dt = dt or now_local()
        decline = (state or {}).get("decline") or {}
        raw = decline.get("until")
        if not raw:
            return False, None, None
        try:
            deadline = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False, None, None
        if deadline.tzinfo is not None and dt.tzinfo is None:
            deadline = deadline.replace(tzinfo=None)
        if deadline <= dt:
            return False, raw, decline.get("reason")
        return True, raw, decline.get("reason")

    def decline_active(
        self, dt: datetime | None = None
    ) -> tuple[bool, str | None, str | None]:
        """是否处在她自己声明的「先别找我」窗口。返回 (是否生效, 到期时间, 理由)。"""
        return self._decline_parts(self.load_state(), dt)

    def set_decline(
        self,
        hours: float = 2.0,
        reason: str = "",
        dt: datetime | None = None,
        journal: bool = True,
    ) -> dict:
        """她主动说「这会儿先别找我」：只写到期时间，到期自动失效，不需要谁去清。"""
        dt = dt or now_local()
        try:
            span = float(hours)
        except (TypeError, ValueError):
            span = 2.0
        span = min(DECLINE_MAX_HOURS, max(DECLINE_MIN_HOURS, span))
        state = self.load_state()
        deadline = dt + timedelta(hours=span)
        note = (reason or "").strip()[:40] or None
        state["decline"] = {"until": iso(deadline), "reason": note, "at": iso(dt)}
        self.save_state(state)
        if journal:
            self._journal(
                {"ts": iso(dt), "kind": "decline", "hours": span, **state["decline"]}
            )
        return state["decline"]

    def clear_decline(self, dt: datetime | None = None, journal: bool = True) -> dict:
        dt = dt or now_local()
        state = self.load_state()
        previous = state.get("decline") or {}
        state["decline"] = _empty_decline()
        self.save_state(state)
        if journal and previous.get("until"):
            self._journal(
                {"ts": iso(dt), "kind": "decline_cleared", "until": previous.get("until")}
            )
        return state["decline"]

    def decide(
        self,
        social_threshold: float = 0.7,
        idle_trigger_minutes: float = 45.0,
        quiet: bool = False,
        asleep: bool = False,
        dt: datetime | None = None,
    ) -> dict:
        """阈值 → 动作。只做判断并把理由落盘，不负责发送（发送另有开关，见 main）。"""
        dt = dt or now_local()
        state = self.load_state()
        drives = state["drives"]
        social = float(drives.get("social", DRIVE_DEFAULT))
        rest = float(drives.get("rest", DRIVE_DEFAULT))
        idle = self.idle_minutes(dt)
        declining, decline_until, decline_reason = self.decline_active(dt)

        if declining:
            action, reason, drive, value = "decline", "self_declared", None, None
        elif asleep:
            action, reason, drive, value = "sleepy", "asleep", "rest", rest
        elif rest >= 0.85:
            action, reason, drive, value = "sleepy", "tired", "rest", rest
        elif quiet:
            action, reason, drive, value = "none", "quiet_hours", "social", social
        elif social >= social_threshold and idle >= idle_trigger_minutes:
            action, reason, drive, value = "opener", "wants_talk", "social", social
        elif social >= social_threshold:
            action, reason, drive, value = "none", "recent_talk", "social", social
        else:
            action, reason, drive, value = "none", "social_low", "social", social

        decision = {
            "action": action,
            "reason": reason,
            "drive": drive,
            "value": _clamp(value) if value is not None else None,
            "idle_minutes": idle,
            "at": iso(dt),
        }
        if declining:
            decision["until"] = decline_until
            decision["note"] = decline_reason
        previous = (state.get("decision") or {}).get("action")
        state["decision"] = decision
        self.save_state(state)
        if previous != action:
            self._journal({"ts": iso(dt), "kind": "decision", **decision})
        return decision

    # ---------------------------------------------------------------- 呈现

    def facts(self, dt: datetime | None = None, state: dict | None = None) -> dict:
        dt = dt or now_local()
        state = state or self.load_state()
        beat = None
        if self.heartbeat_path.exists():
            try:
                beat = self._read_json(self.heartbeat_path)
            except (OSError, ValueError):
                beat = None
        return {
            "now": iso(dt),
            "now_human": human(dt),
            "slot": slot_of(dt),
            "advance_count": int(state.get("advance_count", 0)),
            "last_advance": state.get("last_advance"),
            "last_advance_at": state.get("last_advance_at"),
            "heartbeat": beat.get("ts") if beat else None,
            "heartbeat_fresh": self.heartbeat_is_fresh(),
            "boot_id": self._boot_id,
        }

    def schedule(self, state: dict | None = None) -> dict | None:
        state = state or self.load_state()
        return (state.get("fact") or {}).get("schedule") or None

    def self_fields(self) -> dict:
        return self.load_state()["self"]

    # ------------------------------------------------------- 居民版 availability

    def _slot_end(self, sched: dict, dt: datetime) -> str | None:
        """从作息槽（如 18:10-21:20）取结束时间，跨零点按次日算。"""
        slot = str(sched.get("slot") or "")
        if "-" not in slot:
            return None
        end = slot.rsplit("-", 1)[1].strip()
        try:
            hh, mm = (int(part) for part in end.split(":", 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        end_dt = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end_dt <= dt:
            end_dt += timedelta(days=1)
        return iso(end_dt)

    def derive_availability(
        self,
        state: dict | None = None,
        dt: datetime | None = None,
        quiet: bool | None = None,
        quiet_end: str | None = None,
    ) -> dict:
        """只读推导四状态；不写盘、不改变任何行为。

        优先级：她声明的「先别找我」→ 睡觉/静默 → 上课/作业/工作 → 休息/累 → 有空。
        `next_transition` 取真正会改变状态的那个时刻：安静窗口、静默结束、作息段结束。
        """
        dt = dt or now_local()
        state = state or self.load_state()
        sched = (state.get("fact") or {}).get("schedule") or {}
        doing = str(sched.get("doing") or "").strip()
        self_block = state.get("self") or {}
        activity = str(self_block.get("activity") or "").strip() or doing or None
        decision = state.get("decision") or {}
        drives = state.get("drives") or {}
        inputs = drives.get("inputs") or {}
        declining, until, _why = self._decline_parts(state, dt)
        if quiet is None:
            quiet = bool(inputs.get("quiet"))

        busy_marks = ("课", "作业", "赶", "工作", "开会", "实验", "复习", "考试")
        if declining:
            value, reason = "rest", "self_declared"
        elif "睡" in doing or decision.get("reason") == "asleep":
            value, reason = "sleep", "asleep"
        elif quiet:
            value, reason = "sleep", "quiet_hours"
        elif any(mark in doing for mark in busy_marks):
            value, reason = "focus", "schedule_busy"
        elif "休息" in doing or "躺" in doing:
            value, reason = "rest", "resting"
        elif decision.get("action") == "sleepy" or float(drives.get("rest") or 0) >= 0.85:
            value, reason = "rest", "tired"
        else:
            value, reason = "open", ("open" if sched else "unknown")

        slot_end = self._slot_end(sched, dt)
        if declining:
            next_transition = until or slot_end
        elif reason == "quiet_hours":
            # 静默期间即使到点换作息段也仍是 sleep，真正的变化点是静默结束。
            next_transition = quiet_end or slot_end
        else:
            next_transition = slot_end

        return {
            "availability": value,
            "reason": reason,
            "activity": activity,
            "next_transition": next_transition,
        }

    def apply_availability(
        self, state: dict, derived: dict, dt: datetime | None = None
    ) -> dict:
        """在 state 副本上应用 availability，不写盘；供只读工具使用。"""
        dt = dt or now_local()
        updated = dict(state)
        changed = (
            state.get("availability") != derived.get("availability")
            or state.get("availability_reason") != derived.get("reason")
            or state.get("activity") != derived.get("activity")
        )
        updated["availability"] = derived.get("availability")
        updated["availability_reason"] = derived.get("reason")
        updated["activity"] = derived.get("activity")
        updated["next_transition"] = derived.get("next_transition")
        updated["availability_updated_at"] = iso(dt)
        if changed or not state.get("availability_since"):
            updated["availability_since"] = iso(dt)
        return updated

    def refresh_availability(
        self,
        dt: datetime | None = None,
        journal: bool = True,
        quiet: bool | None = None,
        quiet_end: str | None = None,
    ) -> dict:
        """把推导出的 availability 落盘；只在值/理由/活动/下次变化变化时更新。"""
        dt = dt or now_local()
        state = self.load_state()
        derived = self.derive_availability(state, dt, quiet=quiet, quiet_end=quiet_end)
        value = derived["availability"]
        previous = state.get("availability")
        changed = (
            previous != value
            or state.get("availability_reason") != derived["reason"]
            or state.get("activity") != derived["activity"]
            or state.get("next_transition") != derived["next_transition"]
        )
        if not changed and state.get("availability_since"):
            return {
                **derived,
                "since": state.get("availability_since"),
                "changed": False,
            }

        previous_since = state.get("availability_since")
        state["availability"] = value
        state["availability_reason"] = derived["reason"]
        state["activity"] = derived["activity"]
        state["next_transition"] = derived["next_transition"]
        state["availability_updated_at"] = iso(dt)
        if previous != value or not previous_since:
            state["availability_since"] = iso(dt)
        self.save_state(state)
        if journal and previous != value:
            self._journal(
                {
                    "ts": iso(dt),
                    "kind": "availability",
                    "from": previous,
                    "to": value,
                    "reason": derived["reason"],
                }
            )
        return {**derived, "since": state.get("availability_since"), "changed": True}

    def journal_event(self, payload: dict) -> None:
        """公开追加一条流水；字段由调用方负责（用于急事/睡眠拦截审计）。"""
        self._journal(payload)

    def urgent_count(self, day: str | None = None) -> int:
        """当天已记录几条 kind=urgent；坏行跳过，不补写。"""
        day = day or now_local().strftime("%Y-%m-%d")
        path = self.journal_dir / f"{day}.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("kind") == "urgent":
                count += 1
        return count

    def day_events(self, day: str | None = None, limit: int = 0) -> list[dict]:
        """从当天 journal 里取「作息段变化」时间线；只读，不补写。"""
        day = day or now_local().strftime("%Y-%m-%d")
        path = self.journal_dir / f"{day}.jsonl"
        if not path.exists():
            return []
        events: list[dict] = []
        seen: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict) or row.get("kind") != "fact":
                continue
            slot_key = str(row.get("slot_key") or "")
            if not slot_key or slot_key in seen:
                continue
            seen.add(slot_key)
            time_text = slot_key.split("-", 1)[1] if "-" in slot_key else ""
            events.append(
                {
                    "time": time_text,
                    "place": str(row.get("place") or ""),
                    "doing": str(row.get("doing") or ""),
                    "slot_key": slot_key,
                }
            )
        if limit > 0:
            events = events[-limit:]
        return events

    def day_digest(self, day: str | None = None) -> dict:
        """按天聚合 journal（事实/自述/对话/判断/边界），只读，不补写。"""
        day = day or now_local().strftime("%Y-%m-%d")
        path = self.journal_dir / f"{day}.jsonl"
        digest = {
            "day": day,
            "rows": 0,
            "facts": [],
            "self": [],
            "talks": 0,
            "decisions": {},
            "urgent": 0,
            "sleep_blocks": 0,
        }
        if not path.exists():
            return digest
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return digest
        seen: set[str] = set()
        for line in lines:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            digest["rows"] += 1
            kind = row.get("kind")
            if kind == "fact":
                slot_key = str(row.get("slot_key") or "")
                if not slot_key or slot_key in seen:
                    continue
                seen.add(slot_key)
                digest["facts"].append(
                    {
                        "time": slot_key.split("-", 1)[1] if "-" in slot_key else "",
                        "place": str(row.get("place") or ""),
                        "doing": str(row.get("doing") or ""),
                    }
                )
            elif kind == "self":
                digest["self"].append(
                    {
                        "ts": str(row.get("ts") or ""),
                        "field": str(row.get("field") or ""),
                        "value": row.get("value"),
                    }
                )
            elif kind == "talk":
                try:
                    digest["talks"] += int(row.get("count") or 0)
                except (TypeError, ValueError):
                    pass
            elif kind == "decision":
                action = str(row.get("action") or "unknown")
                digest["decisions"][action] = digest["decisions"].get(action, 0) + 1
            elif kind == "urgent":
                digest["urgent"] += 1
            elif kind == "sleep_block":
                digest["sleep_blocks"] += 1
        return digest

    def render_diary(self, day: str | None = None) -> str:
        """一天的日记摘要：只基于 journal 事实，没记录就明说。"""
        d = self.day_digest(day)
        lines = [f"【日记】{d['day']}"]
        if not d["rows"]:
            lines.append("今天没有记录（不是我忘了，是还没有可记的事）。")
            return "\n".join(lines)
        if d["facts"]:
            lines.append(f"- 走过 {len(d['facts'])} 段：")
            for fact in d["facts"]:
                lines.append(
                    f"  · {fact['time'] or '?'} {fact['place'] or '某处'}，"
                    f"{fact['doing'] or '在忙'}"
                )
        if d["self"]:
            latest: dict[str, str] = {}
            for row in d["self"]:
                if row["field"] in SELF_FIELDS and row["value"]:
                    latest[row["field"]] = str(row["value"])
            if latest:
                lines.append(
                    "- 她记下："
                    + "；".join(f"{SELF_LABELS[k]}={v}" for k, v in latest.items())
                )
        if d["urgent"] or d["sleep_blocks"]:
            lines.append(
                f"- 边界：急事 {d['urgent']} 次，睡眠拦截 {d['sleep_blocks']} 次"
            )
        if len(lines) == 1:
            lines.append("今天没有可写的事。")
        return "\n".join(lines)

    def render_day_brief(self, day: str | None = None, max_chars: int = 180) -> str:
        """次日跟进用的极简摘要：只取当天最后 4 段作息的「时间 地点，在做什么」。

        不读聊天、不生成新事实；没有记录返回空串。超过 max_chars 截断并加省略号，
        保证注入不会把昨天的日记全文塞回上下文。
        """
        digest = self.day_digest(day)
        facts = digest.get("facts") or []
        if not facts:
            return ""
        parts: list[str] = []
        for fact in facts[-4:]:
            time_text = str(fact.get("time") or "?").strip()
            place = str(fact.get("place") or "某处").strip()
            doing = str(fact.get("doing") or "在忙").strip()
            parts.append(f"{time_text} {place}，{doing}")
        text = "；".join(parts)
        max_chars = max(40, int(max_chars))
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip("；，, ") + "…"
        return text

    def write_diary(self, day: str | None = None) -> Path | None:
        """把某天日记落盘；已有文件不覆盖，没有 journal 就不写。"""
        day = day or now_local().strftime("%Y-%m-%d")
        journal = self.journal_dir / f"{day}.jsonl"
        if not journal.exists():
            return None
        path = self.journal_dir / f"{day}.diary.md"
        if path.exists():
            return path
        try:
            path.write_text(self.render_diary(day) + "\n", encoding="utf-8")
        except OSError:
            return None
        return path

    def render_today(self, limit: int = 0) -> str:
        """今天已经走过的作息段；数据不足就明说，不虚构。"""
        events = self.day_events(limit=limit)
        if not events:
            return "【今天】还没有作息记录（从现在开始记）。"
        lines = [f"【今天】已记录 {len(events)} 段："]
        for event in events:
            time_text = event["time"] or "?"
            place = event["place"] or "某处"
            doing = event["doing"] or "在忙"
            lines.append(f"- {time_text} {place}，{doing}")
        return "\n".join(lines)

    def render(self, state: dict | None = None) -> str:
        """给模型/管理员看的完整两栏；未设置的字段明说「未设置」。"""
        state = state or self.load_state()
        facts = self.facts(state=state)
        me = state.get("self") or _empty_self()
        sched = self.schedule(state=state)

        def value(name: str) -> str:
            raw = me.get(name)
            return raw if raw else "未设置"

        if sched:
            moment = (
                f"{sched.get('weekday') or ''} {sched.get('place') or ''}，"
                f"{sched.get('doing') or ''}，{sched.get('mood') or ''}"
            ).strip("， ")
            if sched.get("detail"):
                moment += f"；{sched['detail']}"
        else:
            moment = "未设置（作息模块不可用）"

        beat = facts["heartbeat"] or "无"
        drives = state.get("drives") or _empty_drives()
        decision = state.get("decision") or _empty_decision()
        availability = state.get("availability") or "open"
        availability_label = AVAILABILITY_LABELS.get(availability, availability)
        reason = state.get("availability_reason") or "未设置"
        reason_label = AVAILABILITY_REASON_LABELS.get(reason)
        reason_text = f"{reason}（{reason_label}）" if reason_label else reason
        activity = state.get("activity") or value("activity")
        drive_text = "，".join(
            f"{DRIVE_LABELS[name]}={drives.get(name)}" for name in DRIVES
        )
        lines = [
            "【生活状态（只读）】",
            f"- 系统给的：现在是 {facts['now_human']}，时段 {facts['slot']}；"
            f"此刻 {moment}；"
            f"今天已推进 {facts['advance_count']} 次，最近一次 {facts['last_advance_at'] or '无'}；"
            f"心跳 {beat}。",
            f"- 现在能不能聊：{availability}（{availability_label}；"
            f"{reason_text}）；手头的事：{activity or '未设置'}；"
            f"下次变化：{state.get('next_transition') or '未设置'}。",
            f"- 她自己填的：在做什么={value('activity')}；在哪={value('location')}；"
            f"心情={value('mood')}；备注={value('note')}。",
            f"- 驱力（0–1）：{drive_text}；距上次对话 {self.idle_minutes(state=state)} 分钟，"
            f"累计 {state.get('talk_count', 0)} 轮。",
            f"- 此刻判断：{decision.get('action')}（{decision.get('reason')}，"
            f"{decision.get('drive') or '-'}={decision.get('value')}）。"
            f"opener 只表示她想开口，发不发由开关决定。",
            f"- 她上次改这些是 {me.get('updated_at') or '无'}。没填的就是没填，不要替她补。",
        ]
        return "\n".join(lines)

    def render_line(self) -> str:
        """注入对话的一句话（≤130 字）：只给事实，不给结论。

        概念文档 v4.0 §4.5：情绪与语气的解读交给模型。这里不写「这会儿正想找他」
        这类替她下的结论，只给可核对的事实（时间、地点、在做什么、多久没说话）
        以及她自己填过的字段。
        """
        sched = self.schedule()
        me = self.self_fields()
        if sched:
            head = (
                f"（你此刻：{sched.get('weekday') or ''} {sched.get('local', '')[11:]}，"
                f"{sched.get('place') or ''}，{sched.get('doing') or ''}，{sched.get('mood') or ''}"
            )
            if sched.get("detail"):
                head += f"；{sched['detail']}"
            head += "。"
            idle = int(self.idle_minutes())
            if idle >= 30:
                head += f"距上次说话已 {idle // 5 * 5} 分钟。"
        else:
            head = "（你此刻：时间与作息暂时读不到，别猜。"
        noted = [
            f"{SELF_LABELS[name]}={me[name]}"
            for name in SELF_FIELDS
            if me.get(name)
        ]
        if noted:
            head += "你自己记过：" + "，".join(noted) + "。"
        declining, until, _ = self.decline_active()
        if declining and until:
            head += f"你自己说过 {str(until)[11:16]} 之前先别找人说话。"
        return head + "不必复述这段。）"

    def summary(self) -> dict:
        state = self.load_state()
        return {
            "facts": self.facts(),
            "schedule": self.schedule(),
            "self": self.self_fields(),
            "drives": state.get("drives"),
            "decision": state.get("decision"),
            "decline": state.get("decline"),
            "availability": state.get("availability"),
            "availability_reason": state.get("availability_reason"),
            "availability_since": state.get("availability_since"),
            "next_transition": state.get("next_transition"),
            "activity": state.get("activity"),
            "updated_at": state.get("updated_at"),
            "idle_minutes": self.idle_minutes(),
        }


__all__ = [
    "LifeStore",
    "SCHEMA",
    "SELF_FIELDS",
    "SELF_LABELS",
    "AVAILABILITY",
    "AVAILABILITY_LABELS",
    "AVAILABILITY_REASON_LABELS",
    "FACT_FIELDS",
    "DRIVES",
    "DRIVE_LABELS",
    "DRIVE_DEFAULT",
    "WEEKDAYS",
    "human",
    "iso",
    "now_local",
    "slot_of",
]
