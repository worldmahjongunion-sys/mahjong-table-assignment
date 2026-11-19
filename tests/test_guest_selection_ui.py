"""ゲスト画面の「回戦」「卓を選択」の初期値（2026-11-19、staging リハーサルから）。

- 回戦: 未入力の卓がある最新の回戦（全卓入力済みなら最新の回戦）。開いたままでも、
  新しい回戦ができたら切り替わる。自分で選んだ回戦はそのまま
- 卓: 最初の未入力の卓。送信した直後は、次の未入力の卓へ切り替わる
"""
from streamlit.testing.v1 import AppTest

import db
import tournament_service as svc
from tests.test_tournament_ui import APP_PATH, POINT_CONFIG, _add_user, _find_button, app_env  # noqa: F401


def _setup(n):
    uid = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(uid)["tenant_id"]
    member_ids = [db.add_member(tenant_id, uid, f"M{i}", "") for i in range(n)]
    tid = db.create_tournament(tenant_id, "テスト大会", "ワンデー", "蛇行", "ポイント", POINT_CONFIG, "受付順")
    for i, mid in enumerate(member_ids, start=1):
        db.add_tournament_member(tid, mid, i)
    return uid, tenant_id, tid


def _new_round(tenant_id, tid):
    tournament = db.get_tournament(tenant_id, tid)
    rn, absent, tables = svc.run_next_round(tournament)
    return svc.save_confirmed_round(tid, rn, absent, tables)


def _enter_table(round_id, table_number):
    seats = [s for s in db.get_round_seats(round_id) if s["table_number"] == table_number]
    db.submit_guest_round_results(round_id, {s["member_id"]: 35000 for s in seats}, {})


def _enter_all(round_id):
    for tn in {s["table_number"] for s in db.get_round_seats(round_id)}:
        _enter_table(round_id, tn)


def _open_guest(uid, tid):
    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = db.create_guest_link(tid, uid)
    at.run()
    assert not at.exception
    return at


def _round_value(at):
    return at.selectbox(key="guest_round_select").value


def _table_select(at, round_id):
    return at.selectbox(key=f"guest_table_select_{round_id}")


def _score_inputs(at):
    return [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]


def test_table_defaults_to_first_unentered_table(app_env):
    uid, tenant_id, tid = _setup(12)
    rid = _new_round(tenant_id, tid)
    _enter_table(rid, 1)
    at = _open_guest(uid, tid)
    assert _table_select(at, rid).value == 2
    assert len(_score_inputs(at)) == 4  # 入力済みの卓1ではなく、入力欄が出る


def test_table_switches_to_next_unentered_after_submit(app_env):
    uid, tenant_id, tid = _setup(12)
    rid = _new_round(tenant_id, tid)
    _enter_table(rid, 1)
    at = _open_guest(uid, tid)
    assert _table_select(at, rid).value == 2
    for ni in _score_inputs(at):
        ni.set_value(350)
    at.checkbox[0].check()
    at.run()
    at.button[_find_button(at, "この内容で送信する")].click().run()
    assert not at.exception
    assert len(db.get_round_results(rid)) == 8  # 卓1・卓2の8人分
    assert _table_select(at, rid).value == 3
    assert len(_score_inputs(at)) == 4


def test_table_after_submit_wraps_to_earlier_unentered_table(app_env):
    uid, tenant_id, tid = _setup(12)
    rid = _new_round(tenant_id, tid)
    _enter_table(rid, 2)
    at = _open_guest(uid, tid)
    assert _table_select(at, rid).value == 1
    _table_select(at, rid).set_value(3).run()
    for ni in _score_inputs(at):
        ni.set_value(350)
    at.checkbox[0].check()
    at.run()
    at.button[_find_button(at, "この内容で送信する")].click().run()
    assert not at.exception
    assert _table_select(at, rid).value == 1  # 卓3の後ろに未入力がないので、前に戻って卓1


def test_round_defaults_to_latest_round(app_env):
    uid, tenant_id, tid = _setup(8)
    r1 = _new_round(tenant_id, tid)
    _enter_all(r1)
    _new_round(tenant_id, tid)
    at = _open_guest(uid, tid)
    assert _round_value(at) == 2


def test_round_follows_new_round_while_page_stays_open(app_env):
    uid, tenant_id, tid = _setup(8)
    r1 = _new_round(tenant_id, tid)
    at = _open_guest(uid, tid)
    assert _round_value(at) == 1
    _enter_all(r1)
    r2 = _new_round(tenant_id, tid)
    at.run()
    assert _round_value(at) == 2  # 直す前は第1回戦のままだった
    assert _table_select(at, r2).value == 1


def test_round_chosen_by_user_is_kept(app_env):
    uid, tenant_id, tid = _setup(8)
    r1 = _new_round(tenant_id, tid)
    _enter_all(r1)
    _new_round(tenant_id, tid)
    at = _open_guest(uid, tid)
    assert _round_value(at) == 2
    at.selectbox(key="guest_round_select").set_value(1).run()
    _new_round(tenant_id, tid)  # 新しい回戦ができても、自分で選んだ第1回戦のまま
    at.run()
    assert _round_value(at) == 1


def test_round_prefers_latest_round_with_unentered_table(app_env):
    uid, tenant_id, tid = _setup(8)
    r1 = _new_round(tenant_id, tid)
    _enter_table(r1, 1)  # 第1回戦の卓2が未入力のまま
    r2 = _new_round(tenant_id, tid)
    _enter_all(r2)
    at = _open_guest(uid, tid)
    assert _round_value(at) == 1


def test_round_all_entered_shows_latest_and_first_table(app_env):
    uid, tenant_id, tid = _setup(8)
    r1 = _new_round(tenant_id, tid)
    _enter_all(r1)
    r2 = _new_round(tenant_id, tid)
    _enter_all(r2)
    at = _open_guest(uid, tid)
    assert _round_value(at) == 2
    assert _table_select(at, r2).value == 1
