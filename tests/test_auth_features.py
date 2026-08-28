import sqlite3
from datetime import datetime, timedelta

import pytest

import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


# ---- メール確認 ----

def test_new_user_with_email_starts_unverified(temp_db):
    db.add_user("owner", "hash", email="owner@example.com")

    user = db.get_user_by_username("owner")

    assert user["email"] == "owner@example.com"
    assert user["email_verified"] is False
    assert user["role"] == "admin"


def test_user_created_without_email_is_trusted_immediately(temp_db):
    """migrate_to_auth.py のようなCLIからの作成は、確認手段が無いので即信頼する。"""
    db.add_user("owner", "hash")

    user = db.get_user_by_username("owner")

    assert user["email_verified"] is True


def test_verify_email_token_marks_user_verified(temp_db):
    user_id = db.add_user("owner", "hash", email="owner@example.com")
    token = db.create_email_verification_token(user_id)

    verified_user_id = db.verify_email_token(token)

    assert verified_user_id == user_id
    assert db.get_user_by_id(user_id)["email_verified"] is True


def test_verify_email_token_rejects_unknown_token(temp_db):
    assert db.verify_email_token("no-such-token") is None


def test_verify_email_token_cannot_be_reused(temp_db):
    user_id = db.add_user("owner", "hash", email="owner@example.com")
    token = db.create_email_verification_token(user_id)

    assert db.verify_email_token(token) == user_id
    assert db.verify_email_token(token) is None


def test_verify_email_token_rejects_expired_token(temp_db):
    user_id = db.add_user("owner", "hash", email="owner@example.com")
    token = db.create_email_verification_token(user_id)

    with sqlite3.connect(temp_db) as conn:
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE email_verification_tokens SET expires_at = ? WHERE token_hash = ?",
            (past, db._hash_token(token)),
        )

    assert db.verify_email_token(token) is None


# ---- パスワード再発行 ----

def test_reset_password_with_valid_token_updates_hash(temp_db):
    user_id = db.add_user("owner", "old-hash", email="owner@example.com")
    token = db.create_password_reset_token(user_id)

    result = db.reset_password_with_token(token, "new-hash")

    assert result is True
    assert db.get_user_by_id(user_id)["password_hash"] == "new-hash"


def test_reset_password_token_cannot_be_reused(temp_db):
    user_id = db.add_user("owner", "old-hash", email="owner@example.com")
    token = db.create_password_reset_token(user_id)

    assert db.reset_password_with_token(token, "new-hash-1") is True
    assert db.reset_password_with_token(token, "new-hash-2") is False


def test_reset_password_rejects_unknown_token(temp_db):
    assert db.reset_password_with_token("no-such-token", "new-hash") is False


# ---- テナント招待（ロール付き参加） ----

def test_tenant_invite_lets_new_user_join_existing_tenant_with_role(temp_db):
    admin_id = db.add_user("owner", "hash", email="owner@example.com")
    admin_tenant_id = db.get_user_by_username("owner")["tenant_id"]
    token = db.create_tenant_invite(admin_tenant_id, admin_id, role="member")

    invite = db.get_tenant_invite(token)
    assert invite is not None
    assert invite["tenant_id"] == admin_tenant_id
    assert invite["role"] == "member"

    new_user_id = db.add_user(
        "helper", "hash2", email="helper@example.com", tenant_id=invite["tenant_id"], role=invite["role"]
    )
    new_user = db.get_user_by_id(new_user_id)

    assert new_user["tenant_id"] == admin_tenant_id
    assert new_user["role"] == "member"


def test_tenant_invite_with_max_uses_one_is_exhausted_after_use(temp_db):
    admin_id = db.add_user("owner", "hash", email="owner@example.com")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    token = db.create_tenant_invite(tenant_id, admin_id, role="member", max_uses=1)

    assert db.consume_tenant_invite(token) is not None
    assert db.get_tenant_invite(token) is None


def test_tenant_invite_rejects_unknown_token(temp_db):
    assert db.get_tenant_invite("no-such-token") is None
