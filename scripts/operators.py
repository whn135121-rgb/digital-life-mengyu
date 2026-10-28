"""五算子（O1/O2/O3/O5）的离线实现与安全试跑工具。

设计原则：
1. **不碰线上库**：默认只读；`--apply` 要求显式 `--db` 指向副本，除非再加 `--force-live`。
2. **不删数据**：合并只写 `metadata.superseded_by` / `merged_from`，冷热迁移只写 `metadata.cold`，
   都不改 `status`（`status=archived` 在当前插件里会被检索硬过滤，不能用）。
3. **先剥模板再比**：原子内容普遍带「虚拟2532王浩楠在2026-10-26 23:23」这类模板前缀，
   直接算 bigram Jaccard 会把「挑参数」和「说晚安」判成 0.60 重复（2026-10-28 实测），必须先剥。

五个算子里：
- O1 抽取：`summarize_group()` 给确定性的抽取式摘要（不调模型），够离线评估用。
- O2 合并：`plan_merge()` / `apply_merge()`。
- O3 重标定：`rescale_weights()`（权重守恒缩放）。
- O4 强化剪枝：已在插件里实现（`AtomReinforcer`），此处不重复。
- O5 冷热迁移：`plan_demote()` / `apply_demote()`。

用法：
    python scripts/operators.py                     # 只读试跑，出报告
    python scripts/operators.py --db copy.db --apply --ops merge
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
LIVE_DB = (
    BASE
    / "qqbot"
    / "data"
    / "plugin_data"
    / "astrbot_plugin_livingmemory"
    / "livingmemory.db"
)

JACCARD_MIN = 0.60
CONTAINMENT_MIN = 0.78
MIN_STRIPPED_CHARS = 4
HOT_CAP = 4800
COLD_TRIGGER = 4800
DEMOTE_RATIO = 0.05
DEMOTE_MAX = 250

_BOILER = re.compile(r"^(虚拟\d+王浩楠|梦殇)?\s*在?\s*(2026-\d{2}-\d{2})?\s*(\d{1,2}:\d{2})?\s*")
_TIME = re.compile(r"\d{1,2}:\d{2}|2026-\d{2}-\d{2}")
_PUNCT = re.compile(r"[\s，。、！？；：「」“”,.!?;:（）()\-—…~]+")


def strip_boilerplate(text: str) -> str:
    """去掉「谁在什么时候」的模板前缀与标点，只留命题本身。"""
    clean = _BOILER.sub("", text or "")
    clean = _TIME.sub("", clean)
    return _PUNCT.sub("", clean)


def bigrams(text: str) -> set[str]:
    chars = list(text or "")
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return max(len(a & b) / len(a), len(a & b) / len(b))


def is_duplicate(text_a: str, text_b: str) -> bool:
    """剥模板后同时满足 Jaccard ≥0.60 与包含度 ≥0.78 才算重复。"""
    a = strip_boilerplate(text_a)
    b = strip_boilerplate(text_b)
    if len(a) < MIN_STRIPPED_CHARS or len(b) < MIN_STRIPPED_CHARS:
        return False
    ga, gb = bigrams(a), bigrams(b)
    return jaccard(ga, gb) >= JACCARD_MIN and containment(ga, gb) >= CONTAINMENT_MIN


def plan_merge(atoms: list[dict]) -> list[list[int]]:
    """O2：找出应合并的原子组（union-find），同会话才合并。"""
    by_id = {int(a["id"]): a for a in atoms}
    parent = {aid: aid for aid in by_id}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    ids = sorted(by_id)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            sa = str(by_id[a].get("session") or "")
            sb = str(by_id[b].get("session") or "")
            if sa and sb and sa != sb:
                continue
            if is_duplicate(by_id[a]["text"], by_id[b]["text"]):
                union(a, b)

    groups: dict[int, list[int]] = defaultdict(list)
    for aid in ids:
        groups[find(aid)].append(aid)
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def summarize_group(texts: list[str], max_chars: int = 40) -> str:
    """O1：确定性的抽取式摘要（不调模型），取最长一条的命题作摘要。"""
    cleaned = [strip_boilerplate(t) for t in texts if strip_boilerplate(t)]
    if not cleaned:
        return ""
    best = max(cleaned, key=len)
    return best[:max_chars]


def rescale_weights(weights: list[float], total: float | None = None) -> list[float]:
    """O3：权重守恒缩放 w_i ← w_i · S / Σw（S 默认取原总和），保持组内相对比例。"""
    values = [float(w) for w in weights]
    if not values:
        return []
    current = sum(values)
    if current <= 0:
        return [0.0 for _ in values]
    target = current if total is None else float(total)
    factor = target / current
    return [round(w * factor, 6) for w in values]


def plan_demote(
    atoms: list[dict],
    cap: int = HOT_CAP,
    ratio: float = DEMOTE_RATIO,
    max_move: int = DEMOTE_MAX,
) -> list[int]:
    """O5：热库超过 cap 时，按权重升序取最低的 5%（至多 250 条）。"""
    active = [a for a in atoms if not a.get("cold")]
    if len(active) <= cap:
        return []
    move = min(int(len(active) * ratio), max_move)
    if move <= 0:
        return []
    ranked = sorted(
        active,
        key=lambda a: (
            float(a.get("importance") or 0.5) * float(a.get("confidence") or 0.5),
            int(a["id"]),
        ),
    )
    return [int(a["id"]) for a in ranked[:move]]


def _load_atoms(path: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, content, importance, confidence, session_id, metadata "
            "FROM memory_atoms ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    atoms = []
    for row in rows:
        try:
            meta = json.loads(row[5] or "{}")
        except Exception:
            meta = {}
        atoms.append(
            {
                "id": row[0],
                "text": row[1] or "",
                "importance": row[2],
                "confidence": row[3],
                "session": row[4],
                "cold": bool(meta.get("cold")),
                "meta": meta,
            }
        )
    return atoms


def apply_merge(path: Path, groups: list[list[int]], atoms: list[dict]) -> int:
    """把每组写入 superseded_by / merged_from，不删不归档。"""
    by_id = {int(a["id"]): a for a in atoms}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    updates = []
    for group in groups:
        survivor = max(group, key=lambda i: (float(by_id[i].get("importance") or 0), i))
        others = [i for i in group if i != survivor]
        for aid in group:
            meta = dict(by_id[aid]["meta"])
            if aid == survivor:
                meta["merged_from"] = others
                meta["merged_at"] = now
            else:
                meta["superseded_by"] = survivor
                meta["merged_at"] = now
            updates.append((json.dumps(meta, ensure_ascii=False), aid))
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.executemany("UPDATE memory_atoms SET metadata = ? WHERE id = ?", updates)
    finally:
        conn.close()
    return len(updates)


def apply_demote(path: Path, ids: list[int], atoms: list[dict]) -> int:
    by_id = {int(a["id"]): a for a in atoms}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    updates = []
    for aid in ids:
        meta = dict(by_id[aid]["meta"])
        meta["cold"] = True
        meta["demoted_at"] = now
        updates.append((json.dumps(meta, ensure_ascii=False), aid))
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.executemany("UPDATE memory_atoms SET metadata = ? WHERE id = ?", updates)
    finally:
        conn.close()
    return len(updates)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="五算子离线试跑")
    ap.add_argument("--db", default=str(LIVE_DB))
    ap.add_argument("--ops", default="merge,demote")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-live", action="store_true")
    ap.add_argument("--backup", action="store_true", default=True)
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"库不存在：{db}")
        return 2
    live = db.resolve() == LIVE_DB.resolve()
    if args.apply and live and not args.force_live:
        print("拒绝直接改写线上库；请先复制一份，或显式 --force-live（不推荐）")
        return 3

    atoms = _load_atoms(db)
    ops = {o.strip() for o in args.ops.split(",") if o.strip()}
    print(f"库：{db}（{'线上' if live else '副本'}）原子 {len(atoms)} 条")

    if "merge" in ops:
        groups = plan_merge(atoms)
        sample = [
            (g, [atoms_short(a) for a in g]) for g in groups[:5]
        ]  # 仅展示
        print(f"O2 合并：{len(groups)} 组可合并{'' if not groups else '，样例 ' + str(sample)}")
        if args.apply and groups:
            if args.backup:
                bak = db.with_suffix(db.suffix + f".bak-ops-{datetime.now():%Y%m%d%H%M%S}")
                shutil.copy2(db, bak)
                print(f"  已备份 {bak.name}")
            n = apply_merge(db, groups, atoms)
            print(f"  已写 superseded_by/merged_from：{n} 条")

    if "demote" in ops:
        ids = plan_demote(atoms)
        print(f"O5 冷热迁移：热库 {len(atoms)} / 触发线 {COLD_TRIGGER}，本轮应迁 {len(ids)} 条")
        if args.apply and ids:
            if args.backup:
                bak = db.with_suffix(db.suffix + f".bak-ops-{datetime.now():%Y%m%d%H%M%S}")
                shutil.copy2(db, bak)
                print(f"  已备份 {bak.name}")
            n = apply_demote(db, ids, atoms)
            print(f"  已写 cold 标记：{n} 条")

    if "summary" in ops:
        groups = plan_merge(atoms)
        by_id = {int(a["id"]): a for a in atoms}
        for group in groups[:5]:
            texts = [by_id[i]["text"] for i in group]
            print(f"O1 摘要样例：{summarize_group(texts)}")

    print("未加 --apply 时只读，不写入任何内容。")
    return 0


def atoms_short(aid: int) -> str:
    return f"#{aid}"


if __name__ == "__main__":
    sys.exit(main())
