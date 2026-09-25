"""大会管理・回戦実行・成績入力・ゲスト共有リンクのapp.py側UI配線を
streamlit.testing.v1.AppTest で検証する。

AppTestは、同一セッション内で長く連続した画面遷移（特にst.formの出現・消失を
何度も挟む操作）を行うと内部の状態管理でKeyErrorを起こす既知の制約がある
（得点ポイント評価方式実装依頼.md対応時に確認済み）。そのため、各テストは
db.py/tournament_serviceで前提状態を直接組み立ててから、検証したいUI操作
だけを行う形にし、1つのAppTestセッション内の操作数を絞っている。
"""
from pathlib import Path

import pytest
import streamlit_authenticator as stauth
from streamlit.testing.v1 import AppTest

import db
import tournament_service as svc

APP_PATH = str(Path(__file__).parent.parent / "app.py")

SCORE_CONFIG = {
    "start_point": 35000,
    "return_point": 40000,
    "oka": 20000,
    "tobi_amount": 1000,
    "uma_table": {
        1: [48000, -8000, -16000, -24000],
        2: [24000, 8000, -8000, -24000],
        3: [12000, 8000, 4000, -24000],
    },
}

POINT_CONFIG = {"rank_point_table": [3, 1, -1, -3], "start_point": 35000}

# サークルの通常の大会と同じ開始点数30,000点（画面の入力は300、4人の合計は1200）
SCORE_CONFIG_30000 = {**SCORE_CONFIG, "start_point": 30000}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    monkeypatch.setenv("AUTH_COOKIE_NAME", "test_cookie")
    monkeypatch.setenv("AUTH_COOKIE_KEY", "test-cookie-key-at-least-32-bytes-long!!")
    monkeypatch.setenv("AUTH_INVITE_CODE", "correct-horse-battery-staple")
    return tmp_path


def _add_user(username, password, **kwargs):
    kwargs.setdefault("email_verified", True)
    return db.add_user(username, stauth.Hasher.hash(password), **kwargs)


def _find_button(at, label_substr):
    for i, b in enumerate(at.button):
        if label_substr in b.label:
            return i
    raise ValueError(f"button not found: {label_substr}")


def _login(at, username, password):
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password)
    at.button[_find_button(at, "ログイン")].click().run()


def _setup_admin_with_tournament(tenant_and_members=True):
    user_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(user_id)["tenant_id"]
    member_ids = []
    if tenant_and_members:
        member_ids = [db.add_member(tenant_id, user_id, name, "") for name in ("太郎", "次郎", "三郎", "四郎")]
    tournament_id = db.create_tournament(
        tenant_id, "テスト大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    return user_id, tenant_id, member_ids, tournament_id


def test_tournament_list_shows_created_tournament(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    assert any("テスト大会" in e.label for e in at.expander)


def test_admin_can_add_participants_via_checkboxes(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    for checkbox in at.checkbox:
        if checkbox.label in ("太郎", "次郎", "三郎", "四郎"):
            checkbox.check()
    at.run()
    at.button[_find_button(at, "選択したメンバーを追加")].click().run()

    assert not at.exception
    members_in_tournament = db.get_tournament_members(tournament_id)
    assert {m["member_name"] for m in members_in_tournament} == {"太郎", "次郎", "三郎", "四郎"}


def test_admin_can_run_and_save_a_round(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    assert not at.exception
    assert any("プレビュー" in md.value for md in at.markdown)

    at.button[_find_button(at, "この結果で保存")].click().run()
    assert not at.exception
    assert db.count_rounds(tournament_id) == 1

    rounds = db.get_rounds(tournament_id)
    seats = db.get_round_seats(rounds[0]["id"])
    assert len(seats) == 4


def test_admin_can_enter_round_results(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    svc.save_confirmed_round(tournament_id, round_number, absent, tables)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("score_")]
    assert len(score_inputs) == 4
    scores = [70000, 50000, 25000, -5000]  # 合計140000(開始点数35000×4)
    for ni, score in zip(score_inputs, scores):
        ni.set_value(score // 100)  # 画面は100点単位
    at.run()

    at.button[_find_button(at, "成績を保存")].click().run()
    assert not at.exception

    rounds = db.get_rounds(tournament_id)
    saved_results = db.get_round_results(rounds[0]["id"])
    assert sorted(saved_results.values(), reverse=True) == scores


def test_saved_round_with_scrambled_seat_order_displays_east_south_west_north(app_env):
    """卓組み結果のdictキー挿入順が「北→西→東→南」のようにバラバラでも
    (_random_tables()が実際に起こしうる並び)、主催者の成績入力欄・ゲスト画面の
    卓組み結果とも東→南→西→北の順で表示される。DBの生の保存順（挿入順）自体は
    変えていない前提（表示側だけの並べ替え）であることも合わせて確認する。"""
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    taro, jiro, saburo, shiro = member_ids
    # 東=太郎, 南=次郎, 西=三郎, 北=四郎 という対応だが、わざと挿入順をバラバラにする
    scrambled_table = {"北": shiro, "西": saburo, "東": taro, "南": jiro}
    round_id = db.save_round(tournament_id, 1, [scrambled_table], [])

    # 生のDB順（=挿入順）は依然バラバラなまま（卓組みロジック・保存内容は変更していない）
    raw_seats = db.get_round_seats(round_id)
    assert [s["position"] for s in raw_seats] == ["北", "西", "東", "南"]

    raw_token = db.create_guest_link(tournament_id, user_id)
    expected_names = ["太郎", "次郎", "三郎", "四郎"]  # 東→南→西→北の順

    # 主催者の成績入力欄
    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    score_labels = [ni.label for ni in at.number_input if ni.key and ni.key.startswith("score_")]
    assert [label.split("（")[0] for label in score_labels] == expected_names

    # ゲスト画面の卓組み結果
    at_guest = AppTest.from_file(APP_PATH)
    at_guest.query_params["guest"] = raw_token
    at_guest.run()
    seat_lines = [
        md.value.strip() for md in at_guest.markdown
        if md.value.strip().startswith(("東:", "南:", "西:", "北:"))
    ]
    assert [line.split(": ", 1)[1] for line in seat_lines] == expected_names


def test_admin_saving_one_table_does_not_zero_out_untouched_table(app_env):
    """主催者の成績入力フォームは回戦内の全卓を1画面にまとめて表示するため、
    ある卓だけ入力して保存すると、まだ誰も入力していない別の卓の選手が
    初期値0のまま保存されてしまわないか（ゲストの未入力を潰してしまわないか）を確認する。"""
    user_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(user_id)["tenant_id"]
    member_ids = [db.add_member(tenant_id, user_id, f"選手{i}", "") for i in range(8)]
    tournament_id = db.create_tournament(
        tenant_id, "テスト大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    svc.save_confirmed_round(tournament_id, round_number, absent, tables)
    assert len(tables) == 2  # 8人なので2卓

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("score_")]
    assert len(score_inputs) == 8

    rounds = db.get_rounds(tournament_id)
    seats = db.get_round_seats(rounds[0]["id"])
    table1_member_ids = {s["member_id"] for s in seats if s["table_number"] == 1}
    table2_member_ids = {s["member_id"] for s in seats if s["table_number"] == 2}

    # 卓1だけ入力し、卓2は初期値0のまま何も触らない
    table1_scores = [60000, 40000, 30000, 10000]  # 合計140000(開始点数35000×4)
    i = 0
    for ni in score_inputs:
        member_id = int(ni.key.rsplit("_", 1)[1])
        if member_id in table1_member_ids:
            ni.set_value(table1_scores[i] // 100)  # 画面は100点単位
            i += 1
    at.run()

    at.button[_find_button(at, "成績を保存")].click().run()
    assert not at.exception

    saved_results = db.get_round_results(rounds[0]["id"])
    assert set(saved_results.keys()) == table1_member_ids  # 卓2の選手は一切保存されない
    assert sorted(saved_results.values(), reverse=True) == table1_scores

    # 卓2はDB上「未入力」のままなので、ゲストが本来の素点を送信できる
    table2_scores = {mid: 35000 for mid in table2_member_ids}
    db.submit_guest_round_results(rounds[0]["id"], table2_scores, {})
    assert db.get_round_results(rounds[0]["id"]) == {**saved_results, **table2_scores}


def test_admin_can_issue_and_revoke_guest_link(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert db.get_active_guest_link(tournament_id) is None

    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    assert not at.exception
    assert db.get_active_guest_link(tournament_id) is not None
    assert any("code" in str(type(c)).lower() for c in at.code) or len(at.code) > 0

    at.button[_find_button(at, "リンクを無効化する")].click().run()
    assert not at.exception
    assert db.get_active_guest_link(tournament_id) is None


def test_non_admin_cannot_see_tournament_create_or_delete_controls(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    db.add_user(
        "member1", stauth.Hasher.hash("memberpass123"),
        email="member1@example.com", tenant_id=tenant_id, role="member", email_verified=True,
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "member1", "memberpass123")

    assert not at.exception
    assert any("テスト大会" in e.label for e in at.expander)  # 一覧の閲覧はできる
    assert not any("大会を作成" in b.label for b in at.button)
    assert not any("この大会を削除" in b.label for b in at.button)
    assert not any("次の回戦の卓組みを実行" in b.label for b in at.button)


def test_guest_link_token_survives_round_trip_through_db(app_env):
    """ゲストトークンのURL埋め込み・照合の前提(db.get_tournament_by_guest_token)が
    実際にapp.pyが発行したトークンで機能することを確認する(Stage 2でゲスト画面が
    このトークンを使う前提の回帰確認)。"""
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    raw_token = db.create_guest_link(tournament_id, user_id)

    resolved = db.get_tournament_by_guest_token(raw_token)
    assert resolved is not None
    assert resolved["id"] == tournament_id


def test_admin_can_reissue_guest_link_end_to_end(app_env):
    """3.6対応: 「リンクを再発行する」ボタンを通しで確認する。旧トークンは失効し、
    新トークンで大会が特定できること。"""
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    at.button[_find_button(at, "ゲスト用リンクを発行")].click().run()
    old_link = db.get_active_guest_link(tournament_id)
    old_code_value = next(c.value for c in at.code if "?guest=" in c.value)
    old_token = old_code_value.rsplit("?guest=", 1)[1]
    assert db.get_tournament_by_guest_token(old_token) is not None

    at.button[_find_button(at, "リンクを再発行する")].click().run()
    assert not at.exception
    new_link = db.get_active_guest_link(tournament_id)
    assert new_link["id"] != old_link["id"]
    new_code_value = next(c.value for c in at.code if "?guest=" in c.value)
    new_token = new_code_value.rsplit("?guest=", 1)[1]
    assert new_token != old_token

    assert db.get_tournament_by_guest_token(old_token) is None
    resolved = db.get_tournament_by_guest_token(new_token)
    assert resolved is not None
    assert resolved["id"] == tournament_id


# ---------------------------------------------------------------------------
# Stage 2: ゲスト向け画面(ゲスト向け画面実装依頼_Stage2.md)
# ---------------------------------------------------------------------------

def _setup_tournament_with_round(scoring_mode="得点", scoring_config=SCORE_CONFIG):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    if (scoring_mode, scoring_config) != ("得点", SCORE_CONFIG):
        db.update_tournament(
            tenant_id, tournament_id, "テスト大会", "ワンデー", "蛇行", scoring_mode,
            scoring_config, "受付順", None, None, "準備中",
        )
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    svc.save_confirmed_round(tournament_id, round_number, absent, tables)
    raw_token = db.create_guest_link(tournament_id, user_id)
    return user_id, tenant_id, member_ids, tournament_id, raw_token


def test_guest_view_opens_with_valid_token(app_env):
    *_, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("テスト大会" in md.value for md in list(at.markdown) + list(at.subheader))
    assert not any("ログイン" in b.label for b in at.button)


def test_guest_view_shows_message_for_invalid_token(app_env):
    _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = "no-such-token"
    at.run()

    assert not at.exception
    assert any("このリンクは使えません" in e.value for e in at.error)
    assert not any("ログイン" in b.label for b in at.button)
    assert len(at.text_input) == 0  # ログインフォームが描画されていない


def test_guest_view_shows_message_for_revoked_token(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    link = db.get_active_guest_link(tournament_id)
    db.revoke_guest_link(link["id"])

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("このリンクは使えません" in e.value for e in at.error)


def test_guest_view_cannot_see_other_tournament_or_admin_controls(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    other_user_id = _add_user("admin2", "otherpass123", email="admin2@example.com")
    other_tenant_id = db.get_user_by_id(other_user_id)["tenant_id"]
    db.create_tournament(
        other_tenant_id, "他人の大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    page_text = " ".join(md.value for md in list(at.markdown) + list(at.subheader) + list(at.caption))
    assert "他人の大会" not in page_text
    assert not any("ログアウト" in b.label for b in at.button)
    assert not any("大会を作成" in b.label for b in at.button)
    assert not any("次の回戦の卓組みを実行" in b.label for b in at.button)


def test_guest_score_submit_disabled_when_sum_off_by_one(app_env):
    """5章 境界値: 合計が開始点数×4からずれている場合は送信できない（入力の最小単位は100点）。"""
    *_, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    assert len(score_inputs) == 4

    off_by_one = [600, 400, 300, 99]  # 100点単位で合計1399(期待値1400から1目盛りずれ)
    for ni, v in zip(score_inputs, off_by_one):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()

    submit_btn = at.button[_find_button(at, "この内容で送信する")]
    assert submit_btn.disabled
    assert any("合計が1399です。1400になるよう確認してください。" in e.value for e in at.error)


def test_guest_score_submit_enabled_and_saves_when_sum_exact(app_env):
    """5章: 合計がぴったり開始点数×4のときだけ送信でき、マイナスの素点を含む入力も
    正しく保存される。"""
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    exact = [700, 500, 250, -50]  # 100点単位で合計1400(ぴったり)。マイナスの素点も含める
    for ni, v in zip(score_inputs, exact):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()

    submit_btn = at.button[_find_button(at, "この内容で送信する")]
    assert not submit_btn.disabled
    submit_btn.click().run()
    assert not at.exception

    rounds = db.get_rounds(tournament_id)
    saved = db.get_round_results(rounds[0]["id"])
    # DBには実際の点数（100倍）で保存される
    assert sorted(saved.values(), reverse=True) == [70000, 50000, 25000, -5000]
    meta = db.get_round_result_meta(rounds[0]["id"])
    assert all(m["input_source"] == "guest" for m in meta.values())


def test_guest_submit_rechecks_sum_when_a_score_is_edited_and_submitted_at_once(app_env):
    """合計ぴったりで送信ボタンが押せる状態から、素点を1つ書き換えてそのまま送信ボタンを押す。

    ブラウザでは、入力欄の書き換えと送信ボタンの押下が同じ再実行で届く（ボタンは直前の
    画面では押せる状態だった）。送信ボタンを押せなくするだけでは止まらないので、
    送信時にも合計を確かめ直す。10/2前に実際のブラウザで、合計139900のまま保存される
    ことを確認して直した。ゲストの送信は修正できないため、ここで止めることが重要。
    """
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    for ni, v in zip(score_inputs, [700, 500, 250, -50]):  # 合計1400(ぴったり)
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()
    assert not at.button[_find_button(at, "この内容で送信する")].disabled

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    score_inputs[3].set_value(-51)  # 合計1399に書き換え、同じ再実行で送信
    at.button[_find_button(at, "この内容で送信する")].click().run()

    assert not at.exception
    assert db.get_round_results(db.get_rounds(tournament_id)[0]["id"]) == {}  # 保存されない
    assert any("送信できませんでした" in e.value for e in at.error)


# 合計チェックの境界値（開始点数30,000点＝合計1200）。「ちょうど」だけが通り、
# 100点（入力の1目盛り）多くても少なくても止まることを守る。
# 「足りないときだけ弾く」「多いときだけ弾く」のような片側だけの壊れ方もここで赤になる。
SUM_BOUNDARY_CASES = [
    pytest.param([458, 342, 250, 150], True, id="ちょうど1200→送信できる"),
    pytest.param([458, 342, 250, 151], False, id="100点多い1201→送信できない"),
    pytest.param([458, 342, 250, 149], False, id="100点少ない1199→送信できない"),
]


@pytest.mark.parametrize("hundreds, accepted", SUM_BOUNDARY_CASES)
def test_guest_sum_boundary_at_start_point_30000(app_env, hundreds, accepted):
    """ゲストの代表者送信: 合計が開始点数×4（30,000×4＝1200）ぴったりのときだけ保存される。"""
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_config=SCORE_CONFIG_30000
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    for ni, v in zip(score_inputs, hundreds):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()

    submit_btn = at.button[_find_button(at, "この内容で送信する")]
    saved = db.get_round_results(db.get_rounds(tournament_id)[0]["id"])
    if accepted:
        assert not submit_btn.disabled
        submit_btn.click().run()
        assert not at.exception
        saved = db.get_round_results(db.get_rounds(tournament_id)[0]["id"])
        assert sorted(saved.values(), reverse=True) == [45800, 34200, 25000, 15000]
    else:
        assert submit_btn.disabled
        assert any(
            f"合計が{sum(hundreds)}です。1200になるよう確認してください。" in e.value for e in at.error
        )
        assert saved == {}


def test_guest_view_already_submitted_table_is_read_only(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    rounds = db.get_rounds(tournament_id)
    db.save_round_results(rounds[0]["id"], {mid: 35000 for mid in member_ids}, {})

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("すでに入力済みです" in i.value for i in at.info)
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    assert len(score_inputs) == 0


def test_guest_view_shows_tobi_input_for_score_mode(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_mode="得点"
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    score_inputs[0].set_value(0)
    at.run()

    assert any(ms.key and ms.key.startswith("guest_busters_") for ms in at.multiselect)


def test_guest_view_hides_tobi_input_for_point_mode(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_mode="ポイント", scoring_config=POINT_CONFIG
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    score_inputs[0].set_value(0)
    at.run()

    assert not any(ms.key and ms.key.startswith("guest_busters_") for ms in at.multiselect)


def test_guest_view_blocks_score_entry_when_start_point_missing(app_env):
    """4章①: ポイント方式で開始点数が未設定(既存大会相当)の場合、成績入力を止める。"""
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round(
        scoring_mode="ポイント", scoring_config={"rank_point_table": [3, 1, -1, -3]}
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    assert any("開始点数を設定するまで入力できません" in w.value for w in at.warning)
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    assert len(score_inputs) == 0


# ---------------------------------------------------------------------------
# 主催者の成績保存: 卓ごとの合計チェック(ゲスト向け画面実装依頼_Stage2.md 3.5)・表示整形
# ---------------------------------------------------------------------------

def _setup_two_table_round(scoring_mode="得点", scoring_config=SCORE_CONFIG):
    """8人・2卓の第1回戦を保存し、(tenant_id, tournament_id, round_id, 卓1のmember_id, 卓2のmember_id)を返す。"""
    user_id = _add_user("admin1", "adminpass123", email="admin1@example.com")
    tenant_id = db.get_user_by_id(user_id)["tenant_id"]
    member_ids = [db.add_member(tenant_id, user_id, f"選手{i}", "") for i in range(8)]
    tournament_id = db.create_tournament(
        tenant_id, "テスト大会", "ワンデー", "蛇行", scoring_mode, scoring_config, "受付順"
    )
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)
    tournament = db.get_tournament(tenant_id, tournament_id)
    round_number, absent, tables = svc.run_next_round(tournament)
    round_id = svc.save_confirmed_round(tournament_id, round_number, absent, tables)
    seats = db.get_round_seats(round_id)
    table1 = [s["member_id"] for s in seats if s["table_number"] == 1]
    table2 = [s["member_id"] for s in seats if s["table_number"] == 2]
    return tenant_id, tournament_id, round_id, table1, table2


def _set_admin_scores(at, scores_by_member):
    for ni in at.number_input:
        if ni.key and ni.key.startswith("score_"):
            member_id = int(ni.key.rsplit("_", 1)[1])
            if member_id in scores_by_member:
                ni.set_value(scores_by_member[member_id] // 100)  # 画面は100点単位
    at.run()


def test_admin_save_blocked_when_a_table_total_is_off_by_one(app_env):
    """卓1はぴったり、卓2は100点（入力の1目盛り）足りない: ずれた卓だけを赤字で示し、保存全体を止める
    （ぴったりの卓1も保存されない）。"""
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    _set_admin_scores(at, {
        **dict(zip(table1, (70000, 50000, 25000, -5000))),  # 合計140000
        **dict(zip(table2, (60000, 40000, 30000, 9900))),   # 合計139900(100点足りない)
    })

    error_texts = [e.value for e in at.error]
    assert any("卓2の合計が1399です。1400になるよう確認してください。" in t for t in error_texts)
    assert not any("卓1の合計が" in t for t in error_texts)  # 卓1は赤字にならない

    at.button[_find_button(at, "成績を保存")].click().run()
    assert not at.exception
    assert any("保存できません" in e.value and "卓2" in e.value for e in at.error)
    assert db.get_round_results(round_id) == {}  # 何も保存されない


def test_admin_save_accepts_every_table_exactly_at_start_point_times_four(app_env):
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round()
    scores = {
        **dict(zip(table1, (70000, 50000, 25000, -5000))),
        **dict(zip(table2, (60000, 40000, 30000, 10000))),
    }

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    _set_admin_scores(at, scores)

    assert not any("の合計が" in e.value for e in at.error)
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    assert db.get_round_results(round_id) == scores
    meta = db.get_round_result_meta(round_id)
    assert all(m["input_source"] == "admin" for m in meta.values())


def test_admin_correction_of_saved_table_is_also_checked(app_env):
    """保存済みの卓の修正(3.5)にも同じ合計チェックがかかり、不一致なら既存の値は変わらない。"""
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round()
    original = dict(zip(table1, (70000, 50000, 25000, -5000)))
    db.save_round_results(round_id, original, {})

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    _set_admin_scores(at, {table1[0]: 71000})  # 合計141000になる修正

    assert any("卓1の合計が1410です。1400になるよう確認してください。" in e.value for e in at.error)
    assert not any("卓2の合計が" in e.value for e in at.error)  # 未入力の卓2は対象外
    at.button[_find_button(at, "成績を保存")].click().run()

    assert db.get_round_results(round_id) == original


@pytest.mark.parametrize("hundreds, accepted", SUM_BOUNDARY_CASES)
def test_admin_sum_boundary_at_start_point_30000(app_env, hundreds, accepted):
    """主催者画面での保存: 卓1はぴったり1200、卓2を境界値にする。卓2がずれていれば
    どちらの卓も保存されず、ぴったりなら両方保存される（9/19時点では合計チェックが無かった）。"""
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round(
        scoring_config=SCORE_CONFIG_30000
    )
    scores = {
        **dict(zip(table1, (40000, 35000, 30000, 15000))),  # 合計120000（ぴったり）
        **dict(zip(table2, (v * 100 for v in hundreds))),
    }

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    _set_admin_scores(at, scores)
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    assert not any("卓1の合計が" in e.value for e in at.error)
    if accepted:
        assert db.get_round_results(round_id) == scores
    else:
        assert any(
            f"卓2の合計が{sum(hundreds)}です。1200になるよう確認してください。" in e.value for e in at.error
        )
        assert any("保存できません" in e.value and "卓2" in e.value for e in at.error)
        assert db.get_round_results(round_id) == {}


def test_admin_save_blocked_when_start_point_is_missing(app_env):
    """開始点数が未設定の大会(既存のポイント方式大会など)では、ゲスト画面と同じく成績を保存できない。"""
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round(
        scoring_mode="ポイント", scoring_config={"rank_point_table": [3, 1, -1, -3]}
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    assert any("開始点数が未設定" in w.value for w in at.warning)
    _set_admin_scores(at, dict(zip(table1, (70000, 50000, 25000, -5000))))

    at.button[_find_button(at, "成績を保存")].click().run()

    assert any("開始点数が未設定" in e.value for e in at.error)
    assert db.get_round_results(round_id) == {}


def test_point_mode_values_are_shown_without_trailing_zeros_and_per_table(app_env):
    """順位ポイントは「3.0000」ではなく「3」「2.5」の形で、卓ごとに1〜4位が決まって表示される。
    卓1は1・2位同着(2.5ずつ)、卓2は同着なし。"""
    tenant_id, tournament_id, round_id, table1, table2 = _setup_two_table_round(
        scoring_mode="ポイント", scoring_config={"rank_point_table": [4, 1, -2, -3], "start_point": 35000}
    )
    db.save_round_results(round_id, {
        **dict(zip(table1, (60000, 60000, 10000, 10000))),   # 1・2位同着(4+1)/2=2.5、3・4位同着(-2-3)/2=-2.5
        **dict(zip(table2, (50000, 40000, 30000, 20000))),   # 4, 1, -2, -3
    }, {})

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    value_columns = [[str(v) for v in t.value["値"]] for t in at.table if "値" in t.value.columns]
    assert value_columns, "値の列を持つ表が表示されていない"
    flat = [v for column in value_columns for v in column]
    assert not any(v.endswith(".0000") or v.endswith(".0") for v in flat)
    # この回戦の計算結果表(値の降順): 卓2の1位4、卓1の同着2.5×2、卓2の2位1、...
    assert sorted(value_columns[0], key=float, reverse=True) == ["4", "2.5", "2.5", "1", "-2", "-2.5", "-2.5", "-3"]


def test_guest_link_description_no_longer_mentions_stage_2(app_env):
    _setup_admin_with_tournament()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    captions = " ".join(c.value for c in at.caption)
    assert "アカウント作成なしでこの大会の卓組み結果・成績を閲覧・入力できます" in captions
    assert "Stage 2" not in captions
    assert "実装予定" not in captions


# ---------------------------------------------------------------------------
# 点数は100点単位で入出力する（45,800点は「458」）。DBには実際の点数で保存される
# ---------------------------------------------------------------------------

def test_guest_enters_scores_in_hundreds_and_db_stores_actual_points(app_env):
    """458→45,800点、192→19,200点、-20→-2,000点としてDBに保存される。"""
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    for ni, v in zip(score_inputs, [458, 192, -20, 770]):  # 合計1400
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()

    assert any("合計 1400 です" in s.value for s in at.success)
    at.button[_find_button(at, "この内容で送信する")].click().run()
    assert not at.exception

    rounds = db.get_rounds(tournament_id)
    saved = db.get_round_results(rounds[0]["id"])
    assert sorted(saved.values(), reverse=True) == [77000, 45800, 19200, -2000]


def test_guest_sum_message_uses_hundreds(app_env):
    *_, raw_token = _setup_tournament_with_round()

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    for ni, v in zip(score_inputs, [450, 300, 250, 190]):  # 合計1190
        ni.set_value(v)
    at.run()

    assert any("合計が1190です。1400になるよう確認してください。" in e.value for e in at.error)


def test_guest_out_of_range_input_shows_unit_hint_and_blocks_submit(app_env):
    """絶対値が2000（200,000点）を超える入力は桁の誤りとして知らせ、送信できない。
    境界: ちょうど2000は通り、2001は止まる（どちらも合計は1400で一致している）。"""
    *_, raw_token = _setup_tournament_with_round()
    hint = "100点単位で入力してください(例:45,800点なら458)"

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    for ni, v in zip(score_inputs, [2000, -600, 0, 0]):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()
    assert not any(hint in e.value for e in at.error)
    assert not at.button[_find_button(at, "この内容で送信する")].disabled

    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]  # 再実行後の要素を取り直す
    for ni, v in zip(score_inputs, [2001, -601, 0, 0]):
        ni.set_value(v)
    at.run()
    assert any(hint in e.value for e in at.error)
    assert not any("合計が" in e.value for e in at.error)  # 合計のずれとは別の、単位の誤りとして知らせる
    assert at.button[_find_button(at, "この内容で送信する")].disabled


def test_guest_view_shows_submitted_scores_in_hundreds(app_env):
    user_id, tenant_id, member_ids, tournament_id, raw_token = _setup_tournament_with_round()
    rounds = db.get_rounds(tournament_id)
    db.save_round_results(
        rounds[0]["id"], dict(zip(member_ids, (77000, 45800, 19200, -2000))), {}
    )

    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()

    assert not at.exception
    page_text = " ".join(md.value for md in at.markdown)
    assert "太郎: 770" in page_text
    assert "次郎: 458" in page_text
    assert "三郎: 192" in page_text
    assert "四郎: -20" in page_text
    assert "45,800" not in page_text and "45800" not in page_text


def test_admin_shows_score_mode_totals_in_hundreds(app_env):
    """得点方式の総合得点は100点単位で表示する（94,000点は「940」）。"""
    tenant_id, tournament_id, round_id, table1, table2 = _setup_two_table_round()
    db.save_round_results(round_id, dict(zip(table1, (70000, 50000, 25000, -5000))), {})
    # 2人浮き: ウマ+24000/+8000/-8000/-24000 → 94000, 58000, 17000, -29000

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    assert not at.exception
    columns = [[str(v) for v in t.value["値"]] for t in at.table if "値" in t.value.columns]
    standings_columns = [[str(v) for v in t.value["総合得点"]] for t in at.table if "総合得点" in t.value.columns]
    assert columns[0] == ["940", "580", "170", "-290"]  # この回戦の計算結果(値の降順)
    assert standings_columns[0] == ["940", "580", "170", "-290"]  # 大会内順位表


def test_admin_score_input_in_hundreds_saves_actual_points(app_env):
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    _set_admin_scores(at, dict(zip(table1, (45800, 19200, 77000, -2000))))  # 画面には458,192,770,-20と入力

    at.button[_find_button(at, "成績を保存")].click().run()

    assert db.get_round_results(round_id) == dict(zip(table1, (45800, 19200, 77000, -2000)))


def test_admin_out_of_range_input_blocks_save_with_unit_hint(app_env):
    _, tournament_id, round_id, table1, table2 = _setup_two_table_round()

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    # 45,800点のつもりで「45800」と入力した誤り(=4,580,000点)。合計が偶然一致しても止まる
    _set_admin_scores(at, dict(zip(table1, (4_580_000, -4_440_000, 0, 0))))

    assert any("卓1: 100点単位で入力してください(例:45,800点なら458)" in e.value for e in at.error)
    at.button[_find_button(at, "成績を保存")].click().run()
    assert any("桁がおかしい卓" in e.value and "卓1" in e.value for e in at.error)
    assert db.get_round_results(round_id) == {}


def test_tournament_edit_form_shows_config_in_hundreds_and_saves_actual_points(app_env):
    """大会の編集フォーム: 設定値(開始点数・返し点・オカ・飛び賞額・ウマ)は100点単位で表示・入力し、
    DBには実際の点数で保存される。順位ポイントは変換しない。"""
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    prefix = f"edit_tournament_{tournament_id}"

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    inputs = {ni.key: ni for ni in at.number_input if ni.key and ni.key.startswith(prefix)}
    assert inputs[f"{prefix}_start_point"].value == 350
    assert inputs[f"{prefix}_return_point"].value == 400
    assert inputs[f"{prefix}_oka"].value == 200
    assert inputs[f"{prefix}_tobi_amount"].value == 10
    assert [inputs[f"{prefix}_uma_1_{i}"].value for i in range(4)] == [480, -80, -160, -240]

    inputs[f"{prefix}_start_point"].set_value(250)
    inputs[f"{prefix}_uma_1_0"].set_value(500)
    at.run()
    at.button[_find_button(at, "変更を保存")].click().run()

    assert not at.exception
    saved = db.get_tournament(tenant_id, tournament_id)["scoring_config"]
    assert saved["start_point"] == 25000
    assert saved["uma_table"][1] == [50000, -8000, -16000, -24000]
    assert saved["return_point"] == 40000 and saved["oka"] == 20000 and saved["tobi_amount"] == 1000  # 触っていない値は不変


def test_tournament_edit_form_rejects_wrong_digits(app_env):
    user_id, tenant_id, member_ids, tournament_id = _setup_admin_with_tournament()
    prefix = f"edit_tournament_{tournament_id}"

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")

    next(ni for ni in at.number_input if ni.key == f"{prefix}_start_point").set_value(35000)  # 35,000点のつもりで実点数
    at.run()
    at.button[_find_button(at, "変更を保存")].click().run()

    assert any("100点単位で入力してください(例:45,800点なら458)" in e.value for e in at.error)
    assert db.get_tournament(tenant_id, tournament_id)["scoring_config"]["start_point"] == 35000  # 変わっていない


# ---------------------------------------------------------------------------
# 主催者画面を開いている間にゲストが送信したとき（10/2前の修正）
#
# 主催者画面の入力欄はStreamlitが前回の値を覚えているため、ゲストが送信した後に画面が
# 再表示されても、その卓の欄は古い値(未入力なら0)のまま残る。主催者はそれに気づけない。
# ---------------------------------------------------------------------------

GUEST_T1 = (45000, 35000, 32000, 28000)   # ゲストが送る卓1(合計140000)
ADMIN_T2 = (60000, 40000, 30000, 10000)   # 主催者が入れる卓2(合計140000)
STALE_TABLE_MESSAGE = "卓1は他の人が入力済みです。ページを再読み込みしてください"
CHANGED_JUST_BEFORE_SAVE_MESSAGE = "保存の直前に他の人がこの回戦の成績を入力しました"


def _open_admin(app_env_unused=None):
    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    return at


def test_stale_admin_screen_shows_reload_message_and_still_saves_other_tables(app_env):
    """主催者が画面を開いた後にゲストが卓1を送信。主催者はそのまま卓2を入力して保存する。
    卓1は「0の合計エラー」で保存全体を止めるのではなく、再読み込みを促して卓1だけ保存対象から外す。"""
    _, _, round_id, table1, table2 = _setup_two_table_round()
    at = _open_admin()
    db.submit_guest_round_results(round_id, dict(zip(table1, GUEST_T1)), {})  # 別の端末から

    _set_admin_scores(at, dict(zip(table2, ADMIN_T2)))
    assert any(STALE_TABLE_MESSAGE in w.value for w in at.warning)
    assert not any("卓1の合計が" in e.value for e in at.error)

    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    saved = db.get_round_results(round_id)
    assert [saved[m] for m in table1] == list(GUEST_T1)
    assert [saved[m] for m in table2] == list(ADMIN_T2)
    meta = db.get_round_result_meta(round_id)
    assert all(meta[m]["input_source"] == "guest" for m in table1)


def test_stale_admin_screen_cannot_overwrite_the_table_someone_else_entered(app_env):
    """古い画面のまま、主催者が卓1(ゲストが送信済み)に合計の合う別の数字を打ち込んでも上書きされない。"""
    _, _, round_id, table1, _ = _setup_two_table_round()
    at = _open_admin()
    db.submit_guest_round_results(round_id, dict(zip(table1, GUEST_T1)), {})

    _set_admin_scores(at, dict(zip(table1, ADMIN_T2)))
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    assert any(STALE_TABLE_MESSAGE in w.value for w in at.warning)
    assert [db.get_round_results(round_id)[m] for m in table1] == list(GUEST_T1)


def test_guest_submission_during_admin_save_is_not_lost(app_env, monkeypatch):
    """主催者の保存の再実行が画面を作り終えてから保存するまでの間に、ゲストが別の卓を送信した。"""
    _, _, round_id, table1, table2 = _setup_two_table_round()
    at = _open_admin()
    _set_admin_scores(at, dict(zip(table2, ADMIN_T2)))
    original_save = db.save_round_results

    def guest_slips_in(*args, **kwargs):
        db.submit_guest_round_results(round_id, dict(zip(table1, GUEST_T1)), {})
        return original_save(*args, **kwargs)

    monkeypatch.setattr(db, "save_round_results", guest_slips_in)
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    saved = db.get_round_results(round_id)
    assert [saved[m] for m in table1] == list(GUEST_T1)
    assert [saved[m] for m in table2] == list(ADMIN_T2)


def test_admin_save_stops_when_guest_submits_the_same_table_just_before(app_env, monkeypatch):
    """主催者が卓1を入力して保存する直前に、ゲストも卓1を送信した: 主催者の保存を止めて知らせる。"""
    _, _, round_id, table1, _ = _setup_two_table_round()
    at = _open_admin()
    _set_admin_scores(at, dict(zip(table1, ADMIN_T2)))
    original_save = db.save_round_results

    def guest_slips_in(*args, **kwargs):
        db.submit_guest_round_results(round_id, dict(zip(table1, GUEST_T1)), {})
        return original_save(*args, **kwargs)

    monkeypatch.setattr(db, "save_round_results", guest_slips_in)
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    assert any(CHANGED_JUST_BEFORE_SAVE_MESSAGE in e.value for e in at.error)
    assert [db.get_round_results(round_id)[m] for m in table1] == list(GUEST_T1)


def test_admin_save_keeps_guest_input_source_for_tables_it_did_not_change(app_env):
    """ゲスト入力済みの卓1を主催者が触らずに卓2だけ保存しても、卓1は「ゲストの入力」のまま残る。"""
    _, _, round_id, table1, table2 = _setup_two_table_round()
    db.submit_guest_round_results(round_id, dict(zip(table1, GUEST_T1)), {})
    before = db.get_round_result_meta(round_id)
    at = _open_admin()  # ゲスト送信の後に開いた画面(古くない)

    _set_admin_scores(at, dict(zip(table2, ADMIN_T2)))
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    after = db.get_round_result_meta(round_id)
    assert all(after[m]["input_source"] == "guest" for m in table1)
    assert all(after[m]["created_at"] == before[m]["created_at"] for m in table1)
    assert [db.get_round_results(round_id)[m] for m in table2] == list(ADMIN_T2)


def test_admin_can_still_correct_a_table_entered_by_a_guest(app_env):
    """画面を開く前にゲストが送信した卓を主催者が修正するのは、これまでどおりできる。"""
    _, _, round_id, table1, _ = _setup_two_table_round()
    db.submit_guest_round_results(round_id, dict(zip(table1, GUEST_T1)), {})
    at = _open_admin()

    corrected = dict(zip(table1, (46000, 34000, 32000, 28000)))
    _set_admin_scores(at, corrected)
    assert not any(STALE_TABLE_MESSAGE in w.value for w in at.warning)
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    assert {m: db.get_round_results(round_id)[m] for m in table1} == corrected
    assert all(db.get_round_result_meta(round_id)[m]["input_source"] == "admin" for m in table1)


def test_admin_screen_is_not_stale_after_its_own_save(app_env):
    """主催者自身の保存の後は「他の人が入力済み」扱いにならず、続けて修正・保存できる。"""
    _, _, round_id, table1, _ = _setup_two_table_round()
    at = _open_admin()
    _set_admin_scores(at, dict(zip(table1, ADMIN_T2)))
    at.button[_find_button(at, "成績を保存")].click().run()
    assert not any(STALE_TABLE_MESSAGE in w.value for w in at.warning)

    _set_admin_scores(at, {table1[0]: 61000, table1[1]: 39000})
    at.button[_find_button(at, "成績を保存")].click().run()

    assert not at.exception
    assert not any(STALE_TABLE_MESSAGE in w.value for w in at.warning)
    assert [db.get_round_results(round_id)[m] for m in table1] == [61000, 39000, 30000, 10000]
