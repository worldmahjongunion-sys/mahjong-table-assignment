# 麻雀卓組みアプリ

麻雀の大会で「どの半荘に、誰と誰が同じ卓に座るか」を決めるためのWebアプリです。
主催者が手作業で組んでいた卓組みを数秒で自動化し、大会当日の運営負担を減らすことを目的にしています。

## 何をするもの

- 参加メンバーを登録し、大会ごとに出欠を管理する
- 半荘ごとの卓組み（誰がどの卓の何家に座るか）を自動で決める
- 前の半荘の結果を踏まえて、次の半荘の組み合わせを決める

卓組みの方式は2つを想定しています。

| 方式 | 想定する場面 |
| --- | --- |
| 蛇行方式 | 成績順に並べ、蛇行させて卓を作る |
| ワンデー4半荘方式 | 1日完結の大会。全員が席順を一巡することを優先する |

## 技術構成

- Python / Streamlit
- SQLite（Railway の Volume に保存し、再デプロイでもデータが残る構成）
- Railway でホスティング（本番環境とは別に、検証用の staging 環境あり）

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `app.py` | 画面と操作。Streamlit のエントリーポイント |
| `db.py` | SQLite への読み書き |
| `migrate_to_auth.py` | 既存データをアカウント制に移行するためのスクリプト |
| `Procfile` | Railway での起動コマンド |
| `requirements.txt` | 依存パッケージ |
| `requirements-dev.txt` | 開発・テスト用の追加依存パッケージ（pytest） |
| `tests/` | pytestによる自動テスト |
| `.github/workflows/tests.yml` | push・PR時に自動テストを実行するGitHub Actions設定 |
| `.streamlit/` | Streamlit の設定 |

## ドキュメント

| ファイル | 内容 |
| --- | --- |
| `仕様書.md` | 機能とデータの仕様 |
| `基準.md` | 開発を進めるうえで守る決めごと |
| `開発ロードマップ.md` | フェーズごとの計画と進捗 |
| `Phase0テストケース.md` | 卓組みロジックの検証ケース（蛇行15件・ワンデー14件） |
| `リリース手順.md` | 作業ブランチ〜Railway本番デプロイまでの手順 |
| `コミットメッセージ規約.md` | コミットメッセージの型と、マージ/リベースの使い分け |
| `トラブル復旧ガイド.md` | revert/reset/reflog/stashの使い分けと、Railway本番のロールバック手順 |
| `レビューガイド.md` | レビューで見る観点、建設的コメントの型、main保護＋必須レビューが必要な理由 |

## ローカルで動かす

```bash
pip install -r requirements.txt
streamlit run app.py
```

ブラウザで `http://localhost:8501` が開きます。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

`main` / `staging` へのpushとPR作成時に、GitHub Actions（`.github/workflows/tests.yml`）で自動実行される。

## 開発状況

- 済：メンバー管理、ログイン（アカウント制）、Railway での公開
- 次：卓組みロジック本体（Phase 0）。仕様とテストケースは確定済み

## CI/CD

`main`へのマージをトリガーに、GitHub Actionsのテスト成功後、Railwayが自動でビルド・デプロイする。
