"""惊讶度（bigram 重复度）离线评估：只读数据库，不改任何配置。

v4 §2.9 的说法是：拿「bigram 重复度」当惊讶度的廉价代理 —— 同一段话里反复出现的
字符二元组越多，越像套话/复读。这里只做**离线评估**，不接线上阈值。

流程：
1. 从 `memory_atoms` 取全部原子（去重、按 id 取前 300），算每个原子的
   字符 bigram 重复率（repeated_bigrams / (len-1)）；
2. 输出分位数、直方图，以及 top/bottom 抽样（默认 50 条，供人工标注「像套话 / 不像」）；
3. 把抽样与分数写进 `scripts\\surprise_report.json`，报告 markdown 由人工补标注后写。

用法：
    python scripts\\eval_surprise.py            # 看分布
    python scripts\\eval_surprise.py --sample 50 --write report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"
OUT_PATH = ROOT / "scripts" / "surprise_report.json"

_PUNCT = re.compile(r"[\s，。！？、；：「」『』（）()\[\]{}<>《》…—\-~·,.!?;:'\"]+")


def bigram_repetition(text: str) -> float:
    """重复 bigram 占比：0 = 每个二元组都不同，1 = 全是同一个二元组来回。"""
    cleaned = _PUNCT.sub("", text or "")
    if len(cleaned) < 3:
        return 0.0
    grams = [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]
    counts = Counter(grams)
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    return round(repeated / len(grams), 4)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def histogram(values: list[float], bins: int = 10) -> list[tuple[float, float, int]]:
    if not values:
        return []
    lo, hi = 0.0, max(values) + 1e-9
    width = (hi - lo) / bins
    buckets = [[lo + i * width, lo + (i + 1) * width, 0] for i in range(bins)]
    for v in values:
        idx = min(bins - 1, int((v - lo) / width))
        buckets[idx][2] += 1
    return [(round(a, 3), round(b, 3), c) for a, b, c in buckets]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--write", default="")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, content FROM memory_atoms WHERE status = 'active' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    scored = [(int(i), str(t), bigram_repetition(str(t))) for i, t in rows if t and len(str(t)) >= 8]
    values = [s for _, _, s in scored]
    print(f"原子 {len(rows)} 条，参与评估 {len(scored)} 条（长度 ≥8）")
    print(
        "分位数："
        + "  ".join(f"p{int(q*100)}={percentile(values, q):.3f}" for q in (0.5, 0.75, 0.9, 0.95, 0.99))
    )
    print("直方图：")
    for lo, hi, count in histogram(values):
        print(f"  [{lo:.2f},{hi:.2f}) {'#' * min(60, count)} {count}")

    n = max(0, args.sample)
    top = sorted(scored, key=lambda t: -t[2])[: n // 2]
    rest = [t for t in scored if t not in top]
    picks = top + sorted(rest, key=lambda t: -t[2])[: n - len(top)]

    # 另一路参考：原子两两之间的 bigram Jaccard，看「复读」到底存不存在
    gram_sets = [(i, t, set(t[j : j + 2] for j in range(len(t) - 1))) for i, t, _ in scored]
    pairs = []
    for a in range(len(gram_sets)):
        for b in range(a + 1, len(gram_sets)):
            ga, gb = gram_sets[a][2], gram_sets[b][2]
            if not ga or not gb:
                continue
            inter = len(ga & gb)
            if inter < 3:
                continue
            pairs.append((inter / len(ga | gb), gram_sets[a][0], gram_sets[b][0]))
    pairs.sort(reverse=True)
    dup_counts = {
        "ge_0.8": sum(1 for j, _, _ in pairs if j >= 0.8),
        "ge_0.6": sum(1 for j, _, _ in pairs if j >= 0.6),
        "ge_0.4": sum(1 for j, _, _ in pairs if j >= 0.4),
        "total_pairs_with_3_shared": len(pairs),
    }
    print(
        f"\n两两 bigram Jaccard：≥0.8 {dup_counts['ge_0.8']} 对、≥0.6 {dup_counts['ge_0.6']} 对、"
        f"≥0.4 {dup_counts['ge_0.4']} 对（共享 ≥3 个 bigram 的对共 {dup_counts['total_pairs_with_3_shared']}）"
    )
    print("最相似 5 对：")
    text_by_id = {i: t for i, t, _ in scored}
    for jac, ia, ib in pairs[:5]:
        print(f"  {jac:.3f} #{ia} {text_by_id[ia][:40]} || #{ib} {text_by_id[ib][:40]}")

    payload = {
        "atoms_total": len(rows),
        "atoms_scored": len(scored),
        "percentiles": {f"p{int(q*100)}": percentile(values, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
        "histogram": histogram(values),
        "samples": [
            {"atom_id": i, "score": s, "text": t[:160], "label": ""} for i, t, s in picks
        ],
    }
    if args.write:
        Path(args.write).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.write}")
    else:
        print("\n抽样（前 10）：")
        for item in payload["samples"][:10]:
            print(f"  {item['score']:.3f} #{item['atom_id']} {item['text'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
