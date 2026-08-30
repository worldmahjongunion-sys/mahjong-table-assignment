"""運営専用の管理画面（全テナント一覧・監査ログの横断閲覧）のテスト。

OPERATOR_USERNAMES に載っているユーザーだけが画面を見られること、
一般ユーザーや他テナントの管理者には画面自体が一切描画されないことを確認する。
テストで使うテナント名・ユーザー名・メールアドレスはすべてダミー。
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


def _find_button(at, label_substr):
    for i, b in enumerate(at.button):
        if label_substr in b.label:
            return i
    raise ValueError(f"button not found: {label_substr}")


def _login(at, username, password):
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[_find_button(at, "Login")].click().run()


def _add_user(username, password, **kwargs):
    kwargs.setdefault("email_verified", True)
    return db.add_user(username, stauth.Hasher.hash(password), **kwargs)


def _has_operator_expander(at):
    return any("運営管理" in e.label for e in at.expander)


# ---- ダミーデータでの複数テナント準備 ----

def _seed_two_dummy_tenants():
    op_id = _add_user("ops_owner", "operator-pass-123", email="ops_owner@example.test")
    op_tenant_id = db.get_user_by_id(op_id)["tenant_id"]
    db.add_member(op_tenant_id, op_id, "ダミー太郎", "")
    db.record_audit_log(action="login", tenant_id=op_tenant_id, user_id=op_id, username="ops_owner")

    other_id = _add_user("dummy_admin", "other-pass-123", email="dummy_admin@example.test")
    other_tenant_id = db.get_user_by_id(other_id)["tenant_id"]
    db.add_member(other_tenant_id, other_id, "ダミー花子", "")
    db.add_member(other_tenant_id, other_id, "ダミー次郎", "")
    db.record_audit_log(
        action="member_add", tenant_id=other_tenant_id, user_id=other_id, username="dummy_admin",
        detail="member_id=1, name=ダミー花子",
    )
    return op_tenant_id, other_tenant_id


# ---- アクセス制御 ----

def test_non_operator_admin_does_not_see_operator_section(app_env):
    _add_user("dummy_admin", "other-pass-123", email="dummy_admin@example.test")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "dummy_admin", "other-pass-123")

    assert not at.exception
    assert not _has_operator_expander(at)


def test_operator_username_not_configured_hides_section_even_for_that_user(app_env):
    # OPERATOR_USERNAMES を設定していない状態では、誰であっても画面は出ない。
    _add_user("ops_owner", "operator-pass-123", email="ops_owner@example.test")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "ops_owner", "operator-pass-123")

    assert not at.exception
    assert not _has_operator_expander(at)


def test_operator_sees_operator_section(app_env, monkeypatch):
    monkeypatch.setenv("OPERATOR_USERNAMES", "ops_owner")
    _seed_two_dummy_tenants()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "ops_owner", "operator-pass-123")

    assert not at.exception
    assert _has_operator_expander(at)


def test_operator_section_lists_all_tenants_cross_tenant(app_env, monkeypatch):
    monkeypatch.setenv("OPERATOR_USERNAMES", "ops_owner")
    op_tenant_id, other_tenant_id = _seed_two_dummy_tenants()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "ops_owner", "operator-pass-123")

    tenant_table = at.dataframe[0].value
    tenant_names = set(tenant_table["テナント名"])
    assert {"ops_owner", "dummy_admin"} <= tenant_names
    member_counts = dict(zip(tenant_table["テナント名"], tenant_table["メンバー数"]))
    assert member_counts["ops_owner"] == 1
    assert member_counts["dummy_admin"] == 2


def test_operator_section_lists_audit_logs_cross_tenant(app_env, monkeypatch):
    monkeypatch.setenv("OPERATOR_USERNAMES", "ops_owner")
    _seed_two_dummy_tenants()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "ops_owner", "operator-pass-123")

    audit_table = at.dataframe[1].value
    actions_by_user = set(zip(audit_table["ユーザー"], audit_table["操作"]))
    assert ("ops_owner", "login") in actions_by_user
    assert ("dummy_admin", "member_add") in actions_by_user
    # ops_owner自身のログイン操作も横断ログに含まれる
    assert any(u == "ops_owner" and a == "login" for u, a in actions_by_user)


def test_member_role_user_does_not_see_operator_section_even_if_username_matches_typo(app_env, monkeypatch):
    # OPERATOR_USERNAMES の照合は完全一致（前後空白トリム・小文字化のみ）で、
    # 部分一致や大文字小文字違いですり抜けないことを確認する。
    monkeypatch.setenv("OPERATOR_USERNAMES", "ops_owner")
    _add_user("ops_owner2", "other-pass-123", email="ops_owner2@example.test")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "ops_owner2", "other-pass-123")

    assert not at.exception
    assert not _has_operator_expander(at)
