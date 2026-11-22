"""自動ログイン用 Cookie の読み・書き・削除。

streamlit-authenticator の Cookie 部品（extra-streamlit-components の CookieManager）は使わない。
この部品はブラウザの Cookie が変わると値を返して画面の再実行を要求するため、押したボタンの処理が
途中で打ち切られていた（保存したのに画面に出ない、押したことが無かったことになる。2026-11-22）。
ここでは値を返さない JavaScript で書くだけなので、再実行は起きない。

Cookie の名前・中身（JWT）・属性は streamlit-authenticator と同じ（発行済みの Cookie がそのまま使える）。
"""
import json
import time

import jwt
import streamlit as st

EXPIRY_DAYS = 30
# ログイン・ログアウト直後の作り直しで書き込みが届かなくても F5 で戻らないよう、
# しばらくは作り直すたびに同じ中身の書き込みを置く（2026-11-20 の不具合への対策）
REWRITE_SECONDS = 15
_PENDING_KEY = "auth_cookie_pending"


def read_username(name: str, key: str) -> str | None:
    """有効な Cookie のユーザー名。このセッションでログアウトした後は読まない。"""
    # st.context.cookies は接続したときの値のままなので、削除した Cookie もまだ入っている
    if st.session_state.get("logout"):
        return None
    token = st.context.cookies.get(name)
    if not token:
        return None
    try:
        data = jwt.decode(token, key, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if data.get("exp_date", 0) <= time.time():
        return None
    return data.get("username")


def request_write(name: str, key: str, username: str) -> None:
    expires = time.time() + EXPIRY_DAYS * 24 * 60 * 60
    token = jwt.encode({"username": username, "exp_date": expires}, key, algorithm="HS256")
    _request(f"{name}={token}", expires)


def request_delete(name: str) -> None:
    _request(f"{name}=", 0)


def _request(pair: str, expires: float) -> None:
    st.session_state[_PENDING_KEY] = {
        "cookie": pair,
        "expires_ms": int(expires * 1000),
        "until": time.time() + REWRITE_SECONDS,
    }


def render() -> None:
    """画面の作り直しごとに、同じ位置で1回呼ぶ。

    書くものが無い回も空の要素を置く。要素の数が変わると、下にある部品の位置がずれて作り直される。
    """
    pending = st.session_state.get(_PENDING_KEY)
    if not pending or pending["until"] <= time.time():
        st.empty()
        return
    cookie = json.dumps(pending["cookie"]).replace("<", "\\u003c")  # </script> で途切れないように
    st.html(
        '<style>[data-testid="stElementContainer"]:has(.auth-cookie-writer){display:none}</style>'
        '<span class="auth-cookie-writer"></span>'
        f'<script>document.cookie = {cookie} + "; path=/; samesite=strict; expires="'
        f' + new Date({pending["expires_ms"]}).toUTCString();</script>',
        unsafe_allow_javascript=True,
    )
