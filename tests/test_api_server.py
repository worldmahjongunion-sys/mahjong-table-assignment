import sys

import pytest

import db

TOKEN = "test-token-abc123"


@pytest.fixture
def api_module(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    sys.modules.pop("api_server", None)
    import api_server

    return api_server


@pytest.fixture
def client(api_module):
    api_module.app.config["TESTING"] = True
    return api_module.app.test_client()


def _make_tenant(username: str) -> int:
    db.add_user(username, "hash")
    return db.get_user_by_username(username)["tenant_id"]


def _make_tournament(tenant_id: int, name: str) -> int:
    return db.create_tournament(
        tenant_id, name, "ワンデー", "蛇行", "得点", {"secret": "config"}, "受付順",
    )


@pytest.fixture
def configured(monkeypatch):
    tenant_id = _make_tenant("owner")
    _make_tournament(tenant_id, "春の大会")
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("API_TENANT_ID", str(tenant_id))
    return tenant_id


def test_with_valid_token_returns_tournaments(client, configured):
    resp = client.get("/api/tournaments", headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert [t["name"] for t in body["tournaments"]] == ["春の大会"]


def test_response_excludes_internal_fields(client, configured):
    resp = client.get("/api/tournaments", headers={"Authorization": f"Bearer {TOKEN}"})

    item = resp.get_json()["tournaments"][0]
    assert "tenant_id" not in item
    assert "scoring_config" not in item


def test_without_token_returns_401(client, configured):
    resp = client.get("/api/tournaments")

    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert "春の大会" not in resp.get_data(as_text=True)


def test_wrong_token_returns_401(client, configured):
    resp = client.get("/api/tournaments", headers={"Authorization": "Bearer wrong"})

    assert resp.status_code == 401


def test_non_bearer_scheme_returns_401(client, configured):
    resp = client.get("/api/tournaments", headers={"Authorization": f"Basic {TOKEN}"})

    assert resp.status_code == 401


def test_only_returns_tournaments_of_configured_tenant(client, configured):
    other_tenant = _make_tenant("someone_else")
    _make_tournament(other_tenant, "他団体の大会")

    resp = client.get("/api/tournaments", headers={"Authorization": f"Bearer {TOKEN}"})

    names = [t["name"] for t in resp.get_json()["tournaments"]]
    assert names == ["春の大会"]


def test_token_not_configured_rejects_everyone(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)

    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer anything"}):
        resp = client.get("/api/tournaments", headers=headers)
        assert resp.status_code == 503


def test_write_methods_not_allowed(client, configured):
    resp = client.post("/api/tournaments", headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 405
