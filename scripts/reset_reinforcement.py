"""把被「15 问扫测」污染过的强化计数清零（这些查询是我用 API 打的，不是真实对话）。

做法：先 copy2 备份 livingmemory.db，再 UPDATE memory_atoms SET reinforcement_count=0,
last_reinforced_at=NULL WHERE reinforcement_count > 0；打印前后对比。

用法：python scripts\\reset_reinforcement.py [--apply]
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parents[1] / "qqbot" / "data" / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"


def main() -> int:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB)
    try:
        before = conn.execute(
            "SELECT id, reinforcement_count, last_reinforced_at FROM memory_atoms WHERE reinforcement_count > 0 ORDER BY id"
        ).fetchall()
        print(f"当前被强化的原子：{len(before)} 个")
        for row in before:
            print("  ", row)
        if not apply:
            print("（预演：加 --apply 才清零）")
            return 0

        backup = DB.with_name(f"{DB.name}.bak-reinforce-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(DB, backup)
        print("已备份：", backup.name)

        conn.execute(
            "UPDATE memory_atoms SET reinforcement_count = 0, last_reinforced_at = NULL WHERE reinforcement_count > 0"
        )
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE reinforcement_count > 0").fetchone()[0]
        print(f"清零完成，残留 {left} 个")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
