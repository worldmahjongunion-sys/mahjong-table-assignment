"""大会モニター画面（?monitor=<トークン>、TV表示用・閲覧専用）。"""
import re

import pytest
from streamlit.testing.v1 import AppTest

import db
import monitor_view
import tournament_service as svc
from tests.test_tournament_ui import (  # noqa: F401
    APP_PATH, POINT_CONFIG, SCORE_CONFIG, _add_user, _find_button, _login, app_env,
)

NAMES = [f"選手{c}" for c in "ABCDEFGHIJKL"]
OVERALL_CONFIG = {**POINT_CONFIG, "rank_scope": svc.RANK_SCOPE_OVERALL}


def _setup(n=8, mode="ポイント", config=None, names=None, title="冬の大会"):
    uid = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(uid)["tenant_id"]
    config = config or (SCORE_CONFIG if mode == "得点" else POINT_CONFIG)
    tid = db.create_tournament(tenant_id, title, "ワンデー", "蛇行", mode, config, "受付順")
    for i, name in enumerate((names or NAMES)[:n], start=1):
        db.add_tournament_member(tid, db.add_member(tenant_id, uid, name, ""), i)
    return uid, tenant_id, tid


def _new_round(tenant_id, tid):
    number, absent, tables = svc.run_next_round(db.get_tournament(tenant_id, tid))
    return svc.save_confirmed_round(tid, number, absent, tables)


def _enter(round_id, only_table=None):
    tables = {}
    for seat in db.get_round_seats(round_id):
        tables.setdefault(seat["table_number"], []).append(seat["member_id"])
    for number, member_ids in tables.items():
        if only_table in (None, number):
            db.submit_guest_round_results(round_id, dict(zip(member_ids, [45000, 38000, 31000, 26000])), {})


def _names(tid):
    return {m["member_id"]: m["member_name"] for m in db.get_tournament_members(tid)}


def _open(token, **params):
    at = AppTest.from_file(APP_PATH)
    at.query_params["monitor"] = token
    for key, value in params.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception
    return at


def _screen(at):
    screens = [e.proto.body for e in at.get("html") if '<div class="mon"' in e.proto.body]
    assert len(screens) == 1
    return screens[0]


def _texts(fragment):
    return re.findall(r'<span style="--len[^>]*>([^<]*)</span>', fragment)


def _round_label(html):
    return re.search(r'mon-round"><span[^>]*>([^<]*)<', html).group(1)


def _standings(html):
    part = html.split("mon-standings", 1)[1].split('<div class="mon-foot"', 1)[0]
    cells = _texts(part)
    rows = [cells[i:i + 4] for i in range(0, len(cells), 4)]
    return rows[0], rows[1:]


def _cards(html):
    part = html.split('<div class="mon-panel">')[1]
    return [_texts(card) for card in part.split('<div class="mon-table">')[1:]]


def _assert_no_controls(at):
    for kind in ("button", "checkbox", "number_input", "text_input", "text_area", "selectbox",
                 "multiselect", "radio", "toggle", "slider"):
        assert len(getattr(at, kind)) == 0, kind
    assert len(at.sidebar) == 0
    assert len(at.title) == 0


# ---- 入口・閲覧専用 ----

@pytest.mark.parametrize("token", ["no-such-token", ""])
def test_invalid_token_shows_only_message(app_env, token):
    _setup()
    at = _open(token)
    html = _screen(at)
    assert monitor_view.INVALID_LINK_MESSAGE in html
    assert "冬の大会" not in html and "選手A" not in html
    _assert_no_controls(at)  # ログイン画面にも流れない


@pytest.mark.parametrize("reissue", [False, True])
def test_open_monitor_stops_showing_after_revoke_or_reissue(app_env, reissue):
    uid, _, tid = _setup()
    at = _open(db.create_guest_link(tid, uid))
    assert "冬の大会" in _screen(at)
    db.revoke_guest_link(db.get_active_guest_link(tid)["id"])
    new_token = db.create_guest_link(tid, uid) if reissue else None
    at.run()
    assert monitor_view.INVALID_LINK_MESSAGE in _screen(at)
    assert "冬の大会" not in _screen(at)
    if new_token:
        assert "冬の大会" in _screen(_open(new_token))


@pytest.mark.parametrize("stage", ["before", "playing", "final"])
def test_monitor_has_no_controls(app_env, stage):
    uid, tenant_id, tid = _setup(config={**POINT_CONFIG, "total_rounds": 1})
    if stage != "before":
        rid = _new_round(tenant_id, tid)
        if stage == "final":
            _enter(rid)
    at = _open(db.create_guest_link(tid, uid))
    _assert_no_controls(at)
    assert not re.search(r"<(input|button|select|textarea|form|a)[\s>]", _screen(at))


def test_monitor_wins_over_guest_parameter(app_env):
    uid, tenant_id, tid = _setup()
    _new_round(tenant_id, tid)
    token = db.create_guest_link(tid, uid)
    at = _open(token, guest=token)
    assert "冬の大会" in _screen(at)
    _assert_no_controls(at)


def test_monitor_shows_only_its_own_tournament(app_env):
    uid, tenant_id, tid = _setup()
    other = db.create_tournament(tenant_id, "別の大会", "ワンデー", "蛇行", "ポイント", POINT_CONFIG, "受付順")
    db.add_tournament_member(other, db.add_member(tenant_id, uid, "別の人", ""), 1)
    html = _screen(_open(db.create_guest_link(tid, uid)))
    assert "別の大会" not in html and "別の人" not in html


def test_monitor_does_not_write_to_db(app_env):
    uid, tenant_id, tid = _setup()
    _enter(_new_round(tenant_id, tid))
    token = db.create_guest_link(tid, uid)

    def dump():
        conn = db.get_connection()
        lines = list(conn.iterdump())
        conn.close()
        return lines

    before = dump()
    _open(token)
    assert dump() == before


def test_names_are_escaped(app_env):
    names = ['<img src=x onerror="alert(1)">', "B&C", "三郎", "四郎"]
    uid, _, tid = _setup(4, names=names, title="<script>x</script>杯")
    html = _screen(_open(db.create_guest_link(tid, uid)))
    assert "<img" not in html and "<script>" not in html
    assert "&lt;img" in html and "B&amp;C" in html


# ---- 卓組み ----

def test_before_first_round_shows_all_members(app_env):
    uid, _, tid = _setup(12)
    html = _screen(_open(db.create_guest_link(tid, uid)))
    assert _round_label(html) == "開始前"
    assert "卓組みを待っています" in html
    assert _standings(html)[1] == [["—", name, "—", "—"] for name in NAMES]


def test_latest_round_is_selected_and_follows_new_round(app_env):
    uid, tenant_id, tid = _setup()
    _enter(_new_round(tenant_id, tid))
    _new_round(tenant_id, tid)
    at = _open(db.create_guest_link(tid, uid))
    assert _round_label(_screen(at)) == "第2回戦"
    # 第2回戦に未入力が残っていても、保存済みの最新の回戦に切り替わる
    latest = _new_round(tenant_id, tid)
    at.run()
    html = _screen(at)
    assert _round_label(html) == "第3回戦"
    names = _names(tid)
    cards = _cards(html)
    assert len(cards) == 2
    for number, card in enumerate(cards, start=1):
        seats = {s["position"]: names[s["member_id"]]
                 for s in db.get_round_seats(latest) if s["table_number"] == number}
        assert card == [f"卓{number}"] + [x for p in "東南西北" for x in (p, seats[p])]


def test_entered_mark_only_on_completed_table(app_env):
    uid, tenant_id, tid = _setup(12)
    _enter(_new_round(tenant_id, tid), only_table=2)
    cards = _cards(_screen(_open(db.create_guest_link(tid, uid))))
    assert ["入力済み" in card for card in cards] == [False, True, False]


def test_absent_members_are_shown(app_env):
    uid, tenant_id, tid = _setup(9)
    rid = _new_round(tenant_id, tid)
    html = _screen(_open(db.create_guest_link(tid, uid)))
    assert f"抜け番: {_names(tid)[db.get_round_absences(rid)[0]]}" in html
    assert len(_standings(html)[1]) == 9


# ---- 累計順位表・最終順位 ----

@pytest.mark.parametrize("mode,config,label", [
    ("ポイント", POINT_CONFIG, "合計ポイント"),
    ("ポイント", OVERALL_CONFIG, "合計ポイント"),
    ("得点", SCORE_CONFIG, "総合得点"),
])
def test_standings_match_service_in_each_mode(app_env, mode, config, label):
    uid, tenant_id, tid = _setup(12, mode, config)
    for _ in range(2):
        _enter(_new_round(tenant_id, tid))
    header, rows = _standings(_screen(_open(db.create_guest_link(tid, uid))))
    assert header == ["順位", "氏名", label, "素点合計"]

    names = _names(tid)
    raw = {}
    for round_row in db.get_rounds(tid):
        for mid, score in db.get_round_results(round_row["id"]).items():
            raw[mid] = raw.get(mid, 0) + score
    fmt = monitor_view.scoring_logic.format_point_value
    assert rows == [
        [str(i + 1), names[r["member_id"]],
         fmt(r["value"] / 100 if mode == "得点" else r["value"]), fmt(raw[r["member_id"]] / 100)]
        for i, r in enumerate(svc.compute_standings(db.get_tournament(tenant_id, tid)))
    ]


@pytest.mark.parametrize("config,ranked_count", [(POINT_CONFIG, 4), (OVERALL_CONFIG, 0)])
def test_everyone_is_listed_while_round_is_partly_entered(app_env, config, ranked_count):
    uid, tenant_id, tid = _setup(12, config=config)
    _enter(_new_round(tenant_id, tid), only_table=1)
    rows = _standings(_screen(_open(db.create_guest_link(tid, uid))))[1]
    assert sorted(r[1] for r in rows) == NAMES
    assert [r[0] for r in rows] == [str(i + 1) for i in range(ranked_count)] + ["—"] * (12 - ranked_count)
    assert sum(r[3] != "—" for r in rows) == 4  # 素点合計は入力した4人に出る


@pytest.mark.parametrize("mode", ["ポイント", "得点"])
def test_final_ranking_appears_only_after_final_round_is_complete(app_env, mode):
    base = SCORE_CONFIG if mode == "得点" else OVERALL_CONFIG
    uid, tenant_id, tid = _setup(8, mode, {**base, "total_rounds": 2})
    token = db.create_guest_link(tid, uid)
    _enter(_new_round(tenant_id, tid))
    assert "最終順位" not in _screen(_open(token))
    last = _new_round(tenant_id, tid)
    _enter(last, only_table=1)
    at = _open(token)
    assert "最終順位" not in _screen(at)
    _enter(last, only_table=2)
    at.run()
    html = _screen(at)
    assert "最終順位" in html
    cells = _texts(html.split("mon-final", 1)[1].split('<div class="mon-panel">', 1)[0])[4:]
    names = _names(tid)
    ranking = svc.compute_final_ranking(db.get_tournament(tenant_id, tid))
    assert cells[0] == "優勝"
    assert cells[1::4] == [names[r["member_id"]] for r in ranking]
    assert len(_standings(html)[1]) == 8


def test_round_added_after_final_round_shows_its_tables(app_env):
    uid, tenant_id, tid = _setup(8, config={**POINT_CONFIG, "total_rounds": 1})
    _enter(_new_round(tenant_id, tid))
    _new_round(tenant_id, tid)
    html = _screen(_open(db.create_guest_link(tid, uid)))
    assert _round_label(html) == "第2回戦" and "最終順位" not in html


# ---- 自動更新 ----

def test_refresh_interval_is_15_seconds(app_env, monkeypatch):
    from streamlit.runtime.scriptrunner_utils.script_run_context import ScriptRunContext

    intervals = []
    original = ScriptRunContext.enqueue

    def spy(self, msg):
        if msg.WhichOneof("type") == "auto_rerun":
            intervals.append(msg.auto_rerun.interval)
        return original(self, msg)

    monkeypatch.setattr(ScriptRunContext, "enqueue", spy)
    uid, _, tid = _setup()
    _open(db.create_guest_link(tid, uid))
    assert intervals == [15]


def _locked(_):
    raise RuntimeError("database is locked")


def test_read_failure_keeps_last_screen_then_recovers(app_env, monkeypatch):
    uid, _, tid = _setup()
    at = _open(db.create_guest_link(tid, uid))
    original = db.get_rounds
    monkeypatch.setattr(db, "get_rounds", _locked)
    at.run()
    assert not at.exception
    assert "冬の大会" in _screen(at) and "更新に失敗・再試行中" in _screen(at)
    monkeypatch.setattr(db, "get_rounds", original)
    at.run()
    assert "更新に失敗" not in _screen(at)


def test_read_failure_on_first_load_shows_japanese_message(app_env, monkeypatch):
    uid, _, tid = _setup()
    token = db.create_guest_link(tid, uid)
    monkeypatch.setattr(db, "get_rounds", _locked)
    assert "読み込みに失敗しました" in _screen(_open(token))


# ---- 主催者画面のリンク ----

def _link(at, kind):
    return [c.value.rsplit(f"?{kind}=", 1)[1] for c in at.code if f"?{kind}=" in c.value]


def _admin():
    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    return at


def test_admin_monitor_link_follows_issue_and_reissue(app_env):
    _, _, tid = _setup()
    at = _admin()
    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    old = _link(at, "monitor")
    assert len(old) == 1 and old == _link(at, "guest")
    assert db.get_tournament_by_guest_token(old[0])["id"] == tid
    at.button[_find_button(at, "リンクを再発行する")].click().run()
    new = _link(at, "monitor")
    assert len(new) == 1 and new != old and new == _link(at, "guest")
    assert db.get_tournament_by_guest_token(old[0]) is None


def test_admin_monitor_link_disappears_after_revoke(app_env):
    _setup()
    at = _admin()
    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    assert _link(at, "monitor")
    at.button[_find_button(at, "リンクを無効化する")].click().run()
    at.run()  # AppTestは st.rerun() 前の描画を残すので、もう1回描き直してから見る
    assert _link(at, "monitor") == []
    assert _link(at, "guest") == []


def test_admin_does_not_show_monitor_link_revoked_in_other_session(app_env):
    uid, _, tid = _setup()
    at = _admin()
    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    db.revoke_guest_link(db.get_active_guest_link(tid)["id"])
    db.create_guest_link(tid, uid)
    at.run()
    assert _link(at, "monitor") == []


def test_admin_explains_monitor_link_when_token_cannot_be_shown(app_env):
    uid, _, tid = _setup()
    db.create_guest_link(tid, uid)
    at = _admin()
    assert _link(at, "monitor") == []
    assert any("?monitor=" in c.value for c in at.caption)
