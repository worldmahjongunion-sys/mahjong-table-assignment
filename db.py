import hashlib
import json
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
FREE_PLAN_INVITE_MONTHLY_LIMIT = 3

# ---- 大会 ----
TOURNAMENT_KINDS = ("ワンデー", "シーズン")
TABLE_METHODS = ("蛇行", "ワンデー4半荘")
SCORING_MODES = ("得点", "ポイント")
NUMBERING_METHODS = ("受付順", "くじ引き")
TOURNAMENT_STATUSES = ("準備中", "進行中", "終了")

# ---- レート制限（総当たり攻撃対策） ----
# 資格情報の推測を狙う操作（ログイン・招待コード）は短い窓で厳しめに、
# メール送信系（悪用されるとメール爆撃の踏み台になる）は長い窓でやや緩めに設定する。
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_MINUTES = 15
INVITE_CODE_RATE_LIMIT_MAX_ATTEMPTS = 5
INVITE_CODE_RATE_LIMIT_WINDOW_MINUTES = 15
PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS = 3
PASSWORD_RESET_RATE_LIMIT_WINDOW_MINUTES = 60
EMAIL_VERIFY_RESEND_RATE_LIMIT_MAX_ATTEMPTS = 3
EMAIL_VERIFY_RESEND_RATE_LIMIT_WINDOW_MINUTES = 60
RATE_LIMIT_EVENT_RETENTION_DAYS = 1


class UsernameTakenError(Exception):
    pass


class PlayerNumberTakenError(Exception):
    pass


class MemberAlreadyRegisteredError(Exception):
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
        if "stripe_cancel_at_period_end" not in tenant_columns:
            conn.execute(
                "ALTER TABLE tenants ADD COLUMN stripe_cancel_at_period_end INTEGER NOT NULL DEFAULT 0"
            )
        if "stripe_current_period_end" not in tenant_columns:
            conn.execute("ALTER TABLE tenants ADD COLUMN stripe_current_period_end TEXT")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_events_bucket ON rate_limit_events(bucket, created_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER,
                user_id INTEGER,
                username TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant ON audit_logs(tenant_id, id)"
        )

        # ---- 大会管理・回戦実施・ゲスト共有リンク ----
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                table_method TEXT NOT NULL,
                scoring_mode TEXT NOT NULL,
                scoring_config TEXT NOT NULL,
                numbering_method TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT '準備中',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tournaments_tenant ON tournaments(tenant_id, id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                member_id INTEGER NOT NULL REFERENCES members(id),
                player_number INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tournament_id, player_number),
                UNIQUE(tournament_id, member_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                round_number INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tournament_id, round_number)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
                table_number INTEGER NOT NULL,
                UNIQUE(round_id, table_number)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_id INTEGER NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
                member_id INTEGER NOT NULL REFERENCES members(id),
                position TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS absences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
                member_id INTEGER NOT NULL REFERENCES members(id),
                UNIQUE(round_id, member_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
                member_id INTEGER NOT NULL REFERENCES members(id),
                raw_score INTEGER NOT NULL,
                UNIQUE(round_id, member_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tobi_busters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
                busted_member_id INTEGER NOT NULL REFERENCES members(id),
                buster_member_id INTEGER NOT NULL REFERENCES members(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_guest_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                created_by_user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )


# ---- レート制限（総当たり攻撃対策） ----
# bucketは操作の種類と対象を表す自由形式の文字列
# （例: "login:{username}", "signup_invite_code", "password_reset:{email}"）。
# 直近RATE_LIMIT_EVENT_RETENTION_DAYS分より古いイベントは記録のたびに間引くので、
# テーブルは無制限には増えない。

def record_rate_limit_event(bucket: str) -> None:
    now = _now()
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM rate_limit_events WHERE created_at < ?",
            (_iso(now - timedelta(days=RATE_LIMIT_EVENT_RETENTION_DAYS)),),
        )
        conn.execute(
            "INSERT INTO rate_limit_events (bucket, created_at) VALUES (?, ?)",
            (bucket, _iso(now)),
        )


def count_recent_rate_limit_events(bucket: str, window_minutes: int) -> int:
    window_start = _iso(_now() - timedelta(minutes=window_minutes))
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE bucket = ? AND created_at >= ?",
            (bucket, window_start),
        )
        return cur.fetchone()[0]


def is_rate_limited(bucket: str, max_attempts: int, window_minutes: int) -> bool:
    return count_recent_rate_limit_events(bucket, window_minutes) >= max_attempts


# ---- 監査ログ ----

def record_audit_log(
    action: str,
    tenant_id: int | None = None,
    user_id: int | None = None,
    username: str | None = None,
    detail: str = "",
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (tenant_id, user_id, username, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tenant_id, user_id, username, action, detail, _iso(_now())),
        )


def get_audit_logs(tenant_id: int, limit: int = 200) -> list[dict]:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM audit_logs WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
            (tenant_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_audit_logs_all_tenants(limit: int = 200) -> list[dict]:
    """テナントを横断した直近の監査ログ。運営専用画面向け。"""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT a.id, a.tenant_id, t.name AS tenant_name, a.user_id, a.username,
                   a.action, a.detail, a.created_at
            FROM audit_logs a
            LEFT JOIN tenants t ON t.id = a.tenant_id
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


# ---- 運営専用: テナント横断の一覧 ----

def get_tenant_overview() -> list[dict]:
    """全テナントの一覧（プラン・課金状態・有効メンバー数・作成日）。運営専用画面向け。"""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT
                t.id,
                t.name,
                t.plan,
                t.stripe_subscription_status,
                t.stripe_cancel_at_period_end,
                t.created_at,
                (
                    SELECT COUNT(*) FROM members m
                    WHERE m.tenant_id = t.id AND m.is_active = 1
                ) AS member_count
            FROM tenants t
            ORDER BY t.id ASC
            """
        )
        rows = []
        for row in cur.fetchall():
            row = dict(row)
            row["stripe_cancel_at_period_end"] = bool(row["stripe_cancel_at_period_end"])
            rows.append(row)
        return rows


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
    "stripe_subscription_status, plan_updated_at, "
    "stripe_cancel_at_period_end, stripe_current_period_end"
)


def _row_to_tenant(row: sqlite3.Row) -> dict:
    tenant = dict(row)
    tenant["stripe_cancel_at_period_end"] = bool(tenant["stripe_cancel_at_period_end"])
    return tenant


def get_tenant(tenant_id: int) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        return _row_to_tenant(row) if row else None


def get_tenant_by_stripe_customer_id(stripe_customer_id: str) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
        return _row_to_tenant(row) if row else None


def update_tenant_plan(
    tenant_id: int,
    plan: str,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    stripe_subscription_status: str | None = None,
    cancel_at_period_end: bool = False,
    current_period_end: str | None = None,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
) -> None:
    """テナントの課金状態をまとめて書き換える（全カラムを上書きする）。

    cancel_at_period_end / current_period_end を意図せずリセットしないよう、
    呼び出し側は必要に応じて既存の tenant の値を読み出してから渡すこと
    （invoice.payment_failed など、Stripeのイベントに解約予約の情報が
    含まれない場合は tenant["stripe_cancel_at_period_end"] 等をそのまま渡す）。

    呼び出し元がすべて（Webhook・Checkoutリダイレクト・解約ボタンなど）ここを
    通るため、監査ログへの記録もここに集約している。actor_* はUI操作で人が
    起点の場合のみ渡し、Webhookなどシステム起点の場合はNoneのままでよい。
    """
    if plan not in PLANS:
        raise ValueError(f"不正なプランです: {plan}")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE tenants
            SET plan = ?, stripe_customer_id = ?, stripe_subscription_id = ?,
                stripe_subscription_status = ?, plan_updated_at = ?,
                stripe_cancel_at_period_end = ?, stripe_current_period_end = ?
            WHERE id = ?
            """,
            (
                plan,
                stripe_customer_id,
                stripe_subscription_id,
                stripe_subscription_status,
                _iso(_now()),
                int(cancel_at_period_end),
                current_period_end,
                tenant_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (tenant_id, user_id, username, action, detail, created_at)
            VALUES (?, ?, ?, 'plan_change', ?, ?)
            """,
            (
                tenant_id,
                actor_user_id,
                actor_username,
                f"plan={plan}, status={stripe_subscription_status}, cancel_at_period_end={cancel_at_period_end}",
                _iso(_now()),
            ),
        )


def count_active_members(tenant_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM members WHERE tenant_id = ? AND is_active = 1", (tenant_id,)
        )
        return cur.fetchone()[0]


def count_tenant_users(tenant_id: int) -> int:
    """テナントに所属するユーザー（ログインアカウント）数。初回ガイドの進捗表示に使う。"""
    with get_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id = ?", (tenant_id,))
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


def count_tenant_invites_this_month(tenant_id: int) -> int:
    """今月（暦月）に発行された招待の件数。Freeプランの月次上限チェックに使う。"""
    now = _now()
    month_start = _iso(datetime(now.year, now.month, 1))
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM tenant_invites WHERE tenant_id = ? AND created_at >= ?",
            (tenant_id, month_start),
        )
        return cur.fetchone()[0]


def count_tenant_invites_total(tenant_id: int) -> int:
    """テナントがこれまでに発行した招待リンクの総数（月次上限とは無関係）。初回ガイドの進捗表示に使う。"""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM tenant_invites WHERE tenant_id = ?", (tenant_id,)
        )
        return cur.fetchone()[0]


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

def add_member(
    tenant_id: int, user_id: int, name: str, memo: str, max_members: int | None = None
) -> int | None:
    """メンバーを追加する。max_membersを指定すると、有効メンバー数が上限に
    達していれば追加せずNoneを返す。

    上限チェックと追加をBEGIN IMMEDIATEで開始した単一トランザクション内で行う
    ことで、同時に複数リクエストが来ても上限を超えて追加されないようにする
    （SELECT COUNTしてから別にINSERTする素朴な実装だと、2つのリクエストが
    どちらも上限未満の件数を読んでしまい、両方追加されてしまうレースがある）。
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        if max_members is not None:
            current_count = conn.execute(
                "SELECT COUNT(*) FROM members WHERE tenant_id = ? AND is_active = 1", (tenant_id,)
            ).fetchone()[0]
            if current_count >= max_members:
                conn.execute("ROLLBACK")
                return None
        cur = conn.execute(
            "INSERT INTO members (tenant_id, user_id, name, memo, created_at) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, user_id, name, memo, _iso(_now())),
        )
        conn.execute("COMMIT")
        return cur.lastrowid
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


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


# ---- tournaments (大会) ----

_TOURNAMENT_COLUMNS = (
    "id, tenant_id, name, kind, table_method, scoring_mode, scoring_config, "
    "numbering_method, start_date, end_date, status, created_at"
)


def _row_to_tournament(row: sqlite3.Row) -> dict:
    tournament = dict(row)
    config = json.loads(tournament["scoring_config"])
    if tournament["scoring_mode"] == "得点" and "uma_table" in config:
        # JSONのオブジェクトキーは文字列になるため、浮き人数(int)キーに戻す
        config["uma_table"] = {int(k): v for k, v in config["uma_table"].items()}
    tournament["scoring_config"] = config
    return tournament


def create_tournament(
    tenant_id: int,
    name: str,
    kind: str,
    table_method: str,
    scoring_mode: str,
    scoring_config: dict,
    numbering_method: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    if kind not in TOURNAMENT_KINDS:
        raise ValueError(f"不正な大会種別です: {kind}")
    if table_method not in TABLE_METHODS:
        raise ValueError(f"不正な卓組み方式です: {table_method}")
    if scoring_mode not in SCORING_MODES:
        raise ValueError(f"不正な評価方式です: {scoring_mode}")
    if numbering_method not in NUMBERING_METHODS:
        raise ValueError(f"不正な選手番号の採番方式です: {numbering_method}")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO tournaments
                (tenant_id, name, kind, table_method, scoring_mode, scoring_config,
                 numbering_method, start_date, end_date, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '準備中', ?)
            """,
            (
                tenant_id,
                name,
                kind,
                table_method,
                scoring_mode,
                json.dumps(scoring_config),
                numbering_method,
                start_date,
                end_date,
                _iso(_now()),
            ),
        )
        return cur.lastrowid


def get_tournaments(tenant_id: int) -> list[dict]:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"SELECT {_TOURNAMENT_COLUMNS} FROM tournaments WHERE tenant_id = ? ORDER BY id DESC",
            (tenant_id,),
        )
        return [_row_to_tournament(row) for row in cur.fetchall()]


def get_tournament(tenant_id: int, tournament_id: int) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_TOURNAMENT_COLUMNS} FROM tournaments WHERE id = ? AND tenant_id = ?",
            (tournament_id, tenant_id),
        ).fetchone()
        return _row_to_tournament(row) if row else None


def update_tournament(
    tenant_id: int,
    tournament_id: int,
    name: str,
    kind: str,
    table_method: str,
    scoring_mode: str,
    scoring_config: dict,
    numbering_method: str,
    start_date: str | None,
    end_date: str | None,
    status: str,
) -> None:
    if status not in TOURNAMENT_STATUSES:
        raise ValueError(f"不正な状態です: {status}")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE tournaments
            SET name = ?, kind = ?, table_method = ?, scoring_mode = ?, scoring_config = ?,
                numbering_method = ?, start_date = ?, end_date = ?, status = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                name,
                kind,
                table_method,
                scoring_mode,
                json.dumps(scoring_config),
                numbering_method,
                start_date,
                end_date,
                status,
                tournament_id,
                tenant_id,
            ),
        )


def delete_tournament(tenant_id: int, tournament_id: int) -> None:
    """大会を削除する。参加メンバー・回戦・卓・着席・抜け番・成績・ゲストリンクも
    外部キーのON DELETE CASCADEにより連動して削除される。"""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM tournaments WHERE id = ? AND tenant_id = ?", (tournament_id, tenant_id)
        )


# ---- tournament members (大会参加メンバー) ----

def next_tournament_player_number(tournament_id: int) -> int:
    """受付順の自動採番用: 現在の最大選手番号+1を返す（未参加なら1）。"""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(player_number), 0) FROM tournament_members WHERE tournament_id = ?",
            (tournament_id,),
        )
        return cur.fetchone()[0] + 1


def add_tournament_member(tournament_id: int, member_id: int, player_number: int) -> int:
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO tournament_members (tournament_id, member_id, player_number, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (tournament_id, member_id, player_number, _iso(_now())),
            )
        except sqlite3.IntegrityError as exc:
            existing_numbers = {
                row[0]
                for row in conn.execute(
                    "SELECT player_number FROM tournament_members WHERE tournament_id = ?",
                    (tournament_id,),
                )
            }
            if player_number in existing_numbers:
                raise PlayerNumberTakenError(player_number) from exc
            raise MemberAlreadyRegisteredError(member_id) from exc
        return cur.lastrowid


def get_tournament_members(tournament_id: int) -> list[dict]:
    """選手番号昇順。メンバー名も一緒に返す。"""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT tm.id, tm.tournament_id, tm.member_id, tm.player_number, tm.created_at,
                   m.name AS member_name
            FROM tournament_members tm
            JOIN members m ON m.id = tm.member_id
            WHERE tm.tournament_id = ?
            ORDER BY tm.player_number ASC
            """,
            (tournament_id,),
        )
        return [dict(row) for row in cur.fetchall()]


# ---- rounds / tables / seats / absences (回戦実施) ----

def count_rounds(tournament_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM rounds WHERE tournament_id = ?", (tournament_id,))
        return cur.fetchone()[0]


def get_rounds(tournament_id: int) -> list[dict]:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, tournament_id, round_number, created_at FROM rounds "
            "WHERE tournament_id = ? ORDER BY round_number ASC",
            (tournament_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def save_round(tournament_id: int, round_number: int, tables: list[dict], absent_member_ids: list[int]) -> int:
    """卓組み結果を確定保存する。

    tables: [{"東": member_id, "南": member_id, "西": member_id, "北": member_id}, ...]
    （table_logic.pyの戻り値をそのまま、選手番号→member_idに変換したもの）
    round・卓・着席・抜け番のレコードをまとめて1トランザクションで作成する。
    """
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO rounds (tournament_id, round_number, created_at) VALUES (?, ?, ?)",
            (tournament_id, round_number, _iso(_now())),
        )
        round_id = cur.lastrowid
        for table_number, table in enumerate(tables, start=1):
            table_cur = conn.execute(
                "INSERT INTO tables (round_id, table_number) VALUES (?, ?)",
                (round_id, table_number),
            )
            table_id = table_cur.lastrowid
            for position, member_id in table.items():
                conn.execute(
                    "INSERT INTO seats (table_id, member_id, position) VALUES (?, ?, ?)",
                    (table_id, member_id, position),
                )
        for member_id in absent_member_ids:
            conn.execute(
                "INSERT INTO absences (round_id, member_id) VALUES (?, ?)",
                (round_id, member_id),
            )
        return round_id


def get_round_seats(round_id: int) -> list[dict]:
    """[{"table_number":, "position":, "member_id":}, ...] を卓番号→着席順に返す。"""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT t.table_number, s.position, s.member_id
            FROM seats s
            JOIN tables t ON t.id = s.table_id
            WHERE t.round_id = ?
            ORDER BY t.table_number ASC, s.id ASC
            """,
            (round_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_round_absences(round_id: int) -> list[int]:
    with get_connection() as conn:
        cur = conn.execute("SELECT member_id FROM absences WHERE round_id = ?", (round_id,))
        return [row[0] for row in cur.fetchall()]


# ---- results (成績) ----

def save_round_results(round_id: int, raw_scores: dict, tobi_busters: dict) -> None:
    """素点と飛び賞の「誰が飛ばしたか」を保存する。

    raw_scores: {member_id: 素点}
    tobi_busters: {飛んだmember_id: [飛ばしたmember_id, ...]}
    既存レコードがあれば削除してから入れ直す（成績入力の編集に対応するため）。
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM results WHERE round_id = ?", (round_id,))
        conn.execute("DELETE FROM tobi_busters WHERE round_id = ?", (round_id,))
        for member_id, raw_score in raw_scores.items():
            conn.execute(
                "INSERT INTO results (round_id, member_id, raw_score) VALUES (?, ?, ?)",
                (round_id, member_id, raw_score),
            )
        for busted_member_id, busters in tobi_busters.items():
            for buster_member_id in busters:
                conn.execute(
                    "INSERT INTO tobi_busters (round_id, busted_member_id, buster_member_id) VALUES (?, ?, ?)",
                    (round_id, busted_member_id, buster_member_id),
                )


def get_round_results(round_id: int) -> dict:
    with get_connection() as conn:
        cur = conn.execute("SELECT member_id, raw_score FROM results WHERE round_id = ?", (round_id,))
        return {row[0]: row[1] for row in cur.fetchall()}


def get_round_tobi_busters(round_id: int) -> dict:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT busted_member_id, buster_member_id FROM tobi_busters WHERE round_id = ?",
            (round_id,),
        )
        busters: dict[int, list[int]] = {}
        for busted_member_id, buster_member_id in cur.fetchall():
            busters.setdefault(busted_member_id, []).append(buster_member_id)
        return busters


# ---- ゲスト共有リンク ----
# tenant_invitesと同じくtoken_hashのみ保存し、生トークンはDBに残さない。
# 「発行済みならリンクを再表示」は、生成直後のst.session_state（招待リンクの
# last_invite_linkと同じ扱い）でのみ可能。別セッションで再ログインした場合は
# 「発行済みだが再表示はできない」旨を示し、必要なら再発行してもらう。

def create_guest_link(tournament_id: int, created_by_user_id: int) -> str:
    raw_token = _generate_token()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tournament_guest_links
                (tournament_id, token_hash, created_by_user_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (tournament_id, _hash_token(raw_token), created_by_user_id, _iso(_now())),
        )
    return raw_token


def get_active_guest_link(tournament_id: int) -> dict | None:
    """失効していない最新のゲストリンクのレコードを返す（存在確認・無効化操作用）。"""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT * FROM tournament_guest_links
            WHERE tournament_id = ? AND revoked_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (tournament_id,),
        ).fetchone()
        return dict(row) if row else None


def revoke_guest_link(link_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE tournament_guest_links SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_iso(_now()), link_id),
        )


def get_tournament_by_guest_token(raw_token: str) -> dict | None:
    """有効なゲストトークンなら対応する大会情報を返す。失効・存在しないトークンならNone。"""
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        link_row = conn.execute(
            "SELECT tournament_id FROM tournament_guest_links WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if link_row is None:
            return None
        tournament_row = conn.execute(
            f"SELECT {_TOURNAMENT_COLUMNS} FROM tournaments WHERE id = ?",
            (link_row["tournament_id"],),
        ).fetchone()
        return _row_to_tournament(tournament_row) if tournament_row else None
