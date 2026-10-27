"""生活状态的持久化层（只依赖标准库，可脱离 AstrBot 单测）。

数据目录：`qqbot/data/life/`
  state.json                她的「自选」状态 + 推进记账
  heartbeat.json            心跳，每 60 秒覆写一次
  journal/YYYY-MM-DD.jsonl  当天流水（推进、自改状态各一行）

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

SCHEMA = 1
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

SELF_FIELDS = ("activity", "location", "mood", "note")


def now_local() -> datetime:
    return datetime.now()


def slot_of(dt: datetime) -> str:
    """小时槽，形如 2026-10-27T12。"""
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


def _new_state(dt: datetime, boot_id: str) -> dict:
    return {
        "schema": SCHEMA,
        "boot_id": boot_id,
        "created_at": iso(dt),
        "last_advance": None,
        "last_advance_at": None,
        "advance_count": 0,
        "journal_today": 0,
        "self": _empty_self(),
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
        self_block = dict(_empty_self())
        if isinstance(state.get("self"), dict):
            self_block.update(
                {k: v for k, v in state["self"].items() if k in self_block}
            )
        base["self"] = self_block
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
        state["self"][field] = None if value is None else str(value).strip()[:80] or None
        state["self"]["updated_at"] = iso(now_local())
        state["self"]["source"] = source
        self.save_state(state)
        self._journal(
            {
                "ts": iso(now_local()),
                "kind": "self",
                "field": field,
                "value": state["self"][field],
                "source": source,
            }
        )
        return state["self"]

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

    def render(self, dt: datetime | None = None) -> str:
        """给模型看的一段话；未设置的字段明说「未设置」。"""
        dt = dt or now_local()
        facts = self.facts(dt)
        state = self.load_state()
        me = state["self"]

        def value(name: str) -> str:
            raw = me.get(name)
            return raw if raw else "未设置"

        beat = facts["heartbeat"] or "无"
        lines = [
            "【生活状态（只读）】",
            f"- 系统给的：现在是 {facts['now_human']}，时段 {facts['slot']}；"
            f"今天已推进 {facts['advance_count']} 次，最近一次 {facts['last_advance_at'] or '无'}；"
            f"心跳 {beat}。",
            f"- 她自己填的：活动={value('activity')}；地点={value('location')}；"
            f"心情={value('mood')}；备注={value('note')}。",
            f"- 她上次改这些是 {me.get('updated_at') or '无'}。"
            "没填的就是没填，不要替她补。",
        ]
        return "\n".join(lines)

    def summary(self) -> dict:
        return {"facts": self.facts(), "self": self.load_state()["self"]}


__all__ = [
    "LifeStore",
    "SCHEMA",
    "SELF_FIELDS",
    "WEEKDAYS",
    "human",
    "iso",
    "now_local",
    "slot_of",
]
