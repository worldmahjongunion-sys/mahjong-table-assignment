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


# 順位の付け方（ポイント評価のみ）。scoring_config["rank_scope"]に保存する。
# キーがない既存の大会は「卓ごと」として扱う（後方互換）。
RANK_SCOPE_PER_TABLE = "卓ごと"
RANK_SCOPE_OVERALL = "全員まとめて"
RANK_SCOPES = (RANK_SCOPE_PER_TABLE, RANK_SCOPE_OVERALL)


def get_rank_scope(tournament):
    """大会の順位の付け方。得点評価の大会は常に「卓ごと」。"""
    if tournament["scoring_mode"] != "ポイント":
        return RANK_SCOPE_PER_TABLE
    return tournament["scoring_config"].get("rank_scope", RANK_SCOPE_PER_TABLE)


def get_total_rounds(tournament):
    """大会の全回戦数（scoring_config["total_rounds"]）。未設定ならNone。"""
    value = tournament["scoring_config"].get("total_rounds")
    return int(value) if value else None


def overall_point_table_for(tournament, player_count):
    """全員まとめて方式で使う順位ポイント表。

    主催者が手で直した表（overall_rank_point_table）が人数と同じ長さならそれを使い、
    未設定または人数と合わないときは N点〜1点 の初期値を使う。
    """
    table = tournament["scoring_config"].get("overall_rank_point_table")
    if table and len(table) == player_count:
        return list(table)
    return scoring_logic.default_overall_point_table(player_count)


def overall_point_table_mismatch(tournament):
    """手で直した順位ポイント表の長さが参加人数と合わないか（主催者画面の警告用）。"""
    if get_rank_scope(tournament) != RANK_SCOPE_OVERALL:
        return False
    table = tournament["scoring_config"].get("overall_rank_point_table")
    if not table:
        return False
    return len(table) != len(db.get_tournament_members(tournament["id"]))


def _round_tables(round_id):
    """回戦の卓ごとのmember_idリスト（卓番号順の(卓番号, [member_id...])のリスト）。"""
    table_member_ids = {}
    for seat in db.get_round_seats(round_id):
        table_member_ids.setdefault(seat["table_number"], []).append(seat["member_id"])
    return sorted(table_member_ids.items())


def is_round_complete(round_id):
    """回戦の全卓の素点がそろっているか（卓がない回戦はFalse）。"""
    raw_scores = db.get_round_results(round_id)
    tables = _round_tables(round_id)
    return bool(tables) and all(mid in raw_scores for _, mids in tables for mid in mids)


def compute_round_totals(tournament, round_id):
    """指定回戦の成績（素点）から、評価方式に応じた計算結果を返す（{member_id: 値}）。

    順位の付け方が「卓ごと」（既定）なら、順位付け（ウマ・オカ・飛び賞・順位ポイント）は
    卓ごとに行う。8人2卓の回戦なら、卓1・卓2それぞれの中で1〜4位を決める。まだ成績が
    入力されていない卓は結果に含めない。

    「全員まとめて」（ポイント評価のみ）なら、その回戦の全卓の全員を素点順に並べて
    N点〜1点を付ける。一部の卓だけ入力済みの間は途中の順位に意味がないので、全卓が
    そろうまで空（寄与0）を返す。
    """
    raw_scores = db.get_round_results(round_id)
    if get_rank_scope(tournament) == RANK_SCOPE_OVERALL:
        if not is_round_complete(round_id):
            return {}
        member_to_number, _ = _player_number_map(tournament["id"])
        seated = [mid for _, mids in _round_tables(round_id) for mid in mids]
        # 同点の並び順を選手番号順にそろえるため、選手番号キーで計算してから戻す
        scores_by_number = {member_to_number[mid]: raw_scores[mid] for mid in seated}
        point_table = overall_point_table_for(tournament, len(seated))
        values = scoring_logic.compute_rank_points_overall(scores_by_number, point_table)
        number_to_member = {member_to_number[mid]: mid for mid in seated}
        return {number_to_member[pn]: v for pn, v in values.items()}

    if tournament["scoring_mode"] == "得点":
        uma_config, tobi_amount = _extract_uma_config_and_tobi(tournament["scoring_config"])
        tobi_busters = db.get_round_tobi_busters(round_id)
    else:
        point_table = tournament["scoring_config"]["rank_point_table"]

    round_values = {}
    for _, member_ids in _round_tables(round_id):
        table_scores = {mid: raw_scores[mid] for mid in member_ids if mid in raw_scores}
        if len(table_scores) < len(member_ids):
            continue  # 未入力の卓（一部だけ入力された卓も、順位が決まらないので計算しない）
        if tournament["scoring_mode"] == "得点":
            table_busters = {mid: busters for mid, busters in tobi_busters.items() if mid in table_scores}
            values = scoring_logic.compute_total_score(table_scores, uma_config, tobi_amount, table_busters)
        else:
            values = scoring_logic.compute_rank_points(table_scores, point_table)
        round_values.update(values)
    return round_values


def build_cumulative_scores(tournament):
    """蛇行方式用: 評価方式に応じた累計得点／累計順位ポイントを選手番号キーで返す。

    各回戦の値は卓ごとに計算する（compute_round_totals）。成績がまだ入力されていない
    卓・回戦は寄与0として扱う（蛇行方式は次回戦の卓組みに前回戦までの成績を使うため、
    直前の回戦の成績入力を済ませてから次回戦を実行する運用を前提とする）。
    """
    member_to_number, _ = _player_number_map(tournament["id"])
    cumulative = {}
    for round_row in db.get_rounds(tournament["id"]):
        round_values = compute_round_totals(tournament, round_row["id"])
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


def build_tiebreak_scores(tournament):
    """累計が同点のときの2段目（素点合計）。ポイント方式だけ。得点方式は None。

    大会内順位表（compute_standings）と蛇行の席決め（run_next_round）で同じ値を使う
    （2026-11-19）。
    """
    if tournament["scoring_mode"] == "得点":
        return None
    return _cumulative_raw_score_sum(tournament["id"])


def is_final_round(tournament, round_number):
    """全回戦数が設定されていて、round_numberがその最終回戦か。"""
    total_rounds = get_total_rounds(tournament)
    return total_rounds is not None and round_number == total_rounds


def next_round_is_fixed(tournament, member_count):
    """次の回戦の卓組みが乱数で変わらないか（「作り直す」ボタンを出さないための判定）。

    蛇行方式で、抜け番が出ない人数（4の倍数）のときに、
    - 第2回戦以降（累計順の蛇行・最終回戦の順位順はどちらも決まった並び）
    - 全回戦数を設定した大会の第1回戦（選手番号順）
    はやり直しても同じ結果になる。ワンデー方式・抜け番が出る人数・
    全回戦数なしの第1回戦は乱数を使うのでFalse。
    """
    if tournament["table_method"] != "蛇行" or member_count % 4 != 0:
        return False
    if db.count_rounds(tournament["id"]) >= 1:
        return True
    return get_total_rounds(tournament) is not None


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
            player_numbers, appearance_counts, previous_absent, cumulative_scores, rng,
            final_round=is_final_round(tournament, round_number),
            ordered_first_round=get_total_rounds(tournament) is not None,
            tiebreak_scores=None if round_number == 1 else build_tiebreak_scores(tournament),
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


def compute_standings(tournament):
    """大会内の現在の順位表を返す（1位から順のリスト）。

    各要素: {"member_id", "player_number", "value"}（valueは累計得点または累計順位ポイント）
    得点方式: 累計得点の降順（同点は選手番号昇順）
    ポイント方式: 累計順位ポイント→素点合計→選手番号の3段階タイブレーク
    席決め（蛇行）も table_logic.standings_order で同じ並びにする。
    """
    _, number_to_member = _player_number_map(tournament["id"])
    cumulative = build_cumulative_scores(tournament)

    # 席決め（run_next_round → table_logic.snake_rank）と同じ関数で並べる
    ordered_numbers = table_logic.standings_order(
        list(cumulative.keys()), cumulative, build_tiebreak_scores(tournament)
    )

    return [
        {"member_id": number_to_member[pn], "player_number": pn, "value": cumulative.get(pn, 0)}
        for pn in ordered_numbers
    ]


def compute_final_ranking(tournament):
    """最終順位（12/19大会向け実装依頼 2.3対応）。

    全回戦数が設定されていて、その最終回戦の全卓の素点がそろっているときだけ返す。
    それ以外（未設定・最終回戦がまだ・入力途中）はNone。
    1卓の1位が優勝、2位以下は最終回戦の全員の素点順。最終回戦の素点が同点のときは
    全回戦の累計（順位ポイント方式なら累計順位ポイント、得点方式なら累計得点）→
    全回戦の素点合計（ウマ・オカ抜き）→ 選手番号の若い方 の順で上位にする。

    戻り値: [{"member_id", "player_number", "table_number", "raw_score"}, ...]（優勝から順）
    """
    total_rounds = get_total_rounds(tournament)
    if total_rounds is None:
        return None
    final_round = next(
        (r for r in db.get_rounds(tournament["id"]) if r["round_number"] == total_rounds), None
    )
    if final_round is None or not is_round_complete(final_round["id"]):
        return None

    member_to_number, number_to_member = _player_number_map(tournament["id"])
    raw_scores = db.get_round_results(final_round["id"])
    tables = _round_tables(final_round["id"])
    table_of = {member_to_number[mid]: tn for tn, mids in tables for mid in mids}
    scores_by_number = {member_to_number[mid]: raw_scores[mid] for _, mids in tables for mid in mids}
    ordered = scoring_logic.final_ranking(
        [[member_to_number[mid] for mid in mids] for _, mids in tables],
        scores_by_number,
        cumulative_points=build_cumulative_scores(tournament),
        raw_score_sums=_cumulative_raw_score_sum(tournament["id"]),
    )
    return [
        {
            "member_id": number_to_member[pn],
            "player_number": pn,
            "table_number": table_of[pn],
            "raw_score": scores_by_number[pn],
        }
        for pn in ordered
    ]
