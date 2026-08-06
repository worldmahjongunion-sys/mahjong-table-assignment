"""
既存データ（ログイン機能導入前に登録されたメンバー）を、
最初の主催者アカウントに紐付けるための一回限りの移行スクリプト。

使い方:
    python3 migrate_to_auth.py
"""

import getpass
import sys

import streamlit_authenticator as stauth

import db


def main() -> None:
    db.init_db()

    with db.get_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM members WHERE user_id IS NULL")
        orphan_count = cur.fetchone()[0]

    if orphan_count == 0:
        print("未紐付けのメンバーはありません。移行の必要はなさそうです。")
        if not _ask_yes_no("それでも新しい主催者アカウントを作成しますか？"):
            return

    print(f"未紐付けのメンバーが {orphan_count} 件見つかりました。")
    print("最初の主催者アカウントを作成します。")

    while True:
        username = input("ユーザー名（英数字とアンダースコア、3〜30文字）: ").strip().lower()
        if db.USERNAME_RE.match(username):
            break
        print("ユーザー名は英数字とアンダースコアで3〜30文字にしてください。")

    if db.get_user_by_username(username):
        print(f"エラー: ユーザー名「{username}」は既に登録されています。中止します。")
        sys.exit(1)

    while True:
        password = getpass.getpass(f"パスワード（{db.MIN_PASSWORD_LEN}文字以上）: ")
        if len(password) < db.MIN_PASSWORD_LEN:
            print("パスワードは8文字以上にしてください。")
            continue
        password_confirm = getpass.getpass("パスワード（確認）: ")
        if password != password_confirm:
            print("パスワードが一致しません。")
            continue
        break

    password_hash = stauth.Hasher.hash(password)
    user_id = db.add_user(username, password_hash)
    print(f"ユーザー「{username}」を作成しました（ID: {user_id}）。")

    if orphan_count > 0:
        affected = db.assign_orphaned_members(user_id)
        print(f"{affected} 件のメンバーをこのアカウントに紐付けました。")

    print("完了しました。このスクリプトは再実行しても、既に紐付け済みのメンバーには影響しません。")


def _ask_yes_no(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


if __name__ == "__main__":
    main()
