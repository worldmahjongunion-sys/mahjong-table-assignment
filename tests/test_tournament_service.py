"""大会管理とゲスト共有リンク実装依頼.md 4章のテスト観点のうち、tournament_service.py
（DB保存済みレコード→table_logic.py/scoring_logic.pyの入力形式への状態再構築ロジック）
を検証する。"""
import random

import pytest

import db
import tournament_service as svc


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


SCORE_CONFIG = {
    "start_point": 35000,
    "return_point": 40000,
    "oka": 20000,
    "tobi_amount": 0,
    "uma_table": {
        1: [48000, -8000, -16000, -24000],
        2: [24000, 8000, -8000, -24000],
        3: [12000, 8000, 4000, -24000],
    },
}
POINT_CONFIG = {"rank_point_table": [3, 1, -1, -3]}


def _setup_snake_tournament(n=8):
    uid = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(n)]
    tid = db.create_tournament(
        tenant_id, "蛇行大会", "シーズン", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    for member_id in member_ids:
        pn = db.next_tournament_player_number(tid)
        db.add_tournament_member(tid, member_id, pn)
    return tenant_id, tid, member_ids


def _play_and_save_round(tenant_id, tid, rng, raw_score_by_rank=(45000, 35000, 30000, 25000)):
    tournament = db.get_tournament(tenant_id, tid)
    round_number, absent, tables = svc.run_next_round(tournament, rng)
    round_id = svc.save_confirmed_round(tid, round_number, absent, tables)
    seats = db.get_round_seats(round_id)
    raw_scores = {}
    for table_number in sorted({s["table_number"] for s in seats}):
        members_at_table = [s["member_id"] for s in seats if s["table_number"] == table_number]
        for member_id, score in zip(members_at_table, raw_score_by_rank):
            raw_scores[member_id] = score
    db.save_round_results(round_id, raw_scores, {})
    return round_number, absent, tables, round_id


# ---------------------------------------------------------------------------
# 状態再構築ロジック
# ---------------------------------------------------------------------------

def test_appearance_counts_accumulate_across_rounds(temp_db):
    tenant_id, tid, member_ids = _setup_snake_tournament(n=6)  # T=1, R=2
    rng = random.Random(1)
    _play_and_save_round(tenant_id, tid, rng)
    _play_and_save_round(tenant_id, tid, rng)

    counts = svc.build_appearance_counts(tid)
    # 6人・T=1・R=2なので、2回戦で延べ8人分の出場（4人×2回戦）
    assert sum(counts.values()) == 8
    values = list(counts.values())
    assert max(values) - min(values) <= 1  # 抜け番均等化により出場回数の差は1以内


def test_previous_absent_reflects_only_the_last_round(temp_db):
    tenant_id, tid, member_ids = _setup_snake_tournament(n=6)
    rng = random.Random(2)
    _play_and_save_round(tenant_id, tid, rng)
    _, absent2, _, _ = _play_and_save_round(tenant_id, tid, rng)

    previous_absent = svc.build_previous_absent(tid)
    assert previous_absent == set(absent2)


def test_previous_absent_is_empty_before_any_round(temp_db):
    tenant_id, tid, member_ids = _setup_snake_tournament(n=8)
    assert svc.build_previous_absent(tid) == set()


def test_second_round_uses_first_round_cumulative_scores_for_snake_rank(temp_db):
    tenant_id, tid, member_ids = _setup_snake_tournament(n=8)  # T=2, R=0
    rng = random.Random(3)
    round1, absent1, tables1, round_id1 = _play_and_save_round(tenant_id, tid, rng)
    assert round1 == 1
    assert absent1 == []

    tournament = db.get_tournament(tenant_id, tid)
    cumulative = svc.build_cumulative_scores(tournament)
    # 1回戦で使った選手番号は全員cumulativeに反映されている
    member_to_number = {m["member_id"]: m["player_number"] for m in db.get_tournament_members(tid)}
    assert set(cumulative.keys()) == set(member_to_number.values())

    round2, absent2, tables2, round_id2 = _play_and_save_round(tenant_id, tid, rng)
    assert round2 == 2
    # 2回戦目の卓組みは1回戦の累計成績順を使うため、少なくとも卓組みが実行できていること
    assert len(tables2) == 2


def test_position_history_and_co_seat_counts_accumulate_for_one_day_method(temp_db):
    uid = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(8)]
    tid = db.create_tournament(
        tenant_id, "ワンデー大会", "ワンデー", "ワンデー4半荘", "ポイント", POINT_CONFIG, "くじ引き"
    )
    for i, member_id in enumerate(member_ids):
        db.add_tournament_member(tid, member_id, 100 + i)

    rng = random.Random(4)
    for _ in range(4):
        tournament = db.get_tournament(tenant_id, tid)
        round_number, absent, tables = svc.run_next_round(tournament, rng)
        round_id = svc.save_confirmed_round(tid, round_number, absent, tables)
        seats = db.get_round_seats(round_id)
        raw_scores = {}
        for table_number in sorted({s["table_number"] for s in seats}):
            members_at_table = [s["member_id"] for s in seats if s["table_number"] == table_number]
            for member_id, score in zip(members_at_table, (45000, 35000, 30000, 25000)):
                raw_scores[member_id] = score
        db.save_round_results(round_id, raw_scores, {})

    position_history = svc.build_position_history(tid)
    # 8人・T=2・R=0で4半荘フル出場なので、全員が東南西北の4方位をちょうど1回ずつ経験
    for positions in position_history.values():
        assert positions == {"東", "南", "西", "北"}

    co_seat_counts = svc.build_co_seat_counts(tid)
    assert sum(co_seat_counts.values()) > 0  # 何らかの同卓ペアが記録されている


# ---------------------------------------------------------------------------
# 成績集計（scoring_logic.pyとの橋渡し）
# ---------------------------------------------------------------------------

def test_compute_round_totals_score_mode_applies_uma(temp_db):
    tenant_id, tid, member_ids = _setup_snake_tournament(n=4)  # T=1, R=0, 1卓のみ
    tournament = db.get_tournament(tenant_id, tid)
    round_number, absent, tables = svc.run_next_round(tournament, random.Random(5))
    round_id = svc.save_confirmed_round(tid, round_number, absent, tables)

    seats = db.get_round_seats(round_id)
    raw_scores = {s["member_id"]: score for s, score in zip(seats, (50000, 30000, 30000, -10000))}
    db.save_round_results(round_id, raw_scores, {seats[3]["member_id"]: [seats[0]["member_id"]]})

    tournament = db.get_tournament(tenant_id, tid)
    totals = svc.compute_round_totals(tournament, round_id)

    assert set(totals.keys()) == set(raw_scores.keys())
    # 飛び賞なし(tobi_amount=0)なので、totalsはウマ適用後もtobi_amountの影響を受けない
    assert sum(totals.values()) == pytest.approx(sum(raw_scores.values()))


def test_compute_round_totals_point_mode_uses_rank_points(temp_db):
    uid = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(4)]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "ワンデー4半荘", "ポイント", POINT_CONFIG, "受付順"
    )
    for member_id in member_ids:
        pn = db.next_tournament_player_number(tid)
        db.add_tournament_member(tid, member_id, pn)

    tournament = db.get_tournament(tenant_id, tid)
    round_number, absent, tables = svc.run_next_round(tournament, random.Random(6))
    round_id = svc.save_confirmed_round(tid, round_number, absent, tables)
    seats = db.get_round_seats(round_id)
    raw_scores = {s["member_id"]: score for s, score in zip(seats, (50000, 30000, 25000, -5000))}
    db.save_round_results(round_id, raw_scores, {})

    totals = svc.compute_round_totals(tournament, round_id)
    assert sorted(totals.values(), reverse=True) == [3, 1, -1, -3]  # 同着なしの順位ポイント


def test_compute_standings_orders_by_cumulative_score_descending(temp_db):
    tenant_id, tid, member_ids = _setup_snake_tournament(n=4)
    rng = random.Random(7)
    _play_and_save_round(tenant_id, tid, rng, raw_score_by_rank=(60000, 40000, 30000, 10000))

    tournament = db.get_tournament(tenant_id, tid)
    standings = svc.compute_standings(tournament)

    values = [row["value"] for row in standings]
    assert values == sorted(values, reverse=True)
    assert len(standings) == 4


def test_compute_standings_point_mode_tiebreaks_by_raw_score_then_player_number(temp_db):
    uid = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(4)]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "ワンデー4半荘", "ポイント", POINT_CONFIG, "受付順"
    )
    for member_id in member_ids:
        pn = db.next_tournament_player_number(tid)
        db.add_tournament_member(tid, member_id, pn)

    tournament = db.get_tournament(tenant_id, tid)
    round_number, absent, tables = svc.run_next_round(tournament, random.Random(8))
    round_id = svc.save_confirmed_round(tid, round_number, absent, tables)
    seats = db.get_round_seats(round_id)
    # 1位・2位を同点にして、素点合計によるタイブレークを誘発する
    raw_scores = {s["member_id"]: score for s, score in zip(seats, (40000, 40000, 30000, 10000))}
    db.save_round_results(round_id, raw_scores, {})

    tournament = db.get_tournament(tenant_id, tid)
    standings = svc.compute_standings(tournament)
    top_two_ids = {standings[0]["member_id"], standings[1]["member_id"]}
    assert top_two_ids == {seats[0]["member_id"], seats[1]["member_id"]}


# ---------------------------------------------------------------------------
# 複数卓の回戦: 順位付け・ウマ・オカ・飛び賞は「卓ごと」に行う
#
# 既存の_play_and_save_roundは全卓に同じ素点を入れるため、回戦全員をまとめて順位付け
# しても「卓ごとに同着」となり、卓をまたいだ誤りが表に出なかった。ここでは卓ごとに
# 素点の水準を大きく変え（卓2は卓1より全員低い）、卓をまたいだ順位付けなら結果が
# 必ず食い違うようにしている。
# ---------------------------------------------------------------------------

def _setup_two_table_round(scoring_mode, scoring_config, table_method="蛇行"):
    """8人・2卓の第1回戦を保存し、(tournament, round_id, 卓1の4人, 卓2の4人)を返す。
    卓ごとのmember_idは着席順（＝以降のテストで素点を割り当てる順）。"""
    uid = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(8)]
    tid = db.create_tournament(
        tenant_id, "2卓大会", "シーズン", table_method, scoring_mode, scoring_config, "受付順"
    )
    for member_id in member_ids:
        db.add_tournament_member(tid, member_id, db.next_tournament_player_number(tid))
    tournament = db.get_tournament(tenant_id, tid)
    round_number, absent, tables = svc.run_next_round(tournament, random.Random(11))
    round_id = svc.save_confirmed_round(tid, round_number, absent, tables)
    seats = db.get_round_seats(round_id)
    table1 = [s["member_id"] for s in seats if s["table_number"] == 1]
    table2 = [s["member_id"] for s in seats if s["table_number"] == 2]
    assert len(table1) == len(table2) == 4
    return tournament, round_id, table1, table2


def _save_scores(round_id, members, scores, tobi_busters=None):
    """membersに素点scoresを割り当てて保存する（保存済みの成績は残す）。"""
    saved = db.get_round_results(round_id)
    saved_busters = db.get_round_tobi_busters(round_id)
    saved.update(dict(zip(members, scores)))
    saved_busters.update(tobi_busters or {})
    db.save_round_results(round_id, saved, saved_busters)


def test_point_mode_ranks_each_table_separately(temp_db):
    """8人2卓: 卓1・卓2それぞれの中で1〜4位を決める。卓2の全員が卓1より低い素点でも、
    卓2の1位は「回戦5位」ではなく卓内1位として3ポイントを得る。"""
    tournament, round_id, table1, table2 = _setup_two_table_round("ポイント", POINT_CONFIG)
    _save_scores(round_id, table1, (50000, 30000, 20000, 10000))
    _save_scores(round_id, table2, (8000, 6000, 4000, 2000))

    totals = svc.compute_round_totals(tournament, round_id)

    assert [totals[m] for m in table1] == [3, 1, -1, -3]
    assert [totals[m] for m in table2] == [3, 1, -1, -3]


def test_point_mode_tie_in_one_table_does_not_affect_the_other(temp_db):
    """同着の折半も卓内だけで行う。卓2の素点が卓1の同着と同じ値でも影響しない。"""
    tournament, round_id, table1, table2 = _setup_two_table_round("ポイント", POINT_CONFIG)
    _save_scores(round_id, table1, (40000, 40000, 20000, 10000))  # 卓1は1・2位同着
    _save_scores(round_id, table2, (40000, 30000, 20000, 10000))  # 卓2の1位は卓1と同じ素点

    totals = svc.compute_round_totals(tournament, round_id)

    assert [totals[m] for m in table1] == [2, 2, -1, -3]  # (3+1)/2の折半
    assert [totals[m] for m in table2] == [3, 1, -1, -3]  # 卓1の同着に巻き込まれない


def test_score_mode_applies_uma_oka_and_tobi_per_table(temp_db):
    """得点方式: 浮き人数（ウマ表の選択）・オカ・飛び賞も卓ごとに決まる。
    卓1は2人浮き、卓2は0人浮き（オカがトップに乗る）＋飛び賞あり。"""
    config = {**SCORE_CONFIG, "tobi_amount": 5000}  # 返し40000・オカ20000・ウマは2人浮きで[24000,8000,-8000,-24000]
    tournament, round_id, table1, table2 = _setup_two_table_round("得点", config)
    _save_scores(round_id, table1, (50000, 42000, 20000, 10000))
    # 卓2は全員返し点未満（0人浮き）。4人目が飛び、1人目が飛ばした
    _save_scores(round_id, table2, (36000, 34000, 32000, -2000), {table2[3]: [table2[0]]})

    totals = svc.compute_round_totals(tournament, round_id)

    assert [totals[m] for m in table1] == [
        50000 + 24000, 42000 + 8000, 20000 - 8000, 10000 - 24000,
    ]
    assert [totals[m] for m in table2] == [
        36000 + 20000 + 5000,  # 0人浮きのオカ＋飛ばした側の飛び賞
        34000,
        32000,
        -2000 - 5000,          # 飛んだ側の飛び賞
    ]


def test_uncompleted_table_is_not_scored(temp_db):
    """成績が未入力の卓は計算に含めない（入力済みの卓の結果は出る）。"""
    tournament, round_id, table1, table2 = _setup_two_table_round("ポイント", POINT_CONFIG)
    _save_scores(round_id, table1, (50000, 30000, 20000, 10000))

    totals = svc.compute_round_totals(tournament, round_id)

    assert set(totals) == set(table1)
    assert [totals[m] for m in table1] == [3, 1, -1, -3]


def test_cumulative_and_standings_are_per_table_across_rounds(temp_db):
    """累計・大会内順位表も卓ごとの値の積み上げになる（2回戦分）。"""
    tournament, round1, table1, table2 = _setup_two_table_round("ポイント", POINT_CONFIG)
    tid = tournament["id"]
    _save_scores(round1, table1, (50000, 30000, 20000, 10000))
    _save_scores(round1, table2, (8000, 6000, 4000, 2000))

    tournament = db.get_tournament(db.get_user_by_username("owner")["tenant_id"], tid)
    number_of = {m["member_id"]: m["player_number"] for m in db.get_tournament_members(tid)}

    expected_points = [3, 1, -1, -3]
    cumulative = svc.build_cumulative_scores(tournament)
    assert cumulative == {
        number_of[m]: pts for table in (table1, table2) for m, pts in zip(table, expected_points)
    }

    standings = svc.compute_standings(tournament)
    assert [row["value"] for row in standings] == [3, 3, 1, 1, -1, -1, -3, -3]
    # 同ポイントは素点合計で決まる（各順位とも卓1の方が素点が高い）
    assert [row["member_id"] for row in standings] == [
        table1[0], table2[0], table1[1], table2[1], table1[2], table2[2], table1[3], table2[3],
    ]


def test_snake_next_round_uses_per_table_cumulative_points(temp_db):
    """蛇行方式の次回戦の並び順: 卓ごとの順位ポイント（同ポイントは選手番号順）で1〜8位を
    決め、蛇行で卓A={1,4,5,8位}・卓B={2,3,6,7位}に振り分ける。"""
    tournament, round1, table1, table2 = _setup_two_table_round("ポイント", POINT_CONFIG)
    tid = tournament["id"]
    number_of = {m["member_id"]: m["player_number"] for m in db.get_tournament_members(tid)}
    _save_scores(round1, table1, (50000, 30000, 20000, 10000))
    _save_scores(round1, table2, (8000, 6000, 4000, 2000))

    # 3点・1点・-1点・-3点の組が卓1・卓2に1人ずつ。同点内は選手番号の若い順
    def by_number(a, b):
        return sorted([a, b], key=number_of.get)

    w_a, w_b = by_number(table1[0], table2[0])
    s_a, s_b = by_number(table1[1], table2[1])
    t_a, t_b = by_number(table1[2], table2[2])
    l_a, l_b = by_number(table1[3], table2[3])
    expected_tables = [
        {number_of[m] for m in (w_a, s_b, t_a, l_b)},
        {number_of[m] for m in (w_b, s_a, t_b, l_a)},
    ]

    tournament = db.get_tournament(db.get_user_by_username("owner")["tenant_id"], tid)
    round_number, absent, tables = svc.run_next_round(tournament, random.Random(12))

    assert round_number == 2
    assert absent == []
    assert [set(t.values()) for t in tables] == expected_tables
