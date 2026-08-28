import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "mahjong.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

MAX_NAME_LEN = 50
MAX_MEMO_LEN = 200
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8

ROLES = ("admin", "member")

EMAIL_VERIFICATION_TTL_HOURS = 24
PASSWORD_RESET_TTL_MINUTES = 30
TENANT_INVITE_TTL_HOURS = 72

PLANS = ("free", "pro")
FREE_PLAN_MEMBER_LIMIT = 20


class UsernameTakenError(Exception):
    pass


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


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
        tenant_columns = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
        if "plan" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        if "stripe_customer_id" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT")
        if "stripe_subscription_id" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN stripe_subscription_id TEXT")
        if "stripe_subscription_status" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN stripe_subscription_status TEXT")
        if "plan_updated_at" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN plan_updated_at TEXT")
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
        if "email" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        if "email_verified" not in user_columns:
            # このタイミングで存在する行＝メール確認機能の導入前に作られたアカウント。
            # 本人確認の手段がそもそも無かったので、締め出さないよう確認済み扱いにする。
            conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE users SET email_verified = 1")

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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                token_hash TEXT NOT NULL UNIQUE,
                created_by_user_id INTEGER NOT NULL REFERENCES users(id),
                role TEXT NOT NULL DEFAULT 'member',
                max_uses INTEGER,
                use_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )


# ---- users ----

def add_user(
    username: str,
    password_hash: str,
    email: str | None = None,
    tenant_id: int | None = None,
    role: str = "admin",
    email_verified: bool | None = None,
) -> int:
    """ユーザーを作成する。

    tenant_id が None なら新しいテナントを作って所有者(admin)にする。
    tenant_id を渡すと、既存テナントへの参加（招待経由）として扱う。
    email_verified を省略した場合、email が無いアカウント（CLIでの移行・
    復旧作業など、本人確認の手段が無いケース）は確認済み扱いにする。
    """
    if role not in ROLES:
        raise ValueError(f"不正なロールです: {role}")
    if email_verified is None:
        email_verified = email is None

    with get_connection() as conn:
        created_at = _iso(_now())
        if tenant_id is None:
            cur = conn.execute(
                "INSERT INTO tenants (name, created_at) VALUES (?, ?)", (username, created_at)
            )
            tenant_id = cur.lastrowid
        try:
            cur = conn.execute(
                """
                INSERT INTO users (username, password_hash, created_at, tenant_id, email, role, email_verified)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (username, password_hash, created_at, tenant_id, email, role, int(email_verified)),
            )
        except sqlite3.IntegrityError as exc:
            raise UsernameTakenError(username) from exc
        return cur.lastrowid


_USER_COLUMNS = "id, username, password_hash, created_at, tenant_id, email, role, email_verified"


def _row_to_user(row: sqlite3.Row) -> dict:
    user = dict(row)
    user["email_verified"] = bool(user["email_verified"])
    return user


def get_user_by_username(username: str) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None


def get_all_users() -> list[dict]:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY id ASC")
        return [_row_to_user(row) for row in cur.fetchall()]


def update_password_hash(user_id: int, new_password_hash: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, user_id))


_TENANT_COLUMNS = (
    "id, name, created_at, plan, stripe_customer_id, stripe_subscription_id, "
    "stripe_subscription_status, plan_updated_at"
)


def get_tenant(tenant_id: int) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        return dict(row) if row else None


def update_tenant_plan(
    tenant_id: int,
    plan: str,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    stripe_subscription_status: str | None = None,
) -> None:
    if plan not in PLANS:
        raise ValueError(f"不正なプランです: {plan}")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE tenants
            SET plan = ?, stripe_customer_id = ?, stripe_subscription_id = ?,
                stripe_subscription_status = ?, plan_updated_at = ?
            WHERE id = ?
            """,
            (plan, stripe_customer_id, stripe_subscription_id, stripe_subscription_status, _iso(_now()), tenant_id),
        )


def count_active_members(tenant_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM members WHERE tenant_id = ? AND is_active = 1", (tenant_id,)
        )
        return cur.fetchone()[0]


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


# ---- email verification ----

def create_email_verification_token(user_id: int) -> str:
    raw_token = _generate_token()
    now = _now()
    expires_at = now + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO email_verification_tokens (user_id, token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, _hash_token(raw_token), _iso(now), _iso(expires_at)),
        )
    return raw_token


def verify_email_token(raw_token: str) -> int | None:
    """トークンが有効なら該当ユーザーを確認済みにし、user_id を返す。無効なら None。"""
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, user_id, expires_at, used_at FROM email_verification_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["used_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return None
        conn.execute(
            "UPDATE email_verification_tokens SET used_at = ? WHERE id = ?", (_iso(_now()), row["id"])
        )
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (row["user_id"],))
        return row["user_id"]


# ---- password reset ----

def create_password_reset_token(user_id: int) -> str:
    raw_token = _generate_token()
    now = _now()
    expires_at = now + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, _hash_token(raw_token), _iso(now), _iso(expires_at)),
        )
    return raw_token


def reset_password_with_token(raw_token: str, new_password_hash: str) -> bool:
    """トークンが有効なら新しいパスワードを設定し True を返す。無効/期限切れ/使用済みなら False。"""
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, user_id, expires_at, used_at FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["used_at"] is not None:
            return False
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return False
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (_iso(_now()), row["id"])
        )
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, row["user_id"]))
        return True


# ---- tenant invites (admin がチームメンバーを招待する) ----

def create_tenant_invite(
    tenant_id: int, created_by_user_id: int, role: str = "member", max_uses: int | None = 1
) -> str:
    if role not in ROLES:
        raise ValueError(f"不正なロールです: {role}")
    raw_token = _generate_token()
    now = _now()
    expires_at = now + timedelta(hours=TENANT_INVITE_TTL_HOURS)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tenant_invites
                (tenant_id, token_hash, created_by_user_id, role, max_uses, use_count, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (tenant_id, _hash_token(raw_token), created_by_user_id, role, max_uses, _iso(now), _iso(expires_at)),
        )
    return raw_token


def get_tenant_invite(raw_token: str) -> dict | None:
    """有効な招待なら情報を返す。期限切れ・使い切り・存在しない場合は None。"""
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tenant_invites WHERE token_hash = ?", (token_hash,)).fetchone()
        if row is None:
            return None
        invite = dict(row)
        if datetime.fromisoformat(invite["expires_at"]) < _now():
            return None
        if invite["max_uses"] is not None and invite["use_count"] >= invite["max_uses"]:
            return None
        return invite


def consume_tenant_invite(raw_token: str) -> dict | None:
    """招待を1回消費する。有効だった招待情報を返す。無効なら None。"""
    invite = get_tenant_invite(raw_token)
    if invite is None:
        return None
    with get_connection() as conn:
        conn.execute("UPDATE tenant_invites SET use_count = use_count + 1 WHERE id = ?", (invite["id"],))
    return invite


# ---- members (all scoped to tenant_id) ----

def add_member(tenant_id: int, user_id: int, name: str, memo: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO members (tenant_id, user_id, name, memo, created_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, user_id, name, memo, _iso(_now())),
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
