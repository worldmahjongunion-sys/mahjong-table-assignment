import pytest

import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


def test_new_tenant_defaults_to_free_plan(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    tenant = db.get_tenant(tenant_id)

    assert tenant["plan"] == "free"
    assert tenant["stripe_customer_id"] is None


def test_update_tenant_plan_to_pro_stores_stripe_ids(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    db.update_tenant_plan(
        tenant_id,
        "pro",
        stripe_customer_id="cus_123",
        stripe_subscription_id="sub_456",
        stripe_subscription_status="active",
    )

    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_customer_id"] == "cus_123"
    assert tenant["stripe_subscription_id"] == "sub_456"
    assert tenant["stripe_subscription_status"] == "active"


def test_update_tenant_plan_defaults_to_not_cancelled(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    db.update_tenant_plan(tenant_id, "pro", stripe_customer_id="cus_123")

    tenant = db.get_tenant(tenant_id)
    assert tenant["stripe_cancel_at_period_end"] is False
    assert tenant["stripe_current_period_end"] is None


def test_update_tenant_plan_stores_cancel_at_period_end(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    db.update_tenant_plan(
        tenant_id,
        "pro",
        stripe_customer_id="cus_123",
        stripe_subscription_id="sub_456",
        stripe_subscription_status="active",
        cancel_at_period_end=True,
        current_period_end="2026-09-30T00:00:00+00:00",
    )

    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_cancel_at_period_end"] is True
    assert tenant["stripe_current_period_end"] == "2026-09-30T00:00:00+00:00"


def test_get_tenant_by_stripe_customer_id_returns_cancel_state(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    db.update_tenant_plan(
        tenant_id,
        "pro",
        stripe_customer_id="cus_lookup",
        cancel_at_period_end=True,
        current_period_end="2026-09-30T00:00:00+00:00",
    )

    tenant = db.get_tenant_by_stripe_customer_id("cus_lookup")

    assert tenant["id"] == tenant_id
    assert tenant["stripe_cancel_at_period_end"] is True


def test_update_tenant_plan_rejects_unknown_plan(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    with pytest.raises(ValueError):
        db.update_tenant_plan(tenant_id, "enterprise")


def test_count_active_members_excludes_retired(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    m1 = db.add_member(tenant_id, user_id, "太郎", "")
    db.add_member(tenant_id, user_id, "次郎", "")
    db.retire_member(tenant_id, m1)

    assert db.count_active_members(tenant_id) == 1


def test_count_active_members_is_scoped_to_tenant(temp_db):
    user_a = db.add_user("owner_a", "hash")
    user_b = db.add_user("owner_b", "hash")
    tenant_a = db.get_user_by_username("owner_a")["tenant_id"]
    tenant_b = db.get_user_by_username("owner_b")["tenant_id"]
    db.add_member(tenant_a, user_a, "太郎", "")

    assert db.count_active_members(tenant_a) == 1
    assert db.count_active_members(tenant_b) == 0
