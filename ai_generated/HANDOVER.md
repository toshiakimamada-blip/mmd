# HANDOVER

## 技術スタック
- アプリサーバー: Python 3.12（Ubuntu 24.04 標準）、FastAPI、uvicorn、websockets（WebSocket の部品）。依存は uv（`backend/uv.lock`）で固定
- 画面: TypeScript、React 19、react-router 8（データ経路）、Vite 8。依存は npm（`frontend/package-lock.json`）で固定
- テスト: pytest（+ pytest-cov、httpx2。本物の uvicorn を起動するテストを含む）、Vitest 5（+ Testing Library、jsdom）。lint は ruff と ESLint 10
- 実行基盤: Docker コンテナ 1 つ（土台 ubuntu:24.04）。名前付きボリューム 2 つ（記録・モデル）
- クールダウン: uv は `exclude-newer = "7 days"`（`backend/pyproject.toml`）、npm は `min-release-age=7`（`frontend/.npmrc`）

## ディレクトリ構成
```
output_system/
├── Dockerfile, docker-compose.yml, .dockerignore
├── README.md                 # 配置・コマンド・環境変数・コンテナの扱い
├── backend/
│   ├── pyproject.toml, uv.lock
│   ├── src/meeting_interpreter/
│   │   ├── app.py            # create_app: ミドルウェアと経路の登録を 1 か所にまとめる
│   │   ├── frontend.py       # 配信部（SPA の配信）
│   │   ├── settings.py       # 環境変数 MMD_* の読み込み
│   │   ├── errors.py         # 画面へ返すエラーの共通の形
│   │   ├── healthcheck.py    # HEALTHCHECK 用
│   │   └── api/health.py     # GET /api/health
│   └── test/                 # src と同じ構造。test/container/ は起動したコンテナを相手にする（MMD_CONTAINER_URL のときだけ動く）
├── frontend/
│   ├── src/{routes.ts,router.tsx,components/AppFrame.tsx,screens/*,styles/frame.css}
│   └── test/                 # src と同じ構造
├── tools/with-pinned-node.sh # Dockerfile と同じ版の Node.js で、渡されたコマンド（npm install など）を実行する
├── config/ docs/             # 後続の作業で中身を足す（README のみ）
```

## ビルド・起動方法
- `output_system/` で `docker compose build` → `docker compose up -d`。`docker compose ps` が healthy ならよい
- 開発用コンテナから開く URL: `http://toshiakimamada-blip-mmd-output-system:3001`（`localhost` は使わない）
- テスト: `backend/` で `uv sync --locked && uv run pytest`、`frontend/` で `npm ci && npm test`。`npm ci` と `npm install` は Node 24 / npm 11.10 以上が要る。開発用コンテナ（Node 22 / npm 10）では `../tools/with-pinned-node.sh npm ci` のように付けて実行する
- 出荷するコンテナの WebSocket の確認: `docker compose up -d --build` のあと、`backend/` で `MMD_CONTAINER_URL=http://toshiakimamada-blip-mmd-output-system:3001 uv run pytest test/container --no-cov`
- 社内の証明書: 既定でリポジトリ直下の `ssl-certificates/` を登録する。フォルダーが無い環境は `SSL_CERTS_DIR=<空のフォルダー>` を付ける

## 設計判断
- 経路の接頭辞を `/api`（HTTP）と `/ws`（WebSocket）に分け、それ以外は画面のファイル。存在しない `/api` と `/ws` は、HTTP の要求には画面のファイルではなく 404 の JSON を返し、WebSocket の接続要求は 403 で断る。後続の経路は、`app.py` の「存在しない経路」の受け皿より先に登録する
- 拡張子を持つ名前（`/assets/x.js`）は、無ければ 404。画面の URL にだけ index.html を返す。会議番号に「.」を使わないこと
- uvicorn には WebSocket の実装を名前で指定する（`__main__.py` の `WEBSOCKET_IMPLEMENTATION` = `websockets-sansio`）。既定の `auto` は、部品が無いと黙って WebSocket 無しで起動し、`/ws` がふつうの HTTP になる。名前で指定すると、部品が無いイメージは起動で失敗する
- 接続制限（Task 1.3.1）は、`create_app` にミドルウェアとして足す。配信部（最後の mount）にも効く
- 画面の読み込み先と通信先を限る応答ヘッダー（Task 1.3.3）は、まだ入れていない
- 設定は `Settings`（`MMD_PORT`、`MMD_APP_HOSTNAME`、`MMD_RECORDS_DIR`、`MMD_MODELS_DIR`、`MMD_STATIC_DIR`）。使うのは今は port と static_dir だけ。残りは後続の作業が使う
- ESLint は 10 系。eslint-plugin-react と jsx-a11y は ESLint 10 に未対応のため入れていない。dangerouslySetInnerHTML と innerHTML は `no-restricted-syntax` で禁止（`frontend/eslint.config.js`）
- Node.js は 24.21.0 に固定（24.22.0 は公開から 7 日未満のため見送り）。SHA-256 も Dockerfile で固定
- コンテナは非 root（uid 10001 の app）。2 つのボリュームの置き場は、イメージの中で app が持ち主になる

## はまりポイント
- `.gitignore` の `tools/`（gaido 管理）が `output_system/tools/` にも当たる。`!/output_system/tools/` で打ち消している
- 環境変数 `COMPOSE_PROJECT_NAME` が設定済みのため、compose の `name:` は効かない。ボリュームは `name:` で固定名にしている
- `pkill -f` は、コマンドの文字列が一致して自分のシェルを落とすことがある。PID を指定する
- playwright-cli は root だと Chromium が起動しない。`--config` で `chromiumSandbox: false` と `--no-sandbox` を渡す（設定ファイルはリポジトリの外に置く）
- 開発用コンテナの Node 22 系に同梱の npm 10 系は、`min-release-age` を黙って無視する。`frontend/.npmrc` の `engine-strict=true` で、古い npm の `npm install`・`ci`・`update` は失敗する。依存を足すときは `frontend/` で `../tools/with-pinned-node.sh npm install <パッケージ>` を使う（Dockerfile と同じ Node 24.21.0 を SHA-256 の確認つきで `~/.cache/mmd-node` に入れる）。足したあとは公開日が 7 日以上前かを確かめる
- Starlette の TestClient は uvicorn を通らない。WebSocket の部品が無くても、WebSocket のテストは通る。出荷するイメージの確認は `backend/test/container/` で行う
- starlette の TestClient は httpx2 を使う（httpx は非推奨の警告が出る）
- Docker に buildx が無く、`docker build` は旧ビルダーになる。追加の文脈（`certs`）は `docker compose build` で渡す

## 実装済み機能
- PBI #2: コンテナの起動、共通の枠と 4 画面の骨組み、`GET /api/health`、配信部、名前付きボリューム 2 つ、自動再起動
