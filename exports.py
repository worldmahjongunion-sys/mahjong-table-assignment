"""メンバー一覧をCSV/Excel形式のバイト列に変換する（Proプラン限定のエクスポート機能で使用）。"""

import csv
import io

from openpyxl import Workbook

_HEADERS = ["名前", "メモ", "登録日時", "状態"]


def _member_row(member: dict) -> list[str]:
    status = "在籍" if member["is_active"] else "引退"
    return [member["name"], member["memo"] or "", member["created_at"], status]


def build_members_csv(members: list[dict]) -> bytes:
    # Excelで開いたときに文字化けしないよう、BOM付きUTF-8にする
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_HEADERS)
    for member in members:
        writer.writerow(_member_row(member))
    return buffer.getvalue().encode("utf-8-sig")


def build_members_xlsx(members: list[dict]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "メンバー一覧"
    sheet.append(_HEADERS)
    for member in members:
        sheet.append(_member_row(member))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
