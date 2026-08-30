import os
import secrets
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

import bcrypt
import stripe
import streamlit as st
import streamlit_authenticator as stauth

import db
import exports

st.set_page_config(page_title="麻雀卓組みアプリ", page_icon="🀄")

db.init_db()

# ログイン試行のタイミング差から既存ユーザー名かどうかを推測されないよう、
# ユーザーが存在しない場合もこのダミーハッシュ相手にbcrypt検証を行い、
# 検証にかかる時間を実在ユーザーの場合とそろえる。
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode()


def format_date_jp(iso_str: str | None) -> str:
    if not iso_str:
        return "不明"
    dt = datetime.fromisoformat(iso_str)
    return f"{dt.year}年{dt.month}月{dt.day}日"


def get_auth_setting(env_var: str, secrets_key: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    try:
        return st.secrets["auth"][secrets_key]
    except Exception:
        raise RuntimeError(
            f"認証設定「{secrets_key}」が見つかりません。"
            f"環境変数 {env_var} か .streamlit/secrets.toml を設定してください。"
        )


def get_optional_setting(env_var: str, secrets_key: str, default: str | None = None) -> str | None:
    value = os.environ.get(env_var)
    if value:
        return value
    try:
        return st.secrets["auth"][secrets_key]
    except Exception:
        return default


def build_credentials() -> dict:
    users = db.get_all_users()
    return {
        "usernames": {
            u["username"]: {"name": u["username"], "password": u["password_hash"]}
            for u in users
        }
    }


APP_BASE_URL = get_optional_setting("APP_BASE_URL", "app_base_url", "http://localhost:8501").rstrip("/")

# 運営（自分）専用の管理画面にアクセスできるユーザー名の一覧。
# テナントのrole（admin/member）とは独立した、テナント横断の権限。
# カンマ区切りで複数指定可。ここに載っていないユーザーには画面自体を一切表示しない。
_OPERATOR_USERNAMES_RAW = get_optional_setting("OPERATOR_USERNAMES", "operator_usernames", "") or ""
OPERATOR_USERNAMES = {u.strip().lower() for u in _OPERATOR_USERNAMES_RAW.split(",") if u.strip()}

STRIPE_SECRET_KEY = get_optional_setting("STRIPE_SECRET_KEY", "stripe_secret_key")
STRIPE_PRICE_ID_PRO = get_optional_setting("STRIPE_PRICE_ID_PRO", "stripe_price_id_pro")
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID_PRO)
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def send_email(to_email: str, subject: str, body: str) -> None:
    smtp_host = get_optional_setting("SMTP_HOST", "smtp_host")
    if not smtp_host:
        # SMTP未設定時の開発用フォールバック。ローカル確認用にコンソールと画面の両方に出す。
        print(f"[開発用メール送信]\nTo: {to_email}\n件名: {subject}\n{body}")
        st.info(f"（開発用）メール送信先が未設定のため、ここに内容を表示します。\n\n{body}")
        return

    smtp_port = int(get_optional_setting("SMTP_PORT", "smtp_port", "587"))
    smtp_user = get_optional_setting("SMTP_USER", "smtp_user")
    smtp_password = get_optional_setting("SMTP_PASSWORD", "smtp_password")
    smtp_from = get_optional_setting("SMTP_FROM", "smtp_from", smtp_user)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.send_message(message)


st.title("麻雀卓組みアプリ")

query_params = st.query_params

# ---- メールアドレス確認リンク ----
verify_token = query_params.get("verify")
if verify_token:
    user_id = db.verify_email_token(verify_token)
    if user_id:
        st.success("メールアドレスを確認しました。下のログイン画面からログインしてください。")
    else:
        st.error("認証リンクが無効か、有効期限が切れています。もう一度サインアップするか、確認メールの再送をお試しください。")
    if st.button("ログイン画面へ"):
        st.query_params.clear()
        st.rerun()
    st.stop()

# ---- パスワード再設定リンク ----
reset_token = query_params.get("reset")
if reset_token:
    st.subheader("パスワードの再設定")
    with st.form("reset_password_form"):
        new_password = st.text_input("新しいパスワード（8文字以上）", type="password")
        new_password_confirm = st.text_input("新しいパスワード（確認）", type="password")
        reset_submitted = st.form_submit_button("再設定する")

    if reset_submitted:
        if len(new_password) < db.MIN_PASSWORD_LEN:
            st.error(f"パスワードは{db.MIN_PASSWORD_LEN}文字以上にしてください。")
        elif new_password != new_password_confirm:
            st.error("パスワードが一致しません。")
        else:
            new_hash = stauth.Hasher.hash(new_password)
            if db.reset_password_with_token(reset_token, new_hash):
                st.success("パスワードを再設定しました。")
                if st.button("ログイン画面へ"):
                    st.query_params.clear()
                    st.rerun()
            else:
                st.error("リンクが無効か、有効期限が切れています。もう一度パスワード再設定をお申し込みください。")
    st.stop()

# ---- Stripe決済完了後のリダイレクト ----
checkout_status = query_params.get("checkout")
if checkout_status == "success":
    session_id = query_params.get("session_id")
    session = None
    if session_id and STRIPE_ENABLED:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except Exception:
            session = None

    if session and session.payment_status == "paid" and session.metadata.get("tenant_id"):
        db.update_tenant_plan(
            int(session.metadata["tenant_id"]),
            "pro",
            stripe_customer_id=session.customer,
            stripe_subscription_id=session.subscription,
            stripe_subscription_status="active",
            actor_username="stripe_checkout_redirect",
        )
        st.success("お支払いが完了しました。Proプランになりました！")
    else:
        st.error("決済状況を確認できませんでした。お手数ですが、もう一度アップグレードをお試しください。")

    if st.button("ログイン画面へ"):
        st.query_params.clear()
        st.rerun()
    st.stop()

if checkout_status == "cancel":
    st.info("決済がキャンセルされました。プランはFreeのままです。")
    if st.button("ログイン画面へ"):
        st.query_params.clear()
        st.rerun()
    st.stop()

# ---- 通常のログイン／サインアップ ----
auth_cookie_name = get_auth_setting("AUTH_COOKIE_NAME", "cookie_name")
auth_cookie_key = get_auth_setting("AUTH_COOKIE_KEY", "cookie_key")
auth_invite_code = get_auth_setting("AUTH_INVITE_CODE", "invite_code")

authenticator = stauth.Authenticate(
    build_credentials(),
    auth_cookie_name,
    auth_cookie_key,
    cookie_expiry_days=30,
    auto_hash=False,
)

# streamlit-authenticatorの標準ログイン(authenticator.login())は使わず、
# 自前でフォームと認証チェックを実装している。理由:
# - 標準実装の総当たり対策(max_login_attempts)は、認証情報の辞書を毎リラン
#   再構築しているためリランをまたいで失敗回数を保持できず機能しない
# - 入力されたユーザー名を受け取れないと、ユーザー名単位のレート制限が組めない
# Cookie経由の自動ログインとログアウトは引き続きauthenticatorに任せる。

if not st.session_state.get("authentication_status"):
    cookie_token = authenticator.cookie_controller.get_cookie()
    if cookie_token and "username" in cookie_token:
        cookie_user = db.get_user_by_username(cookie_token["username"])
        if cookie_user:
            st.session_state["authentication_status"] = True
            st.session_state["username"] = cookie_user["username"]
            st.session_state["name"] = cookie_user["username"]
        else:
            # 保存された自動ログイン用Cookieが、存在しないユーザー名を指している場合
            # （DBリセットやアカウント削除後の古いCookieなど）。Cookieを破棄する。
            authenticator.cookie_controller.delete_cookie()
            st.warning("ログイン情報の有効期限が切れました。もう一度ログインしてください。")

if not st.session_state.get("authentication_status"):
    with st.form("login_form"):
        login_username_input = st.text_input("Username", autocomplete="off")
        login_password_input = st.text_input("Password", type="password", autocomplete="off")
        login_submitted = st.form_submit_button("Login")

    if login_submitted:
        login_username_norm = login_username_input.strip().lower()
        login_bucket = f"login:{login_username_norm}"
        if db.is_rate_limited(
            login_bucket, db.LOGIN_RATE_LIMIT_MAX_ATTEMPTS, db.LOGIN_RATE_LIMIT_WINDOW_MINUTES
        ):
            st.session_state["authentication_status"] = None
            st.error(
                "ログイン試行が多すぎます。"
                f"{db.LOGIN_RATE_LIMIT_WINDOW_MINUTES}分ほど時間をおいて再度お試しください。"
            )
        else:
            login_user = db.get_user_by_username(login_username_norm)
            hash_to_check = login_user["password_hash"] if login_user else _DUMMY_PASSWORD_HASH
            password_ok = stauth.Hasher.check_pw(login_password_input, hash_to_check)
            if login_user and password_ok:
                st.session_state["authentication_status"] = True
                st.session_state["username"] = login_user["username"]
                st.session_state["name"] = login_user["username"]
                authenticator.cookie_controller.set_cookie()
                db.record_audit_log(
                    action="login",
                    tenant_id=login_user["tenant_id"],
                    user_id=login_user["id"],
                    username=login_user["username"],
                )
                st.rerun()
            else:
                db.record_rate_limit_event(login_bucket)
                st.session_state["authentication_status"] = False

auth_status = st.session_state.get("authentication_status")

if auth_status is False:
    st.error("ユーザー名またはパスワードが違います。")

if not auth_status:
    st.divider()

    tenant_invite_token = query_params.get("invite")
    tenant_invite = db.get_tenant_invite(tenant_invite_token) if tenant_invite_token else None
    if tenant_invite_token and tenant_invite is None:
        st.warning("招待リンクが無効か、有効期限が切れています。招待した管理者に再発行を依頼してください。")

    signup_label = "新規登録（チームに参加）" if tenant_invite else "新規登録（主催者アカウント作成）"
    with st.expander(signup_label):
        if tenant_invite:
            tenant = db.get_tenant(tenant_invite["tenant_id"])
            st.caption(f"「{tenant['name']}」のチームに参加します。")
        with st.form("signup_form", clear_on_submit=True):
            new_username = st.text_input("ユーザー名（英数字とアンダースコア、3〜30文字）")
            new_email = st.text_input("メールアドレス")
            new_password = st.text_input("パスワード（8文字以上）", type="password")
            new_password_confirm = st.text_input("パスワード（確認）", type="password")
            if not tenant_invite:
                invite_code = st.text_input("招待コード（合言葉）", type="password")
            else:
                invite_code = None
            signup_submitted = st.form_submit_button("登録")

        if signup_submitted:
            new_username = new_username.strip().lower()
            new_email = new_email.strip().lower()
            invite_code_bucket = "signup_invite_code"
            if not tenant_invite and db.is_rate_limited(
                invite_code_bucket,
                db.INVITE_CODE_RATE_LIMIT_MAX_ATTEMPTS,
                db.INVITE_CODE_RATE_LIMIT_WINDOW_MINUTES,
            ):
                st.error(
                    "招待コードの試行回数が多すぎます。"
                    f"{db.INVITE_CODE_RATE_LIMIT_WINDOW_MINUTES}分ほど時間をおいて再度お試しください。"
                )
            elif not tenant_invite and not secrets.compare_digest(invite_code, auth_invite_code):
                db.record_rate_limit_event(invite_code_bucket)
                st.error("招待コードが違います。")
            elif not db.USERNAME_RE.match(new_username):
                st.error("ユーザー名は英数字とアンダースコアで3〜30文字にしてください。")
            elif not db.EMAIL_RE.match(new_email):
                st.error("メールアドレスの形式が正しくありません。")
            elif len(new_password) < db.MIN_PASSWORD_LEN:
                st.error(f"パスワードは{db.MIN_PASSWORD_LEN}文字以上にしてください。")
            elif new_password != new_password_confirm:
                st.error("パスワードが一致しません。")
            elif db.get_user_by_username(new_username):
                st.error("そのユーザー名は既に使われています。")
            elif tenant_invite_token and tenant_invite is None:
                st.error("招待リンクが無効です。招待した管理者に再発行を依頼してください。")
            else:
                try:
                    password_hash = stauth.Hasher.hash(new_password)
                    join_tenant_id = tenant_invite["tenant_id"] if tenant_invite else None
                    join_role = tenant_invite["role"] if tenant_invite else "admin"
                    new_user_id = db.add_user(
                        new_username,
                        password_hash,
                        email=new_email,
                        tenant_id=join_tenant_id,
                        role=join_role,
                    )
                    if tenant_invite:
                        db.consume_tenant_invite(tenant_invite_token)
                    verify_raw_token = db.create_email_verification_token(new_user_id)
                    verify_link = f"{APP_BASE_URL}/?verify={verify_raw_token}"
                    send_email(
                        new_email,
                        "【麻雀卓組みアプリ】メールアドレスの確認",
                        f"以下のリンクをクリックしてメールアドレスを確認してください（{db.EMAIL_VERIFICATION_TTL_HOURS}時間有効）。\n{verify_link}",
                    )
                    st.success("登録しました。確認メールを送信しました。メール内のリンクをクリックしてから、ログインしてください。")
                except db.UsernameTakenError:
                    st.error("そのユーザー名は既に使われています。")

    with st.expander("パスワードを忘れた方はこちら"):
        with st.form("forgot_password_form", clear_on_submit=True):
            forgot_email = st.text_input("登録したメールアドレス")
            forgot_submitted = st.form_submit_button("再設定用リンクを送る")

        if forgot_submitted:
            forgot_email = forgot_email.strip().lower()
            forgot_bucket = f"password_reset:{forgot_email}"
            if forgot_email and db.is_rate_limited(
                forgot_bucket,
                db.PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS,
                db.PASSWORD_RESET_RATE_LIMIT_WINDOW_MINUTES,
            ):
                # このメールアドレス宛のリクエストが直近で既に上限に達している場合の案内。
                # bucketはメール存在有無に関係なく常に記録するため、この分岐が出ても
                # 「そのメールアドレスが登録されている」ことは漏れない。
                st.warning(
                    "リクエストが多すぎます。"
                    f"{db.PASSWORD_RESET_RATE_LIMIT_WINDOW_MINUTES}分ほど時間をおいて再度お試しください。"
                )
            else:
                if forgot_email:
                    db.record_rate_limit_event(forgot_bucket)
                user = db.get_user_by_email(forgot_email) if forgot_email else None
                if user:
                    reset_raw_token = db.create_password_reset_token(user["id"])
                    reset_link = f"{APP_BASE_URL}/?reset={reset_raw_token}"
                    send_email(
                        user["email"],
                        "【麻雀卓組みアプリ】パスワード再設定のご案内",
                        f"以下のリンクからパスワードを再設定してください（{db.PASSWORD_RESET_TTL_MINUTES}分間有効）。\n{reset_link}",
                    )
                # メール登録の有無を教えない（メールアドレスの存在確認への悪用を防ぐ）
                st.success("入力されたメールアドレスが登録されている場合、再設定用のリンクを送信しました。")

    st.stop()

# ---- ここから先はログイン済みユーザーのみ ----

current_user = db.get_user_by_username(st.session_state["username"])
if current_user is None:
    st.error("ユーザー情報の取得に失敗しました。再度ログインしてください。")
    st.stop()
user_id = current_user["id"]
tenant_id = current_user["tenant_id"]
tenant_info = db.get_tenant(tenant_id)
is_admin = current_user["role"] == "admin"
role_label = "管理者" if is_admin else "一般"
is_operator = current_user["username"] in OPERATOR_USERNAMES

with st.sidebar:
    st.write(f"ログイン中: {st.session_state['name']}（{role_label}）")
    authenticator.logout("ログアウト", location="sidebar")

    if is_admin:
        with st.expander("メンバーを招待する"):
            invite_role = st.selectbox(
                "招待する権限",
                options=["member", "admin"],
                format_func=lambda r: "一般" if r == "member" else "管理者",
            )

            is_pro = tenant_info["plan"] == "pro"
            invites_this_month = db.count_tenant_invites_this_month(tenant_id)
            invite_limit_reached = not is_pro and invites_this_month >= db.FREE_PLAN_INVITE_MONTHLY_LIMIT

            if is_pro:
                st.caption("Proプランは招待リンクを無制限に発行できます。")
            else:
                remaining = max(db.FREE_PLAN_INVITE_MONTHLY_LIMIT - invites_this_month, 0)
                st.caption(
                    f"Freeプランは招待リンクの発行が月{db.FREE_PLAN_INVITE_MONTHLY_LIMIT}回までです。"
                    f"今月あと{remaining}回発行できます。"
                )

            if invite_limit_reached:
                st.warning(
                    "今月の発行上限に達しました。サイドバーの「プラン」からProにアップグレードすると無制限になります。"
                )
            if st.button("招待リンクを発行", disabled=invite_limit_reached):
                invite_raw_token = db.create_tenant_invite(tenant_id, user_id, role=invite_role)
                st.session_state["last_invite_link"] = f"{APP_BASE_URL}/?invite={invite_raw_token}"
                db.record_audit_log(
                    action="invite_issued",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    username=current_user["username"],
                    detail=f"role={invite_role}",
                )
                st.rerun()
            if st.session_state.get("last_invite_link"):
                st.code(st.session_state["last_invite_link"])
                st.caption(f"{db.TENANT_INVITE_TTL_HOURS}時間有効・1回限り使用できます。")

        with st.expander("プラン", expanded=(tenant_info["plan"] == "free")):
            if tenant_info["plan"] == "pro":
                st.success("Proプラン（メンバー無制限）")
                if tenant_info["stripe_cancel_at_period_end"]:
                    st.warning(
                        "解約予約中です。今の請求期間の終わり"
                        f"（{format_date_jp(tenant_info['stripe_current_period_end'])}）"
                        "まではProプランを利用できます。"
                    )
                elif STRIPE_ENABLED and tenant_info["stripe_subscription_id"]:
                    if st.button("解約する"):
                        try:
                            subscription = stripe.Subscription.modify(
                                tenant_info["stripe_subscription_id"],
                                cancel_at_period_end=True,
                            )
                            period_end_ts = subscription["items"]["data"][0]["current_period_end"]
                            period_end_iso = datetime.fromtimestamp(
                                period_end_ts, tz=timezone.utc
                            ).isoformat(timespec="seconds")
                            db.update_tenant_plan(
                                tenant_id,
                                "pro",
                                stripe_customer_id=tenant_info["stripe_customer_id"],
                                stripe_subscription_id=subscription.id,
                                stripe_subscription_status=subscription.status,
                                cancel_at_period_end=True,
                                current_period_end=period_end_iso,
                                actor_user_id=user_id,
                                actor_username=current_user["username"],
                            )
                            st.success("解約を予約しました。")
                            st.rerun()
                        except Exception:
                            st.error("解約処理に失敗しました。時間をおいて再度お試しください。")
            else:
                st.write(f"Freeプラン（メンバー{db.FREE_PLAN_MEMBER_LIMIT}人まで）")
                if not STRIPE_ENABLED:
                    st.caption("Stripe未設定のため、アップグレードは準備中です。")
                else:
                    if st.button("Proにアップグレード（¥980/月）"):
                        checkout_session = stripe.checkout.Session.create(
                            mode="subscription",
                            line_items=[{"price": STRIPE_PRICE_ID_PRO, "quantity": 1}],
                            success_url=f"{APP_BASE_URL}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
                            cancel_url=f"{APP_BASE_URL}/?checkout=cancel",
                            customer_email=current_user["email"],
                            metadata={"tenant_id": str(tenant_id)},
                        )
                        st.session_state["checkout_url"] = checkout_session.url
                    if st.session_state.get("checkout_url"):
                        st.link_button("お支払いページへ進む", st.session_state["checkout_url"])

if not current_user["email_verified"]:
    st.warning("メールアドレスの確認がまだ完了していません。確認メール内のリンクをクリックしてください。")
    if st.button("確認メールを再送する"):
        resend_bucket = f"email_verify_resend:{user_id}"
        if db.is_rate_limited(
            resend_bucket,
            db.EMAIL_VERIFY_RESEND_RATE_LIMIT_MAX_ATTEMPTS,
            db.EMAIL_VERIFY_RESEND_RATE_LIMIT_WINDOW_MINUTES,
        ):
            st.warning(
                "再送リクエストが多すぎます。"
                f"{db.EMAIL_VERIFY_RESEND_RATE_LIMIT_WINDOW_MINUTES}分ほど時間をおいて再度お試しください。"
            )
        else:
            db.record_rate_limit_event(resend_bucket)
            verify_raw_token = db.create_email_verification_token(user_id)
            verify_link = f"{APP_BASE_URL}/?verify={verify_raw_token}"
            send_email(
                current_user["email"],
                "【麻雀卓組みアプリ】メールアドレスの確認",
                f"以下のリンクをクリックしてメールアドレスを確認してください（{db.EMAIL_VERIFICATION_TTL_HOURS}時間有効）。\n{verify_link}",
            )
            st.info("確認メールを再送しました。")
    st.stop()

# ---- 運営専用画面 ----
# is_operator は OPERATOR_USERNAMES（環境変数/secrets）に載っているユーザー名だけが
# Trueになる、テナントのrole（admin/member）とは独立した権限。載っていないユーザーは
# 一般利用者はもちろん、他テナントの管理者であってもこのブロック自体が描画されないため、
# 存在にすら気づけない。
if is_operator:
    st.divider()
    with st.expander("🔧 運営管理（Operator Only）", expanded=False):
        st.caption("全テナントの状況をテナント横断で確認できます。運営者のみ閲覧できます。")

        st.subheader("テナント一覧")
        tenants_overview = db.get_tenant_overview()
        if not tenants_overview:
            st.info("テナントがありません。")
        else:
            tenant_rows = [
                {
                    "テナントID": t["id"],
                    "テナント名": t["name"],
                    "プラン": "Pro" if t["plan"] == "pro" else "Free",
                    "課金状態": (
                        (t["stripe_subscription_status"] or "-")
                        + ("（解約予約中）" if t["stripe_cancel_at_period_end"] else "")
                    ),
                    "メンバー数": t["member_count"],
                    "作成日": format_date_jp(t["created_at"]),
                }
                for t in tenants_overview
            ]
            st.dataframe(tenant_rows, hide_index=True, use_container_width=True)
            st.caption(f"テナント数: {len(tenants_overview)}")

        st.subheader("直近の監査ログ（テナント横断）")
        audit_logs_all = db.get_audit_logs_all_tenants(limit=200)
        if not audit_logs_all:
            st.info("監査ログがありません。")
        else:
            audit_rows = [
                {
                    "日時": a["created_at"],
                    "テナント": a["tenant_name"] or f"(削除済み ID:{a['tenant_id']})",
                    "ユーザー": a["username"] or "-",
                    "操作": a["action"],
                    "詳細": a["detail"] or "",
                }
                for a in audit_logs_all
            ]
            st.dataframe(audit_rows, hide_index=True, use_container_width=True)

# ---- 初回ガイド ----
# 「テナント作成→招待リンク発行→最初のメンバーが参加」の3ステップを、
# まだチームメンバー（ログインアカウント）が管理者本人しかいない間だけ表示する。
# 卓組み生成機能は未実装のため、この導線には含めない。
if is_admin and db.count_tenant_users(tenant_id) <= 1:
    with st.container(border=True):
        st.subheader("👋 はじめに：3ステップで最初のチームメンバーを迎えましょう")
        st.caption(
            "ここでの「チームメンバー」は、一緒にこのアプリを運営するログインアカウント"
            "（管理者・一般）のことです。大会参加者の登録は、下の「メンバー登録」から行えます。"
        )

        st.markdown(f"1. ✅ **テナント作成** — 「{tenant_info['name']}」を作成しました。")

        if db.count_tenant_invites_total(tenant_id) > 0:
            st.markdown("2. ✅ **招待リンクを発行** 済みです。")
        else:
            st.markdown(
                "2. ⬜ **招待リンクを発行しましょう** — "
                "サイドバーの「メンバーを招待する」から発行し、一緒に運営する人に共有してください。"
            )

        st.markdown(
            "3. ⬜ **最初のメンバーが参加するのを待ちましょう** — "
            "招待リンクからサインアップが完了すると、この案内は自動的に消えます。"
        )

st.header("メンバー登録")

if not is_admin:
    st.info("メンバーの登録・編集・削除は管理者のみ行えます。一覧の閲覧はできます。")
else:
    member_count = db.count_active_members(tenant_id)
    plan_limit_reached = tenant_info["plan"] == "free" and member_count >= db.FREE_PLAN_MEMBER_LIMIT
    if plan_limit_reached:
        st.warning(
            f"Freeプランはメンバー{db.FREE_PLAN_MEMBER_LIMIT}人までです。"
            "サイドバーの「プラン」からProにアップグレードすると無制限に登録できます。"
        )

    with st.form("member_form", clear_on_submit=True):
        name = st.text_input("名前")
        memo = st.text_input("メモ（任意）")
        submitted = st.form_submit_button("登録")

    if submitted:
        name = name.strip()
        memo = memo.strip()
        if not name:
            st.error("名前を入力してください。")
        elif len(name) > db.MAX_NAME_LEN:
            st.error(f"名前は{db.MAX_NAME_LEN}文字以内にしてください。")
        elif len(memo) > db.MAX_MEMO_LEN:
            st.error(f"メモは{db.MAX_MEMO_LEN}文字以内にしてください。")
        else:
            try:
                if db.member_exists(tenant_id, name):
                    st.warning(f"「{name}」は既に登録されています。重複して登録します。")
                # 上限チェックはadd_member内で追加と同一トランザクションで行う
                # （同時リクエストでもFreeプランの上限を超えて追加されないように）。
                max_members = db.FREE_PLAN_MEMBER_LIMIT if tenant_info["plan"] == "free" else None
                member_id = db.add_member(tenant_id, user_id, name, memo, max_members=max_members)
                if member_id is None:
                    st.error(f"Freeプランの上限（{db.FREE_PLAN_MEMBER_LIMIT}人）に達しています。")
                else:
                    db.record_audit_log(
                        action="member_add",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        username=current_user["username"],
                        detail=f"member_id={member_id}, name={name}",
                    )
                    st.success(f"「{name}」を登録しました（ID: {member_id}）。")
            except Exception:
                st.error("登録に失敗しました。時間をおいて再度お試しください。")

st.divider()
st.header("メンバー一覧")

col_sort, col_show_retired = st.columns([2, 2])
with col_sort:
    sort_option = st.radio("並び順", ["登録順", "名前順"], horizontal=True, key="member_sort")
with col_show_retired:
    show_retired = st.checkbox("引退メンバーも表示", key="show_retired")

order = "name" if sort_option == "名前順" else "created"
try:
    members = db.get_members(tenant_id, order=order, include_retired=show_retired)
except Exception:
    st.error("メンバー一覧の取得に失敗しました。")
    members = []

if is_admin:
    col_export_csv, col_export_xlsx = st.columns(2)
    if tenant_info["plan"] == "pro":
        col_export_csv.download_button(
            "CSVでダウンロード",
            data=exports.build_members_csv(members),
            file_name="members.csv",
            mime="text/csv",
            disabled=not members,
        )
        col_export_xlsx.download_button(
            "Excelでダウンロード",
            data=exports.build_members_xlsx(members),
            file_name="members.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            disabled=not members,
        )
    else:
        col_export_csv.button("🔒 CSVでダウンロード", disabled=True)
        col_export_xlsx.button("🔒 Excelでダウンロード", disabled=True)
        st.caption("CSV/Excelへのエクスポートは Proプランで使えます。")
else:
    st.caption("CSV/Excelへのエクスポートは管理者のみ利用できます。")

if not members:
    st.info("登録されているメンバーがいません。")
else:
    for member in members:
        with st.container(border=True):
            retired = not member["is_active"]
            label = member["name"] + ("（引退）" if retired else "")
            cols = st.columns([3, 4, 2, 2])
            cols[0].write(label)
            cols[1].write(member["memo"] or "")

            edit_key = f"edit_open_{member['id']}"
            delete_key = f"delete_confirm_{member['id']}"

            if is_admin:
                if not retired:
                    if cols[2].button("編集", key=f"edit_btn_{member['id']}"):
                        st.session_state[edit_key] = not st.session_state.get(edit_key, False)
                    if cols[3].button("削除", key=f"delete_btn_{member['id']}"):
                        st.session_state[delete_key] = True
                else:
                    if cols[3].button("復帰", key=f"restore_btn_{member['id']}"):
                        db.restore_member(tenant_id, member["id"])
                        st.success(f"「{member['name']}」を復帰しました。")
                        st.rerun()

            if is_admin and st.session_state.get(edit_key):
                with st.form(f"edit_form_{member['id']}"):
                    new_name = st.text_input("名前", value=member["name"], key=f"edit_name_{member['id']}")
                    new_memo = st.text_input("メモ（任意）", value=member["memo"] or "", key=f"edit_memo_{member['id']}")
                    save = st.form_submit_button("保存")
                if save:
                    new_name = new_name.strip()
                    new_memo = new_memo.strip()
                    if not new_name:
                        st.error("名前を入力してください。")
                    elif len(new_name) > db.MAX_NAME_LEN:
                        st.error(f"名前は{db.MAX_NAME_LEN}文字以内にしてください。")
                    elif len(new_memo) > db.MAX_MEMO_LEN:
                        st.error(f"メモは{db.MAX_MEMO_LEN}文字以内にしてください。")
                    else:
                        try:
                            db.update_member(tenant_id, member["id"], new_name, new_memo)
                            st.session_state[edit_key] = False
                            st.success("更新しました。")
                            st.rerun()
                        except Exception:
                            st.error("更新に失敗しました。")

            if is_admin and st.session_state.get(delete_key):
                st.warning(f"「{member['name']}」を削除（引退扱い）します。よろしいですか？過去の大会記録は保持されます。")
                confirm_cols = st.columns(2)
                if confirm_cols[0].button("はい、削除する", key=f"confirm_delete_{member['id']}"):
                    db.retire_member(tenant_id, member["id"])
                    db.record_audit_log(
                        action="member_retire",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        username=current_user["username"],
                        detail=f"member_id={member['id']}, name={member['name']}",
                    )
                    st.session_state[delete_key] = False
                    st.success("削除しました（引退扱い）。")
                    st.rerun()
                if confirm_cols[1].button("キャンセル", key=f"cancel_delete_{member['id']}"):
                    st.session_state[delete_key] = False
                    st.rerun()
