"""第58課題: ワンデー4半荘の卓組みで、試行回数の上限を変えると
「計算時間」と「同卓の重複」がどう変わるかを比べる（DBは使わない純粋な計算）。

使い方: python -u scripts/compare_restart_cap.py [人数 ...]
乱数の種は環境変数 SEEDS で変えられる（例: SEEDS=1 … 200人の上限なしは1通りで約4分かかる）。
重複 = 4半荘を通して、同じ2人が2回以上同卓した「余分な回数」の合計（0が理想）。
"""
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import table_logic  # noqa: E402

CAPS = (None, 400, 200, 100, 50, 20)  # None = 上限なし(従来: max(80, 40×卓数))
SEEDS = tuple(int(x) for x in os.environ.get("SEEDS", "1,2,3").split(","))


def run_day(n, cap, seed):
    table_logic.GROUPING_MAX_RESTARTS = cap
    rng = random.Random(seed)
    members = list(range(1, n + 1))
    app, hist, co, prev = {}, {}, {}, set()
    slowest = 0.0
    for h in (1, 2, 3, 4):
        t = time.perf_counter()
        absent, tables = table_logic.one_day_hanchan(members, h, app, prev, hist, co, rng)
        slowest = max(slowest, time.perf_counter() - t)
        present = [p for tb in tables for p in tb.values()]
        app = table_logic.update_appearance_counts(app, present)
        hist = table_logic.update_position_history(hist, tables)
        co = table_logic.update_co_seat_counts(co, tables)
        prev = set(absent)
    excess = sum(c - 1 for c in co.values() if c > 1)
    return slowest, excess


def main():
    sizes = [int(a) for a in sys.argv[1:]] or [16, 40, 80, 120, 200]
    print("人数 | 上限 | 実際の試行回数 | 1半荘の最長(秒) | 重複(平均) | 各回")
    for n in sizes:
        t = n // 4
        for cap in CAPS:
            actual = max(80, 40 * t) if cap is None else min(max(80, 40 * t), cap)
            if cap is not None and actual == max(80, 40 * t) and cap != CAPS[1]:
                continue  # 上限が効かない組み合わせは「上限なし」と同じなので省く
            results = [run_day(n, cap, seed) for seed in SEEDS]
            times = [r[0] for r in results]
            dups = [r[1] for r in results]
            label = "なし" if cap is None else str(cap)
            print(f"{n:4d} | {label:>4} | {actual:5d} | {max(times):7.2f} | {sum(dups) / len(dups):6.1f} | {dups}",
                  flush=True)


if __name__ == "__main__":
    main()
