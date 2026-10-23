# HANDOVER

## 技術スタック
- アプリサーバー: Python 3.12（Ubuntu 24.04 標準）、FastAPI、uvicorn、websockets（WebSocket の部品）。依存は uv（`backend/uv.lock`）で固定
- 画面: TypeScript、React 19、react-router 8（データ経路）、Vite 8。依存は npm（`frontend/package-lock.json`）で固定
- テスト: pytest（+ pytest-cov、httpx2。本物の uvicorn を起動するテストを含む）、Vitest 5（+ Testing Library、jsdom）。lint は ruff と ESLint 10
- 実行基盤: Docker コンテナ 1 つ（土台 ubuntu:24.04）。名前付きボリューム 2 つ（記録・モデル）
- クールダウン: uv は `exclude-newer = "7 days"`（`backend/pyproject.toml`）、npm は `min-release-age=7`（`frontend/.npmrc`）
- AI モデルの入手: 依存を足さず、標準ライブラリの HTTPS クライアント（urllib）で自前に行う。実行基盤のライブラリ（faster-whisper、llama.cpp、CTranslate2 など）は、まだ入れていない

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
│   │   ├── access.py         # 接続制限（宛先 Host と送り元 Origin の判定。HTTP も WebSocket も judge_access 1 つ）
│   │   ├── security_headers.py # 応答ヘッダー（CSP・nosniff・Referrer-Policy・X-Frame-Options）
│   │   ├── settings.py       # 環境変数 MMD_* の読み込み（起動時に検証）
│   │   ├── tuning.py         # config/tuning.toml の調整値（MMD_TUNING_* で上書き）
│   │   ├── config_error.py   # 設定の誤りで起動を止める例外
│   │   ├── logs/             # 3 種類のログ（標準出力・JSON 1 行）。許可リストは fields.py
│   │   ├── errors.py         # 画面へ返すエラーの共通の形（3 分類・9 領域・例外ハンドラー）
│   │   ├── healthcheck.py    # HEALTHCHECK 用（外向きの通信の例外: 127.0.0.1 だけ）
│   │   ├── child_process.py  # ワーカー・変換プロセスの起動の入口。自動入手と利用状況の送信を止める環境変数を必ず付ける
│   │   ├── models/           # モデル管理部。definition.py（定義の検証）、store.py（置き場・一致の確認）、download.py（唯一の外向きの通信）、fetch.py（確認→入手の流れ）、manager.py（準備状況）、failure.py（失敗の原因）
│   │   └── api/{health,models}.py  # GET /api/health、GET /api/models/status、POST /api/models/retry
│   └── test/                 # src と同じ構造。test/container/ は起動したコンテナを相手にする（MMD_CONTAINER_URL のときだけ動く）。test/browser/ はブラウザーで手動で確かめる確認用のサーバー（pytest は集めない）。test/distribution_server.py（擬似の配布元、HTTPS）と test/model_fakes.py（小さな定義・偽の入手の部品）は共通の道具
├── frontend/
│   ├── src/{routes.ts,router.tsx,components/AppFrame.tsx,screens/*,styles/{frame,notices}.css}
│   ├── scripts/              # ビルドした dist/ に外部の URL への参照が無いかの検査（npm run build の最後に走る）
│   ├── src/errors/           # エラーの型と、エラーコードごとの文言の一覧（errorMessages.ts）
│   ├── src/components/notices/ # お知らせ帯・7 つのエラー表示パターン・確認の表示（使い方は docs/notices.md）
│   └── test/                 # src と同じ構造
├── tools/with-pinned-node.sh # Dockerfile と同じ版の Node.js で、渡されたコマンド（npm install など）を実行する
├── config/                   # tuning.toml（調整値の仮置き）、models.toml（3 つの AI モデルの定義）、translation_alternatives.md（翻訳モデルの代わりの候補）
├── docs/                     # errors.md（エラーの扱い・コード一覧）、notices.md（画面の部品の使い方）
```

## ビルド・起動方法
- `output_system/` で `docker compose build` → `docker compose up -d`。`docker compose ps` が healthy ならよい
- 開発用コンテナから開く URL: `http://toshiakimamada-blip-mmd-output-system:3001`（`localhost` は使わない）
- テスト: `backend/` で `uv sync --locked && uv run pytest`、`frontend/` で `npm ci && npm test`。`npm ci` と `npm install` は Node 24 / npm 11.10 以上が要る。開発用コンテナ（Node 22 / npm 10）では `../tools/with-pinned-node.sh npm ci` のように付けて実行する
- 出荷するコンテナの WebSocket の確認: `docker compose up -d --build` のあと、`backend/` で `MMD_CONTAINER_URL=http://toshiakimamada-blip-mmd-output-system:3001 uv run pytest test/container --no-cov`
- ブラウザーでの WebSocket の確認（CSP のもとで接続できるか）: `backend/` で `uv run python test/browser/serve_websocket_check.py --port 3101 --app-hostname <開発用コンテナ名>` を起動し、`playwright-cli` で `http://localhost:3101/`・`http://127.0.0.1:3101/`・`http://<コンテナ名>:3101/` を開く。Edge は `--config` の `executablePath` で指す（README「ブラウザーでの WebSocket の確認」）
- 社内の証明書: 既定でリポジトリ直下の `ssl-certificates/` を登録する。フォルダーが無い環境は `SSL_CERTS_DIR=<空のフォルダー>` を付ける
- AI モデルの入手: コンテナを起動すると、自動で始まる（初回は約 3.1GB、10MB/s 前後で約 5 分）。`curl http://toshiakimamada-blip-mmd-output-system:3001/api/models/status` で見る。2 回目以降の起動は、確認（約 5 秒）だけで入手しない。モデル置き場のボリューム `toshiakimamada-blip-mmd-models` を消すと入手し直しになる

## 設計判断
- 経路の接頭辞を `/api`（HTTP）と `/ws`（WebSocket）に分け、それ以外は画面のファイル。存在しない `/api` と `/ws` は、HTTP の要求には画面のファイルではなく 404 の JSON を返し、WebSocket の接続要求は 403 で断る。後続の経路は、`app.py` の「存在しない経路」の受け皿より先に登録する
- 拡張子を持つ名前（`/assets/x.js`）は、無ければ 404。画面の URL にだけ index.html を返す。会議番号に「.」を使わないこと
- uvicorn には WebSocket の実装を名前で指定する（`__main__.py` の `WEBSOCKET_IMPLEMENTATION` = `websockets-sansio`）。既定の `auto` は、部品が無いと黙って WebSocket 無しで起動し、`/ws` がふつうの HTTP になる。名前で指定すると、部品が無いイメージは起動で失敗する
- 接続制限は、`create_app` にミドルウェアとして足す（`AccessControlMiddleware`）。配信部（最後の mount）にも効くため、あとから足した経路も必ず通る。通す宛先は `localhost`・`127.0.0.1`・コンテナ名（`MMD_APP_HOSTNAME`）。`127.0.0.1` は HEALTHCHECK の宛先。送り元は、付いていれば宛先と同じ接続先だけ。GET・HEAD・OPTIONS 以外と WebSocket では必須。条件を切り替える設定は持たない。自動テストのブラウザーはコンテナ名で接続し、そのまま通る（ADR-13）。断った要求は接続ログ（`log_type: connection`、警告）に理由・宛先・送り元の名前だけを残す。CORS の許可ヘッダーは出さない（出さなければ、ほかのサイトは応答を読めない）
- ミドルウェアの順（内側から）: 想定外の例外の変換 → 接続制限 → 応答ヘッダー → 処理の識別子。応答ヘッダーを接続制限の外側に置くのは、断った応答と想定外の例外の応答にも付けるため。経路が同じ名前のヘッダーを付けても、決めた値で置き換える
- 応答ヘッダーの CSP は `default-src 'none'` から始め、スクリプト・スタイル・画像・フォント・通信先を `'self'` だけにする。`base-uri`・`form-action`・`object-src` は `'none'`、`frame-ancestors 'none'`（X-Frame-Options: DENY を重ねる）。`upgrade-insecure-requests` は付けない（アプリは http で開く）。WebSocket の接続（101）にはヘッダーを足さない。`connect-src 'self'` は、Chrome 156 と Edge 155 のどちらでも同じホストとポートの `ws:` を含んだ（接続先の明示は不要）。Firefox と Safari は対象外
- 画面は、インラインのスクリプトとスタイル、`data:`・`blob:` の URL、`eval` を使えない。Vite は `assetsInlineLimit: 0` で画像とフォントの `data:` 埋め込みを止めている。画面の実装の規約（ブラウザーの保存領域へ会議の内容を書かない、など）は README「画面の実装の規約」
- `frontend/scripts/check-no-external-urls.ts` は、`npm run build` の最後（Dockerfile のビルドでも）に、dist/ の外部の URL への参照を検査して失敗させる。スクリプトの中の文字列として許す URL（XML の名前空間、React のエラーの案内など）は `scripts/externalUrls.ts` の `ALLOWED_SCRIPT_URL_PREFIXES` に理由つきで置く。部品の更新で失敗したら、取得に使われない文字列かを確かめてから足す
- 設定は `Settings`（`MMD_PORT`、`MMD_APP_HOSTNAME`、`MMD_RECORDS_DIR`、`MMD_MODELS_DIR`、`MMD_STATIC_DIR`、`MMD_CONFIG_DIR`、`MMD_LOG_LEVEL`）。記録とモデルの置き場は既定値を持たせない（ボリュームと食い違ったまま別の場所へ黙って書く事故を防ぐ）。誤りは起動時にすべて出し、終了コード 2 で止める
- ログは標準出力（標準エラーではない）。許可リスト（`logs/fields.py`）に無い項目と、決めた形に合わない値は捨てる。項目を足すときは `FieldSpec` を足す
- 標準のロガー（`logging`）の記録は `log_type: runtime` で出し、文面を出さない（ロガーの名前・レベル・例外の種類だけ。出さなかった数は `dropped_field_count`）。文面を出すのは、起動の知らせ用の `STARTUP_LOGGER`（`logs/output.py`）と、uvicorn の起動・終了・接続の知らせ（`_UVICORN_FIXED_MESSAGES` にある文の形だけ）。uvicorn のアクセスの記録は `access_log=False` で止めている。会議に関わる記録は `log_processing`・`log_error`・`log_connection` で出す。`runtime` は基本設計書の 3 種別の外で、起動できない理由を日本語の文で出すために設けた。ワーカーなど別のプロセスを足すときは、最初に `configure_logging` を呼ぶ。`print` は ruff（T20）で禁止している
- 調整値（`config/tuning.toml`）は仮置き。うち `generation_input_max_chars`（4000）と `generation_cpu_threads_during_meeting`（2）は、設計書と要件に根拠が無く、測ってもいない。利用者の PC で測り直す。各項目に許す範囲（`tuning.py`）の外と、知らない項目名・`MMD_TUNING_` の知らない環境変数は、起動を止める
- 会議の内容を URL（経路と問い合わせの文字列）に載せない。uvicorn のアクセスの記録は止めている（`access_log=False`）が、会議の内容を受け取る API は、URL ではなく本文で受ける
- 画面へ返すエラーは 4 項目（分類・コード・何が起きたか・次に行う操作）。画面はサーバーが返した文面を表示せず、エラーコードごとの文言の一覧（`errorMessages.ts`）から引く。未登録のコードは分類ごとの汎用の文言。機能を足すときは、コードと文言の両方を足す（`docs/errors.md`）
- WebSocket の処理（接続を受け付けたあと）が `AppError` を起こしたときは、HTTP の応答を返せないため、ログを出して、分類の符号（入力・業務は 1008、システムは 1011）とエラーコードの理由で接続を閉じる（`errors.py` の `_handle_app_error`）。接続を受け付ける前なら 403 で断られる。Starlette の TestClient は受け付け済みの接続へ HTTP の応答を返しても失敗するため、確かめるテストは本物の uvicorn を使う（`test_websocket_server.py`）。画面側で閉じる理由を読む処理は、接続を扱う作業で足す
- お知らせ帯は `AppFrame` が `NoticeProvider` と `NoticeBand` を持つ。画面の部品は `useNoticeActions`（出す・消す）と `useWarning`（条件が成り立つ間だけ出す）を使う。エラーは `role="alert"`、警告は `aria-live="polite"`。警告を入れる領域は、空のときも文書に残す（あとから入った警告を読み上げソフトへ伝えるため）
- エラーと警告は、文字の札（エラーは塗り・警告は枠）・アイコンの形（丸・三角）・色の 3 つで区別する。つながらないことの表示（`kind: 'offline'`）はエラーコードを付けない（サーバーが返せない状況のため）。検出と再接続は Task 3.4.1
- 確認の表示（`ConfirmDialog`）は body 直下へ portal で出し、背景の要素に `inert` を付ける。既定のフォーカスは「取りやめる」。暗い背景を押しても閉じない。削除の確認（Task 6.2.3）と終了の確認（Task 3.4.2）で使い回す
- 対応するブラウザーはユーザーエージェントで判定する（`isSupportedBrowser`）。Chrome の名前を含む別のブラウザー（Opera・Samsung Internet・旧 Edge など）と、iOS 版の Chrome・Edge は非対応として扱う。ヘッドレスの Chromium は Chrome として扱う（画面を通した自動テストに案内が出ないように）
- ESLint は 10 系。eslint-plugin-react と jsx-a11y は ESLint 10 に未対応のため入れていない。dangerouslySetInnerHTML と innerHTML は `no-restricted-syntax` で禁止（`frontend/eslint.config.js`）
- AI モデルの定義（`config/models.toml`）は、起動の時点で `Settings.model_catalog` に読み込んで検証する（版は 40 桁のコミットだけ、SHA-256 は 64 桁の小文字、役割は 3 つ 1 つずつ、非商用のライセンス名は拒否）。配布元の API が返す LFS の sha256 が無い小さなファイルは、固定した版のファイルを入手して求めた。取り込むファイルは、動かすのに要るものだけ（Qwen は Q4_K_M の 1 つ。FuguMT は README と .gitattributes を除く全部）
- 外向きの通信を行うのは `models/download.py` だけ。許可の例外は `healthcheck.py`（127.0.0.1）。この決まりは `test/meeting_interpreter/test_network_boundary.py` が、構文木の検査（違反の見本つき）とソケットの接続の監視の 2 つで守る。新しく通信が要るときは `download.py` に足す
- 子のプロセスの起動は `child_process.py` だけ。止める環境変数（`HF_HUB_OFFLINE` など 6 つ。llama.cpp は `LLAMA_ARG_OFFLINE`）は最後に書き込み、呼び出し側から外せない。`main()` もサーバー自身の環境変数へ反映する。`subprocess`・`multiprocessing` を取り込めるのも、このモジュールだけ
- 入手は標準ライブラリの urllib。証明書の検証は必ず有効（`ssl.create_default_context`）。HTTPS 以外への転送は拒む。環境変数のプロキシに従う。自動の再試行はしない（利用者の「入手をやり直す」）
- 一時名は `<ファイル名>.part`。確認を終えるまで入手済みと扱わず、一致したら同じフォルダーの中で名前を変える。続きに使えない一時名（定義より大きい）は捨てる。置き場の配置は `<モデル名>/<版>/<ファイル名>`
- ディスクの空きは「入手する分 + 予備 2GiB」で確かめる（`fetch.py` の `DISK_RESERVE_BYTES`。設計書の約 5GB は仮置きの見込みで、実際のファイルの大きさが決まったため）
- 準備状況は `ModelManager` がメモリの上で持つ。変更はイベントループの上だけで行い、入手と確認は別のスレッドで動かす。進み具合の通知は 0.25 秒の間隔で間引く（全体に達した報告は必ず通知する）。通知の送り先は `ModelStatusNotifier` の受け口だけで、既定は何もしない
- 翻訳モデルの変換が未接続の間は、準備完了にしない。準備状況は変換中で止まり、取得の `conversion_implemented` が `false`（開発用の区別）。変換を作る作業は `ModelManager(conversion=...)` に `TranslationConversion`（`is_complete()` と `async convert()`）を渡せばよい。変換の失敗は `ModelFailure` で知らせる（「翻訳モデルを変換できない」の原因の種類とエラーコードは、その作業で足す）
- 失敗の原因は `FailureKind`（5 つ + 分類できない `unknown`）。エラーコードは `MODEL_*`。画面の文言は `frontend/src/errors/errorMessages.ts`。取得の応答は HTTP のエラーではなく準備状況の一部として返す。入手失敗でない状態のやり直しだけが業務エラー（409）
- テスト: 本物の入手はしない。`test/distribution_server.py` が手元の HTTPS の擬似の配布元（openssl で自己署名の証明書をテストごとに作る）を立てる。`conftest.py` の自動の見張りが、手元以外への接続と名前の解決をテスト全体で失敗にする。他のテストのアプリは、開始しても何もしない `IdleModelManager` で組み立てる（アプリの起動が入手を始めるため）
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
- 出荷するアプリには、接続を受け付ける `/ws` の経路がまだ無い。CSP のもとでの WebSocket の接続は、確認用のサーバー（`backend/test/browser/serve_websocket_check.py`）で確かめる。出荷するアプリへ WebSocket を張ると、CSP を通ったうえで 403 になる（コンソールの文言が「CSP に違反」ではなく「handshake: Unexpected response code: 403」なら、CSP は通っている）
- 開発用コンテナに Edge は入っていない。Microsoft の公式リポジトリの `microsoft-edge-stable` の .deb を、SHA-256 を `Packages` の値と突き合わせて別のフォルダーへ `dpkg -x` で展開し、playwright-cli の `--config`（`launchOptions.executablePath`、`chromiumSandbox: false`、`--no-sandbox`）で指す。システムへはインストールしない
- コンテナを作り直したつもりで、古いイメージのまま起動していることがある。出荷するコンテナへの確認（`test/container/`）は、応答ヘッダーの有無で古いイメージに気づける
- 追加の `conftest.py` を別のフォルダーに置かない。テストの共通の道具は `test/` 直下（`distribution_server.py`・`model_fakes.py`）に置く。`pytest_plugins` で fixture を登録し、テストのファイル名は全体で一意にする（`__init__.py` が無いため）
- テストのアプリは `create_app(settings, model_manager=...)` で管理部を渡す。渡さないと、conftest の自動の差し替えで開始しない管理部になる。入手を確かめるテストは、本物の `ModelManager` と `FakeDownloader`（または擬似の配布元）を渡す
- 構文木の検査（`test_network_boundary.py`）は、検査の対象のフォルダーの指定を誤ると 0 件を検査して通る。`test_should_actually_scan_the_application_source` が見張っている
- llama.cpp の offline の環境変数は `LLAMA_OFFLINE` ではなく `LLAMA_ARG_OFFLINE`（ソースの `common/arg.cpp` で確認）
- FuguMT の重みは `pytorch_model.bin` だけ（pickle 形式）。変換で torch が読むときは、固定した SHA-256 で確かめた後でも、安全な読み込み（`weights_only=True` など）を使う
- Whisper の CTranslate2 形式は、SYSTRAN（faster-whisper の開発元）の配布。OpenAI の配布ではなく、公式の配布元として扱ってよいかは利用者に確認中（PBI のコメント）。OpenAI の HF 側のライセンス表示は Apache 2.0 で、SYSTRAN 側の MIT と食い違う
- 社内の通信を検査する証明書の下での入手は、実機では未確認。このコンテナの経路では HF の証明書は検査されていない（発行元は Amazon）。仕組みは `SSL_CERT_FILE` を使う自動テストで確認した
- Docker に buildx が無く、`docker build` は旧ビルダーになる。追加の文脈（`certs`）は `docker compose build` で渡す

## 実装済み機能
- PBI #2: コンテナの起動、共通の枠と 4 画面の骨組み、`GET /api/health`、配信部、名前付きボリューム 2 つ、自動再起動
- PBI #7: 設定の読み込み（環境変数と調整値）、3 種類のログ（標準出力・許可リスト）、共通のエラーの形（サーバーと画面）、お知らせ帯・7 つのエラー表示パターン・確認の表示の部品、対応していないブラウザーの案内
- PBI #12: 接続元の制限（宛先 Host と送り元 Origin の判定、WebSocket の送り元必須、CORS なし、断った要求の接続ログ）、応答ヘッダー（CSP・nosniff・Referrer-Policy・X-Frame-Options）、ビルドの外部 URL 検査
- PBI #17: AI モデルの定義と検証、モデル置き場と一致の確認、HTTPS での入手（続きから・進み具合・失敗の原因 5 種）、準備状況の取得とやり直し（`/api/models/status`・`/retry`）、起動時の非同期の開始、外向きの通信を入手の部品だけに限る検査、ワーカー・変換プロセスの起動の入口。翻訳モデルの変換と、通知の送り先は未接続
