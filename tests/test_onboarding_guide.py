"""初回ガイド（テナント作成→招待リンク発行→最初のメンバーが参加）のテスト。

チームメンバー（ログインアカウント）が管理者本人しかいない間だけ表示され、
2人目が参加すると自動的に消えることを確認する。卓組み生成機能はまだ未実装のため、
ガイドの導線に含めていないことも確認する。
"""

from pathlib import Path

import pytest
import streamlit_authenticator as stauth
from streamlit.testing.v1 import AppTest

import db

APP_PATH = str(Path(__file__).parent.parent / "app.py")

_FORBIDDEN_KEYWORDS = ("卓組み生成", "半荘")


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


def _has_guide(at):
    return any("はじめに" in s.value for s in at.subheader)


def _all_text(at):
    texts = [m.value for m in at.markdown]
    texts += [c.value for c in at.caption]
    texts += [s.value for s in at.subheader]
    return "\n".join(texts)


# ---- 表示条件 ----

def test_fresh_admin_sees_guide_with_step1_done_and_step2_pending(app_env):
    _add_user("dummy_owner", "owner-pass-123", email="dummy_owner@example.test")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "dummy_owner", "owner-pass-123")

    assert not at.exception
    assert _has_guide(at)
    text = _all_text(at)
    assert "✅" in text and "テナント作成" in text
    assert "招待リンクを発行しましょう" in text
    assert "済みです" not in text


def test_member_role_user_does_not_see_guide(app_env):
    owner_id = _add_user("dummy_owner", "owner-pass-123", email="dummy_owner@example.test")
    tenant_id = db.get_user_by_id(owner_id)["tenant_id"]
    _add_user("dummy_member", "member-pass-123", email="dummy_member@example.test", tenant_id=tenant_id, role="member")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "dummy_member", "member-pass-123")

    assert not at.exception
    assert not _has_guide(at)


# ---- 進捗表示 ----

def test_after_invite_issued_step2_shows_done(app_env):
    owner_id = _add_user("dummy_owner", "owner-pass-123", email="dummy_owner@example.test")
    tenant_id = db.get_user_by_id(owner_id)["tenant_id"]
    db.create_tenant_invite(tenant_id, owner_id, role="member")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "dummy_owner", "owner-pass-123")

    assert not at.exception
    assert _has_guide(at)
    text = _all_text(at)
    assert "済みです" in text
    assert "招待リンクを発行しましょう" not in text


def test_guide_disappears_once_second_member_joins(app_env):
    owner_id = _add_user("dummy_owner", "owner-pass-123", email="dummy_owner@example.test")
    tenant_id = db.get_user_by_id(owner_id)["tenant_id"]
    db.create_tenant_invite(tenant_id, owner_id, role="member")
    _add_user(
        "dummy_member", "member-pass-123", email="dummy_member@example.test",
        tenant_id=tenant_id, role="member",
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "dummy_owner", "owner-pass-123")

    assert not at.exception
    assert not _has_guide(at)


# ---- 卓組み生成機能への導線を含まないこと ----

def test_guide_does_not_reference_unimplemented_table_assignment_feature(app_env):
    _add_user("dummy_owner", "owner-pass-123", email="dummy_owner@example.test")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "dummy_owner", "owner-pass-123")

    assert _has_guide(at)
    text = _all_text(at)
    for keyword in _FORBIDDEN_KEYWORDS:
        assert keyword not in text
