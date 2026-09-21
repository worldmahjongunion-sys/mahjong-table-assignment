import sys
from datetime import datetime, timedelta

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


# ---- レート制限（認証失敗の総当たり対策） ----

MAX_FAILURES = 10


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _fail_n_times(client, n, headers=None):
    for _ in range(n):
        resp = client.get("/api/tournaments", headers=headers or _auth("wrong"))
        assert resp.status_code == 401


def test_eleventh_failure_is_blocked_with_429(client, configured):
    _fail_n_times(client, MAX_FAILURES)

    resp = client.get("/api/tournaments", headers=_auth("wrong"))

    assert resp.status_code == 429
    assert resp.get_json() == {"error": "too_many_requests"}


def test_failures_without_any_token_are_also_counted(client, configured):
    _fail_n_times(client, MAX_FAILURES, headers={})

    assert client.get("/api/tournaments").status_code == 429


def test_blocked_ip_gets_429_even_with_correct_token(client, configured):
    _fail_n_times(client, MAX_FAILURES)

    resp = client.get("/api/tournaments", headers=_auth(TOKEN))

    assert resp.status_code == 429
    assert "春の大会" not in resp.get_data(as_text=True)


def test_429_response_has_retry_after(client, configured):
    _fail_n_times(client, MAX_FAILURES)

    resp = client.get("/api/tournaments", headers=_auth("wrong"))

    assert resp.headers["Retry-After"] == "900"


def test_successful_requests_are_not_counted(client, configured):
    for _ in range(MAX_FAILURES + 5):
        assert client.get("/api/tournaments", headers=_auth(TOKEN)).status_code == 200

    # 成功をいくら重ねても、失敗1回目はまだ401（ブロックされない）
    assert client.get("/api/tournaments", headers=_auth("wrong")).status_code == 401


def test_requests_are_accepted_again_after_window(client, configured):
    _fail_n_times(client, MAX_FAILURES)
    assert client.get("/api/tournaments", headers=_auth(TOKEN)).status_code == 429

    # 失敗の記録を16分前に巻き戻す（既存のレート制限テストと同じ方式）
    old_time = (datetime.now() - timedelta(minutes=16)).isoformat(timespec="seconds")
    with db.get_connection() as conn:
        conn.execute("UPDATE rate_limit_events SET created_at = ?", (old_time,))

    assert client.get("/api/tournaments", headers=_auth(TOKEN)).status_code == 200


def test_counters_are_independent_per_ip(client, configured):
    for _ in range(MAX_FAILURES):
        client.get("/api/tournaments", headers=_auth("wrong"), environ_overrides={"REMOTE_ADDR": "1.1.1.1"})

    blocked = client.get("/api/tournaments", headers=_auth("wrong"), environ_overrides={"REMOTE_ADDR": "1.1.1.1"})
    other = client.get("/api/tournaments", headers=_auth(TOKEN), environ_overrides={"REMOTE_ADDR": "9.9.9.9"})

    assert blocked.status_code == 429
    assert other.status_code == 200


def test_x_forwarded_for_is_ignored_by_default(client, configured):
    # TRUST_PROXY_HOPS 未設定(=0)のとき、ヘッダを毎回変えて詐称しても制限を回避できない
    for i in range(MAX_FAILURES):
        resp = client.get("/api/tournaments", headers={**_auth("wrong"), "X-Forwarded-For": f"8.8.8.{i}"})
        assert resp.status_code == 401

    resp = client.get("/api/tournaments", headers={**_auth("wrong"), "X-Forwarded-For": "8.8.8.200"})

    assert resp.status_code == 429


def test_default_counts_by_remote_addr_even_if_header_present(client, configured):
    for _ in range(MAX_FAILURES):
        client.get("/api/tournaments", headers={**_auth("wrong"), "X-Forwarded-For": "8.8.8.8"})

    # ヘッダの値(8.8.8.8)は無視され、remote_addrで数えられている
    assert client.get("/api/tournaments", headers=_auth(TOKEN)).status_code == 429
    assert client.get(
        "/api/tournaments", headers=_auth(TOKEN), environ_overrides={"REMOTE_ADDR": "8.8.8.8"}
    ).status_code == 200


def test_trust_proxy_hops_1_uses_rightmost_value(client, configured, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HOPS", "1")
    spoofed_then_real = {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}
    for _ in range(MAX_FAILURES):
        client.get("/api/tournaments", headers={**_auth("wrong"), **spoofed_then_real})

    same_real_ip_other_spoof = client.get(
        "/api/tournaments", headers={**_auth(TOKEN), "X-Forwarded-For": "7.7.7.7, 2.2.2.2"}
    )
    leftmost_only = client.get(
        "/api/tournaments", headers={**_auth(TOKEN), "X-Forwarded-For": "1.1.1.1, 5.5.5.5"}
    )

    # 数えられているのは右端の 2.2.2.2。左端(詐称可能な値)では数えない
    assert same_real_ip_other_spoof.status_code == 429
    assert leftmost_only.status_code == 200


def test_trust_proxy_hops_1_with_single_value_uses_it(client, configured, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HOPS", "1")
    for _ in range(MAX_FAILURES):
        client.get("/api/tournaments", headers={**_auth("wrong"), "X-Forwarded-For": "6.6.6.6"})

    blocked = client.get("/api/tournaments", headers={**_auth(TOKEN), "X-Forwarded-For": "6.6.6.6"})
    other = client.get("/api/tournaments", headers={**_auth(TOKEN), "X-Forwarded-For": "6.6.6.7"})

    assert blocked.status_code == 429
    assert other.status_code == 200


def test_trust_proxy_hops_2_with_single_value_falls_back_to_remote_addr(client, configured, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HOPS", "2")
    for i in range(MAX_FAILURES):
        client.get("/api/tournaments", headers={**_auth("wrong"), "X-Forwarded-For": f"6.6.6.{i}"})

    # 値が段数に満たないヘッダは信用せず remote_addr で数える(=毎回変えても回避できない)
    assert client.get(
        "/api/tournaments", headers={**_auth(TOKEN), "X-Forwarded-For": "6.6.6.99"}
    ).status_code == 429


@pytest.mark.parametrize("value", ["abc", "-1", ""])
def test_invalid_trust_proxy_hops_is_treated_as_zero(client, configured, monkeypatch, value):
    monkeypatch.setenv("TRUST_PROXY_HOPS", value)
    for i in range(MAX_FAILURES):
        client.get("/api/tournaments", headers={**_auth("wrong"), "X-Forwarded-For": f"8.8.8.{i}"})

    assert client.get("/api/tournaments", headers=_auth(TOKEN)).status_code == 429


def test_falls_back_to_remote_addr_without_x_forwarded_for(client, configured):
    for _ in range(MAX_FAILURES):
        client.get("/api/tournaments", headers=_auth("wrong"), environ_overrides={"REMOTE_ADDR": "3.3.3.3"})

    blocked = client.get("/api/tournaments", headers=_auth(TOKEN), environ_overrides={"REMOTE_ADDR": "3.3.3.3"})
    other = client.get("/api/tournaments", headers=_auth(TOKEN), environ_overrides={"REMOTE_ADDR": "4.4.4.4"})

    assert blocked.status_code == 429
    assert other.status_code == 200


def test_503_when_token_unset_is_not_counted(client, configured, monkeypatch):
    monkeypatch.delenv("API_TOKEN")
    for _ in range(MAX_FAILURES + 5):
        assert client.get("/api/tournaments", headers=_auth("anything")).status_code == 503

    monkeypatch.setenv("API_TOKEN", TOKEN)

    assert client.get("/api/tournaments", headers=_auth(TOKEN)).status_code == 200


def test_auth_failure_is_written_to_audit_log(client, configured):
    for _ in range(MAX_FAILURES):
        client.get("/api/tournaments", headers=_auth("wrong"), environ_overrides={"REMOTE_ADDR": "1.1.1.1"})

    logs = db.get_audit_logs(configured)

    assert [log["action"] for log in logs] == ["api_auth_failed"] * MAX_FAILURES
    assert all("ip=1.1.1.1" in log["detail"] for log in logs)


def test_blocked_requests_do_not_write_audit_log(client, configured):
    _fail_n_times(client, MAX_FAILURES)
    rows_before = len(db.get_audit_logs(configured))

    for token in ("wrong", TOKEN, "wrong"):
        assert client.get("/api/tournaments", headers=_auth(token)).status_code == 429

    logs = db.get_audit_logs(configured)
    assert len(logs) == rows_before
    assert "api_rate_limited" not in [log["action"] for log in logs]


def test_presented_token_is_never_logged(client, configured, caplog):
    secret_guess = "super-secret-guess-xyz"
    with caplog.at_level("DEBUG"):
        _fail_n_times(client, MAX_FAILURES, headers=_auth(secret_guess))
        client.get("/api/tournaments", headers=_auth(secret_guess))

    with db.get_connection() as conn:
        dumped = str(conn.execute("SELECT * FROM audit_logs").fetchall())
        dumped += str(conn.execute("SELECT * FROM rate_limit_events").fetchall())
    assert secret_guess not in dumped
    assert secret_guess not in caplog.text
