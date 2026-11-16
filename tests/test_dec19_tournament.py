"""12/19大会向け実装依頼（5半荘・蛇行方式・全員まとめて順位ポイント）のテスト。

- 全員まとめて順位ポイント（8名なら8〜1点、12名なら12〜1点、同点は折半）
- 第2〜4半荘の蛇行配置、第5半荘（最終回戦）の順位順着席
- 一部の卓だけ入力済みの回戦は累計に寄与しない
- 5半荘通しのシミュレーションと最終順位（1卓1位が優勝、2位以下は最終回戦の素点順）
"""
import random

import pytest

import db
import scoring_logic
import table_logic
import tournament_service as svc


# ---------------------------------------------------------------------------
# scoring_logic: 全員まとめて順位ポイント
# ---------------------------------------------------------------------------

def test_default_overall_point_table():
    assert scoring_logic.default_overall_point_table(8) == [8, 7, 6, 5, 4, 3, 2, 1]
    assert scoring_logic.default_overall_point_table(12) == list(range(12, 0, -1))


def test_overall_points_8_players():
    raw = {1: 50000, 2: 20000, 3: 40000, 4: 30000, 5: 45000, 6: 25000, 7: 35000, 8: 15000}
    points = scoring_logic.compute_rank_points_overall(raw, [8, 7, 6, 5, 4, 3, 2, 1])
    assert points == {1: 8, 5: 7, 3: 6, 7: 5, 4: 4, 6: 3, 2: 2, 8: 1}


def test_overall_points_12_players():
    raw = {pn: 1000 * (20 + pn) for pn in range(1, 13)}  # 選手12が最高、選手1が最低
    points = scoring_logic.compute_rank_points_overall(raw, list(range(12, 0, -1)))
    assert points == {pn: pn for pn in range(1, 13)}


def test_overall_points_tie_is_split():
    # 2位と3位が同点 → (7+6)/2 = 6.5 ずつ。卓ごと方式と同じ折半
    raw = {1: 50000, 2: 40000, 3: 40000, 4: 30000, 5: 29000, 6: 28000, 7: 27000, 8: 26000}
    points = scoring_logic.compute_rank_points_overall(raw, [8, 7, 6, 5, 4, 3, 2, 1])
    assert points[1] == 8
    assert points[2] == points[3] == 6.5
    assert points[4] == 5
    assert sum(points.values()) == 36


def test_overall_points_three_way_tie_at_bottom():
    raw = {1: 60000, 2: 50000, 3: 40000, 4: 30000, 5: 20000, 6: 10000, 7: 10000, 8: 10000}
    points = scoring_logic.compute_rank_points_overall(raw, [8, 7, 6, 5, 4, 3, 2, 1])
    assert points[6] == points[7] == points[8] == 2


def test_overall_points_rejects_table_length_mismatch():
    with pytest.raises(ValueError):
        scoring_logic.compute_rank_points_overall({1: 1, 2: 2}, [3, 2, 1])


def test_final_ranking_champion_is_table1_top_then_raw_score_order():
    # 卓1: 1,2,3,4 / 卓2: 5,6,7,8。卓2の選手5が全体最高点でも、優勝は卓1の1位（選手2）
    tables = [[1, 2, 3, 4], [5, 6, 7, 8]]
    raw = {1: 30000, 2: 40000, 3: 25000, 4: 25000,
           5: 60000, 6: 20000, 7: 10000, 8: 30000}
    order = scoring_logic.final_ranking(tables, raw)
    assert order[0] == 2
    # 2位以下は素点順。同点（1と8が30000、3と4が25000）は選手番号の若い方が上位
    assert order[1:] == [5, 1, 8, 3, 4, 6, 7]


def test_final_ranking_table1_tie_uses_player_number():
    tables = [[3, 1, 2, 4], [5, 6, 7, 8]]
    raw = {1: 40000, 3: 40000, 2: 20000, 4: 20000, 5: 35000, 6: 35000, 7: 15000, 8: 15000}
    assert scoring_logic.final_ranking(tables, raw)[0] == 1


# ---------------------------------------------------------------------------
# table_logic: 蛇行配置と最終回戦の順位順着席
# ---------------------------------------------------------------------------

def test_snake_tables_8_players_match_request_example():
    tables = table_logic.snake_tables(list(range(1, 9)))  # 選手番号＝累計順位
    assert tables == [
        {"東": 1, "南": 4, "西": 5, "北": 8},
        {"東": 2, "南": 3, "西": 6, "北": 7},
    ]


def test_snake_tables_12_players_match_request_example():
    tables = table_logic.snake_tables(list(range(1, 13)))
    assert tables == [
        {"東": 1, "南": 6, "西": 7, "北": 12},
        {"東": 2, "南": 5, "西": 8, "北": 11},
        {"東": 3, "南": 4, "西": 9, "北": 10},
    ]


def test_ranked_order_tables_8_and_12():
    assert table_logic.ranked_order_tables(list(range(1, 9))) == [
        {"東": 1, "南": 2, "西": 3, "北": 4},
        {"東": 5, "南": 6, "西": 7, "北": 8},
    ]
    assert table_logic.ranked_order_tables(list(range(1, 13))) == [
        {"東": 1, "南": 2, "西": 3, "北": 4},
        {"東": 5, "南": 6, "西": 7, "北": 8},
        {"東": 9, "南": 10, "西": 11, "北": 12},
    ]


def test_snake_round_final_round_seats_in_rank_order_with_ties_by_player_number():
    members = list(range(1, 9))
    # 累計: 選手8が最高。選手3と選手5が同点 → 選手番号の若い3が上
    cumulative = {8: 30, 7: 25, 3: 20, 5: 20, 1: 10, 2: 9, 4: 8, 6: 1}
    _, tables = table_logic.snake_round(members, {}, set(), cumulative, random.Random(0), final_round=True)
    assert tables == [
        {"東": 8, "南": 7, "西": 3, "北": 5},
        {"東": 1, "南": 2, "西": 4, "北": 6},
    ]
    _, snake = table_logic.snake_round(members, {}, set(), cumulative, random.Random(0))
    assert snake == [
        {"東": 8, "南": 5, "西": 1, "北": 6},
        {"東": 7, "南": 3, "西": 2, "北": 4},
    ]


# ---------------------------------------------------------------------------
# tournament_service: DBを通した計算
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


def _setup(n, scoring_config):
    uid = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(n)]
    tid = db.create_tournament(
        tenant_id, "12/19大会", "ワンデー", "蛇行", "ポイント", scoring_config, "受付順"
    )
    for member_id in member_ids:
        db.add_tournament_member(tid, member_id, db.next_tournament_player_number(tid))
    return tenant_id, tid


OVERALL_CONFIG = {
    "rank_point_table": [3, 1, -1, -3],
    "start_point": 25000,
    "rank_scope": "全員まとめて",
    "total_rounds": 5,
}


def _seats_by_number(tid, round_id):
    """回戦の着席を [{"東": 選手番号, ...}, ...]（卓番号順）で返す。"""
    member_to_number = {m["member_id"]: m["player_number"] for m in db.get_tournament_members(tid)}
    tables = {}
    for seat in db.get_round_seats(round_id):
        tables.setdefault(seat["table_number"], {})[seat["position"]] = member_to_number[seat["member_id"]]
    return [tables[k] for k in sorted(tables)]


def _number_to_member(tid):
    return {m["player_number"]: m["member_id"] for m in db.get_tournament_members(tid)}


def test_existing_tournament_without_rank_scope_is_per_table(temp_db):
    tenant_id, tid = _setup(8, {"rank_point_table": [3, 1, -1, -3], "start_point": 25000})
    tournament = db.get_tournament(tenant_id, tid)
    assert svc.get_rank_scope(tournament) == "卓ごと"
    assert svc.get_total_rounds(tournament) is None


def test_overall_round_gives_8_to_1_only_when_all_tables_entered(temp_db):
    tenant_id, tid = _setup(8, OVERALL_CONFIG)
    tournament = db.get_tournament(tenant_id, tid)
    round_number, absent, tables = svc.run_next_round(tournament, random.Random(1))
    round_id = svc.save_confirmed_round(tid, round_number, absent, tables)
    to_member = _number_to_member(tid)

    # 卓1だけ入力 → 寄与0
    table1 = [to_member[pn] for pn in tables[0].values()]
    db.save_round_results(round_id, dict(zip(table1, [40000, 30000, 20000, 10000])), {})
    assert svc.compute_round_totals(tournament, round_id) == {}
    assert svc.build_cumulative_scores(tournament) == {}

    # 卓2も入力 → 全員まとめて8〜1点
    table2 = [to_member[pn] for pn in tables[1].values()]
    db.save_round_results(round_id, dict(zip(table2, [45000, 35000, 15000, 5000])), {})
    totals = svc.compute_round_totals(tournament, round_id)
    expected_order = [table2[0], table1[0], table2[1], table1[1], table1[2], table2[2], table1[3], table2[3]]
    assert [totals[mid] for mid in expected_order] == [8, 7, 6, 5, 4, 3, 2, 1]


def test_overall_uses_custom_point_table_and_falls_back_on_mismatch(temp_db):
    config = dict(OVERALL_CONFIG, overall_rank_point_table=[20, 15, 10, 8, 6, 4, 2, 0])
    tenant_id, tid = _setup(8, config)
    tournament = db.get_tournament(tenant_id, tid)
    assert svc.overall_point_table_for(tournament, 8) == [20, 15, 10, 8, 6, 4, 2, 0]
    assert not svc.overall_point_table_mismatch(tournament)

    tournament["scoring_config"]["overall_rank_point_table"] = [3, 2, 1]
    assert svc.overall_point_table_for(tournament, 8) == [8, 7, 6, 5, 4, 3, 2, 1]
    assert svc.overall_point_table_mismatch(tournament)


def _random_table_scores(rng, start_point=25000):
    """1卓分の素点（100点単位・合計は開始点数×4）。ときどき同点を混ぜる。"""
    while True:
        a = rng.randrange(-100, 700) * 100
        b = rng.randrange(-100, 700) * 100
        c = a if rng.random() < 0.2 else rng.randrange(-100, 700) * 100
        d = start_point * 4 - a - b - c
        if -20000 <= d <= 80000:
            return [a, b, c, d]


def _expected_overall_points(scores_by_number):
    """全員まとめて・同点折半の順位ポイントを、テスト側で独立に計算する。"""
    n = len(scores_by_number)
    result = {}
    for pn, score in scores_by_number.items():
        higher = sum(1 for s in scores_by_number.values() if s > score)
        same = sum(1 for s in scores_by_number.values() if s == score)
        # 順位 higher+1 〜 higher+same の点（n - 順位 + 1）の平均
        result[pn] = sum(n - r + 1 for r in range(higher + 1, higher + same + 1)) / same
    return result


@pytest.mark.parametrize("n,seed", [(8, 3), (8, 11), (12, 5), (12, 19)])
def test_five_hanchan_simulation(temp_db, n, seed):
    tenant_id, tid = _setup(n, OVERALL_CONFIG)
    rng = random.Random(seed)
    to_member = _number_to_member(tid)
    cumulative = {}

    for round_number in range(1, 6):
        tournament = db.get_tournament(tenant_id, tid)
        rn, absent, tables = svc.run_next_round(tournament, rng)
        assert rn == round_number and absent == []
        round_id = svc.save_confirmed_round(tid, rn, absent, tables)
        seats = _seats_by_number(tid, round_id)

        if round_number >= 2:
            ranked = sorted(range(1, n + 1), key=lambda pn: (-cumulative.get(pn, 0), pn))
            if round_number == 5:
                expected = table_logic.ranked_order_tables(ranked)
            else:
                expected = table_logic.snake_tables(ranked)
            assert seats == expected, f"第{round_number}回戦の席"
            if round_number == 5:
                assert set(seats[0].values()) == set(ranked[:4])  # 1卓は累計1〜4位

        scores_by_number = {}
        for table in seats:
            for pn, score in zip(table.values(), _random_table_scores(rng)):
                scores_by_number[pn] = score
        db.save_round_results(round_id, {to_member[pn]: s for pn, s in scores_by_number.items()}, {})

        # 入力済みの回戦の累計は、テスト側で独立に計算した値と一致する
        for pn, v in _expected_overall_points(scores_by_number).items():
            cumulative[pn] = cumulative.get(pn, 0) + v
        tournament = db.get_tournament(tenant_id, tid)
        assert svc.build_cumulative_scores(tournament) == pytest.approx(cumulative)

        final = svc.compute_final_ranking(tournament)
        if round_number < 5:
            assert final is None
        else:
            final_order = [row["player_number"] for row in final]
            table1 = list(seats[0].values())
            champion = sorted(table1, key=lambda pn: (-scores_by_number[pn], pn))[0]
            rest = sorted((pn for pn in scores_by_number if pn != champion),
                          key=lambda pn: (-scores_by_number[pn], pn))
            assert final_order == [champion] + rest
            assert final[0]["table_number"] == 1
            assert len(final) == n


def test_final_ranking_hidden_while_final_round_partially_entered(temp_db):
    config = dict(OVERALL_CONFIG, total_rounds=1)
    tenant_id, tid = _setup(8, config)
    tournament = db.get_tournament(tenant_id, tid)
    rn, absent, tables = svc.run_next_round(tournament, random.Random(2))
    round_id = svc.save_confirmed_round(tid, rn, absent, tables)
    to_member = _number_to_member(tid)
    table1 = [to_member[pn] for pn in tables[0].values()]
    db.save_round_results(round_id, dict(zip(table1, [40000, 30000, 20000, 10000])), {})
    assert svc.compute_final_ranking(tournament) is None


def test_without_total_rounds_round5_is_still_snake(temp_db):
    config = {k: v for k, v in OVERALL_CONFIG.items() if k != "total_rounds"}
    tenant_id, tid = _setup(8, config)
    rng = random.Random(7)
    to_member = _number_to_member(tid)
    for round_number in range(1, 6):
        tournament = db.get_tournament(tenant_id, tid)
        rn, absent, tables = svc.run_next_round(tournament, rng)
        round_id = svc.save_confirmed_round(tid, rn, absent, tables)
        seats = _seats_by_number(tid, round_id)
        if round_number == 5:
            cumulative = svc.build_cumulative_scores(tournament)
            ranked = sorted(range(1, 9), key=lambda pn: (-cumulative.get(pn, 0), pn))
            assert seats == table_logic.snake_tables(ranked)
            assert svc.compute_final_ranking(tournament) is None
        scores = {}
        for table in seats:
            for pn, s in zip(table.values(), [40000, 30000, 20000, 10000]):
                scores[to_member[pn]] = s
        db.save_round_results(round_id, scores, {})


def test_per_table_tournament_with_total_rounds_seats_final_round_in_rank_order(temp_db):
    # 卓ごと方式でも、全回戦数を入れれば最終回戦は順位順着席になる
    config = {"rank_point_table": [3, 1, -1, -3], "start_point": 25000, "total_rounds": 2}
    tenant_id, tid = _setup(8, config)
    rng = random.Random(4)
    to_member = _number_to_member(tid)
    tournament = db.get_tournament(tenant_id, tid)
    rn, absent, tables = svc.run_next_round(tournament, rng)
    round_id = svc.save_confirmed_round(tid, rn, absent, tables)
    scores = {}
    for table in _seats_by_number(tid, round_id):
        for pn, s in zip(table.values(), [40000, 30000, 20000, 10000]):
            scores[to_member[pn]] = s
    db.save_round_results(round_id, scores, {})
    tournament = db.get_tournament(tenant_id, tid)
    cumulative = svc.build_cumulative_scores(tournament)
    assert sorted(cumulative.values()) == [-3, -3, -1, -1, 1, 1, 3, 3]  # 卓ごとの点のまま
    _, _, tables2 = svc.run_next_round(tournament, rng)
    ranked = sorted(range(1, 9), key=lambda pn: (-cumulative[pn], pn))
    assert tables2 == table_logic.ranked_order_tables(ranked)
