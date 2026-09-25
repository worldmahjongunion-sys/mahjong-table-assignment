"""卓組みロジック（Phase 0）— 蛇行方式・ワンデー4半荘方式の純粋関数実装。

仕様書.md 4章「卓組みロジック仕様」の正本に対応する。
app.py・db.py に依存しない（import しない）。DBアクセスやStreamlit APIを呼ばない。

乱数を使う箇所（抜け番の同数タイ選出、蛇行方式の第1回戦仮順位、
ワンデー方式の半荘1のランダム割当・貪欲法の候補生成）はすべて
`random.Random` インスタンスを外部から注入する設計とし、同一シード・
同一入力から同一結果を再現できる。
"""
from itertools import combinations

POSITIONS = ("東", "南", "西", "北")
_POSITIONS_SET = set(POSITIONS)


def position_sort_key(position):
    """席の表示順（東→南→西→北）を返すソートキー。

    卓組み結果（tables内の各dict）はキーの挿入順が席割り当ての乱数処理の都合で
    バラバラになることがある（例: _random_tables()）。DBの保存順や卓組み自体の
    乱数処理には手を入れず、画面表示の並べ替えにだけこれを使う。
    """
    return POSITIONS.index(position)


def compute_table_count_and_absent(n):
    """N人から卓数T・抜け番人数Rを計算する（4.1）。"""
    return n // 4, n % 4


def select_absent_members(members, appearance_counts, previous_absent, count, rng):
    """抜け番をcount人選出する（4.1 共通ルール）。

    members: 選手番号のリスト（この回戦の参加候補全員）
    appearance_counts: {選手番号: これまでの出場回戦数}（未登場のキーは0扱い）
    previous_absent: 直前回戦で抜け番だった選手番号の集合（初回は空集合）
    count: 抜け番人数（0以上、len(members)以下）
    rng: random.Random インスタンス

    戻り値: (抜け番リスト, 残りメンバーリスト)。どちらもmembersの並び順を保つ。
    """
    if count <= 0:
        return [], list(members)

    pool = list(members)
    selected = []
    needed = count
    while needed > 0:
        max_count = max(appearance_counts.get(p, 0) for p in pool)
        tier = [p for p in pool if appearance_counts.get(p, 0) == max_count]
        if len(tier) <= needed:
            selected.extend(tier)
            needed -= len(tier)
            pool = [p for p in pool if p not in tier]
            continue
        # tier内からneeded人を選ぶ必要がある（同数タイのタイブレーク）
        candidates = [p for p in tier if p not in previous_absent]
        if len(candidates) < needed:
            candidates = tier
        chosen = rng.sample(sorted(candidates), needed)
        selected.extend(chosen)
        needed = 0

    absent = selected
    remaining = [p for p in members if p not in absent]
    return absent, remaining


def update_appearance_counts(appearance_counts, present_members):
    """出場回戦数カウンタを、この回戦に実際に出場したメンバー分だけ+1した新しいdictを返す。"""
    updated = dict(appearance_counts)
    for p in present_members:
        updated[p] = updated.get(p, 0) + 1
    return updated


def update_position_history(position_history, tables):
    """卓組み結果から、経験済みスタート位置の履歴を更新した新しいdictを返す。

    position_history: {選手番号: {経験済み位置の集合}}
    tables: [{"東": pid, "南": pid, "西": pid, "北": pid}, ...]
    """
    updated = {p: set(positions) for p, positions in position_history.items()}
    for table in tables:
        for position, player in table.items():
            updated.setdefault(player, set()).add(position)
    return updated


def update_co_seat_counts(co_seat_counts, tables):
    """同卓履歴（ペアごとの同卓回数）を更新した新しいdictを返す。"""
    updated = dict(co_seat_counts)
    for table in tables:
        members = list(table.values())
        for a, b in combinations(members, 2):
            key = frozenset((a, b))
            updated[key] = updated.get(key, 0) + 1
    return updated


def snake_rank(members, cumulative_scores, rng):
    """蛇行方式の順位付け（4.2手順1・2）。

    cumulative_scores: {選手番号: 大会内累計ポイント}。Noneなら「第1回戦
    （まだ成績がない）」としてランダムな仮順位を用いる。
    戻り値: 選手番号のリスト（1位から順）。
    """
    if cumulative_scores is None:
        ranked = list(members)
        rng.shuffle(ranked)
        return ranked
    return sorted(members, key=lambda p: (-cumulative_scores.get(p, 0), p))


def snake_tables(ranked_players):
    """順位リストから卓割当・着席順を作る（4.2手順3・4）。

    ranked_players: 1位から順に並べた選手番号リスト。len()は4の倍数であること。
    戻り値: 卓のリスト。各卓は {"東": pid, "南": pid, "西": pid, "北": pid}。
    """
    n = len(ranked_players)
    if n == 0:
        return []
    if n % 4 != 0:
        raise ValueError("ranked_players の人数は4の倍数である必要があります")

    t = n // 4
    rank_index = {p: i for i, p in enumerate(ranked_players)}
    tables = [[] for _ in range(t)]
    for block_i in range(4):
        block = ranked_players[block_i * t:(block_i + 1) * t]
        forward = block_i % 2 == 0
        for offset, player in enumerate(block):
            table_i = offset if forward else t - 1 - offset
            tables[table_i].append(player)

    seatings = []
    for table_members in tables:
        ordered = sorted(table_members, key=lambda p: rank_index[p])
        seatings.append(dict(zip(POSITIONS, ordered)))
    return seatings


def snake_round(members, appearance_counts, previous_absent, cumulative_scores, rng):
    """蛇行方式：1回戦分の抜け番選出＋卓組みを行う。

    戻り値: (抜け番リスト, 卓のリスト)
    """
    _, r = compute_table_count_and_absent(len(members))
    absent, remaining = select_absent_members(members, appearance_counts, previous_absent, r, rng)
    ranked = snake_rank(remaining, cumulative_scores, rng)
    tables = snake_tables(ranked)
    return absent, tables


def _random_tables(remaining, rng):
    shuffled = list(remaining)
    rng.shuffle(shuffled)
    t = len(shuffled) // 4
    tables = []
    for i in range(t):
        group = shuffled[i * 4:(i + 1) * 4]
        positions = list(POSITIONS)
        rng.shuffle(positions)
        tables.append(dict(zip(positions, group)))
    return tables


def _assign_position_labels(remaining, position_history, rng):
    """各選手にこの半荘のスタート位置ラベルを、未経験位置から選び、
    ラベルごとにちょうど len(remaining)//4 人になるよう割り当てる（Kuhn法）。

    全位置を経験済みの選手は「制約なし（どの位置でもよい）」として扱う。
    戻り値: {選手番号: 位置ラベル}
    """
    t = len(remaining) // 4
    slots = []
    for label in POSITIONS:
        slots.extend([label] * t)

    players = list(remaining)
    rng.shuffle(players)

    def allowed(p, label):
        unexperienced = _POSITIONS_SET - position_history.get(p, set())
        if not unexperienced:
            return True
        return label in unexperienced

    slot_candidates = [
        [si for si, label in enumerate(slots) if allowed(p, label)]
        for p in players
    ]
    match_to_player = [None] * len(slots)

    def try_kuhn(pi, visited):
        for si in slot_candidates[pi]:
            if si in visited:
                continue
            visited.add(si)
            if match_to_player[si] is None or try_kuhn(match_to_player[si], visited):
                match_to_player[si] = pi
                return True
        return False

    for pi in range(len(players)):
        try_kuhn(pi, set())

    assignment = {}
    for si, pi in enumerate(match_to_player):
        if pi is not None:
            assignment[players[pi]] = slots[si]

    # 保険：理論上起きないはずだが、万一マッチしきれない場合は残り枠を機械的に埋める
    unmatched = [p for p in players if p not in assignment]
    if unmatched:
        used = {si for si, pi in enumerate(match_to_player) if pi is not None}
        free_slots = [si for si in range(len(slots)) if si not in used]
        for p, si in zip(unmatched, free_slots):
            assignment[p] = slots[si]
    return assignment


def _duplicate_score(candidate, co_seat_counts):
    score = 0
    for group in candidate:
        for a, b in combinations(group, 2):
            score += co_seat_counts.get(frozenset((a, b)), 0)
    return score


def _ordering_key(candidate):
    """選手番号昇順に見たときの所属卓番号の並び（一意化の比較キー）。"""
    player_to_table = {}
    for table_i, group in enumerate(candidate):
        for p in group:
            player_to_table[p] = table_i
    return tuple(player_to_table[p] for p in sorted(player_to_table))


def _local_search(candidate, co_seat_counts, max_iters=200):
    """同ラベル列同士のプレイヤーを卓間で入れ替える局所探索（2-opt）。

    同じ位置ラベルの列同士を交換するため、各卓の位置ラベル構成は常に妥当なまま保たれる。
    """
    candidate = [list(g) for g in candidate]

    def table_cost(group):
        return sum(
            co_seat_counts.get(frozenset((a, b)), 0)
            for a, b in combinations(group, 2)
        )

    improved = True
    iters = 0
    while improved and iters < max_iters:
        improved = False
        iters += 1
        for i in range(len(candidate)):
            for j in range(i + 1, len(candidate)):
                for pos_idx in range(4):
                    a, b = candidate[i][pos_idx], candidate[j][pos_idx]
                    if a == b:
                        continue
                    before = table_cost(candidate[i]) + table_cost(candidate[j])
                    candidate[i][pos_idx], candidate[j][pos_idx] = b, a
                    after = table_cost(candidate[i]) + table_cost(candidate[j])
                    if after < before:
                        improved = True
                    else:
                        candidate[i][pos_idx], candidate[j][pos_idx] = a, b
    return [tuple(g) for g in candidate]


def _group_into_tables(labeled_players, co_seat_counts, rng, restarts=None):
    """位置ラベル割当済みのプレイヤーを、同卓重複が少なくなるよう卓へグループ化する。

    複数の卓分け候補（ランダムリスタート＋局所探索で得られたもの）のうち、
    重複スコアが最小のものを採用する。同点の場合は選手番号昇順の一意化ルールで決める。
    """
    by_label = {label: [] for label in POSITIONS}
    for p, label in labeled_players.items():
        by_label[label].append(p)
    t = len(by_label[POSITIONS[0]])
    if t == 0:
        return []
    if restarts is None:
        restarts = max(80, 40 * t)

    best_candidate = None
    best_key = None
    for _ in range(restarts):
        shuffled = {label: list(players) for label, players in by_label.items()}
        for players in shuffled.values():
            rng.shuffle(players)
        candidate = [
            tuple(shuffled[label][i] for label in POSITIONS)
            for i in range(t)
        ]
        candidate = _local_search(candidate, co_seat_counts)
        key = (_duplicate_score(candidate, co_seat_counts), _ordering_key(candidate))
        if best_key is None or key < best_key:
            best_key = key
            best_candidate = candidate

    return [dict(zip(POSITIONS, group)) for group in best_candidate]


def one_day_hanchan(
    members,
    hanchan_number,
    appearance_counts,
    previous_absent,
    position_history,
    co_seat_counts,
    rng,
):
    """ワンデー4半荘方式：1半荘分の抜け番選出＋卓組みを行う（4.3）。

    members: この半荘の参加候補選手番号リスト
    hanchan_number: 半荘番号（1始まり）。1なら経験位置履歴を使わずランダム割当
    appearance_counts: {選手番号: これまでの出場半荘数}
    previous_absent: 直前半荘で抜け番だった選手番号の集合
    position_history: {選手番号: {経験済み位置の集合}}
    co_seat_counts: {frozenset({p1, p2}): これまでの同卓回数}
    rng: random.Random インスタンス

    戻り値: (抜け番リスト, 卓のリスト[{"東": pid, ...}, ...])
    """
    _, r = compute_table_count_and_absent(len(members))
    absent, remaining = select_absent_members(members, appearance_counts, previous_absent, r, rng)

    if hanchan_number <= 1:
        tables = _random_tables(remaining, rng)
    else:
        labeled = _assign_position_labels(remaining, position_history, rng)
        tables = _group_into_tables(labeled, co_seat_counts, rng)

    return absent, tables
