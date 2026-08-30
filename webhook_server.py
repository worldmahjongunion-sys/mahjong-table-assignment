"""
Stripe Webhookを受け取り、tenantsテーブルの課金状態
（plan / stripe_customer_id / stripe_subscription_id / stripe_subscription_status）
を同期する薄いサーバー。

StreamlitはHTTP Webhookを直接受けられないため、app.pyとは別プロセスとして
このFlaskサーバーを動かす。app.py側のCheckout成功時のリダイレクト処理
（?checkout=success）は「ユーザーへの即時フィードバック」用で、
こちらのWebhookが課金状態の正のソース（決済成功・失敗・解約の全て）を反映する。

----------------------------------------------------------------------
ローカルでのテスト手順（Stripe CLI を使用）
----------------------------------------------------------------------
1. 依存パッケージをインストール
       pip install -r requirements.txt

2. Stripe CLIでログイン（初回のみ）
       stripe login

3. このサーバーを起動する別ターミナルで、まず転送を開始し、
   表示される whsec_... をコピーする
       stripe listen --forward-to localhost:4242/stripe/webhook

4. 環境変数 STRIPE_WEBHOOK_SECRET に上記の値を設定してこのサーバーを起動
       STRIPE_WEBHOOK_SECRET=whsec_xxx python webhook_server.py
   （デフォルトで http://localhost:4242 で待ち受ける。WEBHOOK_PORT で変更可）

5. さらに別ターミナルでテストイベントを送る
       stripe trigger checkout.session.completed
       stripe trigger customer.subscription.updated
       stripe trigger customer.subscription.deleted
       stripe trigger invoice.payment_failed

   ※ checkout.session.completed はテナントを紐付けるための
     metadata.tenant_id を含まないため、`stripe trigger` 単体では
     tenants テーブルは更新されない（ログに「tenant_idがありません」と出るのが正常）。
     実際のアップグレード導線（app.py の「Proにアップグレード」ボタン）経由の
     決済で確認するか、`stripe trigger` の --add オプションでmetadataを付与すること。

   ※ 重要: `stripe login` でログインしたStripeアカウントと、
     STRIPE_SECRET_KEY（.streamlit/secrets.toml の stripe_secret_key）が
     属するアカウントが違うと、stripe listen が正しいイベントを転送してくれず、
     何もログに出ないまま静かに失敗する（Stripeダッシュボード上ではイベントは
     ちゃんと発生しているのに、ローカルには届かない）。
     アカウントが一致しているかは以下で確認できる：
         stripe config --list                                  # ログイン中のaccount_id
         python -c "import stripe; stripe.api_key='sk_test_...'; print(stripe.Account.retrieve().id)"
     一致しない場合は stripe listen に --api-key sk_test_... を付けて、
     STRIPE_SECRET_KEY と同じアカウントに強制的に接続すること。
----------------------------------------------------------------------
"""

import logging
import os
from datetime import datetime, timezone

import stripe
from flask import Flask, request

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook_server")

app = Flask(__name__)

db.init_db()

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
if not STRIPE_WEBHOOK_SECRET:
    raise RuntimeError(
        "環境変数 STRIPE_WEBHOOK_SECRET が設定されていません。"
        "`stripe listen` 実行時に表示される whsec_... の値を設定してください。"
        "（署名検証なしにWebhookを受け付けることはできません）"
    )

# active/trialing 以外（canceled, unpaid, incomplete_expired など）はFreeに戻す
_ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}


def _current_period_end_iso(subscription: dict) -> str | None:
    """現在の請求期間の終了日時をISO文字列で返す。

    このAPIバージョンでは current_period_end はSubscription直下ではなく
    items.data[0] に入っている（実際にテストアカウントのSubscriptionを
    取得して確認済み）。単一プラン・単一itemの前提でdata[0]を見る。
    """
    items = (subscription.get("items") or {}).get("data") or []
    if not items:
        return None
    ts = items[0].get("current_period_end")
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _sync_from_subscription(subscription: dict) -> None:
    tenant = db.get_tenant_by_stripe_customer_id(subscription["customer"])
    if tenant is None:
        logger.warning("未知のStripe顧客IDのためスキップします: customer=%s", subscription["customer"])
        return
    status = subscription["status"]
    plan = "pro" if status in _ACTIVE_SUBSCRIPTION_STATUSES else "free"
    db.update_tenant_plan(
        tenant["id"],
        plan,
        stripe_customer_id=subscription["customer"],
        stripe_subscription_id=subscription["id"],
        stripe_subscription_status=status,
        cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
        current_period_end=_current_period_end_iso(subscription),
    )
    logger.info(
        "tenant_id=%s plan=%s status=%s cancel_at_period_end=%s に更新しました",
        tenant["id"], plan, status, subscription.get("cancel_at_period_end"),
    )


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        logger.warning("不正なペイロードを受信しました。")
        return "invalid payload", 400
    except stripe.SignatureVerificationError:
        logger.warning("Webhook署名の検証に失敗しました。")
        return "invalid signature", 400

    event_type = event["type"]
    # StripeObjectは辞書ではないため（.get()等を呼ぶとAttributeErrorになる）、
    # 以降は素の辞書として扱えるようto_dict()で変換する。
    obj = event["data"]["object"].to_dict()
    logger.info("受信: %s (event_id=%s)", event_type, event["id"])

    if event_type == "checkout.session.completed":
        tenant_id = (obj.get("metadata") or {}).get("tenant_id")
        if tenant_id and obj.get("payment_status") == "paid":
            db.update_tenant_plan(
                int(tenant_id),
                "pro",
                stripe_customer_id=obj["customer"],
                stripe_subscription_id=obj.get("subscription"),
                stripe_subscription_status="active",
                cancel_at_period_end=False,
                current_period_end=None,
            )
            logger.info("tenant_id=%s をProプランに更新しました（決済完了）", tenant_id)
        else:
            logger.warning(
                "checkout.session.completedにtenant_idまたは支払い完了状態がありません: session=%s",
                obj.get("id"),
            )

    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        _sync_from_subscription(obj)

    elif event_type == "customer.subscription.deleted":
        tenant = db.get_tenant_by_stripe_customer_id(obj["customer"])
        if tenant is None:
            logger.warning("未知のStripe顧客IDのためスキップします: customer=%s", obj["customer"])
        else:
            db.update_tenant_plan(
                tenant["id"],
                "free",
                stripe_customer_id=obj["customer"],
                stripe_subscription_id=obj["id"],
                stripe_subscription_status="canceled",
                cancel_at_period_end=False,
                current_period_end=None,
            )
            logger.info("tenant_id=%s をFreeプランに戻しました（解約）", tenant["id"])

    elif event_type == "invoice.payment_failed":
        customer_id = obj.get("customer")
        tenant = db.get_tenant_by_stripe_customer_id(customer_id) if customer_id else None
        if tenant is None:
            logger.warning("未知のStripe顧客IDのためスキップします: customer=%s", customer_id)
        else:
            # Stripe側のリトライ・督促（dunning）が続く間はプランを維持し、
            # ステータスだけ past_due にする。実際の解約は customer.subscription.deleted で反映する。
            db.update_tenant_plan(
                tenant["id"],
                tenant["plan"],
                stripe_customer_id=customer_id,
                stripe_subscription_id=obj.get("subscription") or tenant["stripe_subscription_id"],
                stripe_subscription_status="past_due",
                cancel_at_period_end=tenant["stripe_cancel_at_period_end"],
                current_period_end=tenant["stripe_current_period_end"],
            )
            logger.warning("tenant_id=%s の課金に失敗しました（past_due）", tenant["id"])

    elif event_type in ("invoice.payment_succeeded", "invoice.paid"):
        customer_id = obj.get("customer")
        tenant = db.get_tenant_by_stripe_customer_id(customer_id) if customer_id else None
        if tenant is None:
            logger.warning("未知のStripe顧客IDのためスキップします: customer=%s", customer_id)
        else:
            db.update_tenant_plan(
                tenant["id"],
                "pro",
                stripe_customer_id=customer_id,
                stripe_subscription_id=obj.get("subscription") or tenant["stripe_subscription_id"],
                stripe_subscription_status="active",
                cancel_at_period_end=tenant["stripe_cancel_at_period_end"],
                current_period_end=tenant["stripe_current_period_end"],
            )
            logger.info("tenant_id=%s の課金が成功しました（active）", tenant["id"])

    else:
        logger.info("未対応のイベントタイプのため無視します: %s", event_type)

    return "", 200


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", "4242"))
    app.run(host="0.0.0.0", port=port)
