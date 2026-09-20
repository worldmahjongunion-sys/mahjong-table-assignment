"""
外部のアプリやAIが読み取るための REST API（読み取り専用）。

「窓口は、鍵をかけてから開ける」方針で、全エンドポイントを Bearer トークン認証で
保護している。トークンが正しくない・付いていないリクエストはデータに触れる前に
弾く（401）。トークン自体が未設定のときは、誰も通さない（503）。

エンドポイント:
    GET /api/tournaments   大会の一覧（JSON）

環境変数:
    API_TOKEN      合鍵になる長いランダム文字列（必須）。未設定なら全リクエストを拒否する。
    API_TENANT_ID  このトークンで読める団体（tenant）のID（必須）。
                   トークンごとに見える範囲を1団体に限定し、他団体のデータを返さない。
    API_PORT       待ち受けポート（デフォルト 8082）

トークンの作り方:
    python -c "import secrets; print(secrets.token_urlsafe(32))"

呼び出し例:
    curl -H "Authorization: Bearer <トークン>" http://localhost:8082/api/tournaments
"""

import hmac
import os

from flask import Flask, jsonify, request

import db

app = Flask(__name__)
# 日本語をエスケープ（\uXXXX形式）せず、そのまま読める形で返す
app.json.ensure_ascii = False

db.init_db()

# 外部に返してよい項目だけを明示的に選ぶ（許可リスト方式）。
# tenant_id / scoring_config（評価方式の詳細設定）は返さない。
_PUBLIC_TOURNAMENT_FIELDS = (
    "id",
    "name",
    "kind",
    "table_method",
    "scoring_mode",
    "status",
    "start_date",
    "end_date",
)


def _unauthorized():
    resp = jsonify({"error": "unauthorized"})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = "Bearer"
    return resp


@app.before_request
def require_token():
    expected = os.environ.get("API_TOKEN")
    if not expected:
        # 鍵が決まっていない状態で窓口だけ開くことはしない（fail closed）。
        resp = jsonify({"error": "api_not_configured"})
        resp.status_code = 503
        return resp

    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return _unauthorized()

    # 通常の == だと、一致する先頭文字数の違いで応答時間が変わり、
    # 鍵を1文字ずつ推測される恐れがある。一定時間で比較する。
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        return _unauthorized()
    return None


@app.route("/api/tournaments", methods=["GET"])
def list_tournaments():
    tenant_id = os.environ.get("API_TENANT_ID", "")
    if not tenant_id.isdigit():
        resp = jsonify({"error": "api_not_configured"})
        resp.status_code = 503
        return resp

    tournaments = db.get_tournaments(int(tenant_id))
    return jsonify(
        {
            "tournaments": [
                {field: t[field] for field in _PUBLIC_TOURNAMENT_FIELDS}
                for t in tournaments
            ]
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "8082"))
    app.run(host="0.0.0.0", port=port)
