"""大会管理・回戦実行・成績入力・ゲスト共有リンクのapp.py側UI配線を
streamlit.testing.v1.AppTest で検証する。

AppTestは、同一セッション内で長く連続した画面遷移（特にst.formの出現・消失を
何度も挟む操作）を行うと内部の状態管理でKeyErrorを起こす既知の制約がある
（得点ポイント評価方式実装依頼.md対応時に確認済み）。そのため、各テストは
db.py/tournament_serviceで前提状態を直接組み立ててから、検証したいUI操作
だけを行う形にし、1つのAppTestセッション内の操作数を絞っている。
"""
from pathlib import Path

import pytest
import streamlit_authenticator as stauth
from streamlit.testing.v1 import AppTest

import db
import tournament_service as svc

APP_PATH = str(Path(__file__).parent.parent / "app.py")

SCORE_CONFIG = {
    "start_point": 35000,
    "return_point": 40000,
    "oka": 20000,
    "tobi_amount": 1000,
    "uma_table": {
        1: [48000, -8000, -16000, -24000],
        2: [24000, 8000, -8000, -24000],
        3: [12000, 8000, 4000, -24000],
    },
}

POINT_CONFIG = {"rank_point_table": [3, 1, -1, -3], "start_point": 35000}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setenv("AUTH_COOKIE_NAME", "test_cookie")
    monkeypatch.setenv("AUTH_COOKIE_KEY", "test-cookie-key-at-least-32-bytes-long!!")
    monkeypatch.setenv("AUTH_INVITE_CODE", "correct-horse-battery-staple")
    return tmp_path


def _add_user(username, password, **kwargs):
    kwargs.setdefault("email_verified", True)
    return db.add_user(username, stauth.Hasher.hash(password), **kwargs)


def _find_button(at, label_substr):
    for i, b in enumerate(at.button):
        if label_substr in b.label:
            return i
    raise ValueError(f"button not found: {label_substr}")


def _login(at, username, password):
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[_find_button(at, "Login")].click().run()


def _setup_admin_with_tournament(tenant_and_members=True):
    user_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(user_id)["tenant_id"]
    member_ids = []
    if tenant_and_members:
        member_ids = [db.add_member(tenant_id, user_id, name, "") for name in ("太郎", "次郎", "三郎", "四郎")]
    tournament_id = db.create_tournament(
        tenant_id, "テスト大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    return user_id, tenant_id, member_ids, tournament_id


def test_tournament_list_shows_created_tournament(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    assert any("テスト大会" in e.label for e in at.expander)


def test_admin_can_add_participants_via_checkboxes(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    for checkbox in at.checkbox:
        if checkbox.label in ("太郎", "次郎", "三郎", "四郎"):
            checkbox.check()
    at.run()
    at.button[_find_button(at, "選択したメンバーを追加")].click().run()

    assert not at.exception
    members_in_tournament = db.get_tournament_members(tournament_id)
    assert {m["member_name"] for m in members_in_tournament} == {"太郎", "次郎", "三郎", "四郎"}


def test_admin_can_run_and_save_a_round(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert any("プレビュー" in md.value for md in at.markdown)

    at.button[_find_button(at, "この結果で保存")].click().run()
    assert not at.exception
    assert db.count_rounds(tournament_id) == 1

    rounds = db.get_rounds(tournament_id)
    seats = db.get_round_seats(rounds[0]["id"])
    assert len(seats) == 4


def test_admin_can_enter_round_results(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    svc.save_confirmed_round(tournament_id, round_number, absent, tables)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("score_")]
    assert len(score_inputs) == 4
    scores = [50000, 20000, 15000, -5000]
    for ni, score in zip(score_inputs, scores):
        ni.set_value(score)
    at.run()

    at.button[_find_button(at, "成績を保存")].click().run()
    assert not at.exception

    rounds = db.get_rounds(tournament_id)
    saved_results = db.get_round_results(rounds[0]["id"])
    assert sorted(saved_results.values(), reverse=True) == scores


def test_admin_saving_one_table_does_not_zero_out_untouched_table(app_env):
    """主催者の成績入力フォームは回戦内の全卓を1画面にまとめて表示するため、
    ある卓だけ入力して保存すると、まだ誰も入力していない別の卓の選手が
    初期値0のまま保存されてしまわないか（ゲストの未入力を潰してしまわないか）を確認する。"""
    user_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(user_id)["tenant_id"]
    member_ids = [db.add_member(tenant_id, user_id, f"選手{i}", "") for i in range(8)]
    tournament_id = db.create_tournament(
        tenant_id, "テスト大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    svc.save_confirmed_round(tournament_id, round_number, absent, tables)
    assert len(tables) == 2  # 8人なので2卓

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("score_")]
    assert len(score_inputs) == 8

    rounds = db.get_rounds(tournament_id)
    seats = db.get_round_seats(rounds[0]["id"])
    table1_member_ids = {s["member_id"] for s in seats if s["table_number"] == 1}
    table2_member_ids = {s["member_id"] for s in seats if s["table_number"] == 2}

    # 卓1だけ入力し、卓2は初期値0のまま何も触らない
    table1_scores = [40000, 30000, 20000, 10000]
    i = 0
    for ni in score_inputs:
        member_id = int(ni.key.rsplit("_", 1)[1])
        if member_id in table1_member_ids:
            ni.set_value(table1_scores[i])
            i += 1
    at.run()

    at.button[_find_button(at, "成績を保存")].click().run()
    assert not at.exception

    saved_results = db.get_round_results(rounds[0]["id"])
    assert set(saved_results.keys()) == table1_member_ids  # 卓2の選手は一切保存されない
    assert sorted(saved_results.values(), reverse=True) == table1_scores

    # 卓2はDB上「未入力」のままなので、ゲストが本来の素点を送信できる
    table2_scores = {mid: 35000 for mid in table2_member_ids}
    db.submit_guest_round_results(rounds[0]["id"], table2_scores, {})
    assert db.get_round_results(rounds[0]["id"]) == {**saved_results, **table2_scores}


def test_admin_can_issue_and_revoke_guest_link(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert db.get_active_guest_link(tournament_id) is None

    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    assert not at.exception
    assert db.get_active_guest_link(tournament_id) is not None
    assert any("code" in str(type(c)).lower() for c in at.code) or len(at.code) > 0

    at.button[_find_button(at, "リンクを無効化する")].click().run()
    assert not at.exception
    assert db.get_active_guest_link(tournament_id) is None


def test_non_admin_cannot_see_tournament_create_or_delete_controls(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    db.add_user(
        "member1", stauth.Hasher.hash("memberpass123"),
        email="member1@example.com", tenant_id=tenant_id, role="member", email_verified=True,
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "member1", "memberpass123")

    assert not at.exception
    assert any("テスト大会" in e.label for e in at.expander)  # 一覧の閲覧はできる
    assert not any("大会を作成" in b.label for b in at.button)
    assert not any("この大会を削除" in b.label for b in at.button)
    assert not any("次の回戦の卓組みを実行" in b.label for b in at.button)


def test_guest_link_token_survives_round_trip_through_db(app_env):
    """ゲストトークンのURL埋め込み・照合の前提(db.get_tournament_by_guest_token)が
    実際にapp.pyが発行したトークンで機能することを確認する(Stage 2でゲスト画面が
    このトークンを使う前提の回帰確認)。"""
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    raw_token = db.create_guest_link(tournament_id, user_id)

    resolved = db.get_tournament_by_guest_token(raw_token)
    assert resolved is not None
    assert resolved["id"] == tournament_id


def test_admin_can_reissue_guest_link_end_to_end(app_env):
    """3.6対応: 「リンクを再発行する」ボタンを通しで確認する。旧トークンは失効し、
    新トークンで大会が特定できること。"""
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    old_link = db.get_active_guest_link(tournament_id)
    old_code_value = next(c.value for c in at.code if "?guest=" in c.value)
    old_token = old_code_value.rsplit("?guest=", 1)[1]
    assert db.get_tournament_by_guest_token(old_token) is not None

    at.button[_find_button(at, "リンクを再発行する")].click().run()
    assert not at.exception
    new_link = db.get_active_guest_link(tournament_id)
    assert new_link["id"] != old_link["id"]
    new_code_value = next(c.value for c in at.code if "?guest=" in c.value)
    new_token = new_code_value.rsplit("?guest=", 1)[1]
    assert new_token != old_token

    assert db.get_tournament_by_guest_token(old_token) is None
    resolved = db.get_tournament_by_guest_token(new_token)
    assert resolved is not None
    assert resolved["id"] == tournament_id


# ---------------------------------------------------------------------------
# Stage 2: ゲスト向け画面(ゲスト向け画面実装依頼_Stage2.md)
# ---------------------------------------------------------------------------

def _setup_tournament_with_round(scoring_mode="得点", scoring_config=SCORE_CONFIG):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    if scoring_mode != "得点":
        db.update_tournament(
            tenant_id, tournament_id, "テスト大会", "ワンデー", "蛇行", scoring_mode,
            scoring_config, "受付順", None, None, "準備中",
        )
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    svc.save_confirmed_round(tournament_id, round_number, absent, tables)
    raw_token = db.create_guest_link(tournament_id, user_id)
    return user_id, tenant_id, member_ids, tournament_id, raw_token


def test_guest_view_opens_with_valid_token(app_env):
    *_, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("テスト大会" in md.value for md in list(at.markdown) + list(at.subheader))
    assert not any("Login" in b.label for b in at.button)


def test_guest_view_shows_message_for_invalid_token(app_env):
    _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = "no-such-token"
    at.run()

    assert not at.exception
    assert any("このリンクは使えません" in e.value for e in at.error)
    assert not any("Login" in b.label for b in at.button)
    assert len(at.text_input) == 0  # ログインフォームが描画されていない


def test_guest_view_shows_message_for_revoked_token(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    link = db.get_active_guest_link(tournament_id)
    db.revoke_guest_link(link["id"])

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("このリンクは使えません" in e.value for e in at.error)


def test_guest_view_cannot_see_other_tournament_or_admin_controls(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    other_user_id = _add_user("admin2", "otherpass123", email="admin2@example.com")
    other_tenant_id = db.get_user_by_id(other_user_id)["tenant_id"]
    db.create_tournament(
        other_tenant_id, "他人の大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    page_text = " ".join(md.value for md in list(at.markdown) + list(at.subheader) + list(at.caption))
    assert "他人の大会" not in page_text
    assert not any("ログアウト" in b.label for b in at.button)
    assert not any("大会を作成" in b.label for b in at.button)
    assert not any("次の回戦の卓組みを実行" in b.label for b in at.button)


def test_guest_score_submit_disabled_when_sum_off_by_one(app_env):
    """5章 境界値: 合計が開始点数×4から1点ずれている場合は送信できない。"""
    *_, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    assert len(score_inputs) == 4

    off_by_one = [60000, 40000, 30000, 9999]  # 合計139999(期待値140000から1点ずれ)
    for ni, v in zip(score_inputs, off_by_one):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()

    submit_btn = at.button[_find_button(at, "この内容で送信する")]
    assert submit_btn.disabled


def test_guest_score_submit_enabled_and_saves_when_sum_exact(app_env):
    """5章: 合計がぴったり開始点数×4のときだけ送信でき、マイナスの素点を含む入力も
    正しく保存される。"""
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    exact = [70000, 50000, 25000, -5000]  # 合計140000(ぴったり)。マイナスの素点も含める
    for ni, v in zip(score_inputs, exact):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()

    submit_btn = at.button[_find_button(at, "この内容で送信する")]
    assert not submit_btn.disabled
    submit_btn.click().run()
    assert not at.exception

    rounds = db.get_rounds(tournament_id)
    saved = db.get_round_results(rounds[0]["id"])
    assert sorted(saved.values(), reverse=True) == sorted(exact, reverse=True)
    meta = db.get_round_result_meta(rounds[0]["id"])
    assert all(m["input_source"] == "guest" for m in meta.values())


def test_guest_view_already_submitted_table_is_read_only(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    rounds = db.get_rounds(tournament_id)
    db.save_round_results(rounds[0]["id"], {mid: 35000 for mid in member_ids}, {})

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("すでに入力済みです" in i.value for i in at.info)
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    assert len(score_inputs) == 0


def test_guest_view_shows_tobi_input_for_score_mode(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_mode="得点"
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    score_inputs[0].set_value(0)
    at.run()

    assert any(ms.key and ms.key.startswith("guest_busters_") for ms in at.multiselect)


def test_guest_view_hides_tobi_input_for_point_mode(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_mode="ポイント", scoring_config=POINT_CONFIG
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    score_inputs[0].set_value(0)
    at.run()

    assert not any(ms.key and ms.key.startswith("guest_busters_") for ms in at.multiselect)


def test_guest_view_blocks_score_entry_when_start_point_missing(app_env):
    """4章①: ポイント方式で開始点数が未設定(既存大会相当)の場合、成績入力を止める。"""
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_mode="ポイント", scoring_config={"rank_point_table": [3, 1, -1, -3]}
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("開始点数を設定するまで入力できません" in w.value for w in at.warning)
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    assert len(score_inputs) == 0
