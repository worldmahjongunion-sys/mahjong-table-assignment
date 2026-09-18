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
    """
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
