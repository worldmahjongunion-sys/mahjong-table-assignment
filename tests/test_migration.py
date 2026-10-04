import sqlite3

import db


def _legacy_users_table_columns() -> str:
    """users テーブルの旧スキーマ（memo列が存在しない状態）を再現するDDL。"""
    return """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "idempotent.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    db.init_db()
    db.init_db()  # 2回目の呼び出しでも例外にならない

    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(users)")}
    assert "memo" in columns


def test_migration_adds_memo_column_without_losing_existing_data(tmp_path, monkeypatch):
    """staging/本番での実際のシナリオを再現する: memo列がない旧スキーマの
    DBに既存データが入っている状態で、新しいコードの init_db() を実行する
    （＝Railwayでの再デプロイ時と同じ動作）。件数と既存データが保たれ、
    新しい列が追加されていることを確認する。"""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    # 旧スキーマのDBを直接作成し、既存データを入れておく
    conn = sqlite3.connect(db_path)
    conn.execute(_legacy_users_table_columns())
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        ("legacy_user", "legacy_hash", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    before_count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert before_count == 1

    # 新コードの init_db()（マイグレーション処理を含む）を実行
    db.init_db()

    after_count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert after_count == before_count  # 件数が変わっていない

    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(users)")}
    assert "memo" in columns  # 新しい列が追加されている

    row = sqlite3.connect(db_path).execute(
        "SELECT username, password_hash, memo FROM users WHERE username = ?",
        ("legacy_user",),
    ).fetchone()
    assert row == ("legacy_user", "legacy_hash", None)  # 既存データは壊れず、memoはNULL


def test_migration_backfills_tenant_for_legacy_user_and_members(tmp_path, monkeypatch):
    """テナント導入前のDB（tenant_id列がない）に既存のユーザーとメンバーが
    入っている状態で init_db() を実行し、専用テナントが自動作成され、
    メンバーがそのテナントに紐付けられることを確認する。"""
    db_path = tmp_path / "pre_tenant.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(_legacy_users_table_columns())
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        ("legacy_user", "legacy_hash", "2026-01-01T00:00:00"),
    )
    conn.execute(
        """
        CREATE TABLE members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            memo TEXT,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        "INSERT INTO members (user_id, name, memo, created_at) VALUES (1, ?, ?, ?)",
        ("太郎", "", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    db.init_db()

    user = db.get_user_by_username("legacy_user")
    assert user["tenant_id"] is not None

    member_tenant_id = sqlite3.connect(db_path).execute(
        "SELECT tenant_id FROM members WHERE name = ?", ("太郎",)
    ).fetchone()[0]
    assert member_tenant_id == user["tenant_id"]


def test_init_db_adds_seats_index_to_existing_db_without_losing_rows(tmp_path, monkeypatch):
    """索引が無い状態で作られた既存DB(本番・stagingの今の状態)に init_db() を実行すると、
    seats(table_id) の索引が後付けされ、既存の着席データはそのまま読めることを確認する。"""
    db_path = tmp_path / "pre_seats_index.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    db.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX idx_seats_table")  # 索引導入前のDBを再現
    conn.execute("INSERT INTO tenants (name, created_at) VALUES ('t', '2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO members (tenant_id, name, memo, created_at) VALUES (1, '太郎', '', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO tournaments (tenant_id, name, kind, table_method, scoring_mode, scoring_config,"
        " numbering_method, created_at) VALUES (1, 'x', 'ワンデー', '蛇行', 'ポイント', '{}', '受付順',"
        " '2026-01-01T00:00:00')"
    )
    conn.execute("INSERT INTO rounds (tournament_id, round_number, created_at) VALUES (1, 1, '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO tables (round_id, table_number) VALUES (1, 1)")
    conn.execute("INSERT INTO seats (table_id, member_id, position) VALUES (1, 1, '東')")
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # 索引が既にあっても例外にならない

    assert db.get_round_seats(1) == [{"table_number": 1, "position": "東", "member_id": 1}]
    conn = sqlite3.connect(db_path)
    plan = " ".join(
        row[3]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT s.member_id FROM seats s JOIN tables t ON t.id = s.table_id"
            " WHERE t.round_id = ?",
            (1,),
        )
    )
    conn.close()
    assert "idx_seats_table" in plan
