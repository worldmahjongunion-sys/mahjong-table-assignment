import threading
from datetime import datetime, timedelta

import pytest

import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


# ---- レート制限 ----

def test_is_rate_limited_false_below_threshold(temp_db):
    for _ in range(4):
        db.record_rate_limit_event("login:someone")

    assert db.is_rate_limited("login:someone", max_attempts=5, window_minutes=15) is False


def test_is_rate_limited_true_at_threshold(temp_db):
    for _ in range(5):
        db.record_rate_limit_event("login:someone")

    assert db.is_rate_limited("login:someone", max_attempts=5, window_minutes=15) is True


def test_rate_limit_is_scoped_to_bucket(temp_db):
    for _ in range(5):
        db.record_rate_limit_event("login:alice")

    assert db.is_rate_limited("login:alice", max_attempts=5, window_minutes=15) is True
    assert db.is_rate_limited("login:bob", max_attempts=5, window_minutes=15) is False


def test_rate_limit_ignores_events_outside_window(temp_db):
    old_time = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    with db.get_connection() as conn:
        for _ in range(5):
            conn.execute(
                "INSERT INTO rate_limit_events (bucket, created_at) VALUES (?, ?)",
                ("login:someone", old_time),
            )

    # 直近15分の窓には入らないので、まだ制限されていない
    assert db.is_rate_limited("login:someone", max_attempts=5, window_minutes=15) is False


def test_record_rate_limit_event_prunes_old_rows(temp_db):
    very_old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO rate_limit_events (bucket, created_at) VALUES (?, ?)",
            ("login:someone", very_old),
        )

    db.record_rate_limit_event("login:someone")

    with db.get_connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM rate_limit_events").fetchone()[0]
    assert remaining == 1  # 2日以上前の行は間引かれ、今回追加した1件だけが残る


# ---- 監査ログ ----

def test_record_audit_log_and_get_audit_logs(temp_db):
    db.record_audit_log(action="login", tenant_id=1, user_id=10, username="taro")
    db.record_audit_log(action="member_add", tenant_id=1, user_id=10, username="taro", detail="member_id=5")

    logs = db.get_audit_logs(1)

    assert len(logs) == 2
    assert logs[0]["action"] == "member_add"  # 新しい順
    assert logs[1]["action"] == "login"


def test_get_audit_logs_is_scoped_to_tenant(temp_db):
    db.record_audit_log(action="login", tenant_id=1, user_id=10, username="taro")
    db.record_audit_log(action="login", tenant_id=2, user_id=20, username="jiro")

    assert len(db.get_audit_logs(1)) == 1
    assert len(db.get_audit_logs(2)) == 1


def test_get_audit_logs_respects_limit(temp_db):
    for i in range(5):
        db.record_audit_log(action="login", tenant_id=1, user_id=i, username=f"user{i}")

    assert len(db.get_audit_logs(1, limit=3)) == 3


def test_update_tenant_plan_writes_audit_log_with_actor(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    db.update_tenant_plan(
        tenant_id, "pro", stripe_customer_id="cus_1", actor_user_id=1, actor_username="owner"
    )

    logs = db.get_audit_logs(tenant_id)
    assert len(logs) == 1
    assert logs[0]["action"] == "plan_change"
    assert logs[0]["user_id"] == 1
    assert logs[0]["username"] == "owner"
    assert "plan=pro" in logs[0]["detail"]


def test_update_tenant_plan_without_actor_logs_system_change(temp_db):
    db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    db.update_tenant_plan(tenant_id, "free")

    logs = db.get_audit_logs(tenant_id)
    assert len(logs) == 1
    assert logs[0]["user_id"] is None
    assert logs[0]["username"] is None


# ---- Freeプランのメンバー上限（TOCTOU対策） ----

def test_add_member_succeeds_below_limit(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    member_id = db.add_member(tenant_id, user_id, "太郎", "", max_members=2)

    assert member_id is not None
    assert db.count_active_members(tenant_id) == 1


def test_add_member_returns_none_when_limit_reached(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    db.add_member(tenant_id, user_id, "太郎", "", max_members=1)

    result = db.add_member(tenant_id, user_id, "次郎", "", max_members=1)

    assert result is None
    assert db.count_active_members(tenant_id) == 1


def test_add_member_without_max_members_is_unlimited(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]

    for i in range(3):
        assert db.add_member(tenant_id, user_id, f"member{i}", "") is not None


def test_add_member_does_not_exceed_limit_under_concurrent_requests(temp_db):
    """同時に複数リクエストが上限ギリギリで来ても、上限を超えて追加されないことを確認する
    （素朴なSELECT COUNT→INSERTだと両方成功してしまうレースが起きうる）。"""
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    db.add_member(tenant_id, user_id, "既存メンバー", "", max_members=3)  # 現在1人、上限3人

    results = []
    barrier = threading.Barrier(5)

    def try_add(i):
        barrier.wait()
        result = db.add_member(tenant_id, user_id, f"race{i}", "", max_members=3)
        results.append(result)

    threads = [threading.Thread(target=try_add, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert db.count_active_members(tenant_id) == 3  # 上限3人を超えない
    assert sum(1 for r in results if r is not None) == 2  # 5件中、上限まで届いた2件だけ成功
