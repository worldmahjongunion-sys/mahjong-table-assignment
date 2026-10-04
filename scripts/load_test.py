"""第58課題: 負荷実験（スケールの壁を探す）。

本番DBには触れない: 起動時に一時フォルダを作り、DB_PATHをその中のファイルに向けてから
db.pyを読み込む（環境変数DB_PATHが設定されていても無視する）。終了時に一時フォルダごと消す。

実験A: CPU      … 卓組み計算(ワンデー4半荘)の所要時間を参加人数ごとに測る
実験B: DB(量)   … 過去の大会データが積み上がったとき、画面の読み込みがどう遅くなるか
                   （seats(table_id)の索引なし＝修正前 と、init_db()で索引を後付けした後を比べる）
実験C: DB(同時) … ゲストが一斉に成績送信したときの待ち時間と失敗数
                   （描き直しのたびにinit_db()＝修正前 と、起動時に1回だけ＝修正後を比べる）

使い方: python -u scripts/load_test.py [a|b|c の組み合わせ。省略時は abc]
  例: python -u scripts/load_test.py bc
実験Bは5000大会ぶんのデータ作成に1〜2分かかる。
"""
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP_DIR = tempfile.TemporaryDirectory(prefix="mahjong_load_test_", ignore_cleanup_errors=True)
os.environ["DB_PATH"] = str(Path(_TMP_DIR.name) / "load_test.db")
sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import table_logic  # noqa: E402
import tournament_service  # noqa: E402

assert Path(_TMP_DIR.name) in db.DB_PATH.parents, f"一時DB以外では実行しない: {db.DB_PATH}"

POS = ("東", "南", "西", "北")
SCORING = {
    "start_point": 30000, "return_point": 30000, "oka": 0, "tobi_amount": 0,
    "uma_table": {0: [15, 5, -5, -15], 1: [15, 5, -5, -15], 2: [15, 5, -5, -15],
                  3: [15, 5, -5, -15], 4: [15, 5, -5, -15]},
}


def timed(fn, repeat=1):
    best = None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    return best, out


# ---------- 実験A: CPU ----------
def experiment_a():
    print("\n=== 実験A: 卓組み計算(CPU) ワンデー4半荘方式 ===")
    print("人数 | 卓数 | 2半荘目 | 3半荘目 | 4半荘目 (秒)")
    for n in (16, 40, 80, 120, 160, 200):
        rng = random.Random(1)
        members = list(range(1, n + 1))
        app, hist, co, prev = {}, {}, {}, set()
        times = []
        for h in (1, 2, 3, 4):
            t = time.perf_counter()
            absent, tables = table_logic.one_day_hanchan(members, h, app, prev, hist, co, rng)
            times.append(time.perf_counter() - t)
            present = [p for tb in tables for p in tb.values()]
            app = table_logic.update_appearance_counts(app, present)
            hist = table_logic.update_position_history(hist, tables)
            co = table_logic.update_co_seat_counts(co, tables)
            prev = set(absent)
        print(f"{n:4d} | {n // 4:4d} | {times[1]:7.2f} | {times[2]:7.2f} | {times[3]:7.2f}")

    print("\n(参考) 蛇行方式")
    for n in (40, 200, 1000):
        rng = random.Random(1)
        members = list(range(1, n + 1))
        scores = {m: rng.randint(-50, 50) for m in members}
        dt, _ = timed(lambda: table_logic.snake_round(members, {}, set(), scores, rng), 3)
        print(f"{n:4d}人: {dt * 1000:.2f} ms")


# ---------- 実験B: データ量 ----------
def seed_tournaments(count, players=40, rounds=4):
    """過去の大会を count 件、直接SQLでまとめて作る（全員分の成績入り）。"""
    now = "2026-10-04T00:00:00"
    conn = sqlite3.connect(db.DB_PATH)
    cur = conn.cursor()
    import json
    last_tid = None
    for _ in range(count):
        cur.execute("INSERT INTO tenants (name, created_at) VALUES ('t', ?)", (now,))
        tenant_id = cur.lastrowid
        member_ids = []
        for i in range(players):
            cur.execute(
                "INSERT INTO members (tenant_id, name, memo, created_at) VALUES (?, ?, '', ?)",
                (tenant_id, f"m{i}", now),
            )
            member_ids.append(cur.lastrowid)
        cur.execute(
            "INSERT INTO tournaments (tenant_id, name, kind, table_method, scoring_mode, scoring_config,"
            " numbering_method, status, created_at) VALUES (?, 'x', 'ワンデー', '蛇行', '得点', ?, '受付順', '進行中', ?)",
            (tenant_id, json.dumps(SCORING), now),
        )
        tid = cur.lastrowid
        last_tid = tid
        cur.executemany(
            "INSERT INTO tournament_members (tournament_id, member_id, player_number, created_at) VALUES (?,?,?,?)",
            [(tid, m, i + 1, now) for i, m in enumerate(member_ids)],
        )
        for r in range(1, rounds + 1):
            cur.execute("INSERT INTO rounds (tournament_id, round_number, created_at) VALUES (?,?,?)", (tid, r, now))
            rid = cur.lastrowid
            shuffled = member_ids[:]
            random.shuffle(shuffled)
            for t in range(players // 4):
                cur.execute("INSERT INTO tables (round_id, table_number) VALUES (?,?)", (rid, t + 1))
                table_id = cur.lastrowid
                four = shuffled[t * 4:t * 4 + 4]
                cur.executemany(
                    "INSERT INTO seats (table_id, member_id, position) VALUES (?,?,?)",
                    [(table_id, m, POS[i]) for i, m in enumerate(four)],
                )
                cur.executemany(
                    "INSERT INTO results (round_id, member_id, raw_score, created_at, input_source) VALUES (?,?,?,?, 'admin')",
                    [(rid, m, s, now) for m, s in zip(four, (45000, 32000, 25000, 18000))],
                )
    conn.commit()
    conn.close()
    return last_tid


def measure_screens(tid):
    tournament = sqlite3.connect(db.DB_PATH).execute(
        "SELECT tenant_id FROM tournaments WHERE id = ?", (tid,)).fetchone()
    tournament = db.get_tournament(tournament[0], tid)
    last_round = db.get_rounds(tid)[-1]["id"]

    def guest_page():  # ゲスト画面1回の描き直しで走る読み込み（app.pyのゲスト画面相当）
        db.get_tournament_members(tid)
        db.get_rounds(tid)
        db.get_round_seats(last_round)
        db.get_round_absences(last_round)
        db.get_round_results(last_round)
        db.get_round_tobi_busters(last_round)

    g, _ = timed(guest_page, 3)
    s, _ = timed(lambda: tournament_service.compute_standings(tournament), 3)
    n, _ = timed(lambda: tournament_service.run_next_round(tournament, random.Random(1)), 3)
    one, _ = timed(lambda: db.get_round_seats(last_round), 3)
    return g, s, n, one


def experiment_b():
    print("\n=== 実験B: データが積み上がったときの画面読み込み(DB) ===")
    print("1大会 = 40人×4回戦（着席160行）。自分の大会の大きさは変えず、DB全体だけ増やす。")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DROP INDEX IF EXISTS idx_seats_table")  # 修正前(索引なし)のDBを再現
    conn.commit()
    conn.close()
    print("過去の大会数 | 着席の総行数 | ゲスト画面 | 順位表 | 次回戦の卓組み | 着席1回読み (ミリ秒)")
    total = 0
    rows = []
    for target in (1, 100, 1000, 5000):
        tid = seed_tournaments(target - total)
        total = target
        g, s, n, one = measure_screens(tid)
        seats = sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM seats").fetchone()[0]
        rows.append((target, seats, g, s, n, one))
        print(f"{target:12d} | {seats:12d} | {g * 1000:10.1f} | {s * 1000:6.1f} | {n * 1000:14.1f} | {one * 1000:8.2f}")

    conn = sqlite3.connect(db.DB_PATH)
    print("\n着席読み込みSQLの実行計画（索引なし）:")
    q = ("EXPLAIN QUERY PLAN SELECT t.table_number, s.position, s.member_id FROM seats s "
         "JOIN tables t ON t.id = s.table_id WHERE t.round_id = ? ORDER BY t.table_number ASC, s.id ASC")
    for r in conn.execute(q, (1,)):
        print("   ", r[3])

    print("\n--- 既存DBへの適用: この5000大会入りのDBに init_db() を実行して索引を後付けし、測り直す ---")
    count_tables = ("seats", "results", "rounds")
    before_counts = [conn.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0] for tb in count_tables]
    t = time.perf_counter()
    db.init_db()
    print(f"init_db()(索引作成を含む)にかかった時間: {(time.perf_counter() - t) * 1000:.0f} ms")
    after_counts = [conn.execute(f"SELECT COUNT(*) FROM {tb}").fetchone()[0] for tb in count_tables]
    print(f"行数{count_tables} 適用前 {before_counts} → 適用後 {after_counts}")
    assert before_counts == after_counts
    conn.close()
    conn = sqlite3.connect(db.DB_PATH)  # 実行計画は開き直した接続で見る(古い接続は前の計画を覚えている)
    print("着席読み込みSQLの実行計画（索引あり）:")
    for r in conn.execute(q, (1,)):
        print("   ", r[3])
    conn.close()
    g, s, n, one = measure_screens(tid)
    print(f"{'5000(索引あり)':>12} | {rows[-1][1]:12d} | {g * 1000:10.1f} | {s * 1000:6.1f} | {n * 1000:14.1f} | {one * 1000:8.2f}")
    return tid, rows[-1], (g, s, n, one)


# ---------- 実験C: 同時アクセス ----------
def make_live_round(players):
    """成績が未入力の回戦を1つ持つ大会を作り、(tid, round_id, 卓ごとのmember_id) を返す。"""
    import json
    now = "2026-10-04T00:00:00"
    conn = sqlite3.connect(db.DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO tenants (name, created_at) VALUES ('live', ?)", (now,))
    tenant_id = cur.lastrowid
    mids = []
    for i in range(players):
        cur.execute("INSERT INTO members (tenant_id, name, memo, created_at) VALUES (?,?, '', ?)", (tenant_id, f"p{i}", now))
        mids.append(cur.lastrowid)
    cur.execute(
        "INSERT INTO tournaments (tenant_id, name, kind, table_method, scoring_mode, scoring_config,"
        " numbering_method, status, created_at) VALUES (?, 'live', 'ワンデー', '蛇行', '得点', ?, '受付順', '進行中', ?)",
        (tenant_id, json.dumps(SCORING), now))
    tid = cur.lastrowid
    cur.executemany("INSERT INTO tournament_members (tournament_id, member_id, player_number, created_at) VALUES (?,?,?,?)",
                    [(tid, m, i + 1, now) for i, m in enumerate(mids)])
    cur.execute("INSERT INTO rounds (tournament_id, round_number, created_at) VALUES (?,1,?)", (tid, now))
    rid = cur.lastrowid
    groups = []
    for t in range(players // 4):
        cur.execute("INSERT INTO tables (round_id, table_number) VALUES (?,?)", (rid, t + 1))
        table_id = cur.lastrowid
        four = mids[t * 4:t * 4 + 4]
        cur.executemany("INSERT INTO seats (table_id, member_id, position) VALUES (?,?,?)",
                        [(table_id, m, POS[i]) for i, m in enumerate(four)])
        groups.append(four)
    conn.commit()
    conn.close()
    return tid, rid, groups


def experiment_c(label, init_every_rerun):
    mode = "修正前: 描き直しのたびにinit_db()" if init_every_rerun else "修正後: init_db()は起動時に1回だけ"
    print(f"\n=== 実験C: ゲストの一斉送信(DB同時書き込み) {label} [{mode}] ===")
    print("各卓の代表が同時に「画面を開く→成績を送信→画面が再描画」を行う。1卓=1スレッド。")
    print("同時卓数 | 中央値 | 最大 | 失敗(locked) | 全員が終わるまで (秒)")
    for tables in (8, 50, 200):
        tid, rid, groups = make_live_round(tables * 4)
        lat, errors = [], []
        barrier = threading.Barrier(tables)

        def guest(four):
            def page():
                if init_every_rerun:
                    db.init_db()
                db.get_tournament_members(tid)
                db.get_rounds(tid)
                db.get_round_seats(rid)
                db.get_round_results(rid)
                db.get_round_tobi_busters(rid)
            barrier.wait()
            t = time.perf_counter()
            try:
                page()
                db.submit_guest_round_results(rid, dict(zip(four, (45000, 32000, 25000, 18000))), {})
                page()
            except sqlite3.OperationalError as exc:
                errors.append(str(exc))
            lat.append(time.perf_counter() - t)

        threads = [threading.Thread(target=guest, args=(g,)) for g in groups]
        t0 = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        wall = time.perf_counter() - t0
        print(f"{tables:8d} | {statistics.median(lat):6.2f} | {max(lat):5.2f} | {len(errors):12d} | {wall:6.2f}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "abc"
    db.init_db()
    if "a" in which:
        experiment_a()
    if "b" in which:
        experiment_b()
    if "c" in which:
        label = "(5000大会ぶん積んだDB・索引あり)" if "b" in which else "(小さいDB)"
        experiment_c(label, init_every_rerun=True)
        experiment_c(label, init_every_rerun=False)
    _TMP_DIR.cleanup()
