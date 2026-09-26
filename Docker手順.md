# Docker手順（手元のPCでアプリを箱ごと動かす）

作成: 2026-09-26（第52課題）

Docker は「アプリ＋Python＋部品」をまとめて1つの箱（イメージ）にし、どのPCでも同じように動かすための道具です。
本番（Railway）は今も Procfile で動いています。**この Dockerfile は手元での確認用で、10/2 が終わるまで main には入れません**（下の「Railway に載せるときの注意」参照）。

## 用意するもの
- Docker Desktop（起動して左下が「Engine running」になっていること）
- `.env`（`.env.example` をコピーして値を入れる。GitHub にも箱にも入らない）

## 1. 箱の中でテストを全部流す
```
docker build --target test -t mahjong-app-test .
```
作る途中で `pytest` が走り、1つでも失敗すると箱が作られずに止まります。
（2026-09-26 時点: Python 3.13 で 282 passed）

## 2. 本番用の箱を作って起動する
```
docker build -t mahjong-app .
docker run -d --name mahjong-app --env-file .env -p 8501:8501 -v mahjong-data:/data mahjong-app
```
ブラウザで http://localhost:8501 を開くとログイン画面が出ます。

- `-v mahjong-data:/data` … DB（`/data/mahjong.db`）を箱の外に置く。箱を消して作り直しても成績は残る
- `docker ps` の STATUS が `(healthy)` なら、アプリが応答している

止める・消す:
```
docker stop mahjong-app
docker rm mahjong-app
```
DB ごと消したいときだけ `docker volume rm mahjong-data`（元に戻せないので注意）。

## このPCでの注意: Norton が HTTPS を検査している
このPCでは Norton の Web/Mail Shield が HTTPS 通信を検査しているため、箱の中の pip が
`CERTIFICATE_VERIFY_FAILED` で止まります。Norton のルート証明書を、作るときだけ渡します:
```
docker build --secret id=extra_ca,src=%USERPROFILE%\local-only-data\docker\norton_root_ca.pem --target test -t mahjong-app-test .
docker build --secret id=extra_ca,src=%USERPROFILE%\local-only-data\docker\norton_root_ca.pem -t mahjong-app .
```
- 証明書は pip のインストール中だけ使われ、箱には残らない
- 渡さなければ何もしない（他のPCや Railway は普通に作れる）
- 証明書ファイルは Windows の証明書ストアから書き出したもの（公開証明書で秘密情報ではないが、PC固有なのでリポジトリには入れない）

## Railway に載せるときの注意（10/2 の後に検討）
- リポジトリのルートに Dockerfile があると、Railway は Railpack ではなく Dockerfile で作るようになる。Procfile は使われず、起動は Dockerfile の `CMD` になる
- Dockerfile の最後のステージ（runtime）が本番の箱になる。テスト用ステージを最後に置かないこと
- Dockerfile には `VOLUME` 命令を書いていない（Railway では使われないため）。ボリュームは Railway の画面で `/data` に付ける
- 箱は一般ユーザー（app）で動くが、Railway のボリュームは root の持ち物で付くため `/data` に書けない。サービスの変数に `RAILWAY_RUN_UID=0` を足すこと
