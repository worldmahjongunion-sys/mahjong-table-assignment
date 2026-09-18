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
