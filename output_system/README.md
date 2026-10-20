# 会議通訳アプリ（output_system）

英語の会議を日本語の字幕で追い、会議後に要約と質問を得るための Web アプリです。要件と基本設計は、リポジトリ直下の `ai_generated/` にあります。

## 配置

| フォルダ | 中身 |
|---|---|
| `backend/` | アプリサーバー。Python 3.12 と FastAPI で作ります。ロックファイルは `uv.lock` です |
| `frontend/` | 画面。TypeScript と React を Vite でビルドします。ロックファイルは `package-lock.json` です |
| `config/` | アプリに同梱する設定ファイル |
| `tools/` | 測定などの補助の道具 |
| `docs/` | 実装に付ける文書 |

テストは、各フォルダの `src/` と同じ階層の `test/` に置き、`src/` と同じ構造にします。

## 画面の URL

1 つのページの中で、4 つの画面を切り替えます。URL で画面を指せるため、再読み込みしても同じ画面が開きます。URL の形は `frontend/src/routes.ts` で定義します。

| 画面 | URL |
|---|---|
| 会議の一覧（S-01） | `/` |
| 会議の準備（S-02） | `/meetings/new` |
| 会議中（S-03） | `/meetings/<会議番号>/live` |
| 会議の記録（S-04） | `/meetings/<会議番号>` |

どの画面にも当たらない URL は、会議の一覧へ移ります。

## アプリサーバーの経路と設定

アプリサーバーは、1 つのポート（既定は 3001）で、画面のファイル・HTTP・WebSocket を受けます。経路は次の 3 つに分けます。

| 経路 | 役割 |
|---|---|
| `/api` 以下 | HTTP（JSON）。動作の確認は `GET /api/health` です。存在しない経路は、画面のファイルではなく、404 の JSON のエラーを返します |
| `/ws` 以下 | WebSocket。存在しない経路は、HTTP には 404 の JSON を返し、WebSocket の接続は閉じます |
| それ以外 | 画面のファイル。画面の URL にはトップのファイル（`index.html`）を返します。拡張子を持つ名前（`/assets/app.js` など）が存在しなければ 404 です。そのため、会議番号に「.」を使いません |

ミドルウェアと経路の登録は、`backend/src/meeting_interpreter/app.py` の `create_app` にまとめます。

動かす場所で変わる値は、環境変数で渡します。コンテナでは `docker-compose.yml` で指定します。

| 環境変数 | 意味 | 既定値 |
|---|---|---|
| `MMD_PORT` | 待ち受けるポート | `3001` |
| `MMD_APP_HOSTNAME` | 要求の宛先として常に通す、アプリのコンテナ名 | 未設定 |
| `MMD_RECORDS_DIR` | 記録データベースを置く場所 | `/var/lib/mmd/records` |
| `MMD_MODELS_DIR` | AI モデルを置く場所 | `/var/lib/mmd/models` |
| `MMD_STATIC_DIR` | ビルドした画面のファイルの場所 | `/app/static` |

空の環境変数は、未設定として扱います。

### コンテナを使わずにアプリサーバーを動かす（開発用）

先に `frontend/` で `npm run build` を実行してから、`backend/` で次を実行します。

```bash
MMD_STATIC_DIR=../frontend/dist PYTHONPATH=src uv run --no-dev python -m meeting_interpreter
```

## 必要な道具

| 道具 | 版 | 用途 |
|---|---|---|
| Python | 3.12 系（Ubuntu 24.04 の標準） | アプリサーバーの実行 |
| uv | 0.12 以降 | Python の依存の導入とロック |
| Node.js | 24 系（LTS） | 画面のビルドとテスト |
| npm | 11.10 以上（Node.js 24 系に同梱） | 画面の依存の導入とロック |
| Docker と Docker Compose | 最新 | イメージのビルドとコンテナの起動 |

## コマンド

### アプリサーバー（`backend/` で実行）

```bash
uv sync --locked          # 依存を入れる（ロックファイルの版をそのまま入れる）
uv run pytest             # テストとカバレッジ
uv run ruff check .       # lint
uv run ruff format --check .  # 書式の確認
```

### 画面（`frontend/` で実行）

```bash
npm ci                    # 依存を入れる（ロックファイルの版をそのまま入れる）
npm test                  # テスト（Vitest）
npm run test:coverage     # カバレッジ付きのテスト
npm run lint              # lint（ESLint）
npm run typecheck         # 型チェック
npm run build             # 本番用のビルド（dist/ に出る）
```

## 依存の追加と更新

依存の版は、ロックファイルで固定します。追加と更新は、次のとおり行います。

- Python: `backend/` で `uv add <パッケージ>` を実行し、`uv.lock` も一緒にコミットします
- 画面: `frontend/` で `npm install <パッケージ>` を実行し、`package-lock.json` も一緒にコミットします
- 依存は公式の配布元（PyPI と npm）から入手します。既知の脆弱性がある版は選びません

### 公開から 7 日未満の版を使わない設定

サプライチェーン攻撃は、公開から 1 週間以内に見つかって取り下げられることが大半です。そのため、版を新しく決めるときは、公開から 7 日未満の版を対象にしません。

| 道具 | 設定 | 置き場所 |
|---|---|---|
| uv | `exclude-newer = "7 days"` | `backend/pyproject.toml` の `[tool.uv]` |
| npm | `min-release-age=7` | `frontend/.npmrc` |

- どちらの設定も、ロックファイルに記録済みの版には影響しません。版が新しく決まる `uv add`・`uv lock`・`npm install` のときに効きます。
- `min-release-age` は npm 11.10 以上で効きます。古い npm は、この設定を無視します。そのため `frontend/package.json` の `engines` で npm 11.10 以上を求めています。
- すべての版が 7 日未満の新しいパッケージは、導入できません。その場合は、設定を外さずに、7 日たつのを待つか、直前の版を選びます。
- イメージのビルドでは `npm ci` と `uv sync --locked` を使い、ロックファイルの版だけを入れます。

## コンテナの起動

コンテナは 1 つです。イメージの土台は `ubuntu:24.04` で、`Dockerfile` が、画面のビルド・サーバーの依存の導入・実行の 3 つの段に分けて作ります。最終のイメージには、Node.js・uv・pip を残しません。

`output_system/` で実行します。

```bash
docker compose build      # イメージを作る
docker compose up -d      # コンテナを起動する
docker compose ps         # 状態を見る。STATUS が healthy なら、動作の確認（GET /api/health）に成功している
docker compose down       # コンテナを止めて消す（名前付きボリュームは残る）
```

| 項目 | 値 |
|---|---|
| コンテナ名 | `toshiakimamada-blip-mmd-output-system` |
| ネットワーク | `toshiakimamada-blip-mmd-network`（既存のネットワークに参加する。`external: true`） |
| 利用者が開く URL | `http://localhost:3001` |
| 開発用のコンテナから開く URL | `http://toshiakimamada-blip-mmd-output-system:3001` |
| ポートの公開 | `127.0.0.1:3001` だけ。ほかの PC からは届かない |
| 再起動 | `restart: unless-stopped`。異常終了したときと、Docker の再起動のあとに、立ち上げ直す |
| 名前付きボリューム | 記録データベース用 `toshiakimamada-blip-mmd-records`（コンテナの中は `/var/lib/mmd/records`）、モデル置き場用 `toshiakimamada-blip-mmd-models`（同 `/var/lib/mmd/models`） |

- ポート 3002 と、開発環境が割り当てるもう 1 つのコンテナ名は使いません。
- PC やほかのコンテナのフォルダーをコンテナへつなぎません（バインドマウントを使いません）。
- `docker compose down -v` は、名前付きボリュームも消します。記録と AI モデルが消えるため、使いません。
- イメージを作り直してコンテナを入れ替えるときも、ボリュームは残ります。

### 社内の証明書

社内のネットワークが通信を検査する場合は、証明書をイメージのビルドで登録します。証明書がある環境と無い環境で、同じ手順でビルドできます。

- 既定では、リポジトリ直下の `ssl-certificates/` の `.cer`・`.crt`・`.pem` を登録します。PEM 形式と DER 形式の両方に対応します。フォルダーが空なら、何も登録しません。
- このフォルダーが存在しない環境では、空のフォルダーを作り、`SSL_CERTS_DIR=<空のフォルダー> docker compose build` でビルドします。
- `docker compose` を使わずにビルドする場合は、証明書がある環境で `--build-context certs=<証明書のフォルダー>` を付けます。

### イメージの版の固定

- Node.js は、`Dockerfile` の `NODE_VERSION` と、アーカイブの SHA-256（`NODE_SHA256_X64` と `NODE_SHA256_ARM64`）で固定します。版を上げるときは、公開から 7 日以上たった版を選び、配布元の `SHASUMS256.txt` の値へ更新します。
- uv は、`Dockerfile` の `UV_VERSION` で固定します。
- 画面の依存は `npm ci`、サーバーの依存は `uv sync --locked` で、ロックファイルの版だけを入れます。
