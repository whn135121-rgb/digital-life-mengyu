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
from datetime import datetime
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

SELF_FIELDS = ("activity", "location", "mood", "note")
SELF_LABELS = {
    "activity": "在做什么",
    "location": "在哪",
    "mood": "心情",
    "note": "备注",
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

    def load_state(self) -> dict:
        self.ensure()
        if not self.state_path.exists():
            state = _new_state(now_local(), self._boot_id)
            self._write_json(self.state_path, state)
            return state
        try:
            state = self._read_json(self.state_path)
        except (OSError, ValueError):
            bad = self.state_path.with_suffix(".json.bad")
            try:
                os.replace(self.state_path, bad)
            except OSError:
                pass
            state = _new_state(now_local(), self._boot_id)
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
        return base

    def save_state(self, state: dict) -> None:
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

    def refresh_schedule(self, data: dict | None, journal: bool = True) -> dict | None:
        """把作息算出的「此刻」写成事实快照；data 为空表示作息模块不可用。"""
        if not data:
            return None
        snapshot = {}
        for key in FACT_FIELDS:
            value = data.get(key)
            if value is None:
                continue
            text = str(value)
            snapshot[key] = text[:FACT_MAX]
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

    def idle_minutes(self, dt: datetime | None = None) -> float:
        """距上次对话过去多少分钟；从没说过话就从建库时间算。"""
        dt = dt or now_local()
        state = self.load_state()
        last = state.get("last_talk_at") or state.get("created_at")
        try:
            last_dt = datetime.fromisoformat(str(last))
        except (TypeError, ValueError):
            return 0.0
        return round(max(0.0, (dt - last_dt).total_seconds() / 60.0), 1)

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

        if asleep:
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
            "value": _clamp(value),
            "idle_minutes": idle,
            "at": iso(dt),
        }
        previous = (state.get("decision") or {}).get("action")
        state["decision"] = decision
        self.save_state(state)
        if previous != action:
            self._journal({"ts": iso(dt), "kind": "decision", **decision})
        return decision

    # ---------------------------------------------------------------- 呈现

    def facts(self, dt: datetime | None = None) -> dict:
        dt = dt or now_local()
        state = self.load_state()
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

    def schedule(self) -> dict | None:
        state = self.load_state()
        return (state.get("fact") or {}).get("schedule") or None

    def self_fields(self) -> dict:
        return self.load_state()["self"]

    def render(self) -> str:
        """给模型/管理员看的完整两栏；未设置的字段明说「未设置」。"""
        facts = self.facts()
        me = self.self_fields()
        sched = self.schedule()

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
        state = self.load_state()
        drives = state.get("drives") or _empty_drives()
        decision = state.get("decision") or _empty_decision()
        drive_text = "，".join(
            f"{DRIVE_LABELS[name]}={drives.get(name)}" for name in DRIVES
        )
        lines = [
            "【生活状态（只读）】",
            f"- 系统给的：现在是 {facts['now_human']}，时段 {facts['slot']}；"
            f"此刻 {moment}；"
            f"今天已推进 {facts['advance_count']} 次，最近一次 {facts['last_advance_at'] or '无'}；"
            f"心跳 {beat}。",
            f"- 她自己填的：在做什么={value('activity')}；在哪={value('location')}；"
            f"心情={value('mood')}；备注={value('note')}。",
            f"- 驱力（0–1）：{drive_text}；距上次对话 {self.idle_minutes()} 分钟，"
            f"累计 {state.get('talk_count', 0)} 轮。",
            f"- 此刻判断：{decision.get('action')}（{decision.get('reason')}，"
            f"{decision.get('drive') or '-'}={decision.get('value')}）。"
            f"opener 只表示她想开口，发不发由开关决定。",
            f"- 她上次改这些是 {me.get('updated_at') or '无'}。没填的就是没填，不要替她补。",
        ]
        return "\n".join(lines)

    def render_line(self) -> str:
        """注入对话的一句话（≤130 字）：只有系统事实 + 她自己填过的字段。"""
        sched = self.schedule()
        me = self.self_fields()
        decision = (self.load_state().get("decision") or _empty_decision())
        if sched:
            head = (
                f"（你此刻：{sched.get('weekday') or ''} {sched.get('local', '')[11:]}，"
                f"{sched.get('place') or ''}，{sched.get('doing') or ''}，{sched.get('mood') or ''}"
            )
            if sched.get("detail"):
                head += f"；{sched['detail']}"
            head += "。这是你真实的一天，不用主动交代，也别把这句括号原样说出来。"
        else:
            head = "（你此刻：时间与作息暂时读不到，别猜。"
        if decision.get("action") == "opener":
            head += "你隔了一阵没人说话，这会儿正想找他。"
        noted = [
            f"{SELF_LABELS[name]}={me[name]}"
            for name in SELF_FIELDS
            if me.get(name)
        ]
        if noted:
            head += "你自己记过：" + "，".join(noted) + "。）"
        else:
            head += "）"
        return head

    def summary(self) -> dict:
        state = self.load_state()
        return {
            "facts": self.facts(),
            "schedule": self.schedule(),
            "self": self.self_fields(),
            "drives": state.get("drives"),
            "decision": state.get("decision"),
            "idle_minutes": self.idle_minutes(),
        }


__all__ = [
    "LifeStore",
    "SCHEMA",
    "SELF_FIELDS",
    "SELF_LABELS",
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
