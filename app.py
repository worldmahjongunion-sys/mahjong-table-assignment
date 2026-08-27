import os

import streamlit as st
import streamlit_authenticator as stauth

import db

st.set_page_config(page_title="麻雀卓組みアプリ", page_icon="🀄")

db.init_db()


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


def build_credentials() -> dict:
    users = db.get_all_users()
    return {
        "usernames": {
            u["username"]: {"name": u["username"], "password": u["password_hash"]}
            for u in users
        }
    }


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

st.title("麻雀卓組みアプリ")
authenticator.login(location="main")

auth_status = st.session_state.get("authentication_status")

if auth_status is False:
    st.error("ユーザー名またはパスワードが違います。")

if not auth_status:
    st.divider()
    with st.expander("新規登録（主催者アカウント作成）"):
        with st.form("signup_form", clear_on_submit=True):
            new_username = st.text_input("ユーザー名（英数字とアンダースコア、3〜30文字）")
            new_password = st.text_input("パスワード（8文字以上）", type="password")
            new_password_confirm = st.text_input("パスワード（確認）", type="password")
            invite_code = st.text_input("招待コード（合言葉）", type="password")
            signup_submitted = st.form_submit_button("登録")

        if signup_submitted:
            new_username = new_username.strip().lower()
            if invite_code != auth_invite_code:
                st.error("招待コードが違います。")
            elif not db.USERNAME_RE.match(new_username):
                st.error("ユーザー名は英数字とアンダースコアで3〜30文字にしてください。")
            elif len(new_password) < db.MIN_PASSWORD_LEN:
                st.error(f"パスワードは{db.MIN_PASSWORD_LEN}文字以上にしてください。")
            elif new_password != new_password_confirm:
                st.error("パスワードが一致しません。")
            elif db.get_user_by_username(new_username):
                st.error("そのユーザー名は既に使われています。")
            else:
                try:
                    password_hash = stauth.Hasher.hash(new_password)
                    db.add_user(new_username, password_hash)
                    st.success("登録しました。上のフォームからログインしてください。")
                except db.UsernameTakenError:
                    st.error("そのユーザー名は既に使われています。")
    st.stop()

# ---- ここから先はログイン済みユーザーのみ ----

current_user = db.get_user_by_username(st.session_state["username"])
if current_user is None:
    st.error("ユーザー情報の取得に失敗しました。再度ログインしてください。")
    st.stop()
user_id = current_user["id"]
tenant_id = current_user["tenant_id"]

with st.sidebar:
    st.write(f"ログイン中: {st.session_state['name']}")
    authenticator.logout("ログアウト", location="sidebar")

st.header("メンバー登録")

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
            member_id = db.add_member(tenant_id, user_id, name, memo)
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

            if st.session_state.get(edit_key):
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

            if st.session_state.get(delete_key):
                st.warning(f"「{member['name']}」を削除（引退扱い）します。よろしいですか？過去の大会記録は保持されます。")
                confirm_cols = st.columns(2)
                if confirm_cols[0].button("はい、削除する", key=f"confirm_delete_{member['id']}"):
                    db.retire_member(tenant_id, member["id"])
                    st.session_state[delete_key] = False
                    st.success("削除しました（引退扱い）。")
                    st.rerun()
                if confirm_cols[1].button("キャンセル", key=f"cancel_delete_{member['id']}"):
                    st.session_state[delete_key] = False
                    st.rerun()
