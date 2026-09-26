"""運用ログ（第53課題: 監視と運用）。

会の最中に「止まったら一番困る所」（ゲストの成績送信・回戦の卓組み）で、
決まった形のログを1行ずつ標準出力に出す。Railway の Logs 画面で
イベント名（例: guest_submit_failed）を検索すれば、いつ・どの大会の何回戦の
何卓で何が起きたかを後から追える。

    2026-10-02 05:03:11 ERROR guest_submit_failed tournament=3 round=5 table=2 error=OperationalError: database is locked

ログに出すのは大会・回戦・卓の番号とエラーの種類だけ。氏名や点数は出さない
（ログは Railway 側に残り、アプリの権限管理の外に出るため）。
時刻はサーバーの時計（Railway・Docker では UTC）。
"""
import logging
import sys

LOGGER_NAME = "mahjong_app"


def get_logger() -> logging.Logger:
    # Streamlit は画面操作のたびに app.py を実行し直すが、このモジュールは1回しか
    # 読み込まれない。それでも念のため、ハンドラーが二重に付いて同じ行が
    # 何度も出ることがないよう、付いていないときだけ付ける
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Streamlit 自身のログ設定と混ざって同じ行が2回出ないよう、上位には渡さない
        logger.propagate = False
    return logger


def log_event(event: str, level: int = logging.INFO, exc_info: bool = False, **fields) -> None:
    """event（英小文字のイベント名）と key=value を1行で出す。"""
    parts = [event] + [f"{key}={value}" for key, value in fields.items()]
    get_logger().log(level, " ".join(parts), exc_info=exc_info)


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
