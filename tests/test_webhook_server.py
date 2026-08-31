import sys

import pytest
import stripe

import db


@pytest.fixture
def webhook_module(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    sys.modules.pop("webhook_server", None)
    import webhook_server

    return webhook_server


@pytest.fixture
def client(webhook_module):
    webhook_module.app.config["TESTING"] = True
    return webhook_module.app.test_client()


def _fake_construct_event(event):
    """event["data"]["object"] は本物のStripe Webhookと同じくStripeObject
    （.get()を呼ぶとAttributeErrorになる、dictではない型）として渡す。
    プレーンなdictのままだと本番と挙動が変わってしまい、webhook_server側の
    バグ（StripeObjectに対してobj.get(...)を呼んでしまう等）を見逃すため。
    """
    wrapped = dict(event)
    wrapped["data"] = dict(event["data"])
    wrapped["data"]["object"] = stripe.StripeObject.construct_from(event["data"]["object"], key=None)

    def construct_event(payload, sig_header, secret):
        return wrapped

    return construct_event


def _make_tenant(tenant_id_owner: str = "owner") -> int:
    db.add_user(tenant_id_owner, "hash")
    return db.get_user_by_username(tenant_id_owner)["tenant_id"]


def test_invalid_signature_returns_400(client, webhook_module, monkeypatch):
    def raise_sig_error(payload, sig_header, secret):
        raise stripe.SignatureVerificationError("bad signature", sig_header)

    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", raise_sig_error)

    resp = client.post(
        "/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "bogus"}
    )

    assert resp.status_code == 400


def test_checkout_session_completed_upgrades_tenant_to_pro(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    event = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": "cus_123",
                "subscription": "sub_123",
                "payment_status": "paid",
                "metadata": {"tenant_id": str(tenant_id)},
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_customer_id"] == "cus_123"
    assert tenant["stripe_subscription_id"] == "sub_123"
    assert tenant["stripe_subscription_status"] == "active"


def test_checkout_session_completed_without_tenant_id_is_ignored(client, webhook_module, monkeypatch):
    event = {
        "id": "evt_2",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_2",
                "customer": "cus_999",
                "subscription": "sub_999",
                "payment_status": "paid",
                "metadata": {},
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    assert db.get_tenant_by_stripe_customer_id("cus_999") is None


def test_subscription_updated_to_active_sets_pro(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(tenant_id, "free", stripe_customer_id="cus_abc")
    event = {
        "id": "evt_3",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_abc",
                "customer": "cus_abc",
                "status": "active",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_subscription_status"] == "active"


def test_subscription_updated_to_unpaid_downgrades_to_free(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(tenant_id, "pro", stripe_customer_id="cus_def", stripe_subscription_id="sub_def")
    event = {
        "id": "evt_4",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_def",
                "customer": "cus_def",
                "status": "unpaid",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "free"
    assert tenant["stripe_subscription_status"] == "unpaid"


def test_subscription_updated_stores_cancel_at_period_end(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(tenant_id, "pro", stripe_customer_id="cus_pqr", stripe_subscription_id="sub_pqr")
    event = {
        "id": "evt_cancel_scheduled",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_pqr",
                "customer": "cus_pqr",
                "status": "active",
                "cancel_at_period_end": True,
                # このAPIバージョンでは current_period_end は items.data[0] 側にある
                "items": {"data": [{"current_period_end": 1790000000}]},
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_cancel_at_period_end"] is True
    assert tenant["stripe_current_period_end"] is not None


def test_subscription_deleted_sets_free_and_canceled(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(
        tenant_id,
        "pro",
        stripe_customer_id="cus_ghi",
        stripe_subscription_id="sub_ghi",
        cancel_at_period_end=True,
        current_period_end="2026-09-30T00:00:00+00:00",
    )
    event = {
        "id": "evt_5",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_ghi",
                "customer": "cus_ghi",
                "status": "canceled",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "free"
    assert tenant["stripe_subscription_status"] == "canceled"
    assert tenant["stripe_cancel_at_period_end"] is False
    assert tenant["stripe_current_period_end"] is None


def test_invoice_payment_failed_sets_past_due_without_downgrading(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(tenant_id, "pro", stripe_customer_id="cus_jkl", stripe_subscription_id="sub_jkl")
    event = {
        "id": "evt_6",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_1",
                "customer": "cus_jkl",
                "subscription": "sub_jkl",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_subscription_status"] == "past_due"


def test_invoice_payment_failed_preserves_cancel_at_period_end(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(
        tenant_id,
        "pro",
        stripe_customer_id="cus_stu",
        stripe_subscription_id="sub_stu",
        cancel_at_period_end=True,
        current_period_end="2026-09-30T00:00:00+00:00",
    )
    event = {
        "id": "evt_preserve",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_3",
                "customer": "cus_stu",
                "subscription": "sub_stu",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["stripe_cancel_at_period_end"] is True
    assert tenant["stripe_current_period_end"] == "2026-09-30T00:00:00+00:00"


def test_invoice_payment_succeeded_sets_active_and_pro(client, webhook_module, monkeypatch):
    tenant_id = _make_tenant()
    db.update_tenant_plan(
        tenant_id,
        "pro",
        stripe_customer_id="cus_mno",
        stripe_subscription_id="sub_mno",
        stripe_subscription_status="past_due",
    )
    event = {
        "id": "evt_7",
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_2",
                "customer": "cus_mno",
                "subscription": "sub_mno",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200
    tenant = db.get_tenant(tenant_id)
    assert tenant["plan"] == "pro"
    assert tenant["stripe_subscription_status"] == "active"


def test_unknown_customer_id_does_not_crash(client, webhook_module, monkeypatch):
    event = {
        "id": "evt_8",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_unknown",
                "customer": "cus_unknown",
                "status": "active",
            }
        },
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200


def test_unhandled_event_type_is_ignored(client, webhook_module, monkeypatch):
    event = {
        "id": "evt_9",
        "type": "customer.created",
        "data": {"object": {"id": "cus_new"}},
    }
    monkeypatch.setattr(webhook_module.stripe.Webhook, "construct_event", _fake_construct_event(event))

    resp = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})

    assert resp.status_code == 200


def test_missing_webhook_secret_raises_at_import(tmp_path, monkeypatch):
    db_path = tmp_path / "test2.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    sys.modules.pop("webhook_server", None)

    with pytest.raises(RuntimeError):
        import webhook_server  # noqa: F401
