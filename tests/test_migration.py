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
