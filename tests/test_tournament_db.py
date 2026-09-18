"""大会管理とゲスト共有リンク実装依頼.md 4章のテスト観点のうち、db.py（DBアクセス層）
のCRUD操作を検証する。"""
import pytest

import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


@pytest.fixture
def tenant_and_members(temp_db):
    user_id = db.add_user("owner", "hash")
    tenant_id = db.get_user_by_username("owner")["tenant_id"]
    member_ids = [db.add_member(tenant_id, user_id, name, "") for name in ("Alice", "Bob", "Carol", "Dave")]
    return {"user_id": user_id, "tenant_id": tenant_id, "member_ids": member_ids}


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
POINT_CONFIG = {"rank_point_table": [3, 1, -1, -3]}


# ---------------------------------------------------------------------------
# tournaments
# ---------------------------------------------------------------------------

def test_create_and_get_tournament(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    tid = db.create_tournament(
        tenant_id, "第1回道場", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    tournament = db.get_tournament(tenant_id, tid)

    assert tournament["name"] == "第1回道場"
    assert tournament["status"] == "準備中"
    assert tournament["scoring_config"]["uma_table"][1] == [48000, -8000, -16000, -24000]
    # JSON往復後もintキーに戻っていること
    assert all(isinstance(k, int) for k in tournament["scoring_config"]["uma_table"])


def test_create_tournament_rejects_invalid_choice(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    with pytest.raises(ValueError):
        db.create_tournament(tenant_id, "不正", "月例", "蛇行", "得点", SCORE_CONFIG, "受付順")


def test_get_tournaments_is_scoped_to_tenant(temp_db):
    user_a = db.add_user("owner_a", "hash")
    tenant_a = db.get_user_by_username("owner_a")["tenant_id"]
    user_b = db.add_user("owner_b", "hash")
    tenant_b = db.get_user_by_username("owner_b")["tenant_id"]

    db.create_tournament(tenant_a, "Aの大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順")
    db.create_tournament(tenant_b, "Bの大会", "ワンデー", "ワンデー4半荘", "ポイント", POINT_CONFIG, "くじ引き")

    names_a = [t["name"] for t in db.get_tournaments(tenant_a)]
    assert names_a == ["Aの大会"]


def test_update_tournament(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    tid = db.create_tournament(
        tenant_id, "元の名前", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    db.update_tournament(
        tenant_id, tid, "新しい名前", "シーズン", "ワンデー4半荘", "ポイント",
        POINT_CONFIG, "くじ引き", "2026-01-01", "2026-12-31", "進行中",
    )
    tournament = db.get_tournament(tenant_id, tid)

    assert tournament["name"] == "新しい名前"
    assert tournament["scoring_mode"] == "ポイント"
    assert tournament["scoring_config"] == POINT_CONFIG
    assert tournament["status"] == "進行中"


def test_delete_tournament_cascades_related_records(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "削除対象", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tid, member_id, i)
    round_id = db.save_round(
        tid, 1, [dict(zip(("東", "南", "西", "北"), member_ids))], []
    )
    db.save_round_results(round_id, {mid: 30000 for mid in member_ids}, {})
    db.create_guest_link(tid, tenant_and_members["user_id"])

    db.delete_tournament(tenant_id, tid)

    assert db.get_tournament(tenant_id, tid) is None
    assert db.get_tournament_members(tid) == []
    assert db.get_rounds(tid) == []


# ---------------------------------------------------------------------------
# tournament members
# ---------------------------------------------------------------------------

def test_add_tournament_member_and_auto_numbering(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "受付順大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )

    for member_id in member_ids:
        pn = db.next_tournament_player_number(tid)
        db.add_tournament_member(tid, member_id, pn)

    members = db.get_tournament_members(tid)
    assert [m["player_number"] for m in members] == [1, 2, 3, 4]
    assert [m["member_name"] for m in members] == ["Alice", "Bob", "Carol", "Dave"]


def test_add_tournament_member_duplicate_player_number_raises(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "くじ引き大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "くじ引き"
    )
    db.add_tournament_member(tid, member_ids[0], 7)
    with pytest.raises(db.PlayerNumberTakenError):
        db.add_tournament_member(tid, member_ids[1], 7)


def test_add_tournament_member_same_member_twice_raises(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    db.add_tournament_member(tid, member_ids[0], 1)
    with pytest.raises(db.MemberAlreadyRegisteredError):
        db.add_tournament_member(tid, member_ids[0], 2)


# ---------------------------------------------------------------------------
# rounds / tables / seats / absences
# ---------------------------------------------------------------------------

def test_save_round_and_read_back(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    tables = [dict(zip(("東", "南", "西", "北"), member_ids))]
    round_id = db.save_round(tid, 1, tables, [])

    assert db.count_rounds(tid) == 1
    rounds = db.get_rounds(tid)
    assert rounds[0]["round_number"] == 1

    seats = db.get_round_seats(round_id)
    assert len(seats) == 4
    seat_map = {s["position"]: s["member_id"] for s in seats}
    assert seat_map["東"] == member_ids[0]
    assert seat_map["北"] == member_ids[3]
    assert db.get_round_absences(round_id) == []


def test_save_round_with_absences(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    round_id = db.save_round(tid, 1, [], [member_ids[0], member_ids[1]])
    assert set(db.get_round_absences(round_id)) == {member_ids[0], member_ids[1]}


def test_multiple_rounds_are_ordered_by_round_number(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    tables = [dict(zip(("東", "南", "西", "北"), member_ids))]
    db.save_round(tid, 1, tables, [])
    db.save_round(tid, 2, tables, [])
    rounds = db.get_rounds(tid)
    assert [r["round_number"] for r in rounds] == [1, 2]


# ---------------------------------------------------------------------------
# results / tobi_busters
# ---------------------------------------------------------------------------

def test_save_and_get_round_results(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    round_id = db.save_round(tid, 1, [dict(zip(("東", "南", "西", "北"), member_ids))], [])

    raw_scores = {member_ids[0]: 45000, member_ids[1]: -2000, member_ids[2]: 25000, member_ids[3]: 32000}
    tobi_busters = {member_ids[1]: [member_ids[0], member_ids[2]]}
    db.save_round_results(round_id, raw_scores, tobi_busters)

    assert db.get_round_results(round_id) == raw_scores
    assert db.get_round_tobi_busters(round_id) == tobi_busters


def test_save_round_results_overwrites_previous_entry(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    member_ids = tenant_and_members["member_ids"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    round_id = db.save_round(tid, 1, [dict(zip(("東", "南", "西", "北"), member_ids))], [])

    db.save_round_results(round_id, {member_ids[0]: 10000}, {})
    db.save_round_results(round_id, {member_ids[0]: 20000}, {})

    assert db.get_round_results(round_id) == {member_ids[0]: 20000}


# ---------------------------------------------------------------------------
# ゲスト共有リンク
# ---------------------------------------------------------------------------

def test_guest_link_token_resolves_to_tournament(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    user_id = tenant_and_members["user_id"]
    tid = db.create_tournament(
        tenant_id, "ゲスト大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    raw_token = db.create_guest_link(tid, user_id)

    resolved = db.get_tournament_by_guest_token(raw_token)
    assert resolved is not None
    assert resolved["id"] == tid


def test_guest_link_unknown_token_does_not_resolve(tenant_and_members):
    assert db.get_tournament_by_guest_token("no-such-token") is None


def test_guest_link_revoked_token_does_not_resolve(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    user_id = tenant_and_members["user_id"]
    tid = db.create_tournament(
        tenant_id, "ゲスト大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    raw_token = db.create_guest_link(tid, user_id)
    link = db.get_active_guest_link(tid)
    db.revoke_guest_link(link["id"])

    assert db.get_tournament_by_guest_token(raw_token) is None
    assert db.get_active_guest_link(tid) is None


def test_get_active_guest_link_returns_none_when_not_issued(tenant_and_members):
    tenant_id = tenant_and_members["tenant_id"]
    tid = db.create_tournament(
        tenant_id, "大会", "ワンデー", "蛇行", "得点", SCORE_CONFIG, "受付順"
    )
    assert db.get_active_guest_link(tid) is None
