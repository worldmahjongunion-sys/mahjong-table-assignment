"""大会運営（回戦実行・成績集計）の橋渡しロジック。

db.py（永続化、member_id基準）と table_logic.py・scoring_logic.py（純粋ロジック、
table_logic.pyは選手番号基準）の間をつなぐ。DBの保存済みレコードから各ロジックが
必要とする入力形式を再構築し、実行結果をDBへ保存できる形に変換する。

大会管理とゲスト共有リンク実装依頼.md Stage 1対応。
"""
import random
from itertools import combinations

import db
import scoring_logic
import table_logic


def _player_number_map(tournament_id):
    """member_id <-> 選手番号 の対応表を返す。"""
    members = db.get_tournament_members(tournament_id)
    member_to_number = {m["member_id"]: m["player_number"] for m in members}
    number_to_member = {m["player_number"]: m["member_id"] for m in members}
    return member_to_number, number_to_member


def build_appearance_counts(tournament_id):
    """大会を通した各選手の出場回戦数（抜け番でない回戦の数）を選手番号キーで返す。"""
    member_to_number, _ = _player_number_map(tournament_id)
    counts = {}
    for round_row in db.get_rounds(tournament_id):
        for seat in db.get_round_seats(round_row["id"]):
            player_number = member_to_number[seat["member_id"]]
            counts[player_number] = counts.get(player_number, 0) + 1
    return counts


def build_previous_absent(tournament_id):
    """直前回戦で抜け番だった選手番号の集合。回戦がまだなければ空集合。"""
    member_to_number, _ = _player_number_map(tournament_id)
    rounds = db.get_rounds(tournament_id)
    if not rounds:
        return set()
    last_round = rounds[-1]
    return {member_to_number[mid] for mid in db.get_round_absences(last_round["id"])}


def build_position_history(tournament_id):
    """ワンデー4半荘方式用: 各選手の経験済みスタート位置の集合（選手番号キー）。"""
    member_to_number, _ = _player_number_map(tournament_id)
    history = {}
    for round_row in db.get_rounds(tournament_id):
        for seat in db.get_round_seats(round_row["id"]):
            player_number = member_to_number[seat["member_id"]]
            history.setdefault(player_number, set()).add(seat["position"])
    return history


def build_co_seat_counts(tournament_id):
    """ワンデー4半荘方式用: 選手番号ペアごとの同卓回数。"""
    member_to_number, _ = _player_number_map(tournament_id)
    counts = {}
    for round_row in db.get_rounds(tournament_id):
        seats = db.get_round_seats(round_row["id"])
        by_table = {}
        for seat in seats:
            by_table.setdefault(seat["table_number"], []).append(member_to_number[seat["member_id"]])
        for members_at_table in by_table.values():
            for a, b in combinations(members_at_table, 2):
                key = frozenset((a, b))
                counts[key] = counts.get(key, 0) + 1
    return counts


def _extract_uma_config_and_tobi(scoring_config):
    uma_config = {
        "start_point": scoring_config["start_point"],
        "return_point": scoring_config["return_point"],
        "oka": scoring_config["oka"],
        "uma_table": scoring_config["uma_table"],
    }
    tobi_amount = scoring_config.get("tobi_amount", 0)
    return uma_config, tobi_amount


def build_cumulative_scores(tournament):
    """蛇行方式用: 評価方式に応じた累計得点／累計順位ポイントを選手番号キーで返す。

    成績がまだ入力されていない回戦は寄与0として扱う（蛇行方式は次回戦の卓組みに
    前回戦までの成績を使うため、直前の回戦の成績入力を済ませてから次回戦を
    実行する運用を前提とする）。
    """
    member_to_number, _ = _player_number_map(tournament["id"])
    cumulative = {}
    scoring_mode = tournament["scoring_mode"]
    scoring_config = tournament["scoring_config"]
    if scoring_mode == "得点":
        uma_config, tobi_amount = _extract_uma_config_and_tobi(scoring_config)
    else:
        point_table = scoring_config["rank_point_table"]

    for round_row in db.get_rounds(tournament["id"]):
        round_id = round_row["id"]
        raw_scores = db.get_round_results(round_id)
        if not raw_scores:
            continue
        if scoring_mode == "得点":
            tobi_busters = db.get_round_tobi_busters(round_id)
            round_values = scoring_logic.compute_total_score(raw_scores, uma_config, tobi_amount, tobi_busters)
        else:
            round_values = scoring_logic.compute_rank_points(raw_scores, point_table)
        round_values_by_number = {member_to_number[mid]: v for mid, v in round_values.items()}
        cumulative = scoring_logic.update_cumulative(cumulative, round_values_by_number)
    return cumulative


def _cumulative_raw_score_sum(tournament_id):
    """②ポイント評価の最終順位のタイブレーク用: ウマ・オカ抜きの素点合計（選手番号キー）。

    仕様書.md 7.2「同点の場合は①のロジックで計算した累計得点で二次評価」とあるが、
    ②ポイント評価の大会はウマ・オカ設定を保持しない（仕様書.md 3.2）ため、ここでは
    付随するウマ計算は行わず、生の素点合計をタイブレークの代替値として使う
    （実装時の判断。詳細は仕様書.mdに記載）。
    """
    member_to_number, _ = _player_number_map(tournament_id)
    cumulative = {}
    for round_row in db.get_rounds(tournament_id):
        raw_scores = db.get_round_results(round_row["id"])
        raw_scores_by_number = {member_to_number[mid]: v for mid, v in raw_scores.items()}
        cumulative = scoring_logic.update_cumulative(cumulative, raw_scores_by_number)
    return cumulative


def run_next_round(tournament, rng=None):
    """次回戦の卓組みをその場で実行する（DB未保存、プレビュー用）。

    戻り値: (round_number, absent_player_numbers, tables)
    tables は table_logic.py の戻り値そのまま（選手番号ベース、
    [{"東": 選手番号, "南": 選手番号, ...}, ...]）。
    """
    rng = rng or random.Random()
    tournament_id = tournament["id"]
    player_numbers = [m["player_number"] for m in db.get_tournament_members(tournament_id)]

    round_number = db.count_rounds(tournament_id) + 1
    appearance_counts = build_appearance_counts(tournament_id)
    previous_absent = build_previous_absent(tournament_id)

    if tournament["table_method"] == "蛇行":
        cumulative_scores = None if round_number == 1 else build_cumulative_scores(tournament)
        absent, tables = table_logic.snake_round(
            player_numbers, appearance_counts, previous_absent, cumulative_scores, rng
        )
    else:
        position_history = build_position_history(tournament_id)
        co_seat_counts = build_co_seat_counts(tournament_id)
        absent, tables = table_logic.one_day_hanchan(
            player_numbers, round_number, appearance_counts, previous_absent,
            position_history, co_seat_counts, rng,
        )
    return round_number, absent, tables


def save_confirmed_round(tournament_id, round_number, absent_player_numbers, tables):
    """プレビューで確定した卓組み結果を、選手番号→member_idに変換してDBへ保存する。"""
    _, number_to_member = _player_number_map(tournament_id)
    tables_by_member = [
        {position: number_to_member[player_number] for position, player_number in table.items()}
        for table in tables
    ]
    absent_member_ids = [number_to_member[pn] for pn in absent_player_numbers]
    return db.save_round(tournament_id, round_number, tables_by_member, absent_member_ids)


def compute_round_totals(tournament, round_id):
    """指定回戦の成績（素点）から、評価方式に応じた計算結果を返す（{member_id: 値}）。"""
    raw_scores = db.get_round_results(round_id)
    if tournament["scoring_mode"] == "得点":
        uma_config, tobi_amount = _extract_uma_config_and_tobi(tournament["scoring_config"])
        tobi_busters = db.get_round_tobi_busters(round_id)
        return scoring_logic.compute_total_score(raw_scores, uma_config, tobi_amount, tobi_busters)
    point_table = tournament["scoring_config"]["rank_point_table"]
    return scoring_logic.compute_rank_points(raw_scores, point_table)


def compute_standings(tournament):
    """大会内の現在の順位表を返す（1位から順のリスト）。

    各要素: {"member_id", "player_number", "value"}（valueは累計得点または累計順位ポイント）
    得点方式: 累計得点の降順（同点は選手番号昇順）
    ポイント方式: 累計順位ポイント→素点合計→選手番号の3段階タイブレーク
    """
    _, number_to_member = _player_number_map(tournament["id"])
    cumulative = build_cumulative_scores(tournament)

    if tournament["scoring_mode"] == "得点":
        ordered_numbers = sorted(cumulative.keys(), key=lambda pn: (-cumulative.get(pn, 0), pn))
    else:
        raw_score_sums = _cumulative_raw_score_sum(tournament["id"])
        ordered_numbers = scoring_logic.rank_by_points_then_score(
            list(cumulative.keys()), cumulative, raw_score_sums
        )

    return [
        {"member_id": number_to_member[pn], "player_number": pn, "value": cumulative.get(pn, 0)}
        for pn in ordered_numbers
    ]
