"""自動ログイン用 Cookie（auth_cookie.py）。

- 値を返す部品を置かない（再実行の要求で、押したボタンの処理が打ち切られた。2026-11-22）
- F5 でログアウトしない（ログイン直後は作り直すたびに書き込みを置く。2026-11-20）

実際の打ち切りはブラウザが無いと起きないので、ここでは「打ち切りの元になる部品が無いこと」を確かめる。
"""
import time
from pathlib import Path

import jwt
import pytest
import streamlit as st
import streamlit_authenticator as stauth
from streamlit.testing.v1 import AppTest

import auth_cookie
import db

APP_PATH = str(Path(__file__).parent.parent / "app.py")
NAME = "test_cookie"
KEY = "test-cookie-key-at-least-32-bytes-long!!"


@pytest.fixture
def browser_cookies(tmp_path, monkeypatch):
    """接続したときにブラウザが送ってきた Cookie。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setenv("AUTH_COOKIE_NAME", NAME)
    monkeypatch.setenv("AUTH_COOKIE_KEY", KEY)
    monkeypatch.setenv("AUTH_INVITE_CODE", "correct-horse-battery-staple")
    cookies = {}
    monkeypatch.setattr(type(st.context), "cookies", property(lambda self: cookies))
    db.add_user("admin1", stauth.Hasher.hash("adminpass123"), email="a@example.com", email_verified=True)
    return cookies


def _token(username="admin1", key=KEY, lifetime=3600, algorithm="HS256"):
    data = {"username": username, "exp_date": time.time() + lifetime}
    return jwt.encode(data, key, algorithm=algorithm)


def _open():
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    return at


def _login(at):
    at.text_input[0].set_value("admin1")
    at.text_input[1].set_value("adminpass123")
    next(b for b in at.button if b.label == "ログイン").click().run()
    assert not at.exception


def _logout(at):
    at.button(key="Logout").click().run()
    assert not at.exception


def _logged_in(at):
    return any("ログイン中" in m.value for m in at.markdown)


def _scripts(at):
    return [h.proto.body for h in at.get("html") if "document.cookie" in h.proto.body]


def _written_token(body):
    return body.split(f'"{NAME}=', 1)[1].split('"', 1)[0]


def test_no_value_returning_component_on_any_auth_screen(browser_cookies):
    at = _open()
    assert not at.get("component_instance")  # ログイン画面
    _login(at)
    assert not at.get("component_instance")  # ログイン直後
    at.run()
    assert not at.get("component_instance")  # その後の操作
    _logout(at)
    assert not at.get("component_instance")  # ログアウト直後


def test_no_component_after_auto_login(browser_cookies):
    browser_cookies[NAME] = _token()
    at = _open()
    assert _logged_in(at)
    assert not at.get("component_instance")
    assert not _scripts(at)  # 自動ログインでは書き直さない


def test_login_places_same_cookie_writer_on_reruns_for_a_while(browser_cookies):
    at = _open()
    _login(at)
    first = _scripts(at)
    assert len(first) == 1
    data = jwt.decode(_written_token(first[0]), KEY, algorithms=["HS256"])
    assert data["username"] == "admin1"
    assert data["exp_date"] == pytest.approx(time.time() + 30 * 24 * 60 * 60, abs=60)
    assert "path=/; samesite=strict; expires=" in first[0]
    at.run()
    assert _scripts(at) == first  # 置き直しても中身は同じ（Cookie の値が動かない）


def test_cookie_writer_stops_after_the_window(browser_cookies):
    at = _open()
    _login(at)
    pending = at.session_state["auth_cookie_pending"]
    at.session_state["auth_cookie_pending"] = {**pending, "until": 0}
    at.run()
    assert not at.exception
    assert not _scripts(at)
    assert _logged_in(at)


def test_written_cookie_logs_in_a_new_session(browser_cookies):
    at = _open()
    _login(at)
    browser_cookies[NAME] = _written_token(_scripts(at)[0])
    assert _logged_in(_open())  # F5


def test_cookie_issued_before_this_change_still_works(browser_cookies):
    from streamlit_authenticator.models.cookie_model import CookieModel

    old = CookieModel.__new__(CookieModel)  # __init__ は部品を置くので通さない
    old.cookie_key, old.exp_date = KEY, time.time() + 3600
    st.session_state["username"] = "admin1"
    try:
        browser_cookies[NAME] = old._token_encode()
    finally:
        del st.session_state["username"]
    assert _logged_in(_open())


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        _token(key="another-key-at-least-32-bytes-long!!!!!"),
        _token(lifetime=-1),
        _token(key=None, algorithm="none"),
        jwt.encode({"username": "admin1"}, KEY, algorithm="HS256"),
    ],
    ids=["empty", "broken", "wrong-key", "expired", "unsigned", "no-expiry"],
)
def test_invalid_cookie_does_not_log_in(browser_cookies, token):
    browser_cookies[NAME] = token
    at = _open()
    assert not at.exception
    assert not _logged_in(at)


def test_cookie_of_missing_user_is_deleted_with_warning(browser_cookies):
    browser_cookies[NAME] = _token(username="deleted_user")
    at = _open()
    assert not at.exception
    assert not _logged_in(at)
    assert any("有効期限が切れました" in w.value for w in at.warning)
    assert f'"{NAME}="' in _scripts(at)[0]
    assert "new Date(0)" in _scripts(at)[0]


def test_logout_deletes_cookie_in_the_same_run_and_stays_out(browser_cookies):
    browser_cookies[NAME] = _token()
    at = _open()
    _logout(at)
    assert any(b.label == "ログイン" for b in at.button)
    script = _scripts(at)
    assert len(script) == 1
    assert f'"{NAME}="' in script[0] and "new Date(0)" in script[0]
    at.run()  # 接続時の Cookie は残って見えるが、ログインし直さない
    assert not _logged_in(at)


def test_login_again_after_logout_replaces_delete_with_write(browser_cookies):
    at = _open()
    _login(at)
    _logout(at)
    at.run()
    _login(at)
    assert _logged_in(at)
    script = _scripts(at)
    assert len(script) == 1
    assert jwt.decode(_written_token(script[0]), KEY, algorithms=["HS256"])["username"] == "admin1"


def test_cookie_value_cannot_close_the_script_tag(monkeypatch):
    bodies = []
    pending = {"cookie": "x=</script><b>", "expires_ms": 0, "until": time.time() + 5}
    monkeypatch.setattr(st, "session_state", {"auth_cookie_pending": pending})
    monkeypatch.setattr(st, "html", lambda body, **kwargs: bodies.append(body))
    auth_cookie.render()
    assert bodies[0].count("</script>") == 1
