"""12/19大会向け実装依頼: 大会編集画面の「順位の付け方」「全回戦数」と、最終順位の表示（主催者・ゲスト）。

AppTestの長いセッションでの既知の制約を避けるため、test_tournament_ui.pyと同じく
前提はdb.py/tournament_serviceで組み立て、確かめたい操作だけを画面で行う。
"""
import random

import pytest
from streamlit.testing.v1 import AppTest

import db
import scoring_logic
import tournament_service as svc
from test_tournament_ui import APP_PATH, SCORE_CONFIG, _add_user, _find_button, _login, app_env  # noqa: F401

OVERALL_CONFIG = {
    "rank_point_table": [3, 1, -1, -3],
    "start_point": 25000,
    "rank_scope": "全員まとめて",
    "total_rounds": 2,
}


def _setup_admin_with_point_tournament(n=8, config=None):
    user_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(user_id)["tenant_id"]
    member_ids = [db.add_member(tenant_id, user_id, f"選手{i}", "") for i in range(1, n + 1)]
    tournament_id = db.create_tournament(
        tenant_id, "12月大会", "ワンデー", "蛇行", "ポイント",
        config or {"rank_point_table": [3, 1, -1, -3], "start_point": 25000}, "受付順",
    )
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    return user_id, tenant_id, tournament_id


def _play_rounds(tenant_id, tournament_id, count, rng):
    """countの回戦を実行し、各卓 400/300/200/100（100点単位）で全卓入力する。"""
    to_member = {m["player_number"]: m["member_id"] for m in db.get_tournament_members(tournament_id)}
    for _ in range(count):
        tournament = db.get_tournament(tenant_id, tournament_id)
        rn, absent, tables = svc.run_next_round(tournament, rng)
        round_id = svc.save_confirmed_round(tournament_id, rn, absent, tables)
        scores = {}
        for table in tables:
            for pn, s in zip(sorted(table.values()), [40000, 30000, 20000, 10000]):
                scores[to_member[pn]] = s
        db.save_round_results(round_id, scores, {})


def test_parse_point_table_text():
    assert scoring_logic.parse_point_table_text("8,7,6,5,4,3,2,1") == [8, 7, 6, 5, 4, 3, 2, 1]
    assert scoring_logic.parse_point_table_text("１２、１１ 10，9") == [12, 11, 10, 9]
    assert scoring_logic.parse_point_table_text("2.5, -1") == [2.5, -1]
    assert scoring_logic.parse_point_table_text("  ") == []
    for bad in ("8,七,6", "nan", "inf"):
        with pytest.raises(ValueError):
            scoring_logic.parse_point_table_text(bad)


def test_edit_form_prefills_n_to_1_and_saves_overall_scope_and_total_rounds(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8)
    prefix = f"edit_tournament_{tournament_id}"

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    table_input = next(ti for ti in at.text_input if ti.key == f"{prefix}_overall_rank_point_table")
    assert table_input.value == "8,7,6,5,4,3,2,1"
    next(sb for sb in at.selectbox if sb.key == f"{prefix}_rank_scope").set_value("全員まとめて")
    next(ni for ni in at.number_input if ni.key == f"{prefix}_total_rounds").set_value(5)
    at.run()
    at.button[_find_button(at, "変更を保存")].click().run()

    assert not at.exception
    saved = db.get_tournament(tenant_id, tournament_id)["scoring_config"]
    assert saved["rank_scope"] == "全員まとめて"
    assert saved["total_rounds"] == 5
    assert "overall_rank_point_table" not in saved  # N〜1のままなら保存せず、人数に合わせて自動
    assert saved["rank_point_table"] == [3, 1, -1, -3]  # 卓ごとの表は残る


def test_edit_form_saves_hand_edited_overall_table(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    prefix = f"edit_tournament_{tournament_id}"

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    next(ti for ti in at.text_input if ti.key == f"{prefix}_overall_rank_point_table").set_value(
        "20,15,10,8,6,4,2,0"
    )
    at.run()
    at.button[_find_button(at, "変更を保存")].click().run()

    assert not at.exception
    saved = db.get_tournament(tenant_id, tournament_id)["scoring_config"]
    assert saved["overall_rank_point_table"] == [20, 15, 10, 8, 6, 4, 2, 0]


@pytest.mark.parametrize("text", ["8,7,6", "8,7,abc,5,4,3,2,1"])
def test_edit_form_rejects_bad_overall_table(app_env, text):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    prefix = f"edit_tournament_{tournament_id}"

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    next(ti for ti in at.text_input if ti.key == f"{prefix}_overall_rank_point_table").set_value(text)
    at.run()
    at.button[_find_button(at, "変更を保存")].click().run()

    assert any("全員まとめての順位ポイント表" in e.value for e in at.error)
    assert "overall_rank_point_table" not in db.get_tournament(tenant_id, tournament_id)["scoring_config"]


def test_admin_and_guest_see_final_ranking_after_final_round(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    _play_rounds(tenant_id, tournament_id, 2, random.Random(3))
    raw_token = db.create_guest_link(tournament_id, user_id)
    tournament = db.get_tournament(tenant_id, tournament_id)
    final = svc.compute_final_ranking(tournament)
    names = {m["member_id"]: m["member_name"] for m in db.get_tournament_members(tournament_id)}
    champion_name = names[final[0]["member_id"]]

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    assert not at.exception
    assert any("最終順位" in md.value for md in at.markdown)
    final_table = next(t for t in at.table if "素点" in t.value.columns)
    assert final_table.value.iloc[0]["順位"] == "優勝"
    assert final_table.value.iloc[0]["氏名"] == champion_name

    guest = AppTest.from_file(APP_PATH)
    guest.query_params["guest"] = raw_token
    guest.run()
    assert not guest.exception
    guest_final = next(t for t in guest.table if "素点" in t.value.columns)
    assert guest_final.value.iloc[0]["氏名"] == champion_name
    assert any("選手番号の若い方 の順で上位" in c.value for c in guest.caption)
    assert any("累計ポイントが多い方" in c.value for c in at.caption)


def test_final_ranking_waits_until_final_round_is_complete(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    _play_rounds(tenant_id, tournament_id, 1, random.Random(3))
    raw_token = db.create_guest_link(tournament_id, user_id)

    guest = AppTest.from_file(APP_PATH)
    guest.query_params["guest"] = raw_token
    guest.run()
    assert not guest.exception
    assert any("全卓の入力がそろうと表示されます" in c.value for c in guest.caption)
    assert not any("素点" in t.value.columns for t in guest.table)


def test_final_round_preview_says_rank_order_seating(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    _play_rounds(tenant_id, tournament_id, 1, random.Random(3))

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert any("累計順位の順に着席" in c.value for c in at.caption)


def test_warns_before_next_snake_round_when_previous_round_incomplete(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    tournament = db.get_tournament(tenant_id, tournament_id)
    rn, absent, tables = svc.run_next_round(tournament, random.Random(1))
    svc.save_confirmed_round(tournament_id, rn, absent, tables)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    assert not at.exception
    assert any("入力がまだそろっていません" in w.value for w in at.warning)


def test_first_round_preview_in_player_number_order_without_reroll(app_env):
    # 全回戦数を設定した蛇行の大会: 第1回戦は選手番号順。結果が変わらないので「作り直す」は出さない
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert any("選手番号（受付順）の順に着席" in c.value for c in at.caption)
    assert not any("作り直す" in b.label for b in at.button)
    texts = [m.value for m in at.markdown]
    assert any(t.startswith("卓1: 東: 選手1") and "北: 選手4" in t for t in texts)
    assert any(t.startswith("卓2: 東: 選手5") and "北: 選手8" in t for t in texts)


def test_first_round_without_total_rounds_keeps_reroll(app_env):
    config = {k: v for k, v in OVERALL_CONFIG.items() if k != "total_rounds"}
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=config)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert any("作り直す" in b.label for b in at.button)
    assert not any("選手番号（受付順）の順に着席" in c.value for c in at.caption)


def test_second_round_preview_has_no_reroll(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    _play_rounds(tenant_id, tournament_id, 1, random.Random(3))

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert not any("作り直す" in b.label for b in at.button)


# ---------------------------------------------------------------------------
# 成績入力の「回戦を選択」（2026-11-18 staging 確認で発見）
# 「次の回戦の卓組みを実行」などの st.rerun() のあとに、表示は第3回戦のまま
# 入力欄が第1回戦に戻っていた。
# ---------------------------------------------------------------------------

def _score_labels(at):
    """成績入力の入力欄の見出し（「氏名（東）」…）を画面の順に返す。"""
    return [ni.label for ni in at.number_input if ni.label.endswith(("（東）", "（南）", "（西）", "（北）"))]


def _expected_labels(tournament_id, round_number):
    names = {m["member_id"]: m["member_name"] for m in db.get_tournament_members(tournament_id)}
    round_row = next(r for r in db.get_rounds(tournament_id) if r["round_number"] == round_number)
    seats = sorted(db.get_round_seats(round_row["id"]),
                   key=lambda s: (s["table_number"], "東南西北".index(s["position"])))
    return [f"{names[s['member_id']]}（{s['position']}）" for s in seats]


def _round_select(at, tournament_id):
    return at.selectbox(key=f"score_round_select_{tournament_id}")


def test_round_select_keeps_choice_after_running_next_round(app_env):
    config = dict(OVERALL_CONFIG, total_rounds=5)
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=config)
    _play_rounds(tenant_id, tournament_id, 3, random.Random(3))
    assert _expected_labels(tournament_id, 3) != _expected_labels(tournament_id, 1)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    _round_select(at, tournament_id).set_value(3).run()
    assert _score_labels(at) == _expected_labels(tournament_id, 3)

    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert _round_select(at, tournament_id).value == 3
    assert _score_labels(at) == _expected_labels(tournament_id, 3)
    assert any(m.value == "**第3回戦の成績入力**" for m in at.markdown)
    assert any(b.label == "第3回戦の成績を保存" for b in at.button)


def test_saving_new_round_switches_score_entry_to_it(app_env):
    config = dict(OVERALL_CONFIG, total_rounds=5)
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=config)
    _play_rounds(tenant_id, tournament_id, 2, random.Random(3))

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    assert _round_select(at, tournament_id).value == 1  # 最初は第1回戦（これまでどおり）
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    at.button[_find_button(at, "この結果で保存")].click().run()
    assert not at.exception
    assert len(db.get_rounds(tournament_id)) == 3
    assert _round_select(at, tournament_id).value == 3
    assert _score_labels(at) == _expected_labels(tournament_id, 3)
    assert any(b.label == "第3回戦の成績を保存" for b in at.button)


def test_standings_tie_note_only_for_point_tournaments(app_env):
    user_id, tenant_id, tournament_id = _setup_admin_with_point_tournament(n=8, config=OVERALL_CONFIG)
    _play_rounds(tenant_id, tournament_id, 1, random.Random(3))
    raw_token = db.create_guest_link(tournament_id, user_id)
    note = "素点合計の多い方 → 選手番号の若い方 の順で上にしています（次の回戦の席決めも同じ順です）"

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    assert any(note in c.value for c in at.caption)

    guest = AppTest.from_file(APP_PATH)
    guest.query_params["guest"] = raw_token
    guest.run()
    assert any(note in c.value for c in guest.caption)

    tournament = db.get_tournament(tenant_id, tournament_id)
    db.update_tournament(
        tenant_id, tournament_id, tournament["name"], tournament["kind"], tournament["table_method"],
        "得点", SCORE_CONFIG,
        tournament["numbering_method"], None, None, tournament["status"],
    )
    guest2 = AppTest.from_file(APP_PATH)
    guest2.query_params["guest"] = raw_token
    guest2.run()
    assert not guest2.exception
    assert not any(note in c.value for c in guest2.caption)
