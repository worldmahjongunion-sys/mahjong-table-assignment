"""得点ポイント評価方式実装依頼.md 4章「テスト」に対応するpytest。"""
import scoring_logic as sl


UMA_CONFIG = {
    "start_point": 35000,
    "return_point": 40000,
    "oka": 20000,
    "uma_table": {
        1: [48000, -8000, -16000, -24000],
        2: [24000, 8000, -8000, -24000],
        3: [12000, 8000, 4000, -24000],
    },
}


# ---------------------------------------------------------------------------
# ①得点評価: ウマ・オカ
# ---------------------------------------------------------------------------

def test_uma_table_1_floating():
    """1人浮き（1人だけ返し点超え）の場合のウマ表が適用される。"""
    raw_scores = {1: 45000, 2: 32000, 3: 33000, 4: 30000}  # 1のみ40000超え
    totals = sl.compute_total_score(raw_scores, UMA_CONFIG)
    # 順位: 1(45000)>3(33000)>2(32000)>4(30000) → ウマ +48000/-8000/-16000/-24000
    assert totals[1] == 45000 + 48000
    assert totals[3] == 33000 - 8000
    assert totals[2] == 32000 - 16000
    assert totals[4] == 30000 - 24000


def test_uma_table_2_floating():
    """2人浮きの場合のウマ表が適用される。"""
    raw_scores = {1: 50000, 2: 42000, 3: 28000, 4: 30000}  # 1,2が40000超え
    totals = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[1] == 50000 + 24000
    assert totals[2] == 42000 + 8000
    assert totals[4] == 30000 - 8000
    assert totals[3] == 28000 - 24000


def test_uma_table_3_floating():
    """3人浮きの場合のウマ表が適用される。"""
    raw_scores = {1: 50000, 2: 45000, 3: 41000, 4: 4000}
    totals = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[1] == 50000 + 12000
    assert totals[2] == 45000 + 8000
    assert totals[3] == 41000 + 4000
    assert totals[4] == 4000 - 24000


def test_uma_table_0_floating_only_oka_to_top():
    """0人浮き（全員40000以下）の場合、オカのみがトップに加算される。"""
    raw_scores = {1: 38000, 2: 36000, 3: 34000, 4: 32000}
    totals = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[1] == 38000 + UMA_CONFIG["oka"]
    assert totals[2] == 36000
    assert totals[3] == 34000
    assert totals[4] == 32000


def test_uma_tie_split():
    """同着の場合、該当順位分の順位点を合計して同点人数で折半する。"""
    # 2位と3位が同点(35000)、1人浮き(1のみ40000超え)
    raw_scores = {1: 45000, 2: 35000, 3: 35000, 4: 30000}
    totals = sl.compute_total_score(raw_scores, UMA_CONFIG)
    tied_share = (-8000 + -16000) / 2
    assert totals[1] == 45000 + 48000
    assert totals[2] == 35000 + tied_share
    assert totals[3] == 35000 + tied_share
    assert totals[4] == 30000 - 24000


def test_uma_parameters_are_not_hardcoded():
    """開始点数・返し点・ウマ表・オカを差し替えても、その通りに計算される（ハードコードなし）。"""
    custom_config = {
        "start_point": 25000,
        "return_point": 30000,
        "oka": 5000,
        "uma_table": {
            1: [10, -1, -3, -6],
            2: [5, 5, -5, -5],
            3: [3, 3, 3, -9],
        },
    }
    raw_scores = {1: 40000, 2: 28000, 3: 27000, 4: 25000}  # 1人浮き
    totals = sl.compute_total_score(raw_scores, custom_config)
    assert totals[1] == 40000 + 10
    assert totals[2] == 28000 - 1
    assert totals[3] == 27000 - 3
    assert totals[4] == 25000 - 6

    zero_floating_scores = {1: 29000, 2: 28000, 3: 27000, 4: 26000}
    totals0 = sl.compute_total_score(zero_floating_scores, custom_config)
    assert totals0[1] == 29000 + 5000  # カスタムオカが使われる
    assert totals0[2] == 28000


# ---------------------------------------------------------------------------
# ①得点評価: 飛び賞
# ---------------------------------------------------------------------------

def test_tobi_full_deduction_from_busted_player():
    """飛んだ選手は飛び賞額の全額を負担する。"""
    raw_scores = {1: 50000, 2: 30000, 3: 25000, 4: -5000}
    totals = sl.compute_total_score(
        raw_scores, UMA_CONFIG, tobi_amount=10000, tobi_busters={4: [1]}
    )
    uma_only = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[4] == uma_only[4] - 10000


def test_tobi_single_buster_gets_full_amount():
    raw_scores = {1: 50000, 2: 30000, 3: 25000, 4: -5000}
    totals = sl.compute_total_score(
        raw_scores, UMA_CONFIG, tobi_amount=10000, tobi_busters={4: [1]}
    )
    uma_only = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[1] == uma_only[1] + 10000


def test_tobi_multiple_busters_split_evenly():
    """複数人がチェックされた場合、飛び賞額を人数で折半して加算する。"""
    raw_scores = {1: 50000, 2: 30000, 3: 25000, 4: -5000}
    totals = sl.compute_total_score(
        raw_scores, UMA_CONFIG, tobi_amount=9000, tobi_busters={4: [1, 2, 3]}
    )
    uma_only = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[1] == uma_only[1] + 3000
    assert totals[2] == uma_only[2] + 3000
    assert totals[3] == uma_only[3] + 3000
    assert totals[4] == uma_only[4] - 9000
    # 合計が飛んだ人の負担額と一致する
    added = (totals[1] - uma_only[1]) + (totals[2] - uma_only[2]) + (totals[3] - uma_only[3])
    assert added == 9000


def test_tobi_split_remainder_sums_exactly():
    """端数が出ても、加算合計が飛んだ人の負担額と必ず一致する。"""
    raw_scores = {1: 50000, 2: 30000, 3: 25000, 4: -5000}
    totals = sl.compute_total_score(
        raw_scores, UMA_CONFIG, tobi_amount=10000, tobi_busters={4: [1, 2, 3]}
    )
    uma_only = sl.compute_total_score(raw_scores, UMA_CONFIG)
    added = (totals[1] - uma_only[1]) + (totals[2] - uma_only[2]) + (totals[3] - uma_only[3])
    assert added == 10000


def test_tobi_no_busters_checked_means_no_adjustment():
    """誰もチェックされなかった場合、その選手には飛び賞を適用しない（減算も加算もなし）。"""
    raw_scores = {1: 50000, 2: 30000, 3: 25000, 4: -5000}
    totals = sl.compute_total_score(
        raw_scores, UMA_CONFIG, tobi_amount=10000, tobi_busters={}
    )
    uma_only = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals == uma_only

    totals_empty_list = sl.compute_total_score(
        raw_scores, UMA_CONFIG, tobi_amount=10000, tobi_busters={4: []}
    )
    assert totals_empty_list == uma_only


def test_tobi_multiple_busted_players_independent():
    """同一半荘で複数人が飛ぶ場合、それぞれ独立して判定・配分される。"""
    raw_scores = {1: 55000, 2: -2000, 3: 48000, 4: -1000}
    totals = sl.compute_total_score(
        raw_scores,
        UMA_CONFIG,
        tobi_amount=8000,
        tobi_busters={2: [1], 4: [1, 3]},
    )
    uma_only = sl.compute_total_score(raw_scores, UMA_CONFIG)
    assert totals[2] == uma_only[2] - 8000
    assert totals[4] == uma_only[4] - 8000
    assert totals[1] == uma_only[1] + 8000 + 4000
    assert totals[3] == uma_only[3] + 4000


# ---------------------------------------------------------------------------
# ②ポイント評価
# ---------------------------------------------------------------------------

def test_rank_points_with_arbitrary_table():
    """任意の順位ポイント表で正しく計算される。"""
    point_table = [3, 1, -1, -3]
    raw_scores = {10: 40000, 20: 30000, 30: 20000, 40: 10000}
    points = sl.compute_rank_points(raw_scores, point_table)
    assert points == {10: 3, 20: 1, 30: -1, 40: -3}

    other_table = [10, 5, -5, -10]
    points2 = sl.compute_rank_points(raw_scores, other_table)
    assert points2 == {10: 10, 20: 5, 30: -5, 40: -10}


def test_rank_points_tie_split():
    point_table = [3, 1, -1, -3]
    raw_scores = {10: 40000, 20: 40000, 30: 20000, 40: 10000}  # 1,2位タイ
    points = sl.compute_rank_points(raw_scores, point_table)
    assert points[10] == points[20] == (3 + 1) / 2
    assert points[30] == -1
    assert points[40] == -3


def test_cumulative_rank_points_and_tiebreak_by_score():
    """累計ポイント同点時は累計得点で二次評価される。"""
    point_table = [3, 1, -1, -3]

    cumulative_points = {}
    cumulative_scores = {}
    rounds = [
        {1: 40000, 2: 38000, 3: 32000, 4: 30000},
        {1: 30000, 2: 42000, 3: 38000, 4: 30000},
    ]
    for raw_scores in rounds:
        points = sl.compute_rank_points(raw_scores, point_table)
        totals = sl.compute_total_score(raw_scores, UMA_CONFIG)
        cumulative_points = sl.update_cumulative(cumulative_points, points)
        cumulative_scores = sl.update_cumulative(cumulative_scores, totals)

    ranking = sl.rank_by_points_then_score([1, 2, 3, 4], cumulative_points, cumulative_scores)
    # 累計ポイントが最優先
    for a, b in zip(ranking, ranking[1:]):
        assert cumulative_points.get(a, 0) >= cumulative_points.get(b, 0)


def test_cumulative_points_tie_uses_score_then_player_number():
    """累計ポイントが同点の場合は累計得点、それも同点なら選手番号の若い順。"""
    cumulative_points = {9: 5, 2: 5, 15: 5}
    cumulative_scores = {9: 1000, 2: 1000, 15: 2000}  # 15が最高得点
    ranking = sl.rank_by_points_then_score([9, 2, 15], cumulative_points, cumulative_scores)
    assert ranking[0] == 15  # 累計得点最高

    # 累計得点も同点なら選手番号の若い順
    cumulative_scores_tied = {9: 1000, 2: 1000, 15: 1000}
    ranking2 = sl.rank_by_points_then_score([9, 2, 15], cumulative_points, cumulative_scores_tied)
    assert ranking2 == [2, 9, 15]
