"""app.py の主要フロー（ログイン・レート制限・監査ログ・エクスポートのゲート）を
streamlit.testing.v1.AppTest で実際にスクリプトを実行して検証する。

db.py単体のテストと違い、app.py側の配線ミス（例: レート制限の判定を書いたのに
実際のボタンには繋がっていない、等）まで検出できる。
"""

from pathlib import Path

import pytest
import streamlit_authenticator as stauth
from streamlit.testing.v1 import AppTest

import db

APP_PATH = str(Path(__file__).parent.parent / "app.py")


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setenv("AUTH_COOKIE_NAME", "test_cookie")
    monkeypatch.setenv("AUTH_COOKIE_KEY", "test-cookie-key-at-least-32-bytes-long!!")
    monkeypatch.setenv("AUTH_INVITE_CODE", "correct-horse-battery-staple")
    return tmp_path


def _find_input(at, label_substr):
    for i, ti in enumerate(at.text_input):
        if label_substr in ti.label:
            return i
    raise ValueError(f"text_input not found: {label_substr}")


def _find_button(at, label_substr):
    for i, b in enumerate(at.button):
        if label_substr in b.label:
            return i
    raise ValueError(f"button not found: {label_substr}")


def _login(at, username, password):
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[_find_button(at, "ログイン")].click().run()


def _add_user(username, password, **kwargs):
    kwargs.setdefault("email_verified", True)
    return db.add_user(username, stauth.Hasher.hash(password), **kwargs)


# ---- ログイン ----

def test_correct_login_succeeds_and_records_audit_log(app_env):
    _add_user("taro", "correct-password-123", email="taro@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "taro", "correct-password-123")

    assert not at.exception
    assert at.session_state["authentication_status"] is True
    tenant_id = db.get_user_by_username("taro")["tenant_id"]
    logs = db.get_audit_logs(tenant_id)
    assert any(l["action"] == "login" and l["username"] == "taro" for l in logs)


def test_wrong_password_shows_generic_error(app_env):
    _add_user("taro", "correct-password-123", email="taro@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "taro", "wrong-password")

    assert not at.exception
    assert at.session_state["authentication_status"] is False
    assert any("ユーザー名またはパスワードが違います" in e.value for e in at.error)


def test_login_is_rate_limited_after_threshold_even_with_correct_password(app_env):
    _add_user("taro", "correct-password-123", email="taro@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    for _ in range(db.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
        _login(at, "taro", "wrong-password")

    # 上限に達した直後は正しいパスワードでもブロックされる
    _login(at, "taro", "correct-password-123")

    assert at.session_state["authentication_status"] is not True
    assert any("ログイン試行が多すぎます" in e.value for e in at.error)


# ---- サインアップの招待コード ----

def test_signup_invite_code_rate_limited_after_threshold(app_env):
    at = AppTest.from_file(APP_PATH)
    at.run()

    def try_signup(i, code):
        at.text_input[_find_input(at, "ユーザー名")].set_value(f"newadmin{i}")
        at.text_input[_find_input(at, "メールアドレス")].set_value(f"newadmin{i}@example.com")
        at.text_input[_find_input(at, "パスワード（8文字以上）")].set_value("somepassword123")
        at.text_input[_find_input(at, "パスワード（確認）")].set_value("somepassword123")
        at.text_input[_find_input(at, "招待コード")].set_value(code)
        at.button[_find_button(at, "登録")].click().run()

    for i in range(db.INVITE_CODE_RATE_LIMIT_MAX_ATTEMPTS):
        try_signup(i, "wrong-code")

    # 上限到達後は、正しい合言葉を入れてもブロックされる
    try_signup(99, "correct-horse-battery-staple")

    assert not at.exception
    assert any("招待コードの試行回数が多すぎます" in e.value for e in at.error)
    assert db.get_user_by_username("newadmin99") is None


# ---- メンバー登録・監査ログ・Freeプラン上限 ----

def test_admin_adds_member_and_it_is_audit_logged(app_env):
    admin_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(admin_id)["tenant_id"]

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.text_input[_find_input(at, "名前")].set_value("テスト太郎")
    at.button[_find_button(at, "登録")].click().run()

    assert not at.exception
    assert any("を登録しました" in s.value for s in at.success)
    logs = db.get_audit_logs(tenant_id)
    assert any(l["action"] == "member_add" and "テスト太郎" in (l["detail"] or "") for l in logs)


def test_free_plan_member_limit_is_enforced_via_ui(app_env):
    admin_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(admin_id)["tenant_id"]
    for i in range(db.FREE_PLAN_MEMBER_LIMIT):
        db.add_member(tenant_id, admin_id, f"既存{i}", "")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.text_input[_find_input(at, "名前")].set_value("上限超え")
    at.button[_find_button(at, "登録")].click().run()

    assert not at.exception
    assert any("上限" in e.value for e in at.error)
    assert db.count_active_members(tenant_id) == db.FREE_PLAN_MEMBER_LIMIT


# ---- CSV/Excelエクスポートのゲート ----

def test_export_buttons_hidden_for_non_admin(app_env):
    admin_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(admin_id)["tenant_id"]
    _add_user("member1", "memberpass123", email="member1@example.com", tenant_id=tenant_id, role="member")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "member1", "memberpass123")

    assert not at.exception
    button_labels = [b.label for b in at.button]
    assert not any("ダウンロード" in label for label in button_labels)
    assert any("管理者のみ" in c.value for c in at.caption)


def test_export_buttons_locked_for_admin_on_free_plan(app_env):
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    locked_buttons = [b for b in at.button if "🔒" in b.label and "ダウンロード" in b.label]
    assert len(locked_buttons) == 2
    assert all(b.disabled for b in locked_buttons)


def test_export_buttons_enabled_for_admin_on_pro_plan(app_env):
    admin_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(admin_id)["tenant_id"]
    db.update_tenant_plan(tenant_id, "pro")
    db.add_member(tenant_id, admin_id, "太郎", "")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    download_buttons = [b for b in at.download_button if "ダウンロード" in b.label]
    assert len(download_buttons) == 2
    assert all(not b.disabled for b in download_buttons)


# ---- 表紙画面（評価方式の選択） ----
# 得点ポイント評価方式実装依頼.md 3.3対応。


def _find_number_input(at, label_substr):
    for i, ni in enumerate(at.number_input):
        if label_substr in ni.label:
            return i
    raise ValueError(f"number_input not found: {label_substr}")


def test_cover_screen_shows_two_choice_buttons_before_a_mode_is_picked(app_env):
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    assert any("①得点で評価" in b.label for b in at.button)
    assert any("②ポイントで評価" in b.label for b in at.button)
    assert not any("現在の評価方式" in md.value for md in at.markdown)


def test_choosing_score_mode_shows_uma_form_with_defaults_and_saves(app_env):
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "①得点で評価")].click().run()
    assert not at.exception
    assert any("現在の評価方式" in md.value and "①得点で評価" in md.value for md in at.markdown)

    # プレースホルダはscoring_logic.DEFAULT_UMA_CONFIGの参考値が入っている。
    # 画面は100点単位（35,000点は350）で表示し、保存する値は実際の点数のまま。
    assert at.number_input[_find_number_input(at, "開始点数")].value == 350
    assert at.number_input[_find_number_input(at, "返し点")].value == 400

    at.button[_find_button(at, "設定を保存")].click().run()
    assert not at.exception
    config = at.session_state["scoring_config"]
    assert config["start_point"] == 35000
    assert config["return_point"] == 40000
    assert config["uma_table"][1] == [48000, -8000, -16000, -24000]


def test_score_mode_config_is_entered_in_hundreds_and_saved_as_actual_points(app_env):
    """開始点数・返し点・オカ・飛び賞額・ウマは100点単位で入力し、実際の点数で保持する。"""
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "①得点で評価")].click().run()

    at.number_input[_find_number_input(at, "開始点数")].set_value(250)
    at.number_input[_find_number_input(at, "返し点")].set_value(300)
    at.number_input[_find_number_input(at, "0人浮き")].set_value(200)
    at.number_input[_find_number_input(at, "飛び賞額")].set_value(10)
    at.run()
    at.button[_find_button(at, "設定を保存")].click().run()

    assert not at.exception
    config = at.session_state["scoring_config"]
    assert (config["start_point"], config["return_point"], config["oka"], config["tobi_amount"]) == (
        25000, 30000, 20000, 1000,
    )
    assert config["uma_table"][1] == [48000, -8000, -16000, -24000]  # 画面上は480,-80,-160,-240


def test_score_mode_config_with_wrong_digits_shows_unit_hint_and_is_not_saved(app_env):
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "①得点で評価")].click().run()

    at.number_input[_find_number_input(at, "開始点数")].set_value(25000)  # 25,000点のつもりで実点数を入力
    at.run()
    at.button[_find_button(at, "設定を保存")].click().run()

    assert not at.exception
    assert any("100点単位で入力してください(例:45,800点なら458)" in e.value for e in at.error)
    assert "scoring_config" not in at.session_state or not at.session_state["scoring_config"]


def test_choosing_points_mode_shows_rank_point_form_with_defaults_and_saves(app_env):
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "②ポイントで評価")].click().run()
    assert not at.exception
    assert any("現在の評価方式" in md.value and "②ポイントで評価" in md.value for md in at.markdown)
    rank_point_values = [
        ni.value for ni in at.number_input if ni.label in ("1位", "2位", "3位", "4位")
    ]
    assert rank_point_values == [3, 1, -1, -3]

    at.button[_find_button(at, "設定を保存")].click().run()
    assert not at.exception
    config = at.session_state["scoring_config"]
    assert config["rank_point_table"] == [3, 1, -1, -3]


def test_reset_button_returns_to_choice_screen(app_env):
    _add_user("admin1", "adminpass123", email="admin1@example.com")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "①得点で評価")].click().run()
    at.button[_find_button(at, "設定を保存")].click().run()
    at.button[_find_button(at, "評価方式を選び直す")].click().run()

    assert not at.exception
    assert any("①得点で評価" in b.label for b in at.button)
    assert any("②ポイントで評価" in b.label for b in at.button)
    assert at.session_state["scoring_mode"] is None
