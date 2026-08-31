"""テナント境界の再検証。

実在の複数テナント（管理者・一般ユーザー・メンバー・招待リンク・監査ログ・
レート制限イベントを持つ）を用意し、あるテナントの操作が他のテナントの
データに一切影響しない／見えないことを、db.py の各関数と実際のapp.py画面
（AppTest）の両方で確認する。
"""

from pathlib import Path

import pytest
import streamlit_authenticator as stauth
from streamlit.testing.v1 import AppTest

import db

APP_PATH = str(Path(__file__).parent.parent / "app.py")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return tmp_path


@pytest.fixture
def two_tenants(temp_db):
    """互いに無関係な2つのテナント（管理者・一般ユーザー・メンバー2名・
    招待リンク・監査ログ・レート制限イベント）を用意する。"""

    admin_a_id = db.add_user("admin_a", "hash_a", email="admin_a@example.com", email_verified=True)
    tenant_a_id = db.get_user_by_id(admin_a_id)["tenant_id"]
    member_a1 = db.add_member(tenant_a_id, admin_a_id, "たろうA", "秘密メモA")
    member_a2 = db.add_member(tenant_a_id, admin_a_id, "じろうA", "")
    invite_a_token = db.create_tenant_invite(tenant_a_id, admin_a_id, role="member")
    db.record_audit_log(action="login", tenant_id=tenant_a_id, user_id=admin_a_id, username="admin_a")
    db.record_audit_log(
        action="member_add", tenant_id=tenant_a_id, user_id=admin_a_id, username="admin_a",
        detail=f"member_id={member_a1}, name=たろうA",
    )
    db.update_tenant_plan(
        tenant_a_id, "pro", stripe_customer_id="cus_a", actor_user_id=admin_a_id, actor_username="admin_a"
    )
    db.record_rate_limit_event("login:admin_a")

    admin_b_id = db.add_user("admin_b", "hash_b", email="admin_b@example.com", email_verified=True)
    tenant_b_id = db.get_user_by_id(admin_b_id)["tenant_id"]
    member_b1 = db.add_member(tenant_b_id, admin_b_id, "たろうB", "秘密メモB")
    member_b2 = db.add_member(tenant_b_id, admin_b_id, "じろうB", "")
    invite_b_token = db.create_tenant_invite(tenant_b_id, admin_b_id, role="member")
    db.record_audit_log(action="login", tenant_id=tenant_b_id, user_id=admin_b_id, username="admin_b")
    db.record_audit_log(
        action="member_add", tenant_id=tenant_b_id, user_id=admin_b_id, username="admin_b",
        detail=f"member_id={member_b1}, name=たろうB",
    )

    return {
        "tenant_a_id": tenant_a_id,
        "admin_a_id": admin_a_id,
        "member_a1": member_a1,
        "member_a2": member_a2,
        "invite_a_token": invite_a_token,
        "tenant_b_id": tenant_b_id,
        "admin_b_id": admin_b_id,
        "member_b1": member_b1,
        "member_b2": member_b2,
        "invite_b_token": invite_b_token,
    }


# ---- メンバー一覧 ----

def test_get_members_never_returns_other_tenants_members(two_tenants):
    members_a = db.get_members(two_tenants["tenant_a_id"], include_retired=True)
    members_b = db.get_members(two_tenants["tenant_b_id"], include_retired=True)

    names_a = {m["name"] for m in members_a}
    names_b = {m["name"] for m in members_b}
    assert names_a == {"たろうA", "じろうA"}
    assert names_b == {"たろうB", "じろうB"}
    assert names_a.isdisjoint(names_b)


def test_member_exists_is_scoped_to_tenant(two_tenants):
    assert db.member_exists(two_tenants["tenant_a_id"], "たろうA") is True
    assert db.member_exists(two_tenants["tenant_a_id"], "たろうB") is False
    assert db.member_exists(two_tenants["tenant_b_id"], "たろうA") is False


def test_count_active_members_is_scoped_to_tenant(two_tenants):
    assert db.count_active_members(two_tenants["tenant_a_id"]) == 2
    assert db.count_active_members(two_tenants["tenant_b_id"]) == 2


def test_admin_a_cannot_update_tenant_b_member_using_its_real_id(two_tenants):
    """tenant_aのadminがtenant_bのmember_idを（推測などで）知っていても、
    自分のtenant_idと組み合わせて呼ぶ限り更新できないことを確認する。"""
    db.update_member(two_tenants["tenant_a_id"], two_tenants["member_b1"], "改ざんされた名前", "改ざん")

    member_b = next(
        m for m in db.get_members(two_tenants["tenant_b_id"], include_retired=True)
        if m["id"] == two_tenants["member_b1"]
    )
    assert member_b["name"] == "たろうB"
    assert member_b["memo"] == "秘密メモB"


def test_admin_a_cannot_retire_tenant_b_member(two_tenants):
    db.retire_member(two_tenants["tenant_a_id"], two_tenants["member_b1"])

    member_b = next(
        m for m in db.get_members(two_tenants["tenant_b_id"], include_retired=True)
        if m["id"] == two_tenants["member_b1"]
    )
    assert member_b["is_active"] == 1


def test_admin_a_cannot_restore_tenant_b_member(two_tenants):
    db.retire_member(two_tenants["tenant_b_id"], two_tenants["member_b1"])  # 正規に引退させておく

    db.restore_member(two_tenants["tenant_a_id"], two_tenants["member_b1"])  # tenant_aから復帰を試みる

    member_b = next(
        m for m in db.get_members(two_tenants["tenant_b_id"], include_retired=True)
        if m["id"] == two_tenants["member_b1"]
    )
    assert member_b["is_active"] == 0  # 引退のままで、復帰されていない


def test_add_member_free_plan_limit_is_per_tenant(two_tenants):
    """tenant_bのメンバー数がtenant_aのFreeプラン上限判定に影響しないことを確認する。"""
    for i in range(db.FREE_PLAN_MEMBER_LIMIT):
        db.add_member(two_tenants["tenant_b_id"], two_tenants["admin_b_id"], f"埋め要員{i}", "")
    assert db.count_active_members(two_tenants["tenant_b_id"]) >= db.FREE_PLAN_MEMBER_LIMIT

    # tenant_aはまだ2人なので、Free上限（20人）には全く達していない
    result = db.add_member(
        two_tenants["tenant_a_id"], two_tenants["admin_a_id"], "追加太郎", "",
        max_members=db.FREE_PLAN_MEMBER_LIMIT,
    )
    assert result is not None


# ---- 招待リンク ----

def test_tenant_invite_belongs_only_to_issuing_tenant(two_tenants):
    invite_a = db.get_tenant_invite(two_tenants["invite_a_token"])
    invite_b = db.get_tenant_invite(two_tenants["invite_b_token"])

    assert invite_a["tenant_id"] == two_tenants["tenant_a_id"]
    assert invite_b["tenant_id"] == two_tenants["tenant_b_id"]


def test_count_tenant_invites_this_month_is_scoped_to_tenant(two_tenants):
    # fixtureで各テナント1件ずつ発行済み。tenant_aでさらに1件発行する。
    db.create_tenant_invite(two_tenants["tenant_a_id"], two_tenants["admin_a_id"])

    assert db.count_tenant_invites_this_month(two_tenants["tenant_a_id"]) == 2
    assert db.count_tenant_invites_this_month(two_tenants["tenant_b_id"]) == 1


def test_tenant_b_invite_token_does_not_let_you_join_tenant_a(two_tenants):
    """招待トークンは発行元テナントのIDしか持たないため、
    tenant_bのトークンで参加してもtenant_aには絶対に参加できない。"""
    invite = db.get_tenant_invite(two_tenants["invite_b_token"])
    assert invite["tenant_id"] != two_tenants["tenant_a_id"]
    assert invite["tenant_id"] == two_tenants["tenant_b_id"]


# ---- プラン情報 ----

def test_tenant_plan_change_does_not_affect_other_tenant(two_tenants):
    tenant_a = db.get_tenant(two_tenants["tenant_a_id"])
    tenant_b = db.get_tenant(two_tenants["tenant_b_id"])

    assert tenant_a["plan"] == "pro"
    assert tenant_a["stripe_customer_id"] == "cus_a"
    assert tenant_b["plan"] == "free"  # tenant_aだけをProにしたので、tenant_bはFreeのまま
    assert tenant_b["stripe_customer_id"] is None


def test_get_tenant_by_stripe_customer_id_does_not_cross_tenants(two_tenants):
    db.update_tenant_plan(two_tenants["tenant_b_id"], "pro", stripe_customer_id="cus_b")

    found_by_cus_a = db.get_tenant_by_stripe_customer_id("cus_a")
    found_by_cus_b = db.get_tenant_by_stripe_customer_id("cus_b")

    assert found_by_cus_a["id"] == two_tenants["tenant_a_id"]
    assert found_by_cus_b["id"] == two_tenants["tenant_b_id"]


# ---- 監査ログ ----

def test_get_audit_logs_never_returns_other_tenants_entries(two_tenants):
    logs_a = db.get_audit_logs(two_tenants["tenant_a_id"])
    logs_b = db.get_audit_logs(two_tenants["tenant_b_id"])

    assert all(log["tenant_id"] == two_tenants["tenant_a_id"] for log in logs_a)
    assert all(log["tenant_id"] == two_tenants["tenant_b_id"] for log in logs_b)
    usernames_in_a_logs = {log["username"] for log in logs_a}
    usernames_in_b_logs = {log["username"] for log in logs_b}
    assert "admin_b" not in usernames_in_a_logs
    assert "admin_a" not in usernames_in_b_logs


def test_plan_change_audit_log_is_scoped_to_the_tenant_that_changed(two_tenants):
    # fixtureでtenant_aだけをProに変更済み（plan_changeログが1件入っているはず）
    logs_a = db.get_audit_logs(two_tenants["tenant_a_id"])
    logs_b = db.get_audit_logs(two_tenants["tenant_b_id"])

    assert any(log["action"] == "plan_change" for log in logs_a)
    assert not any(log["action"] == "plan_change" for log in logs_b)


# ---- レート制限イベント ----

def test_login_rate_limit_bucket_is_isolated_per_username(two_tenants):
    # fixtureでadmin_aは既に1回ログイン失敗を記録済み。さらに4回失敗させ、上限(5)に到達させる。
    for _ in range(4):
        db.record_rate_limit_event("login:admin_a")

    assert db.is_rate_limited(
        "login:admin_a", db.LOGIN_RATE_LIMIT_MAX_ATTEMPTS, db.LOGIN_RATE_LIMIT_WINDOW_MINUTES
    ) is True
    # tenant_bの管理者は一度も失敗していないので、影響を受けていない
    assert db.is_rate_limited(
        "login:admin_b", db.LOGIN_RATE_LIMIT_MAX_ATTEMPTS, db.LOGIN_RATE_LIMIT_WINDOW_MINUTES
    ) is False


def test_password_reset_rate_limit_bucket_is_isolated_per_email(two_tenants):
    for _ in range(db.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS):
        db.record_rate_limit_event("password_reset:admin_a@example.com")

    assert db.is_rate_limited(
        "password_reset:admin_a@example.com",
        db.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS,
        db.PASSWORD_RESET_RATE_LIMIT_WINDOW_MINUTES,
    ) is True
    assert db.is_rate_limited(
        "password_reset:admin_b@example.com",
        db.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS,
        db.PASSWORD_RESET_RATE_LIMIT_WINDOW_MINUTES,
    ) is False


def test_email_verify_resend_rate_limit_bucket_is_isolated_per_user(two_tenants):
    for _ in range(db.EMAIL_VERIFY_RESEND_RATE_LIMIT_MAX_ATTEMPTS):
        db.record_rate_limit_event(f"email_verify_resend:{two_tenants['admin_a_id']}")

    assert db.is_rate_limited(
        f"email_verify_resend:{two_tenants['admin_a_id']}",
        db.EMAIL_VERIFY_RESEND_RATE_LIMIT_MAX_ATTEMPTS,
        db.EMAIL_VERIFY_RESEND_RATE_LIMIT_WINDOW_MINUTES,
    ) is True
    assert db.is_rate_limited(
        f"email_verify_resend:{two_tenants['admin_b_id']}",
        db.EMAIL_VERIFY_RESEND_RATE_LIMIT_MAX_ATTEMPTS,
        db.EMAIL_VERIFY_RESEND_RATE_LIMIT_WINDOW_MINUTES,
    ) is False


# ---- 実際の画面（AppTest）でtenant_bのデータが一切表示されないことを確認 ----

def _find_button(at, label_substr):
    for i, b in enumerate(at.button):
        if label_substr in b.label:
            return i
    raise ValueError(f"button not found: {label_substr}")


def _login(at, username, password):
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[_find_button(at, "Login")].click().run()


def _all_rendered_text(at) -> str:
    """ページ上に描画された文字列を（サイドバー含め）ひとまとめにする。"""
    chunks = []
    for getter in ("markdown", "caption", "text", "success", "warning", "error", "info", "code", "title", "header", "subheader"):
        for w in at.get(getter):
            chunks.append(str(w.value))
    return "\n".join(chunks)


@pytest.fixture
def two_tenants_with_real_passwords(temp_db):
    """AppTestでのログインに使うため、パスワードをbcryptでハッシュ化した実ユーザーで用意する。"""
    admin_a_id = db.add_user(
        "admin_a", stauth.Hasher.hash("password-a-123"), email="admin_a@example.com", email_verified=True
    )
    tenant_a_id = db.get_user_by_id(admin_a_id)["tenant_id"]
    db.add_member(tenant_a_id, admin_a_id, "たろうA", "秘密メモA")
    invite_a = db.create_tenant_invite(tenant_a_id, admin_a_id)

    admin_b_id = db.add_user(
        "admin_b", stauth.Hasher.hash("password-b-456"), email="admin_b@example.com", email_verified=True
    )
    tenant_b_id = db.get_user_by_id(admin_b_id)["tenant_id"]
    db.add_member(tenant_b_id, admin_b_id, "たろうB", "秘密メモB")
    invite_b = db.create_tenant_invite(tenant_b_id, admin_b_id)
    db.update_tenant_plan(tenant_b_id, "pro", stripe_customer_id="cus_b_secret")

    return {
        "tenant_a_id": tenant_a_id,
        "tenant_b_id": tenant_b_id,
        "invite_a": invite_a,
        "invite_b": invite_b,
    }


def test_tenant_a_admin_screen_never_shows_tenant_b_data(two_tenants_with_real_passwords, monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_NAME", "test_cookie")
    monkeypatch.setenv("AUTH_COOKIE_KEY", "test-cookie-key-at-least-32-bytes-long!!")
    monkeypatch.setenv("AUTH_INVITE_CODE", "does-not-matter-here")

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin_a", "password-a-123")

    assert not at.exception
    assert at.session_state["authentication_status"] is True

    page_text = _all_rendered_text(at)

    # tenant_bのメンバー名・メモは一切現れない
    assert "たろうB" not in page_text
    assert "秘密メモB" not in page_text
    # tenant_bの招待トークン（実際のリンク文字列）も現れない
    ctx = two_tenants_with_real_passwords
    assert ctx["invite_b"] not in page_text
    # tenant_bのStripe顧客IDも現れない
    assert "cus_b_secret" not in page_text
    # tenant_bはPro、tenant_aはFreeのはずなので、Free側の画面が出ていることを確認する。
    # 「Proプラン」という文字列自体はFree向けのアップセル文言にも登場するため、
    # 実際にPro状態の時だけ出る成功バナー（完全一致文言）で判定する。
    assert "Freeプラン（メンバー" in page_text
    assert "Proプラン（メンバー無制限）" not in page_text

    # 自分（tenant_a）のデータは普通に表示されている
    assert "たろうA" in page_text
