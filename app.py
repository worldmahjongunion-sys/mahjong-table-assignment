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
import scoring_logic
import tournament_service
from table_logic import position_sort_key

st.set_page_config(page_title="麻雀卓組みアプリ", page_icon="🀄")

# Streamlitが出力するHTMLシェル(pipパッケージ同梱・このリポジトリでは編集不可。
# Railwayもデプロイのたびにpip installし直すため、そちら側を直接書き換えても残らない)は
# <html lang="en">固定になっている。画面の文言は全て日本語のため、宣言言語と実際の内容が
# 食い違い、モバイルブラウザの自動翻訳が誤作動しやすい(issue #34の予防策)。
# st.markdown(unsafe_allow_html=True)での<script>挿入はStreamlitのレンダリング方式
# (dangerouslySetInnerHTML)により実行されないため、st.iframeのiframe(srcdoc)経由で
# window.parent.document(＝実際のページ)を書き換える。失敗しても画面には影響しない
# (try/catchで握りつぶす)、翻訳を完全に禁止する保証はない、あくまで予防策。
st.iframe(
    """
    <script>
    try {
      var d = window.parent.document;
      d.documentElement.lang = "ja";
      d.documentElement.setAttribute("translate", "no");
      if (!d.querySelector('meta[name="google"]')) {
        var m = d.createElement("meta");
        m.name = "google";
        m.content = "notranslate";
        d.head.appendChild(m);
      }
    } catch (e) {}
    </script>
    """,
    height=1,
)

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


def points_input(label: str, points: int, key: str, min_points: int | None = None) -> int:
    """点数を100点単位で入力するnumber_input。実際の点数(int)を受け取り、実際の点数を返す。

    画面の入力値は100点単位（45,800点なら458）で、DB・計算ロジックへ渡す値は実点数のまま
    にするため、画面の入出力の境界（ここ）でだけ100倍/100分の1に変換する。
    """
    value = scoring_logic.points_to_hundreds(points)
    is_int = isinstance(value, int)
    kwargs = {}
    if min_points is not None:
        min_hundreds = scoring_logic.points_to_hundreds(min_points)
        kwargs["min_value"] = min_hundreds if is_int else float(min_hundreds)
    entered = st.number_input(label, value=value, step=1 if is_int else 1.0, key=key, **kwargs)
    return scoring_logic.hundreds_to_points(entered)


def format_result_value(scoring_mode: str, value) -> str:
    """成績表の「値」列の表示。得点方式は100点単位、ポイント方式の順位ポイントはそのままの数値。"""
    if scoring_mode == "得点":
        return scoring_logic.format_hundreds(value)
    return scoring_logic.format_point_value(value)


def standings_value_column_label(scoring_mode: str) -> str:
    """大会内順位表の値の列見出し。「点」(実際の点数)と「ポイント」(評価値)の用語を統一するため、
    汎用的な「値」ではなく評価方式に応じた具体的な名称にする。"""
    return "総合得点" if scoring_mode == "得点" else "合計ポイント"


def render_scoring_config_inputs(scoring_mode: str, key_prefix: str, defaults: dict | None = None) -> dict:
    """評価方式(得点/ポイント)のパラメータ入力欄を描画し、入力値の辞書を返す。

    表紙画面(519〜622行目付近)と同じ入力項目・初期値を、大会作成・編集フォームでも
    再利用するための共通処理。key_prefixで呼び出しごとにウィジェットキーを一意化する。

    ポイント方式には表紙画面にはない「開始点数」欄を追加で描画する
    （ゲスト向け画面実装依頼_Stage2.md 4章①対応。詳細は関数末尾のコメント参照）。
    """
    defaults = defaults or {}
    st.caption(scoring_logic.HUNDREDS_INPUT_HINT + "。順位ポイントは点数ではないのでそのままの数値です。")
    if scoring_mode == "得点":
        default_uma_table = scoring_logic.DEFAULT_UMA_CONFIG["uma_table"]
        saved_uma_table = defaults.get("uma_table", {})
        start_point = points_input(
            "開始点数（持ち点）",
            defaults.get("start_point", scoring_logic.DEFAULT_UMA_CONFIG["start_point"]),
            f"{key_prefix}_start_point",
        )
        return_point = points_input(
            "返し点（オカの基準点）",
            defaults.get("return_point", scoring_logic.DEFAULT_UMA_CONFIG["return_point"]),
            f"{key_prefix}_return_point",
        )
        oka = points_input(
            "オカ（0人浮き時にトップへ加算する額）",
            defaults.get("oka", scoring_logic.DEFAULT_UMA_CONFIG["oka"]),
            f"{key_prefix}_oka",
        )
        tobi_amount = points_input(
            "飛び賞額（0なら飛び賞なし）",
            defaults.get("tobi_amount", 0),
            f"{key_prefix}_tobi_amount",
            min_points=0,
        )

        st.caption("ウマ表（浮き人数別・4着分の順位点）")
        uma_table_input = {}
        for floating_count in (1, 2, 3):
            st.write(f"{floating_count}人浮き")
            default_row = saved_uma_table.get(floating_count, default_uma_table[floating_count])
            cols = st.columns(4)
            row = []
            for i, col in enumerate(cols):
                with col:
                    row.append(
                        points_input(
                            f"{i + 1}着",
                            default_row[i],
                            f"{key_prefix}_uma_{floating_count}_{i}",
                        )
                    )
            uma_table_input[floating_count] = row

        return {
            "start_point": start_point,
            "return_point": return_point,
            "oka": oka,
            "tobi_amount": tobi_amount,
            "uma_table": uma_table_input,
        }

    default_rank_point_table = defaults.get("rank_point_table", scoring_logic.DEFAULT_RANK_POINT_TABLE)
    st.caption("順位ポイント表（1〜4位）")
    cols = st.columns(4)
    rank_point_table_input = []
    for i, col in enumerate(cols):
        with col:
            rank_point_table_input.append(
                st.number_input(
                    f"{i + 1}位",
                    value=default_rank_point_table[i],
                    key=f"{key_prefix}_rank_point_{i}",
                )
            )

    # ゲスト向け画面実装依頼_Stage2.md 4章①対応: ポイント方式の大会にも開始点数だけを
    # 必須入力として持たせる（ウマ表・オカ・飛び賞額は追加しない）。ゲスト画面の成績入力で
    # 「4人分の素点合計＝開始点数×4」チェックに使うためで、点数計算そのものには使わない。
    start_point = points_input(
        "開始点数（持ち点。ゲスト成績入力の合計チェックに使用）",
        defaults.get("start_point", scoring_logic.DEFAULT_UMA_CONFIG["start_point"]),
        f"{key_prefix}_start_point",
    )
    return {"rank_point_table": rank_point_table_input, "start_point": start_point}


def render_guest_view(guest_token: str) -> None:
    """ゲスト共有リンク(`?guest=<トークン>`)専用画面（ゲスト向け画面実装依頼_Stage2.md 3章対応）。

    ログイン・アカウント作成を経由しない。呼び出し元でこの関数のあとに必ずst.stop()すること
    ――主催者向け画面・ログイン画面のコードを一切実行させないことで、この画面から他大会・
    主催者向けデータへ一切アクセスできないことを担保する（3.1対応）。
    """
    tournament = db.get_tournament_by_guest_token(guest_token)
    if tournament is None:
        st.error("このリンクは使えません。主催者に最新のリンクを確認してください。")
        return

    st.subheader(f"🀄 {tournament['name']}")

    tournament_members = db.get_tournament_members(tournament["id"])
    member_id_to_name = {tm["member_id"]: tm["member_name"] for tm in tournament_members}

    rounds = db.get_rounds(tournament["id"])
    if not rounds:
        st.info("まだ回戦がありません。主催者が回戦を実行するまでお待ちください。")
        return

    round_options = {r["round_number"]: r["id"] for r in rounds}
    round_numbers = list(round_options.keys())
    selected_round_number = st.selectbox(
        "回戦", options=round_numbers, index=len(round_numbers) - 1, key="guest_round_select"
    )
    selected_round_id = round_options[selected_round_number]

    seats = db.get_round_seats(selected_round_id)
    absences = db.get_round_absences(selected_round_id)
    existing_scores = db.get_round_results(selected_round_id)
    existing_busters = db.get_round_tobi_busters(selected_round_id)

    by_table: dict[int, list] = {}
    for seat in seats:
        by_table.setdefault(seat["table_number"], []).append(seat)
    for table_seats in by_table.values():
        table_seats.sort(key=lambda s: position_sort_key(s["position"]))

    st.write("**卓組み結果**")
    for table_number, table_seats in sorted(by_table.items()):
        table_member_ids = [s["member_id"] for s in table_seats]
        status = "入力済み" if all(mid in existing_scores for mid in table_member_ids) else "未入力"
        st.write(f"卓{table_number}（{status}）")
        for seat in table_seats:
            st.write(f"　{seat['position']}: {member_id_to_name.get(seat['member_id'], seat['member_id'])}")

    if absences:
        absent_names = "、".join(member_id_to_name.get(mid, str(mid)) for mid in absences)
        st.caption(f"抜け番: {absent_names}さん")

    st.divider()
    st.write("**成績の入力**")
    st.caption(scoring_logic.HUNDREDS_INPUT_HINT)

    start_point = tournament["scoring_config"].get("start_point")
    if start_point is None:
        st.warning("主催者が開始点数を設定するまで入力できません。")
    elif not by_table:
        st.caption("この回戦は卓がありません。")
    else:
        table_numbers = sorted(by_table.keys())
        selected_table_number = st.selectbox("卓を選択", options=table_numbers, key="guest_table_select")
        table_seats = by_table[selected_table_number]
        table_member_ids = [s["member_id"] for s in table_seats]
        already_submitted = all(mid in existing_scores for mid in table_member_ids)

        if already_submitted:
            st.info("この卓はすでに入力済みです。修正は主催者に依頼してください。")
            for mid in table_member_ids:
                st.write(f"{member_id_to_name.get(mid, mid)}: {scoring_logic.format_hundreds(existing_scores[mid])}")
        else:
            raw_scores_input = {}
            for seat in table_seats:
                member_id = seat["member_id"]
                raw_scores_input[member_id] = points_input(
                    f"{member_id_to_name.get(member_id, member_id)}（{seat['position']}）の素点",
                    0,
                    f"guest_score_{selected_round_id}_{member_id}",
                )

            tobi_busters_input = {}
            if tournament["scoring_mode"] == "得点":
                for seat in table_seats:
                    member_id = seat["member_id"]
                    if raw_scores_input[member_id] <= 0:
                        other_member_ids = [m for m in table_member_ids if m != member_id]
                        default_busters = [
                            b for b in existing_busters.get(member_id, []) if b in other_member_ids
                        ]
                        chosen_busters = st.multiselect(
                            f"「{member_id_to_name.get(member_id, member_id)}」を飛ばした人",
                            options=other_member_ids,
                            default=default_busters,
                            format_func=lambda m: member_id_to_name.get(m, m),
                            key=f"guest_busters_{selected_round_id}_{member_id}",
                            placeholder="選択してください",
                        )
                        if chosen_busters:
                            tobi_busters_input[member_id] = chosen_busters

            total = sum(raw_scores_input.values())
            expected_total = start_point * 4
            digits_ok = all(scoring_logic.is_hundreds_input_plausible(v) for v in raw_scores_input.values())
            sum_ok = digits_ok and total == expected_total
            if not digits_ok:
                st.error(scoring_logic.HUNDREDS_OUT_OF_RANGE_MESSAGE)
            elif sum_ok:
                st.success(f"合計 {scoring_logic.format_hundreds(total)} です。")
            else:
                st.error(
                    f"合計が{scoring_logic.format_hundreds(total)}です。"
                    f"{scoring_logic.format_hundreds(expected_total)}になるよう確認してください。"
                )

            confirm = st.checkbox(
                "送信すると修正できません。内容を確認しました。",
                key=f"guest_confirm_{selected_round_id}_{selected_table_number}",
            )
            if st.button(
                "この内容で送信する",
                disabled=not (sum_ok and confirm),
                width="stretch",
                key=f"guest_submit_{selected_round_id}_{selected_table_number}",
            ):
                # ボタンを押せなくするだけでは足りない: 素点を書き換えてすぐ送信ボタンを押すと、
                # 書き換えと押下が同じ再実行で届き(ボタンは直前の画面では押せる状態だった)、
                # ここに合計のずれた値のまま来る。ゲストの送信は修正できないので、送信時にも確かめる。
                if not (sum_ok and confirm):
                    st.error("送信できませんでした。合計と確認のチェックを見直してから、もう一度送信してください。")
                else:
                    try:
                        db.submit_guest_round_results(selected_round_id, raw_scores_input, tobi_busters_input)
                        st.success("送信しました。")
                        st.rerun()
                    except db.ResultsAlreadySubmittedError:
                        st.warning("すでに入力済みです。ページを再読み込みしてください。")

    st.divider()
    st.write("**大会内順位表**")
    if tournament["scoring_mode"] == "得点":
        st.caption("総合得点は100点単位で表示しています（458は45,800点）")
    standings = tournament_service.compute_standings(tournament)
    appearance_counts = tournament_service.build_appearance_counts(tournament["id"])
    if not standings:
        st.caption("まだ成績がありません。")
    else:
        st.table(
            [
                {
                    "順位": i + 1,
                    "氏名": member_id_to_name.get(row["member_id"], row["member_id"]),
                    standings_value_column_label(tournament["scoring_mode"]): format_result_value(
                        tournament["scoring_mode"], row["value"]
                    ),
                    "参加回戦数": appearance_counts.get(row["player_number"], 0),
                }
                for i, row in enumerate(standings)
            ]
        )


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

# ---- ゲスト共有リンク ----
# ログイン・アカウント作成フローより前に判定する。ここで処理してst.stop()することで、
# 以降のログイン画面・主催者向け画面のコードを一切実行させない
# （ゲスト向け画面実装依頼_Stage2.md 3.1「ゲスト画面からは主催者向けの画面・
# 他の大会のデータに一切アクセスできないこと」に対応）。
guest_token = query_params.get("guest")
if guest_token:
    render_guest_view(guest_token)
    st.stop()

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
        login_username_input = st.text_input("ユーザー名", autocomplete="off")
        login_password_input = st.text_input("パスワード", type="password", autocomplete="off")
        login_submitted = st.form_submit_button("ログイン")

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

# ---- 表紙画面（評価方式の選択） ----
# 得点ポイント評価方式実装依頼.md 3.3対応。大会管理・回戦実施画面(Phase 3・4)が
# まだ無いため、選んだ評価方式と入力値の保存先は今回はst.session_stateのみとする
# (大会ごとのDB保存はそれぞれのフェーズの実装時に行う)。
st.divider()
st.subheader("📋 評価方式")

if not st.session_state.get("scoring_mode"):
    st.write("このアプリでの成績評価方式を選んでください。")
    col_score, col_points = st.columns(2)
    with col_score:
        if st.button("①得点で評価", width="stretch"):
            st.session_state["scoring_mode"] = "score"
            st.rerun()
    with col_points:
        if st.button("②ポイントで評価", width="stretch"):
            st.session_state["scoring_mode"] = "points"
            st.rerun()
else:
    scoring_mode = st.session_state["scoring_mode"]
    scoring_mode_label = "①得点で評価" if scoring_mode == "score" else "②ポイントで評価"
    st.write(f"現在の評価方式: **{scoring_mode_label}**")

    saved_scoring_config = st.session_state.get("scoring_config", {})

    if scoring_mode == "score":
        default_uma_table = scoring_logic.DEFAULT_UMA_CONFIG["uma_table"]
        saved_uma_table = saved_scoring_config.get("uma_table", {})
        with st.form("scoring_config_score_form"):
            st.caption(scoring_logic.HUNDREDS_INPUT_HINT)
            start_point = points_input(
                "開始点数（持ち点）",
                saved_scoring_config.get("start_point", scoring_logic.DEFAULT_UMA_CONFIG["start_point"]),
                "scoring_start_point",
            )
            return_point = points_input(
                "返し点（オカの基準点）",
                saved_scoring_config.get("return_point", scoring_logic.DEFAULT_UMA_CONFIG["return_point"]),
                "scoring_return_point",
            )
            oka = points_input(
                "オカ（0人浮き時にトップへ加算する額）",
                saved_scoring_config.get("oka", scoring_logic.DEFAULT_UMA_CONFIG["oka"]),
                "scoring_oka",
            )
            tobi_amount = points_input(
                "飛び賞額（0なら飛び賞なし）",
                saved_scoring_config.get("tobi_amount", 0),
                "scoring_tobi_amount",
                min_points=0,
            )

            st.caption("ウマ表（浮き人数別・4着分の順位点）")
            uma_table_input = {}
            for floating_count in (1, 2, 3):
                st.write(f"{floating_count}人浮き")
                default_row = saved_uma_table.get(floating_count, default_uma_table[floating_count])
                cols = st.columns(4)
                row = []
                for i, col in enumerate(cols):
                    with col:
                        row.append(
                            points_input(
                                f"{i + 1}着",
                                default_row[i],
                                f"uma_{floating_count}_{i}",
                            )
                        )
                uma_table_input[floating_count] = row

            score_submitted = st.form_submit_button("設定を保存", key="scoring_score_submit")
            if score_submitted:
                submitted_config = {
                    "start_point": start_point,
                    "return_point": return_point,
                    "oka": oka,
                    "tobi_amount": tobi_amount,
                    "uma_table": uma_table_input,
                }
                if scoring_logic.scoring_config_points_out_of_range(submitted_config):
                    st.error(scoring_logic.HUNDREDS_OUT_OF_RANGE_MESSAGE)
                else:
                    st.session_state["scoring_config"] = submitted_config
                    st.success("設定を保存しました。")
    else:
        default_rank_point_table = saved_scoring_config.get(
            "rank_point_table", scoring_logic.DEFAULT_RANK_POINT_TABLE
        )
        with st.form("scoring_config_points_form"):
            st.caption("順位ポイント表（1〜4位）")
            cols = st.columns(4)
            rank_point_table_input = []
            for i, col in enumerate(cols):
                with col:
                    rank_point_table_input.append(
                        st.number_input(
                            f"{i + 1}位",
                            value=default_rank_point_table[i],
                            key=f"rank_point_{i}",
                        )
                    )
            points_submitted = st.form_submit_button("設定を保存", key="scoring_points_submit")
            if points_submitted:
                st.session_state["scoring_config"] = {"rank_point_table": rank_point_table_input}
                st.success("設定を保存しました。")

    if st.button("評価方式を選び直す"):
        st.session_state["scoring_mode"] = None
        st.rerun()

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
            st.dataframe(tenant_rows, hide_index=True, width="stretch")
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
            st.dataframe(audit_rows, hide_index=True, width="stretch")

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

# ---- 大会管理・回戦実施・ゲスト共有リンク ----
# 大会管理とゲスト共有リンク実装依頼.md Stage 1対応。ログイン必須(主催者向け)。
# ゲスト自身が使う閲覧・入力画面(トークン付きURLでのアクセス)はStage 2で対応する。
st.divider()
st.header("大会管理")

if not is_admin:
    st.info("大会の作成・編集・削除・回戦実施は管理者のみ行えます。一覧の閲覧はできます。")
else:
    with st.expander("新しい大会を作成"):
        new_name = st.text_input("大会名", key="new_tournament_name")
        new_kind = st.selectbox("種別", db.TOURNAMENT_KINDS, key="new_tournament_kind")
        new_table_method = st.selectbox("卓組み方式", db.TABLE_METHODS, key="new_tournament_table_method")
        new_scoring_mode = st.selectbox("評価方式", db.SCORING_MODES, key="new_tournament_scoring_mode")
        new_numbering_method = st.selectbox(
            "選手番号の採番方式", db.NUMBERING_METHODS, key="new_tournament_numbering_method"
        )
        col_start, col_end = st.columns(2)
        with col_start:
            new_start_date = st.text_input("開始日（任意、例: 2026-10-01）", key="new_tournament_start_date")
        with col_end:
            new_end_date = st.text_input("終了日（任意）", key="new_tournament_end_date")

        with st.form("new_tournament_form"):
            new_scoring_config = render_scoring_config_inputs(new_scoring_mode, "new_tournament")
            create_submitted = st.form_submit_button("大会を作成")
            if create_submitted:
                if not new_name.strip():
                    st.error("大会名を入力してください。")
                elif scoring_logic.scoring_config_points_out_of_range(new_scoring_config):
                    st.error(scoring_logic.HUNDREDS_OUT_OF_RANGE_MESSAGE)
                else:
                    tournament_id = db.create_tournament(
                        tenant_id,
                        new_name.strip(),
                        new_kind,
                        new_table_method,
                        new_scoring_mode,
                        new_scoring_config,
                        new_numbering_method,
                        start_date=new_start_date or None,
                        end_date=new_end_date or None,
                    )
                    db.record_audit_log(
                        action="tournament_create",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        username=current_user["username"],
                        detail=f"tournament_id={tournament_id}, name={new_name}",
                    )
                    st.success(f"「{new_name}」を作成しました。")
                    st.rerun()

st.subheader("大会一覧")

tournaments = db.get_tournaments(tenant_id)
if not tournaments:
    st.info("大会がまだありません。")

for tournament in tournaments:
    tournament_id = tournament["id"]
    with st.expander(f"{tournament['name']}（{tournament['status']}・{tournament['table_method']}・{tournament['scoring_mode']}評価）"):
        st.write(
            f"種別: {tournament['kind']} / 選手番号採番: {tournament['numbering_method']} / "
            f"期間: {tournament['start_date'] or '未設定'}〜{tournament['end_date'] or '未設定'}"
        )

        # ---- 編集・削除 ----
        if is_admin:
            with st.expander("大会情報を編集"):
                edit_prefix = f"edit_tournament_{tournament_id}"
                edit_name = st.text_input("大会名", value=tournament["name"], key=f"{edit_prefix}_name")
                edit_kind = st.selectbox(
                    "種別", db.TOURNAMENT_KINDS,
                    index=db.TOURNAMENT_KINDS.index(tournament["kind"]), key=f"{edit_prefix}_kind",
                )
                edit_table_method = st.selectbox(
                    "卓組み方式", db.TABLE_METHODS,
                    index=db.TABLE_METHODS.index(tournament["table_method"]), key=f"{edit_prefix}_table_method",
                )
                edit_scoring_mode = st.selectbox(
                    "評価方式", db.SCORING_MODES,
                    index=db.SCORING_MODES.index(tournament["scoring_mode"]), key=f"{edit_prefix}_scoring_mode",
                )
                edit_numbering_method = st.selectbox(
                    "選手番号の採番方式", db.NUMBERING_METHODS,
                    index=db.NUMBERING_METHODS.index(tournament["numbering_method"]),
                    key=f"{edit_prefix}_numbering_method",
                )
                edit_status = st.selectbox(
                    "状態", db.TOURNAMENT_STATUSES,
                    index=db.TOURNAMENT_STATUSES.index(tournament["status"]), key=f"{edit_prefix}_status",
                )
                col_edit_start, col_edit_end = st.columns(2)
                with col_edit_start:
                    edit_start_date = st.text_input(
                        "開始日", value=tournament["start_date"] or "", key=f"{edit_prefix}_start_date"
                    )
                with col_edit_end:
                    edit_end_date = st.text_input(
                        "終了日", value=tournament["end_date"] or "", key=f"{edit_prefix}_end_date"
                    )

                with st.form(f"{edit_prefix}_form"):
                    edit_scoring_config = render_scoring_config_inputs(
                        edit_scoring_mode,
                        edit_prefix,
                        defaults=tournament["scoring_config"] if edit_scoring_mode == tournament["scoring_mode"] else None,
                    )
                    edit_submitted = st.form_submit_button("変更を保存")
                    if edit_submitted:
                        if not edit_name.strip():
                            st.error("大会名を入力してください。")
                        elif scoring_logic.scoring_config_points_out_of_range(edit_scoring_config):
                            st.error(scoring_logic.HUNDREDS_OUT_OF_RANGE_MESSAGE)
                        else:
                            db.update_tournament(
                                tenant_id, tournament_id, edit_name.strip(), edit_kind,
                                edit_table_method, edit_scoring_mode, edit_scoring_config,
                                edit_numbering_method, edit_start_date or None, edit_end_date or None,
                                edit_status,
                            )
                            st.success("大会情報を更新しました。")
                            st.rerun()

            delete_confirm_key = f"delete_tournament_confirm_{tournament_id}"
            if st.button("この大会を削除", key=f"delete_tournament_btn_{tournament_id}"):
                st.session_state[delete_confirm_key] = True
            if st.session_state.get(delete_confirm_key):
                st.warning(
                    f"「{tournament['name']}」を削除します。回戦・成績・ゲストリンクもすべて削除され、元に戻せません。よろしいですか？"
                )
                confirm_delete_cols = st.columns(2)
                if confirm_delete_cols[0].button("はい、削除する", key=f"confirm_delete_tournament_{tournament_id}"):
                    db.delete_tournament(tenant_id, tournament_id)
                    db.record_audit_log(
                        action="tournament_delete",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        username=current_user["username"],
                        detail=f"tournament_id={tournament_id}, name={tournament['name']}",
                    )
                    st.session_state[delete_confirm_key] = False
                    st.success("削除しました。")
                    st.rerun()
                if confirm_delete_cols[1].button("キャンセル", key=f"cancel_delete_tournament_{tournament_id}"):
                    st.session_state[delete_confirm_key] = False
                    st.rerun()

        st.divider()

        # ---- 参加メンバー ----
        st.write("**参加メンバー**")
        tournament_members = db.get_tournament_members(tournament_id)
        if tournament_members:
            st.table(
                [
                    {"選手番号": tm["player_number"], "氏名": tm["member_name"]}
                    for tm in tournament_members
                ]
            )
        else:
            st.caption("参加メンバーがまだいません。")

        if is_admin:
            registered_member_ids = {tm["member_id"] for tm in tournament_members}
            candidate_members = [m for m in db.get_members(tenant_id, order="name") if m["id"] not in registered_member_ids]
            if candidate_members:
                with st.expander("参加メンバーを追加"):
                    selected_member_ids = []
                    for member in candidate_members:
                        checked = st.checkbox(
                            member["name"], key=f"add_participant_{tournament_id}_{member['id']}"
                        )
                        if checked:
                            selected_member_ids.append(member["id"])

                    manual_numbers: dict[int, int] = {}
                    if tournament["numbering_method"] == "くじ引き":
                        for member_id in selected_member_ids:
                            member_name = next(m["name"] for m in candidate_members if m["id"] == member_id)
                            manual_numbers[member_id] = st.number_input(
                                f"「{member_name}」の選手番号（くじ引き）",
                                min_value=1,
                                step=1,
                                key=f"player_number_{tournament_id}_{member_id}",
                            )

                    if selected_member_ids and st.button(
                        "選択したメンバーを追加", key=f"confirm_add_participants_{tournament_id}"
                    ):
                        errors = []
                        for member_id in selected_member_ids:
                            try:
                                if tournament["numbering_method"] == "受付順":
                                    player_number = db.next_tournament_player_number(tournament_id)
                                else:
                                    player_number = int(manual_numbers[member_id])
                                db.add_tournament_member(tournament_id, member_id, player_number)
                            except db.PlayerNumberTakenError:
                                errors.append(f"選手番号 {manual_numbers.get(member_id)} は既に使われています。")
                            except db.MemberAlreadyRegisteredError:
                                errors.append("既に参加登録済みのメンバーです。")
                        if errors:
                            for err in errors:
                                st.error(err)
                        else:
                            st.success("参加メンバーを追加しました。")
                        st.rerun()

        st.divider()

        # ---- 回戦実行 ----
        st.write("**回戦実行**")
        preview_key = f"round_preview_{tournament_id}"

        if is_admin:
            if len(tournament_members) < 4:
                st.caption("卓組みを実行するには、参加メンバーが4人以上必要です。")
            else:
                col_run, col_reroll = st.columns(2)
                if col_run.button("次の回戦の卓組みを実行", key=f"run_round_{tournament_id}"):
                    round_number, absent, tables = tournament_service.run_next_round(tournament)
                    st.session_state[preview_key] = {
                        "round_number": round_number,
                        "absent": absent,
                        "tables": tables,
                    }
                    st.rerun()
                if st.session_state.get(preview_key) and col_reroll.button(
                    "作り直す（乱数を引き直す）", key=f"reroll_round_{tournament_id}"
                ):
                    round_number, absent, tables = tournament_service.run_next_round(tournament)
                    st.session_state[preview_key] = {
                        "round_number": round_number,
                        "absent": absent,
                        "tables": tables,
                    }
                    st.rerun()

        preview = st.session_state.get(preview_key)
        if preview:
            player_number_to_name = {
                tm["player_number"]: tm["member_name"] for tm in tournament_members
            }
            st.write(f"第{preview['round_number']}回戦のプレビュー（未保存）")
            for table_number, table in enumerate(preview["tables"], start=1):
                seat_line = "　".join(
                    f"{position}: {player_number_to_name.get(player_number, player_number)}"
                    for position, player_number in sorted(table.items(), key=lambda kv: position_sort_key(kv[0]))
                )
                st.write(f"卓{table_number}: {seat_line}")
            if preview["absent"]:
                absent_names = "、".join(
                    player_number_to_name.get(pn, str(pn)) for pn in preview["absent"]
                )
                st.write(f"抜け番: {absent_names}")
            else:
                st.write("抜け番: なし")

            if is_admin and st.button("この結果で保存", key=f"save_round_{tournament_id}"):
                tournament_service.save_confirmed_round(
                    tournament_id, preview["round_number"], preview["absent"], preview["tables"]
                )
                db.record_audit_log(
                    action="round_save",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    username=current_user["username"],
                    detail=f"tournament_id={tournament_id}, round_number={preview['round_number']}",
                )
                st.session_state.pop(preview_key, None)
                st.success("回戦結果を保存しました。")
                st.rerun()

        st.divider()

        # ---- 成績入力 ----
        st.write("**成績入力**")
        rounds = db.get_rounds(tournament_id)
        if not rounds:
            st.caption("まだ回戦がありません。")
        else:
            round_options = {r["round_number"]: r["id"] for r in rounds}
            selected_round_number = st.selectbox(
                "回戦を選択", options=list(round_options.keys()), key=f"score_round_select_{tournament_id}"
            )
            selected_round_id = round_options[selected_round_number]
            seats = db.get_round_seats(selected_round_id)
            existing_scores = db.get_round_results(selected_round_id)
            existing_busters = db.get_round_tobi_busters(selected_round_id)
            member_id_to_name = {tm["member_id"]: tm["member_name"] for tm in tournament_members}

            by_table: dict[int, list] = {}
            for seat in seats:
                by_table.setdefault(seat["table_number"], []).append(seat)
            for table_seats in by_table.values():
                table_seats.sort(key=lambda s: position_sort_key(s["position"]))

            start_point = tournament["scoring_config"].get("start_point")
            if start_point is None:
                st.warning(
                    "開始点数が未設定のため、成績を保存できません。大会設定で開始点数を設定してください。"
                )
            expected_total = start_point * 4 if start_point is not None else None
            st.caption(scoring_logic.HUNDREDS_INPUT_HINT)

            raw_scores_input = {}
            tobi_busters_input = {}
            # このフォームは回戦内の全卓をまとめて表示するため、まだ誰も入力していない
            # (＝ゲストもまだ入力していない)卓の入力欄は初期値の0のままになっている。
            # フィルタせずに保存すると、その卓の全員分が0点として保存されてしまい、
            # (1)実際には未入力の卓に虚偽の0点記録が残る、
            # (2)resultsのUNIQUE(round_id, member_id)制約により、後でゲストが
            #    submit_guest_round_resultsで本来の素点を送信しようとしても
            #    「すでに入力済み」として拒否されてしまう、という問題が起きる。
            # そのため、既存レコードがある(＝既に入力済み)卓か、今回いずれかの選手の
            # 値が0以外に変更された(＝この保存で実際に入力しようとしている)卓だけを
            # 保存対象(save_tables)にし、合計チェックもその卓にだけかける。
            save_tables: dict[int, list[int]] = {}
            mismatched_tables: list[int] = []
            implausible_tables: list[int] = []
            for table_number, table_seats in sorted(by_table.items()):
                st.write(f"卓{table_number}")
                score_cols = st.columns(4)
                table_member_ids = [s["member_id"] for s in table_seats]
                for col, seat in zip(score_cols, table_seats):
                    with col:
                        member_id = seat["member_id"]
                        score = points_input(
                            f"{member_id_to_name.get(member_id, member_id)}（{seat['position']}）",
                            existing_scores.get(member_id, 0),
                            f"score_{selected_round_id}_{member_id}",
                        )
                        raw_scores_input[member_id] = score
                for seat in table_seats:
                    member_id = seat["member_id"]
                    if raw_scores_input[member_id] <= 0:
                        other_member_ids = [m for m in table_member_ids if m != member_id]
                        default_busters = [b for b in existing_busters.get(member_id, []) if b in other_member_ids]
                        chosen_busters = st.multiselect(
                            f"「{member_id_to_name.get(member_id, member_id)}」を飛ばした人",
                            options=other_member_ids,
                            default=default_busters,
                            format_func=lambda m: member_id_to_name.get(m, m),
                            key=f"busters_{selected_round_id}_{member_id}",
                            placeholder="選択してください",
                        )
                        if chosen_busters:
                            tobi_busters_input[member_id] = chosen_busters

                already_entered = any(mid in existing_scores for mid in table_member_ids)
                touched = any(raw_scores_input[mid] != 0 for mid in table_member_ids)
                if already_entered or touched:
                    save_tables[table_number] = table_member_ids
                    if not all(
                        scoring_logic.is_hundreds_input_plausible(raw_scores_input[mid]) for mid in table_member_ids
                    ):
                        # 桁がおかしい入力(45,800点を45800と入力した等)は、合計のずれではなく
                        # 単位の誤りとして知らせる
                        implausible_tables.append(table_number)
                        st.error(f"卓{table_number}: {scoring_logic.HUNDREDS_OUT_OF_RANGE_MESSAGE}")
                    elif expected_total is not None:
                        table_total = sum(raw_scores_input[mid] for mid in table_member_ids)
                        if table_total == expected_total:
                            st.caption(f"卓{table_number}の合計 {scoring_logic.format_hundreds(table_total)}")
                        else:
                            mismatched_tables.append(table_number)
                            st.error(
                                f"卓{table_number}の合計が{scoring_logic.format_hundreds(table_total)}です。"
                                f"{scoring_logic.format_hundreds(expected_total)}になるよう確認してください。"
                            )

            if is_admin and st.button("成績を保存", key=f"save_results_{selected_round_id}"):
                # 依頼書3.5: 主催者の保存(修正を含む)にも、ゲスト入力と同じ「卓ごとの素点合計＝
                # 開始点数×4」のチェックをかける。ずれている卓は上で赤字表示済みなので、ここでは
                # 保存を止めるだけにする。
                if expected_total is None:
                    st.error("開始点数が未設定のため保存できません。")
                elif implausible_tables:
                    table_names = "、".join(f"卓{n}" for n in implausible_tables)
                    st.error(f"点数の桁がおかしい卓があるため保存できません（{table_names}）。")
                elif mismatched_tables:
                    table_names = "、".join(f"卓{n}" for n in mismatched_tables)
                    st.error(
                        f"合計が{scoring_logic.format_hundreds(expected_total)}になっていない卓があるため"
                        f"保存できません（{table_names}）。"
                    )
                else:
                    scores_to_save = {}
                    busters_to_save = {}
                    for table_member_ids in save_tables.values():
                        for mid in table_member_ids:
                            scores_to_save[mid] = raw_scores_input[mid]
                            if mid in tobi_busters_input:
                                busters_to_save[mid] = tobi_busters_input[mid]
                    db.save_round_results(selected_round_id, scores_to_save, busters_to_save)
                    st.success("成績を保存しました。")
                    st.rerun()

            if existing_scores:
                totals = tournament_service.compute_round_totals(tournament, selected_round_id)
                st.caption(
                    "この回戦の計算結果（"
                    + ("総合得点・100点単位" if tournament["scoring_mode"] == "得点" else "順位ポイント")
                    + "）"
                )
                st.table(
                    [
                        {"氏名": member_id_to_name.get(mid, mid), "値": format_result_value(tournament["scoring_mode"], value)}
                        for mid, value in sorted(totals.items(), key=lambda kv: -kv[1])
                    ]
                )

        # ---- 大会内順位表 ----
        if rounds:
            st.write("**大会内順位表**")
            if tournament["scoring_mode"] == "得点":
                st.caption("総合得点は100点単位で表示しています（458は45,800点）")
            standings = tournament_service.compute_standings(tournament)
            member_id_to_name_all = {tm["member_id"]: tm["member_name"] for tm in tournament_members}
            st.table(
                [
                    {
                        "順位": i + 1,
                        "選手番号": row["player_number"],
                        "氏名": member_id_to_name_all.get(row["member_id"], row["member_id"]),
                        standings_value_column_label(tournament["scoring_mode"]): format_result_value(
                            tournament["scoring_mode"], row["value"]
                        ),
                    }
                    for i, row in enumerate(standings)
                ]
            )

        st.divider()

        # ---- ゲスト共有リンク ----
        st.write("**ゲスト共有リンク**")
        st.caption(
            "リンクを知っている人は、アカウント作成なしでこの大会の卓組み結果・成績を閲覧・入力できます。"
        )
        active_link = db.get_active_guest_link(tournament_id)
        guest_token_key = f"guest_link_raw_{tournament_id}"

        if is_admin:
            if active_link is None:
                if st.button("ゲスト用リンクを発行", key=f"issue_guest_link_{tournament_id}"):
                    raw_token = db.create_guest_link(tournament_id, user_id)
                    st.session_state[guest_token_key] = raw_token
                    st.rerun()
            else:
                st.success("ゲスト共有リンクは発行済みです。")
                raw_token = st.session_state.get(guest_token_key)
                if raw_token:
                    st.code(f"{APP_BASE_URL}/?guest={raw_token}")
                else:
                    st.caption(
                        "このブラウザセッションで発行したリンクではないため、文字列は再表示できません"
                        "（生トークンはDBに保存していません）。共有し忘れた場合は下から再発行してください。"
                    )
                link_action_cols = st.columns(2)
                if link_action_cols[0].button("リンクを再発行する（旧リンクは失効）", key=f"reissue_guest_link_{tournament_id}"):
                    db.revoke_guest_link(active_link["id"])
                    raw_token = db.create_guest_link(tournament_id, user_id)
                    st.session_state[guest_token_key] = raw_token
                    st.rerun()
                if link_action_cols[1].button("リンクを無効化する", key=f"revoke_guest_link_{tournament_id}"):
                    db.revoke_guest_link(active_link["id"])
                    st.session_state.pop(guest_token_key, None)
                    st.success("ゲスト共有リンクを無効化しました。")
                    st.rerun()
