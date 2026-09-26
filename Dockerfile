# 麻雀卓組みアプリを「動く環境ごと」箱（コンテナ）に詰めるための設計図。
# 使い方は Docker手順.md を参照。
#
#   本番用の箱を作る:   docker build -t mahjong-app .
#   箱の中でテストする: docker build --target test -t mahjong-app-test .
#
# ステージは base → test → runtime の順。いちばん最後のステージ（runtime）が
# 「何も指定しないときに作られる箱」になるので、本番用を必ず最後に置く。
# （Railway など --target を指定しない環境で、テスト用の箱が本番として動くのを防ぐため）

# ---- base: アプリを動かすのに必要なものだけ入った箱（本番と同じ中身） ----
# 本番（Railway）と CI（.github/workflows/tests.yml）と同じ Python 3.13 に揃える
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # SQLiteのDBは箱の外（ボリューム）に置く。箱を作り直しても成績が消えないように。
    # Railwayの本番（/data/）と同じ場所にしてある
    DB_PATH=/data/mahjong.db \
    PORT=8501 \
    WEBHOOK_PORT=8081

WORKDIR /app

# 部品（requirements.txt）を先に入れる。コードだけ変えたときは、この層が使い回されて速く作れる
#
# extra_ca: ウイルス対策ソフト（例: Norton の Web/Mail Shield）が HTTPS を検査していて
# pip が「証明書を確認できない」で止まるPC向けの逃げ道。そのソフトのルート証明書を
#   docker build --secret id=extra_ca,src=<証明書.pem> ...
# で渡したときだけ、pip のインストール中に一時的に信頼する。箱（イメージ）には残らない。
# 渡さなければ何もしない（Railway や他のPCでは普通に作られる）
COPY requirements.txt .
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then \
      cat "$(python -c 'import pip._vendor.certifi as c; print(c.where())')" /run/secrets/extra_ca > /tmp/ca.pem \
      && export PIP_CERT=/tmp/ca.pem; \
    fi \
    && pip install -r requirements.txt \
    && rm -f /tmp/ca.pem

COPY . .

# root（管理者）ではなく一般ユーザーで動かす。
# 注意: Railway のボリュームは root の持ち物として付くので、このまま Railway で使うと
# /data に書き込めない。Railway で Dockerfile を使う日が来たら、サービスの変数に
# RAILWAY_RUN_UID=0 を足すこと（10/2 までは Railway にこの Dockerfile は載せない）
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /data
USER app


# ---- test: baseにpytestとテストを足した箱。作る途中でテストを全部流す ----
FROM base AS test
USER root
COPY requirements-dev.txt .
# extra_ca の意味は base の pip install と同じ
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then \
      cat "$(python -c 'import pip._vendor.certifi as c; print(c.where())')" /run/secrets/extra_ca > /tmp/ca.pem \
      && export PIP_CERT=/tmp/ca.pem; \
    fi \
    && pip install -r requirements-dev.txt \
    && rm -f /tmp/ca.pem
USER app
# -p no:cacheprovider: /app は root の持ち物なので、pytest のキャッシュ（.pytest_cache）を書かせない
RUN python -m pytest -q -p no:cacheprovider


# ---- runtime: 本番用の箱（最後に置く＝何も指定しないときはこれが作られる） ----
FROM base AS runtime

# DB の置き場 /data には、起動するときにボリュームを付ける（VOLUME 命令は書かない）。
#   手元: docker run -v mahjong-data:/data ...   Railway: サービスの画面で /data にボリュームを付ける
EXPOSE 8501

# 箱の中のアプリが応答しているかをDocker自身が定期的に確かめる（docker ps の STATUS に出る）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/_stcore/health', timeout=4)"

# Procfile（Railway）と同じ起動の流れ: DBの表を用意 → webhookを裏で起動 → Streamlitを起動
# 注意: ルートに Dockerfile があると Railway は Procfile ではなくこの CMD で起動する
CMD ["sh", "-c", "python -c 'import db; db.init_db()' && (python webhook_server.py &) && exec streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true"]
