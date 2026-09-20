"""
外部のアプリやAIが読み取るための REST API（読み取り専用）。

「窓口は、鍵をかけてから開ける」方針で、全エンドポイントを Bearer トークン認証で
保護している。トークンが正しくない・付いていないリクエストはデータに触れる前に
弾く（401）。トークン自体が未設定のときは、誰も通さない（503）。

総当たり対策として、認証に失敗したリクエストを呼び出し元IPごとに数え、
15分以内に10回失敗したIPは429で締め出す（既存のログインと同じ rate_limit_events を利用）。
ブロック中は鍵の正否を判定せず、正しい鍵でも429を返す。失敗とブロックは audit_logs に
記録するが、送られてきたトークン自体は記録しない。

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


# 既存のレート制限にはブロック解除までの残り時間を取る仕組みが無いため、
# Retry-After は窓の長さ（15分）で固定にする。
_RETRY_AFTER_SECONDS = db.API_AUTH_FAILURE_RATE_LIMIT_WINDOW_MINUTES * 60


def _unauthorized():
    resp = jsonify({"error": "unauthorized"})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = "Bearer"
    return resp


def _too_many_requests():
    resp = jsonify({"error": "too_many_requests"})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(_RETRY_AFTER_SECONDS)
    return resp


def _client_ip() -> str:
    # 本番(Railway)ではリバースプロキシ経由になり remote_addr がプロキシのIPに
    # なるため、X-Forwarded-For の一番左を使う。付いていなければ remote_addr。
    forwarded = request.headers.get("X-Forwarded-For", "")
    first = forwarded.split(",")[0].strip()
    return first or request.remote_addr or "unknown"


def _audit_tenant_id() -> int | None:
    tenant_id = os.environ.get("API_TENANT_ID", "")
    return int(tenant_id) if tenant_id.isdigit() else None


@app.before_request
def require_token():
    expected = os.environ.get("API_TOKEN")
    if not expected:
        # 鍵が決まっていない状態で窓口だけ開くことはしない（fail closed）。
        # 設定ミスなので、レート制限のカウント対象にはしない。
        resp = jsonify({"error": "api_not_configured"})
        resp.status_code = 503
        return resp

    ip = _client_ip()
    bucket = f"api_auth_failure:{ip}"

    # ブロック判定は鍵の検証より前。先に検証すると、ブロック中でも
    # 「正しい鍵かどうか」がレスポンスの違いから漏れてしまう。
    if db.is_rate_limited(
        bucket,
        db.API_AUTH_FAILURE_RATE_LIMIT_MAX_ATTEMPTS,
        db.API_AUTH_FAILURE_RATE_LIMIT_WINDOW_MINUTES,
    ):
        db.record_audit_log(
            action="api_rate_limited",
            tenant_id=_audit_tenant_id(),
            detail=f"ip={ip}, path={request.path}",
        )
        return _too_many_requests()

    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    # 通常の == だと、一致する先頭文字数の違いで応答時間が変わり、
    # 鍵を1文字ずつ推測される恐れがある。一定時間で比較する。
    if (
        scheme.lower() == "bearer"
        and presented
        and hmac.compare_digest(presented.encode(), expected.encode())
    ):
        return None

    # 送られてきたトークン自体は記録しない（ログから鍵が漏れるのを防ぐ）。
    # 成功時にカウンタをリセットしない点は、既存のログインの実装に合わせている。
    db.record_rate_limit_event(bucket)
    db.record_audit_log(
        action="api_auth_failed",
        tenant_id=_audit_tenant_id(),
        detail=f"ip={ip}, path={request.path}",
    )
    return _unauthorized()


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
