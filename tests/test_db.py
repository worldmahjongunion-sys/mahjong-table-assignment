import pytest

import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


def test_add_user_and_get_by_username(temp_db):
    db.add_user("piedra", "hashed-password")

    user = db.get_user_by_username("piedra")

    assert user is not None
    assert user["username"] == "piedra"
    assert user["password_hash"] == "hashed-password"


def test_get_user_by_username_returns_none_for_unknown_user(temp_db):
    assert db.get_user_by_username("nobody") is None


def test_add_user_duplicate_raises_username_taken_error(temp_db):
    db.add_user("piedra", "hash1")

    with pytest.raises(db.UsernameTakenError):
        db.add_user("piedra", "hash2")


def test_add_user_creates_dedicated_tenant(temp_db):
    user_id = db.add_user("owner", "hash")

    user = db.get_user_by_username("owner")

    assert user["id"] == user_id
    assert user["tenant_id"] is not None


def test_add_member_and_get_members(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    db.add_member(tenant_id, user_id, "太郎", "初参加")
    members = db.get_members(tenant_id)

    assert len(members) == 1
    assert members[0]["name"] == "太郎"
    assert members[0]["memo"] == "初参加"


def test_retire_member_excludes_it_from_default_list(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_id = db.add_member(tenant_id, user_id, "次郎", "")

    db.retire_member(tenant_id, member_id)

    assert db.get_members(tenant_id) == []
    assert len(db.get_members(tenant_id, include_retired=True)) == 1


def test_members_are_isolated_between_tenants(temp_db):
    user_a = db.add_user("owner_a", "hash")
    user_b = db.add_user("owner_b", "hash")
    tenant_a = db.get_user_by_username("owner_a")["tenant_id"]
    tenant_b = db.get_user_by_username("owner_b")["tenant_id"]

    db.add_member(tenant_a, user_a, "太郎", "")
    db.add_member(tenant_b, user_b, "次郎", "")

    assert [m["name"] for m in db.get_members(tenant_a)] == ["太郎"]
    assert [m["name"] for m in db.get_members(tenant_b)] == ["次郎"]
