import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "mahjong.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

MAX_NAME_LEN = 50
MAX_MEMO_LEN = 200
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
MIN_PASSWORD_LEN = 8


class UsernameTakenError(Exception):
    pass


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                memo TEXT,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(members)")}
        if "is_active" not in columns:
            conn.execute("ALTER TABLE members ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "user_id" not in columns:
            conn.execute("ALTER TABLE members ADD COLUMN user_id INTEGER")
        if "tenant_id" not in columns:
            conn.execute("ALTER TABLE members ADD COLUMN tenant_id INTEGER")

        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "memo" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN memo TEXT")
        if "tenant_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN tenant_id INTEGER")

        # 既存ユーザーに専用テナントを割り当てる（1テナント=1ユーザー）
        users_without_tenant = conn.execute(
            "SELECT id, username, created_at FROM users WHERE tenant_id IS NULL"
        ).fetchall()
        for user_row in users_without_tenant:
            uid, username, created_at = user_row
            cur = conn.execute(
                "INSERT INTO tenants (name, created_at) VALUES (?, ?)",
                (username, created_at),
            )
            conn.execute("UPDATE users SET tenant_id = ? WHERE id = ?", (cur.lastrowid, uid))

        # 既存メンバーを、紐付くユーザーのテナントへバックフィル
        conn.execute(
            """
            UPDATE members
            SET tenant_id = (SELECT tenant_id FROM users WHERE users.id = members.user_id)
            WHERE tenant_id IS NULL AND user_id IS NOT NULL
            """
        )


# ---- users ----

def add_user(username: str, password_hash: str) -> int:
    with get_connection() as conn:
        created_at = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO tenants (name, created_at) VALUES (?, ?)", (username, created_at)
        )
        tenant_id = cur.lastrowid
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at, tenant_id) VALUES (?, ?, ?, ?)",
                (username, password_hash, created_at, tenant_id),
            )
        except sqlite3.IntegrityError as exc:
            raise UsernameTakenError(username) from exc
        return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, username, password_hash, created_at, tenant_id FROM users WHERE username = ?",
            (username,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_all_users() -> list[dict]:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, username, password_hash, created_at, tenant_id FROM users ORDER BY id ASC"
        )
        return [dict(row) for row in cur.fetchall()]


# ---- migration helper ----

def assign_orphaned_members(user_id: int) -> int:
    """Assign all members with no owner (pre-auth data) to user_id's tenant. Returns rows affected."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE members
            SET user_id = ?, tenant_id = (SELECT tenant_id FROM users WHERE id = ?)
            WHERE user_id IS NULL
            """,
            (user_id, user_id),
        )
        return cur.rowcount


# ---- members (all scoped to tenant_id) ----

def add_member(tenant_id: int, user_id: int, name: str, memo: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO members (tenant_id, user_id, name, memo, created_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, user_id, name, memo, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def member_exists(tenant_id: int, name: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM members WHERE tenant_id = ? AND name = ? LIMIT 1", (tenant_id, name)
        )
        return cur.fetchone() is not None


def get_members(tenant_id: int, order: str = "created", include_retired: bool = False) -> list[dict]:
    order_clause = "name ASC" if order == "name" else "id ASC"
    where_clause = "WHERE tenant_id = ?" if include_retired else "WHERE tenant_id = ? AND is_active = 1"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"SELECT id, name, memo, created_at, is_active FROM members {where_clause} ORDER BY {order_clause}",
            (tenant_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def update_member(tenant_id: int, member_id: int, name: str, memo: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE members SET name = ?, memo = ? WHERE id = ? AND tenant_id = ?",
            (name, memo, member_id, tenant_id),
        )


def retire_member(tenant_id: int, member_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE members SET is_active = 0 WHERE id = ? AND tenant_id = ?", (member_id, tenant_id)
        )


def restore_member(tenant_id: int, member_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE members SET is_active = 1 WHERE id = ? AND tenant_id = ?", (member_id, tenant_id)
        )
