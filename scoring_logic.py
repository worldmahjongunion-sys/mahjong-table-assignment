"""評価方式（得点／ポイント）の集計ロジック（得点ポイント評価方式実装依頼.md対応）。

仕様書.md 3章末尾・7章に対応する。table_logic.pyと同じ方針で、
app.py・db.pyに依存しない純粋関数として実装する。乱数は使わないため
再現性の考慮は不要。

用語（仕様書.md 3章末尾の用語整理に対応）:
- 累計得点: ①得点評価で使う、素点＋ウマ＋オカ（＋飛び賞）の総和
- 累計順位ポイント: ②ポイント評価で使う、各回戦の順位ポイントの総和
"""

# ウマ・オカの初期値（画面の初期表示用デフォルト値／プレースホルダ）。
# 一人麻雀アプリ（mugen-mahjong-red）の数値を参考値として使う。
# 依頼文書にはオカの具体的な数値は示されていないため、0を安全なプレースホルダとする
# （主催者が大会ごとに必ず入力し直す前提の値であり、推奨値ではない）。
DEFAULT_UMA_CONFIG = {
    "start_point": 35000,
    "return_point": 40000,
    "oka": 0,
    "uma_table": {
        1: [48000, -8000, -16000, -24000],
        2: [24000, 8000, -8000, -24000],
        3: [12000, 8000, 4000, -24000],
    },
}

# 順位ポイント表の初期値（画面の初期表示用デフォルト値／プレースホルダ）。
DEFAULT_RANK_POINT_TABLE = [3, 1, -1, -3]


def _apply_placement_table(raw_scores, table):
    """素点降順で順位付けし、tableの該当箇所を各選手に割り当てる（同着は合算して折半）。

    raw_scores: {選手番号: 素点}
    table: 1位から4位までの値のリスト（長さ4）
    戻り値: {選手番号: 割当値}

    順位付けは1卓分（4人）の素点に対して行う。table_logic.pyの卓ごとに呼び出すこと。
    複数卓ぶんをまとめて渡すとtableの長さを超えた順位に値が割り当てられず、
    黙って0扱いになってしまうため、tableより多い人数はエラーにする。
    """
    if len(raw_scores) > len(table):
        raise ValueError(
            f"順位付けは1卓ずつ行う必要があります（{len(raw_scores)}人分が渡されました。最大{len(table)}人）"
        )
    ordered = sorted(raw_scores.keys(), key=lambda p: (-raw_scores[p], p))
    result = {}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j + 1 < n and raw_scores[ordered[j + 1]] == raw_scores[ordered[i]]:
            j += 1
        group = ordered[i:j + 1]
        share = sum(table[i:j + 1]) / len(group)
        for p in group:
            result[p] = share
        i = j + 1
    return result


def _split_amount(amount, n):
    """amountをn人に、合計が必ずamountと一致するように分配する。

    amountが整数の場合は端数を先頭（選手番号昇順で呼び出し側が並べたリストの先頭）から
    1ずつ配分し、そうでない場合は単純な等分割にする。
    """
    if isinstance(amount, int) and isinstance(n, int) and n > 0:
        base, remainder = divmod(amount, n)
        shares = [base] * n
        for i in range(remainder):
            shares[i] += 1
        return shares
    base = amount / n
    return [base] * n


def compute_total_score(raw_scores, uma_config, tobi_amount=0, tobi_busters=None):
    """①得点評価: 素点＋ウマ＋オカ（＋飛び賞）を合計した総合得点を計算する（仕様書.md 7章対応）。

    raw_scores: その回戦の素点（選手番号→素点の辞書、4人分）。素点が0以下（0またはマイナス）の
        選手は「飛んだ人候補」としてUI側でチェックボックスの対象になる想定だが、本関数自体は
        素点の符号を見ない。飛び賞が実際に発生するかどうかは素点の値ではなく、tobi_bustersに
        「飛ばした人」が1人以上チェックされているかどうかだけで決まる。
    uma_config: {"start_point", "return_point", "oka", "uma_table": {1: [...], 2: [...], 3: [...]}}
        （すべて大会ごとに主催者が入力する値。コードにハードコードしない）
    tobi_amount: 飛び賞額（0なら飛び賞なし）
    tobi_busters: {飛んだ選手の選手番号: [飛ばした選手の選手番号, ...]}。
        飛んだ選手がこの辞書にキーとして存在しない、またはリストが空の場合は
        その選手には飛び賞を適用しない（減算も加算も発生しない）。素点がちょうど0で
        誰もチェックされていない場合も同様に適用しない。

    戻り値: {選手番号: ウマ・オカ・飛び賞適用後の総合得点}
    """
    tobi_busters = tobi_busters or {}
    return_point = uma_config["return_point"]
    oka = uma_config["oka"]
    uma_table_map = uma_config["uma_table"]

    floating_count = sum(1 for score in raw_scores.values() if score > return_point)
    table = [oka, 0, 0, 0] if floating_count == 0 else uma_table_map[floating_count]

    uma_amounts = _apply_placement_table(raw_scores, table)
    totals = {p: raw_scores[p] + uma_amounts[p] for p in raw_scores}

    for busted, busters in tobi_busters.items():
        if busted not in raw_scores:
            continue
        busters = sorted(b for b in busters if b in raw_scores)
        if not busters:
            continue
        totals[busted] -= tobi_amount
        shares = _split_amount(tobi_amount, len(busters))
        for buster, share in zip(busters, shares):
            totals[buster] += share

    return totals


def compute_rank_points(raw_scores, point_table):
    """②ポイント評価: その回戦の素点順位に応じた順位ポイントを計算する。

    raw_scores: その回戦の素点（選手番号→素点の辞書）
    point_table: 1位から4位までの順位ポイントのリスト（長さ4、大会ごとに主催者が入力する値）

    戻り値: {選手番号: その回戦の順位ポイント}
    """
    return _apply_placement_table(raw_scores, point_table)


def default_overall_point_table(n):
    """全員まとめて方式の順位ポイント表の初期値（N人ならN点・N-1点…1点）。"""
    return list(range(n, 0, -1))


def compute_rank_points_overall(raw_scores, point_table):
    """②ポイント評価（全員まとめて方式）: その回戦の全卓の全員を素点順に並べて順位ポイントを付ける。

    12/19大会向け実装依頼対応。卓ごと方式（compute_rank_points）と違い、全卓の全員の素点を
    1つにまとめて渡す。N人ならpoint_tableは長さN（初期値はN点〜1点）。
    素点が同点の人は、卓ごと方式と同じく該当順位のポイントを合算して折半する。

    raw_scores: {選手キー: 素点}（その回戦に出場した全員分）
    point_table: 1位から順の順位ポイントのリスト（長さはraw_scoresの人数と同じ）
    """
    if len(point_table) != len(raw_scores):
        raise ValueError(
            f"順位ポイント表の長さ（{len(point_table)}）が人数（{len(raw_scores)}）と一致しません"
        )
    return _apply_placement_table(raw_scores, point_table)


def final_ranking(final_round_tables, raw_scores, cumulative_points=None, raw_score_sums=None):
    """最終回戦の結果から最終順位を決める（12/19大会向け実装依頼 2.3対応）。

    final_round_tables: 卓番号順の卓のリスト。各卓はその卓の選手キーのリスト。
    raw_scores: {選手キー: 最終回戦の素点}
    cumulative_points: {選手キー: 全回戦の累計順位ポイント}（同点処理の1段目。省略時は全員0）
    raw_score_sums: {選手キー: 全回戦の素点合計}（同点処理の2段目。省略時は全員0）

    1卓の1位（素点最上位）が優勝。2位以下は、残りの全員を卓に関係なく最終回戦の素点の
    高い順に並べる。最終回戦の素点が同点のときは（優勝・2位以下とも）、
    全回戦の累計順位ポイントが多い方 → 全回戦の素点合計が多い方 → 選手キー（選手番号）の
    小さい方 の順で上位にする（2026-11-18 追加依頼）。
    戻り値: 選手キーのリスト（優勝から順）
    """
    if not final_round_tables:
        return []
    cumulative_points = cumulative_points or {}
    raw_score_sums = raw_score_sums or {}

    def order_key(p):
        return (-raw_scores[p], -cumulative_points.get(p, 0), -raw_score_sums.get(p, 0), p)

    champion = sorted(final_round_tables[0], key=order_key)[0]
    others = [p for table in final_round_tables for p in table if p != champion]
    return [champion] + sorted(others, key=order_key)


def parse_point_table_text(text):
    """「12,11,10,…」のようなカンマ区切りの順位ポイント表を数値のリストにする。

    全角数字・全角カンマ・読点・空白区切りも受け付ける。空欄なら空リスト（＝N点〜1点を自動）。
    数値でないものがあればValueError。
    """
    normalized = text.translate(str.maketrans("０１２３４５６７８９．－，、", "0123456789.-,,"))
    parts = [p for p in normalized.replace(" ", ",").replace("　", ",").split(",") if p.strip()]
    values = []
    for part in parts:
        number = float(part)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError(f"数値ではありません: {part}")
        values.append(int(number) if number == int(number) else number)
    return values


# ---- 点数の入出力単位（100点単位） ----
# 画面では点数を「下2桁を省略した100点単位」で入出力する（45,800点は「458」）。
# DB・計算ロジックは実際の点数のまま持ち、画面の入出力の境界でだけ100倍/100分の1に
# 変換する。順位ポイントは点数ではないため対象外（そのままの数値で扱う）。
POINT_UNIT = 100
# 100点単位の入力として許容する絶対値の上限（2000＝200,000点）。これを超える入力は
# 「45,800点なのに45800と入力した」ような桁の誤りとみなして保存させない。
MAX_HUNDREDS_ABS = 2000
HUNDREDS_INPUT_HINT = "点数は100点単位で入力します（例：45,800点なら458、-2,000点なら-20）"
HUNDREDS_OUT_OF_RANGE_MESSAGE = "100点単位で入力してください(例:45,800点なら458)"


def points_to_hundreds(points):
    """実際の点数を、画面入力用の100点単位の値にする（45800→458）。

    100で割り切れない点数（過去データなど）は小数のまま返し、丸めて情報を失わない。
    """
    if points % POINT_UNIT == 0:
        return int(points // POINT_UNIT)
    return points / POINT_UNIT


def hundreds_to_points(hundreds):
    """画面入力の100点単位の値を、実際の点数に戻す（458→45800）。"""
    return int(round(hundreds * POINT_UNIT))


def is_hundreds_input_plausible(points):
    """100点単位で入力された点数（実点数に戻した値）の桁が妥当か（絶対値200,000点以内）。"""
    return abs(points) <= MAX_HUNDREDS_ABS * POINT_UNIT


def scoring_config_points_out_of_range(scoring_config):
    """評価方式の設定値（実点数）に、桁が明らかにおかしいものが含まれるか。

    対象は開始点数・返し点・オカ・飛び賞額・ウマ表。順位ポイント表は点数ではないので見ない。
    """
    values = [
        scoring_config.get(key)
        for key in ("start_point", "return_point", "oka", "tobi_amount")
        if scoring_config.get(key) is not None
    ]
    for row in scoring_config.get("uma_table", {}).values():
        values.extend(row)
    return any(not is_hundreds_input_plausible(v) for v in values)


def format_hundreds(points):
    """実際の点数を、100点単位の表示用文字列にする（45800→"458"、-2000→"-20"）。"""
    return format_point_value(points / POINT_UNIT)


def format_point_value(value):
    """順位ポイント・得点の表示用に、必要な桁だけの文字列にする（5.0→"5"、2.5→"2.5"）。

    同着の折半で生じる小数は小数第4位まで見て、末尾の0は落とす。
    """
    rounded = round(value, 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0")


def update_cumulative(cumulative, round_values):
    """累計（累計得点／累計順位ポイント）に1回戦分を積み上げた新しいdictを返す。"""
    updated = dict(cumulative)
    for p, v in round_values.items():
        updated[p] = updated.get(p, 0) + v
    return updated


def rank_by_points_then_score(players, cumulative_points, cumulative_scores):
    """②ポイント評価の最終順位付け：累計順位ポイント→累計得点→選手番号の3段階タイブレーク。

    戻り値: 選手番号のリスト（1位から順）
    """
    return sorted(
        players,
        key=lambda p: (
            -cumulative_points.get(p, 0),
            -cumulative_scores.get(p, 0),
            p,
        ),
    )
