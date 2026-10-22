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
│   │   ├── settings.py       # 環境変数 MMD_* の読み込み（起動時に検証）
│   │   ├── tuning.py         # config/tuning.toml の調整値（MMD_TUNING_* で上書き）
│   │   ├── config_error.py   # 設定の誤りで起動を止める例外
│   │   ├── logs/             # 3 種類のログ（標準出力・JSON 1 行）。許可リストは fields.py
│   │   ├── errors.py         # 画面へ返すエラーの共通の形（3 分類・9 領域・例外ハンドラー）
│   │   ├── healthcheck.py    # HEALTHCHECK 用
│   │   └── api/health.py     # GET /api/health
│   └── test/                 # src と同じ構造。test/container/ は起動したコンテナを相手にする（MMD_CONTAINER_URL のときだけ動く）
├── frontend/
│   ├── src/{routes.ts,router.tsx,components/AppFrame.tsx,screens/*,styles/{frame,notices}.css}
│   ├── src/errors/           # エラーの型と、エラーコードごとの文言の一覧（errorMessages.ts）
│   ├── src/components/notices/ # お知らせ帯・7 つのエラー表示パターン・確認の表示（使い方は docs/notices.md）
│   └── test/                 # src と同じ構造
├── tools/with-pinned-node.sh # Dockerfile と同じ版の Node.js で、渡されたコマンド（npm install など）を実行する
├── config/                   # tuning.toml（調整値の仮置き）、models.toml（置き場のみ。中身は Task 2.1.1）
├── docs/                     # errors.md（エラーの扱い・コード一覧）、notices.md（画面の部品の使い方）
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
- 設定は `Settings`（`MMD_PORT`、`MMD_APP_HOSTNAME`、`MMD_RECORDS_DIR`、`MMD_MODELS_DIR`、`MMD_STATIC_DIR`、`MMD_CONFIG_DIR`、`MMD_LOG_LEVEL`）。記録とモデルの置き場は既定値を持たせない（ボリュームと食い違ったまま別の場所へ黙って書く事故を防ぐ）。誤りは起動時にすべて出し、終了コード 2 で止める
- ログは標準出力（標準エラーではない）。許可リスト（`logs/fields.py`）に無い項目と、決めた形に合わない値は捨てる。項目を足すときは `FieldSpec` を足す
- 標準のロガー（`logging`）の記録は `log_type: runtime` で出し、文面を出さない（ロガーの名前・レベル・例外の種類だけ。出さなかった数は `dropped_field_count`）。文面を出すのは、起動の知らせ用の `STARTUP_LOGGER`（`logs/output.py`）と、uvicorn の起動・終了・接続の知らせ（`_UVICORN_FIXED_MESSAGES` にある文の形だけ）。uvicorn のアクセスの記録は `access_log=False` で止めている。会議に関わる記録は `log_processing`・`log_error`・`log_connection` で出す。`runtime` は基本設計書の 3 種別の外で、起動できない理由を日本語の文で出すために設けた。ワーカーなど別のプロセスを足すときは、最初に `configure_logging` を呼ぶ。`print` は ruff（T20）で禁止している
- 画面へ返すエラーは 4 項目（分類・コード・何が起きたか・次に行う操作）。画面はサーバーが返した文面を表示せず、エラーコードごとの文言の一覧（`errorMessages.ts`）から引く。未登録のコードは分類ごとの汎用の文言。機能を足すときは、コードと文言の両方を足す（`docs/errors.md`）
- WebSocket の処理（接続を受け付けたあと）が `AppError` を起こしたときは、HTTP の応答を返せないため、ログを出して、分類の符号（入力・業務は 1008、システムは 1011）とエラーコードの理由で接続を閉じる（`errors.py` の `_handle_app_error`）。接続を受け付ける前なら 403 で断られる。Starlette の TestClient は受け付け済みの接続へ HTTP の応答を返しても失敗するため、確かめるテストは本物の uvicorn を使う（`test_websocket_server.py`）。画面側で閉じる理由を読む処理は、接続を扱う作業で足す
- お知らせ帯は `AppFrame` が `NoticeProvider` と `NoticeBand` を持つ。画面の部品は `useNoticeActions`（出す・消す）と `useWarning`（条件が成り立つ間だけ出す）を使う。エラーは `role="alert"`、警告は `aria-live="polite"`。警告を入れる領域は、空のときも文書に残す（あとから入った警告を読み上げソフトへ伝えるため）
- エラーと警告は、文字の札（エラーは塗り・警告は枠）・アイコンの形（丸・三角）・色の 3 つで区別する。つながらないことの表示（`kind: 'offline'`）はエラーコードを付けない（サーバーが返せない状況のため）。検出と再接続は Task 3.4.1
- 確認の表示（`ConfirmDialog`）は body 直下へ portal で出し、背景の要素に `inert` を付ける。既定のフォーカスは「取りやめる」。暗い背景を押しても閉じない。削除の確認（Task 6.2.3）と終了の確認（Task 3.4.2）で使い回す
- 対応するブラウザーはユーザーエージェントで判定する（`isSupportedBrowser`）。Chrome の名前を含む別のブラウザー（Opera・Samsung Internet・旧 Edge など）と、iOS 版の Chrome・Edge は非対応として扱う。ヘッドレスの Chromium は Chrome として扱う（画面を通した自動テストに案内が出ないように）
- ESLint は 10 系。eslint-plugin-react と jsx-a11y は ESLint 10 に未対応のため入れていない。dangerouslySetInnerHTML と innerHTML は `no-restricted-syntax` で禁止（`frontend/eslint.config.js`）
- Node.js は 24.21.0 に固定（24.22.0 は公開から 7 日未満のため見送り）。SHA-256 も Dockerfile で固定
- コンテナは非 root（uid 10001 の app）。2 つのボリュームの置き場は、イメージの中で app が持ち主になる

## はまりポイント
- `.gitignore` の `tools/`（gaido 管理）が `output_system/tools/` にも当たる。`!/output_system/tools/` で打ち消している
- 環境変数 `COMPOSE_PROJECT_NAME` が設定済みのため、compose の `name:` は効かない。ボリュームは `name:` で固定名にしている
- `pkill -f` は、コマンドの文字列が一致して自分のシェルを落とすことがある。PID を指定する
- playwright-cli は root だと Chromium が起動しない。`--config` で `chromiumSandbox: false` と `--no-sandbox` を渡す（設定ファイルはリポジトリの外に置く）
- 開発用コンテナの Node 22 系に同梱の npm 10 系は、`min-release-age` を黙って無視する。`frontend/.npmrc` の `engine-strict=true` で、古い npm の `npm install`・`ci`・`update` は失敗する。依存を足すときは `frontend/` で `../tools/with-pinned-node.sh npm install <パッケージ>` を使う（Dockerfile と同じ Node 24.21.0 を SHA-256 の確認つきで `~/.cache/mmd-node` に入れる）。足したあとは公開日が 7 日以上前かを確かめる
- jsdom の既定のユーザーエージェントは Chrome でも Edge でもなく、そのままだと全画面に「Edge または Chrome で開いてください」の警告が出る。`frontend/test/setup.ts` が、各テストの前に Chrome のものへ差し替える。ブラウザーを切り替えるテストは `vi.spyOn(window.navigator, 'userAgent', 'get')` で上書きする
- `ConfirmDialog` のフォーカスの閉じ込めは、`inert` に加えて、document の keydown（Tab・Shift+Tab・Esc）でも行う。`inert` だけだと、端のボタンから先へ進んだ Tab が、ブラウザーのアドレス欄などへ出るため
- `frontend/test/lint/noDangerousHtml.test.ts` は ESLint を起動するため、機械が忙しいと既定の 5 秒の制限に届かないことが 1 度あった（再実行で通る）
- pytest は、ルートのロガーに取り込み用の出力先を付ける。この出力先は、文の形と引数が合わない記録でテストを失敗させる。形の食い違いを確かめるテストは、`test_output.py` の `isolated_logger`（親へ流さないロガー）を使う
- uvicorn の `Config(...)` は、`log_config=None` を渡さないと、既定のログの設定をプロセス全体へ書き込む。ほかのテストの出力が変わるため、テストでも `log_config=None` を渡す
- uvicorn を更新すると、起動と終了の知らせの文の形が変わることがある。変わった記録は文面なしで出る。`backend/test/meeting_interpreter/test_entrypoint_process.py` が、起動と終了の知らせが読める形で出ることを確かめる
- Starlette の TestClient は uvicorn を通らない。WebSocket の部品が無くても、WebSocket のテストは通る。出荷するイメージの確認は `backend/test/container/` で行う
- starlette の TestClient は httpx2 を使う（httpx は非推奨の警告が出る）
- Docker に buildx が無く、`docker build` は旧ビルダーになる。追加の文脈（`certs`）は `docker compose build` で渡す

## 実装済み機能
- PBI #2: コンテナの起動、共通の枠と 4 画面の骨組み、`GET /api/health`、配信部、名前付きボリューム 2 つ、自動再起動
- PBI #7: 設定の読み込み（環境変数と調整値）、3 種類のログ（標準出力・許可リスト）、共通のエラーの形（サーバーと画面）、お知らせ帯・7 つのエラー表示パターン・確認の表示の部品、対応していないブラウザーの案内
