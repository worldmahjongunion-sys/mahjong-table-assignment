"""
Stripeのテストモードに「Proプラン」の商品と価格を作成する一回限りのセットアップスクリプト。

使い方:
    STRIPE_SECRET_KEY=sk_test_... python3 stripe_setup.py

同じ lookup_key の価格が既にあれば作り直さず、その price_id を表示するだけなので
何度実行しても安全（冪等）。表示された price_id を、
.streamlit/secrets.toml の [auth] セクションの stripe_price_id_pro に設定する。
"""

import os
import sys

import stripe

PRICE_LOOKUP_KEY = "pro_monthly_jpy_980"
PRICE_AMOUNT_JPY = 980
PRODUCT_NAME = "麻雀卓組みアプリ Pro プラン"


def main() -> None:
    api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        print("環境変数 STRIPE_SECRET_KEY にStripeのテストモード秘密鍵（sk_test_...）を設定してください。")
        sys.exit(1)
    if not api_key.startswith("sk_test_"):
        print("警告: sk_test_ で始まらないキーです（本番キーの可能性があります）。")
        if input("それでも続行しますか？ [y/N]: ").strip().lower() not in ("y", "yes"):
            sys.exit(1)

    stripe.api_key = api_key

    existing = stripe.Price.list(lookup_keys=[PRICE_LOOKUP_KEY], active=True, limit=1)
    if existing.data:
        price = existing.data[0]
        print("既存の価格が見つかりました。作り直しません。")
        print(f"price_id: {price.id}")
        return

    product = stripe.Product.create(name=PRODUCT_NAME)
    price = stripe.Price.create(
        product=product.id,
        currency="jpy",
        unit_amount=PRICE_AMOUNT_JPY,
        recurring={"interval": "month"},
        lookup_key=PRICE_LOOKUP_KEY,
    )
    print("Stripeに商品と価格を作成しました。")
    print(f"price_id: {price.id}")
    print(
        "この値を .streamlit/secrets.toml の [auth] セクションの "
        "stripe_price_id_pro に設定してください。"
    )


if __name__ == "__main__":
    main()
