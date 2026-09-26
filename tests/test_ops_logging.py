"""運用ログ（ops_log.py・第53課題）と、失敗時の案内表示の検証。

会の最中に「止まったら一番困る所」（ゲストの成績送信・回戦の保存）で、
- 失敗しても英語のエラー画面を出さず、保存されていないこと・もう一度送ればよいことを伝える
- 決まった形のログ（イベント名 + 大会・回戦・卓の番号）が1行出る
- ログに氏名や点数が出ない
ことを確かめる。DBのロック待ち切れは、db の関数を sqlite3.OperationalError を
投げるものに差し替えて再現する（本物のロックは Docker の練習で確かめる）。
"""
import logging
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

import db
import ops_log
from test_tournament_ui import (
    APP_PATH,
    _find_button,
    _login,
    _setup_admin_with_tournament,
    _setup_tournament_with_round,
    app_env,  # noqa: F401  (pytest のフィクスチャとして使う)
)

EXACT_HUNDREDS = [700, 500, 250, -50]  # 100点単位で合計1400（開始点数35,000×4）
MEMBER_NAMES = ("太郎", "次郎", "三郎", "四郎")


@pytest.fixture
def ops_records(caplog):
    """mahjong_app ロガーの出力を集める（本番では標準出力に出るもの）。"""
    logger = logging.getLogger(ops_log.LOGGER_NAME)
    saved_propagate = logger.propagate
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    # 本番（ops_log.get_logger）と同じく上位に渡さない。渡すと caplog が root 側でも拾い、同じ行が2回数えられる
    logger.propagate = False
    yield caplog
    logger.removeHandler(caplog.handler)
    logger.propagate = saved_propagate


def _open_guest_and_fill_exact(raw_token):
    at = AppTest.from_file(APP_PATH)
    at.query_params["guest"] = raw_token
    at.run()
    score_inputs = [ni for ni in at.number_input if ni.key and ni.key.startswith("guest_score_")]
    for ni, v in zip(score_inputs, EXACT_HUNDREDS):
        ni.set_value(v)
    at.checkbox[0].check()
    at.run()
    return at


def _messages(records, event):
    return [r.getMessage() for r in records if r.getMessage().startswith(event + " ")]


def test_guest_submit_failure_shows_retry_message_and_saves_nothing(app_env, ops_records, monkeypatch):
    _, _, _, tournament_id, raw_token = _setup_tournament_with_round()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "submit_guest_round_results", locked)
    at = _open_guest_and_fill_exact(raw_token)
    at.button[_find_button(at, "この内容で送信する")].click().run()

    # 英語のエラー画面ではなく、案内文が出る
    assert not at.exception
    assert any("保存されていません" in e.value for e in at.error)
    round_id = db.get_rounds(tournament_id)[0]["id"]
    assert db.get_round_results(round_id) == {}

    failed = [r for r in ops_records.records if r.getMessage().startswith("guest_submit_failed ")]
    assert len(failed) == 1
    assert failed[0].levelno == logging.ERROR
    assert failed[0].getMessage() == (
        f"guest_submit_failed tournament={tournament_id} round=1 table=1 "
        "error=OperationalError: database is locked"
    )


def test_guest_can_resubmit_after_failure(app_env, ops_records, monkeypatch):
    """案内文どおり「もう一度送信」すれば保存される（失敗した送信は何も残していない）。"""
    _, _, _, tournament_id, raw_token = _setup_tournament_with_round()
    real_submit = db.submit_guest_round_results
    calls = {"n": 0}

    def locked_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(db, "submit_guest_round_results", locked_once)
    at = _open_guest_and_fill_exact(raw_token)
    at.button[_find_button(at, "この内容で送信する")].click().run()
    assert any("保存されていません" in e.value for e in at.error)

    at.button[_find_button(at, "この内容で送信する")].click().run()
    assert not at.exception
    round_id = db.get_rounds(tournament_id)[0]["id"]
    assert sorted(db.get_round_results(round_id).values(), reverse=True) == [70000, 50000, 25000, -5000]
    assert len(_messages(ops_records.records, "guest_submit_failed")) == 1
    assert _messages(ops_records.records, "guest_submit_ok") == [
        f"guest_submit_ok tournament={tournament_id} round=1 table=1"
    ]


def test_guest_submit_duplicate_is_logged_as_warning(app_env, ops_records, monkeypatch):
    _, _, _, tournament_id, raw_token = _setup_tournament_with_round()

    def already(round_id, *args, **kwargs):
        raise db.ResultsAlreadySubmittedError(round_id)

    monkeypatch.setattr(db, "submit_guest_round_results", already)
    at = _open_guest_and_fill_exact(raw_token)
    at.button[_find_button(at, "この内容で送信する")].click().run()

    assert not at.exception
    assert any("すでに入力済み" in w.value for w in at.warning)
    dup = [r for r in ops_records.records if r.getMessage().startswith("guest_submit_duplicate ")]
    assert len(dup) == 1 and dup[0].levelno == logging.WARNING


def test_ops_log_never_contains_member_names_or_scores(app_env, ops_records, monkeypatch):
    """成功・失敗どちらのログにも、氏名と点数が出ない。"""
    _, _, _, _, raw_token = _setup_tournament_with_round()
    real_submit = db.submit_guest_round_results
    calls = {"n": 0}

    def locked_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(db, "submit_guest_round_results", locked_once)
    at = _open_guest_and_fill_exact(raw_token)
    at.button[_find_button(at, "この内容で送信する")].click().run()
    at.button[_find_button(at, "この内容で送信する")].click().run()

    logged = "\n".join(r.getMessage() for r in ops_records.records)
    assert "guest_submit_ok" in logged and "guest_submit_failed" in logged
    for name in MEMBER_NAMES:
        assert name not in logged
    for points in ("70000", "50000", "25000", "-5000", "700", "500", "250", "-50"):
        assert f"={points}" not in logged and f" {points} " not in logged


def test_round_save_failure_keeps_preview_and_logs(app_env, ops_records, monkeypatch):
    _, _, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    import tournament_service
    monkeypatch.setattr(tournament_service, "save_confirmed_round", locked)
    at.button[_find_button(at, "この結果で保存")].click().run()

    assert not at.exception
    assert any("回戦結果を保存できませんでした" in e.value for e in at.error)
    assert db.count_rounds(tournament_id) == 0
    # プレビューが残っていて、もう一度「この結果で保存」を押せる
    assert any("プレビュー" in md.value for md in at.markdown)
    _find_button(at, "この結果で保存")
    assert _messages(ops_records.records, "round_save_failed") == [
        f"round_save_failed tournament={tournament_id} round=1 "
        "error=OperationalError: database is locked"
    ]


def test_round_save_success_is_logged(app_env, ops_records):
    _, _, member_ids, tournament_id = _setup_admin_with_tournament()
    for i, member_id in enumerate(member_ids, start=1):
        db.add_tournament_member(tournament_id, member_id, i)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _login(at, "admin1", "adminpass123")
    at.button[_find_button(at, "次の回戦の卓組みを実行")].click().run()
    at.button[_find_button(at, "この結果で保存")].click().run()

    assert not at.exception
    assert db.count_rounds(tournament_id) == 1
    assert _messages(ops_records.records, "round_save_ok") == [f"round_save_ok tournament={tournament_id} round=1"]


def test_ops_log_handler_is_attached_only_once():
    """Streamlit の再実行で get_logger() が何度呼ばれても、同じ行が重複して出ない。"""
    logger = logging.getLogger(ops_log.LOGGER_NAME)
    saved = list(logger.handlers)
    for h in saved:
        logger.removeHandler(h)
    try:
        for _ in range(3):
            ops_log.get_logger()
        assert len(logger.handlers) == 1
        assert logger.propagate is False
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
        for h in saved:
            logger.addHandler(h)
