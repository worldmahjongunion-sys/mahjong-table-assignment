"""大会モニター画面（`?monitor=<トークン>`、TV表示用・閲覧専用。仕様書.md 3.9）。

入力部品は1つも置かず、画面全体を1つのHTMLとして描く。15秒ごとに、そのHTMLの中身だけを
入れ替える（ページの再読み込みはしない）。並び順・点数の計算は tournament_service のものを
そのまま使い、ここでは表示用に整形するだけ。DBへは書き込まない。
"""
import logging
import math
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape

import streamlit as st

import db
import scoring_logic
import tournament_service
from table_logic import position_sort_key

REFRESH_SECONDS = 15
INVALID_LINK_MESSAGE = "このリンクは使えません。主催者に最新のリンクを確認してください。"

# サーバー(Railway)の時計はUTC。Windowsにはtzdataがないことがあるのでzoneinfoは使わない
_JST = timezone(timedelta(hours=9))
_NO_VALUE = "—"
_LAST_SCREEN_KEY = "monitor_last_screen"

_logger = logging.getLogger(__name__)

# 文字の大きさは「行の高さ」と「欄の幅÷文字数(--len)」の小さい方。人数や名前の長さによらず、
# スクロールも文字切れもなしで1画面に収めるため（cqh/cqw＝その欄の高さ・幅に対する割合）。
STYLE = """<style>
html, body { overflow: hidden !important; background: #0b0f14 !important; }
.stApp, .stMain { overflow: hidden !important; background: #0b0f14 !important; }
header, footer, [data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"], [data-testid="stSidebar"] { display: none !important; }
/* 更新中に画面が薄くなる（ちらついて見える）のを止める */
[data-testid="stElementContainer"] { opacity: 1 !important; transition: none !important; }

.mon { position: fixed; inset: 0; z-index: 100; box-sizing: border-box; padding: 1.4vh 1.2vw;
  display: grid; grid-template-rows: 11vh minmax(0, 1fr) 3.6vh; gap: 1vh;
  background: #0b0f14; color: #fff; cursor: default; line-height: 1.12;
  font-family: "Yu Gothic UI", "Meiryo", "Noto Sans JP", sans-serif; font-weight: 700;
  font-variant-numeric: tabular-nums; }
.mon * { box-sizing: border-box; }
.mon .fit { container-type: size; min-width: 0; min-height: 0; display: flex; align-items: center; }
.mon .fit > span { white-space: pre; font-size: min(76cqh, calc(97cqw / var(--len))); }
.mon .right { justify-content: flex-end; }
.mon .center { justify-content: center; }

.mon-head { display: grid; grid-template-columns: minmax(0, 1fr) 30vw; gap: 2vw; min-height: 0;
  border-bottom: .3vh solid #3a4656; }
.mon-round { color: #ffd84d; }
.mon-body { display: grid; grid-template-columns: minmax(0, 52fr) minmax(0, 48fr); gap: 1.4vw;
  min-height: 0; }
.mon-panel { display: grid; grid-template-rows: 4.8vh minmax(0, 1fr); min-width: 0; min-height: 0; }
.mon-caption { color: #8fd3ff; font-size: 3.4vh; white-space: nowrap; align-self: center; }

.mon-tables { display: grid; gap: 1vh .8vw; min-height: 0; }
.mon-table { display: grid; grid-template-rows: minmax(0, 24fr) minmax(0, 76fr); min-height: 0;
  min-width: 0; border: .3vh solid #3a4656; border-radius: 1vh; background: #141b24; }
.mon-table-head { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  padding: 0 .8vw; min-height: 0; background: #22303f; }
.mon-done { color: #6dff9c; }
.mon-seats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-template-rows: repeat(2, minmax(0, 1fr)); min-height: 0; padding: 0 .5vw; }
.mon-seat { display: grid; grid-template-columns: 18% minmax(0, 1fr); min-width: 0; min-height: 0; }
.mon-wind { color: #ffd84d; }

.mon-list { display: grid; min-height: 0; }
.mon-row { display: grid; column-gap: .7vw; padding: 0 .5vw; min-height: 0; }
.mon-row:nth-child(even) { background: #19222d; }
.mon-standings .mon-row { grid-template-columns: 9fr 48fr 22fr 21fr; }
.mon-final .mon-row { grid-template-columns: 15fr 52fr 14fr 19fr; }
.mon-row > * { min-width: 0; }
.mon-colhead { color: #b9c6d6; border-bottom: .2vh solid #3a4656; }
.mon-colhead .fit > span { font-size: min(52cqh, calc(97cqw / var(--len))); }
.mon-value { color: #ffd84d; }
.mon-champion { color: #ffd84d; }

.mon-foot { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 2vw;
  min-height: 0; font-size: 2.4vh; color: #c5d0dc; white-space: nowrap; }
.mon-absent { color: #ffb86b; height: 100%; }
.mon .mon-absent > span { font-size: min(90cqh, calc(97cqw / var(--len))); }
.mon-message { display: flex; align-items: center; justify-content: center; text-align: center;
  font-size: 5vh; }
.mon-alert { color: #ff8f8f; }
</style>"""


def _display_length(text: str) -> float:
    """文字幅の目安（全角1文字＝1）。欄に収まる文字の大きさの計算に使う。"""
    total = 0.0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F", "A"):
            total += 1.0
        elif char in "MWmw@%":
            total += 0.95
        elif char.isupper():
            total += 0.75
        else:
            total += 0.62
    return max(total, 1.0)


def _fit(text, css_class: str = "", min_length: float = 1.0) -> str:
    text = str(text)
    length = max(_display_length(text), min_length)
    return f'<div class="fit {css_class}"><span style="--len:{length:.2f}">{escape(text)}</span></div>'


def load_monitor_data(tournament: dict) -> dict:
    """モニターに出す内容をDBから読み、表示用にまとめる。"""
    tournament_id = tournament["id"]
    members = db.get_tournament_members(tournament_id)
    names = {m["member_id"]: m["member_name"] for m in members}
    rounds = db.get_rounds(tournament_id)
    latest = rounds[-1] if rounds else None  # 回戦番号の昇順で返る

    tables, absent = [], []
    if latest:
        entered = db.get_round_results(latest["id"])
        by_table: dict[int, list] = {}
        for seat in db.get_round_seats(latest["id"]):
            by_table.setdefault(seat["table_number"], []).append(seat)
        for number, seats in sorted(by_table.items()):
            seats.sort(key=lambda s: position_sort_key(s["position"]))
            tables.append({
                "number": number,
                "seats": [(s["position"], names[s["member_id"]]) for s in seats],
                "entered": all(s["member_id"] in entered for s in seats),
            })
        absent = [names[mid] for mid in db.get_round_absences(latest["id"])]

    # 最終順位は最終回戦が最新の回戦のときだけ（その後に回戦が足されたら、そちらの卓組みを出す）
    final = None
    if latest and tournament_service.is_final_round(tournament, latest["round_number"]):
        final_rows = tournament_service.compute_final_ranking(tournament)
        if final_rows:
            final = [
                {
                    "rank": "優勝" if i == 0 else str(i + 1),
                    "name": names[row["member_id"]],
                    "table": f"卓{row['table_number']}",
                    "raw": scoring_logic.format_hundreds(row["raw_score"]),
                }
                for i, row in enumerate(final_rows)
            ]

    is_score_mode = tournament["scoring_mode"] == "得点"
    format_value = scoring_logic.format_hundreds if is_score_mode else scoring_logic.format_point_value
    ranked = tournament_service.compute_standings(tournament)
    # compute_standings の raw_total は得点方式だと None なので、両方式ともこちらから取る
    raw_sums = tournament_service._cumulative_raw_score_sum(tournament_id)

    def standings_row(rank, member_id, player_number, value):
        raw = raw_sums.get(player_number)
        return {
            "rank": rank,
            "name": names[member_id],
            "value": value,
            "raw": _NO_VALUE if raw is None else scoring_logic.format_hundreds(raw),
        }

    standings = [
        standings_row(str(i + 1), r["member_id"], r["player_number"], format_value(r["value"]))
        for i, r in enumerate(ranked)
    ]
    # 計算結果がまだない人は compute_standings に入らない。全員を常に出すため下に選手番号順で足す
    ranked_ids = {r["member_id"] for r in ranked}
    standings += [
        standings_row(_NO_VALUE, m["member_id"], m["player_number"], _NO_VALUE)
        for m in members
        if m["member_id"] not in ranked_ids
    ]

    return {
        "name": tournament["name"],
        "round_number": latest["round_number"] if latest else None,
        "tables": tables,
        "absent": absent,
        "final": final,
        "standings": standings,
        "value_label": "総合得点" if is_score_mode else "合計ポイント",
    }


def _tables_html(tables: list) -> str:
    if not tables:
        return '<div class="mon-message">卓組みを待っています</div>'
    columns = 1 if len(tables) <= 3 else 2
    rows = math.ceil(len(tables) / columns)
    # 氏名の大きさは全卓でそろえず、長い名前の人だけ小さくする（短い名前を巻き添えにしない）
    cards = "".join(
        '<div class="mon-table"><div class="mon-table-head">'
        + _fit(f"卓{table['number']}")
        + (_fit("入力済み", "right mon-done") if table["entered"] else "<div></div>")
        + '</div><div class="mon-seats">'
        + "".join(
            '<div class="mon-seat">'
            + _fit(position, "center mon-wind")
            + _fit(name, min_length=4)
            + "</div>"
            for position, name in table["seats"]
        )
        + "</div></div>"
        for table in tables
    )
    return (
        f'<div class="mon-tables" style="grid-template-columns:repeat({columns},minmax(0,1fr));'
        f'grid-template-rows:repeat({rows},minmax(0,1fr))">{cards}</div>'
    )


def _list_html(css_class: str, columns: list, rows: list) -> str:
    """順位の表。columns は (見出し, 行のキー, CSSクラス)。"""
    # 数字の列は、列の中で一番長い値に合わせて大きさをそろえる
    widths = {
        key: 4.0 if key == "name" else max([_display_length(str(r[key])) for r in rows] + [2.0])
        for _, key, _ in columns
    }
    header = '<div class="mon-row mon-colhead">' + "".join(
        _fit(label, css, min_length=2) for label, _, css in columns
    ) + "</div>"
    body = "".join(
        f'<div class="mon-row{" mon-champion" if row["rank"] == "優勝" else ""}">'
        + "".join(_fit(row[key], css, widths[key]) for _, key, css in columns)
        + "</div>"
        for row in rows
    )
    return (
        f'<div class="mon-list {css_class}" style="grid-template-rows:repeat({len(rows) + 1},minmax(0,1fr))">'
        f"{header}{body}</div>"
    )


def build_screen_html(data: dict, updated_at: str) -> str:
    number = data["round_number"]
    round_label = f"第{number}回戦" if number else "開始前"
    if data["final"]:
        left_title = "最終順位"
        left = _list_html(
            "mon-final",
            [("順位", "rank", "center"), ("氏名", "name", ""), ("卓", "table", "center"),
             ("素点", "raw", "right")],
            data["final"],
        )
    else:
        left_title = "卓組み"
        left = _tables_html(data["tables"])

    standings = _list_html(
        "mon-standings",
        [("順位", "rank", "center"), ("氏名", "name", ""), (data["value_label"], "value", "right mon-value"),
         ("素点合計", "raw", "right")],
        data["standings"],
    )
    absent = _fit(f"抜け番: {'、'.join(data['absent'])}", "mon-absent") if data["absent"] else "<div></div>"
    units = "総合得点・素点は100点単位" if data["value_label"] == "総合得点" else "素点は100点単位"
    return (
        '<div class="mon">'
        f'<div class="mon-head">{_fit(data["name"], min_length=8)}'
        f'{_fit(round_label, "right mon-round", min_length=4)}</div>'
        '<div class="mon-body">'
        f'<div class="mon-panel"><div class="mon-caption">{left_title}</div>{left}</div>'
        f'<div class="mon-panel"><div class="mon-caption">累計順位表</div>{standings}</div>'
        "</div>"
        f'<div class="mon-foot">{absent}'
        f"<span>{units}　最終更新 {updated_at}</span></div>"
        "</div>"
    )


def _message_html(message: str) -> str:
    return (
        '<div class="mon" style="grid-template-rows:minmax(0,1fr)">'
        f'<div class="mon-message">{escape(message)}</div></div>'
    )


@st.fragment(run_every=REFRESH_SECONDS)
def _render_screen(token: str) -> None:
    # トークンは毎回確かめる（開いたままのTVも、無効化・再発行したら次の更新で見られなくなる）
    try:
        tournament = db.get_tournament_by_guest_token(token) if token else None
        if tournament is None:
            st.session_state.pop(_LAST_SCREEN_KEY, None)
            screen = _message_html(INVALID_LINK_MESSAGE)
        else:
            updated_at = datetime.now(_JST).strftime("%H:%M:%S")
            screen = build_screen_html(load_monitor_data(tournament), updated_at)
            st.session_state[_LAST_SCREEN_KEY] = (token, screen)
    except Exception:
        # TVに英語のエラーを出さない。直前の画面を残して、次の回に読み直す（トークンはログに出さない）
        _logger.exception("モニター画面の更新に失敗")
        last_token, last_screen = st.session_state.get(_LAST_SCREEN_KEY, (None, None))
        if last_token == token:
            screen = last_screen.replace(
                "最終更新", '<b class="mon-alert">更新に失敗・再試行中</b>　最終更新'
            )
        else:
            screen = _message_html("読み込みに失敗しました。自動で読み直します。")
    st.html(screen)


def render_monitor_view(token: str) -> None:
    """モニター専用画面。呼び出し元でこの直後に必ず st.stop() すること（ゲスト画面と同じ）。"""
    st.html(STYLE)
    _render_screen(token)
