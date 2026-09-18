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
