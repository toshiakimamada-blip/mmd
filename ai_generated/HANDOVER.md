# HANDOVER

## 技術スタック
- アプリサーバー: Python 3.12（Ubuntu 24.04 標準）、FastAPI、uvicorn、websockets（WebSocket の部品）。依存は uv（`backend/uv.lock`）で固定
- 画面: TypeScript、React 19、react-router 8（データ経路）、Vite 8。依存は npm（`frontend/package-lock.json`）で固定
- テスト: pytest（+ pytest-cov、httpx2。本物の uvicorn を起動するテストを含む）、Vitest 5（+ Testing Library、jsdom）。lint は ruff と ESLint 10
- 実行基盤: Docker コンテナ 1 つ（土台 ubuntu:24.04）。名前付きボリューム 2 つ（記録・モデル）
- クールダウン: uv は `exclude-newer = "7 days"`（`backend/pyproject.toml`）、npm は `min-release-age=7`（`frontend/.npmrc`）
- AI モデルの入手: 依存を足さず、標準ライブラリの HTTPS クライアント（urllib）で自前に行う
- 通訳ワーカー: faster-whisper（Whisper small.en。Silero VAD の ONNX 形式が同梱。ONNX Runtime で動かす）、CTranslate2（FuguMT）、SentencePiece。依存グループ `interpreter`（`uv.lock` で固定。開発環境の `uv sync` の既定に入る）。PyTorch は使わない。出荷するイメージのアプリサーバーの環境には、このグループと `generation` だけを入れた（Dockerfile は `--no-default-groups --group interpreter --group generation`。アプリサーバー自身は取り込まない）
- 翻訳モデルの変換: 部品（PyTorch の CPU 版 wheel、transformers、CTranslate2、SentencePiece）は依存グループ `conversion`（`uv.lock` で固定）。開発環境の `uv sync` には入り、イメージではアプリサーバーの環境に入れず `/opt/conversion-venv` にだけ入れる（展開後 約 1.33GB）。llama.cpp は、生成ワーカーの部品（下）として入れた
- 生成ワーカー: llama-cpp-python（llama.cpp の Python の結び付け。PyPI に wheel が無く、ソースから組み立てる。組み立ての指定は `pyproject.toml` の `[tool.uv.config-settings-package]`）で Qwen3-4B（Q4_K_M の GGUF）を動かす。依存グループ `generation`（`uv.lock` で固定。開発環境の `uv sync` の既定に入る）。出荷するイメージにも入れた（Dockerfile の `--group generation`。ソースからの組み立てに要る build-essential・cmake は、サーバーの依存を入れる段にだけ入れ、最終のイメージには残らない。`llama_cpp 0.3.36`）

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
│   │   ├── interpreter/      # 通訳ワーカー（会議の間だけ動く別のプロセス）。protocol.py（メッセージの型とフレーム）、client.py（親側の起動・送受信・停止）、entry.py/__main__.py（子の入口。通信を断つ備え）、worker.py（待ち行列と字幕の組み立て）、segmenter.py/vad.py（発話の区切り）、transcriber.py、translator.py、engines.py/fake.py（差し替え口と偽のエンジン）、locations.py、settings.py。使い方・測定値は docs/interpreter.md
│   │   ├── worker_ipc.py, worker_client.py # 2 つのワーカー共通の枠の読み書き・出力の書き込み・親側のプロセス管理（固まったワーカーの強制終了を含む）
│   │   ├── generation/       # 生成ワーカー（要約と質問を作る別のプロセス）。protocol.py（メッセージの型）、client.py（親側）、entry.py/__main__.py（子の入口。通信を断つ備え）、worker.py（待ち行列・中断・割り当ての反映）、pipeline.py（ブロック・中間メモ・統合・作り直し）、prompts.py（指示の文面・出力の形）、results.py（出力の検証）、inputs.py（ブロック分けと質問の入力の組み立て。依頼を作る側が使う）、cpu.py、engines.py/llama.py/fake.py（差し替え口・本物・偽）、locations.py、settings.py。使い方・測定値は docs/generation.md
│   │   ├── jobs/             # 作成ジョブ管理部（要約と質問の作成を 1 件ずつ決めた順に実行）。models.py（種類・状況・待ち時間 `JobTimeline`）、registry.py（メモリの上の置き場・重複の判定。鍵つき）、policy.py（実行順の規則。純粋な関数。見直すときはここだけ）、manager.py（管理のタスク。生成ワーカーの起動・停止・依頼・中断・失敗・処理ログ）、workers.py（生成ワーカーの起動の入口。テストは偽のエンジンを渡す）、outcomes.py（結果の受け取り先の窓口）、summary_requests.py（要約を作る依頼の受け付け。POST と会議の終了の直後）、summary_outcomes.py（要約の保存と、完了・要約失敗への状態の変更）、meeting_state.py（`meeting.state_changed` の通知）、notices.py（`job.*` の通知）、errors.py（`SUMMARY_*`・`QUESTION_*`）。使い方は docs/jobs.md
│   │   ├── records/          # 記録保存部。database.py（接続・構造の版の確認・専用の実行器）、migrations.py（版の一覧と移行）、schema.py（版 1 の 5 つの表・桁の上限・区分コード）、models.py（Meeting・Subtitle・区分）、times.py（UTC のミリ秒の変換）、store.py（会議・字幕・要約の保存と読み出し・失敗の変換とエラーログ。`save_summary` が要約の完成の 1 回の確定）、errors.py（RecordSchemaError と RecordError 系）
│   │   ├── models/           # モデル管理部。definition.py（定義の検証）、store.py（置き場・一致の確認）、download.py（唯一の外向きの通信）、hosts.py（通信を許すホスト）、fetch.py（確認→入手の流れ）、manager.py（準備状況）、failure.py（失敗の原因）
│   │   │   └── conversion/   # 翻訳モデルの変換。アプリサーバー側: service.py（管理部への口）・process.py（起動と見届け）・receipt.py（控えと判定）・layout.py・protocol.py。変換プロセス側: worker.py・network_guard.py・sandbox.py・safe_torch.py
│   │   ├── meetings/         # 会議管理部。transitions.py（遷移規則の表）、manager.py（開始・終了の窓口、進行中は 1 件）、session.py（会議 1 件の音声・字幕・終了・中断・ワーカーの起動し直し）、inputs.py、errors.py、workers.py（ワーカーの起動の入口。テストは偽のエンジンを渡す）。使い方は docs/meetings.md
│   │   ├── notify/hub.py     # 通知部（/ws/events）
│   │   └── api/{health,models,meetings,ws}.py  # /api/health、/api/models/*、POST /api/meetings と /end と /summary、GET /api/meetings/{会議番号}/subtitles、/ws/audio/{会議番号} と /ws/events
│   └── test/                 # src と同じ構造。test/container/ は起動したコンテナを相手にする（MMD_CONTAINER_URL のときだけ動く）。test/browser/ はブラウザーで手動で確かめる確認用のサーバー（pytest は集めない）。test/distribution_server.py（擬似の配布元、HTTPS）と test/model_fakes.py（小さな定義・偽の入手の部品）と test/interpreter_audio.py（合成音声・音声ファイルの読み込み）は共通の道具。test/measure/measure_interpreter.py は、本物のモデルで字幕の遅れとメモリを測る道具、test/measure/measure_generation.py は、生成ワーカーの作成時間とメモリを測る道具（どちらも pytest は集めない）。test/meeting_fakes.py は会議管理部のテストの道具（偽のエンジンの入口、本物の uvicorn を起動する running_app、記録の読み取り）。test/generation_fakes.py（偽の文章生成）と test/meeting_subtitles.py（英語の会議の字幕の見本。測定と動作確認用）も共通の道具。test/meeting_fakes.py の `make_app` は、会議の終了で要約を始める呼び出し（`on_meeting_ended`）を、既定では何もしないものにする（本番の組み立てで確かめるテストは `on_meeting_ended=None` と `generation_launcher=InProcessLauncher()` を渡す）。test/job_fakes.py は作成ジョブ管理部のテストの道具（同じプロセスで動かす偽の生成ワーカー `InProcessLauncher`、別のプロセスで動かす `ProcessLauncher`、ブロックの区切りで止める位置を決める `BlockGate`）
├── frontend/
│   ├── src/{routes.ts,router.tsx,components/AppFrame.tsx,screens/*,styles/{frame,notices,live}.css}
│   ├── src/events/           # 通知の受信と再接続。EventConnection.ts（/ws/events への接続・つなぎ直し）、EventsProvider.tsx（useEvent・useReloadOnConnect。AppFrame が置く）、eventTypes.ts（通知の型と読み替え）。使い方は docs/events.md
│   ├── src/{api,meetings,subtitles}/ # api: apiClient.ts（ApiError・requestJson）と meetingsApi.ts。meetings: liveMeeting.ts（「会議中」が受け取る会議）・経過時間。subtitles: 字幕の型・mergeSubtitles・useMeetingSubtitles・formatClockTime
│   ├── src/screens/prepare/ + src/meetings/ + src/models/ # 「会議の準備」: PrepareForm（入力・二度押しの防止）・PrepareGuidance・ModelStatusPanel。meetings/: meetingInput.ts（検査）・startMeeting.ts（開始の流れ）・MeetingCaptureProvider.tsx（取り込みの預け先）・guidanceTexts.ts（案内の文言を集めた 1 か所）。models/: modelStatus.ts・useModelStatus.ts。docs/prepare-screen.md
│   ├── src/screens/live/ + src/components/subtitles/ # 「会議中」の中身と字幕の行・一覧・追いかける領域（SubtitleRow は「会議の記録」でも使い回す）。docs/live-screen.md
│   ├── src/screens/record/ + src/records/ + src/components/questions/ # 「会議の記録」: MeetingRecordView（領域の組み立て）・RecordHeader・SummaryRegion/SummaryContent/SummaryProgress・QuestionRegion・SubtitleRegion。records/: recordTypes.ts（`GET /api/meetings/{会議番号}` の応答の検査と読み替え）・recordView.ts（状態別の表示と操作の表。純粋な関数）・useMeetingRecord.ts（読み込みと通知での更新）・useSummaryRequest.ts（要約の依頼）・summaryLines.ts・jobTexts.ts・formatDateTime.ts。QuestionSetList は「会議中」でも使い回す。docs/record-screen.md
│   ├── scripts/              # ビルドした dist/ に外部の URL への参照が無いかの検査（npm run build の最後に走る）
│   ├── src/errors/           # エラーの型と、エラーコードごとの文言の一覧（errorMessages.ts）
│   ├── src/audio/            # ブラウザーの音声の取り込み。audioInput.ts（画面共有・マイク）、audioErrors.ts（理由の種類）、pcmConversion.ts（16kHz・モノラル・16 ビットへの変換）、pcmCaptureWorklet.ts（AudioWorklet の処理部）、audioPipeline.ts、audioSender.ts（順序つきの送信）、audioCaptureSession.ts（会議 1 回ぶん。停止の検知・やり直し）、browserEnvironment.ts（実ブラウザーの部品の作り方）。使い方・確かめた結果は docs/audio-capture.md
│   ├── src/components/notices/ # お知らせ帯・7 つのエラー表示パターン・確認の表示（使い方は docs/notices.md）
│   └── test/                 # src と同じ構造。test/browser/capture-check/ は実ブラウザーで音声の取り込みを確かめる確認用のページ（ビルドの対象外。出荷しない）。test/audio/fakes.ts は偽のトラック・ストリーム・WebSocket・AudioContext・処理部
├── tools/with-pinned-node.sh # Dockerfile と同じ版の Node.js で、渡されたコマンド（npm install など）を実行する
├── config/                   # tuning.toml（調整値の仮置き）、models.toml（3 つの AI モデルの定義）、translation_alternatives.md（翻訳モデルの代わりの候補）
├── docs/                     # errors.md（エラーの扱い・コード一覧）、notices.md（画面の部品の使い方）、models.md（入手・変換）、interpreter.md（通訳ワーカー・測定値）、generation.md（生成ワーカー・測定値）、meetings.md（会議の開始と終了・通知・実機での確認）、events.md（通知の受信と再接続）、record-screen.md（「会議の記録」の画面と確認）、live-screen.md（「会議中」の画面と確認）、prepare-screen.md（「会議の準備」の画面と確認）、jobs.md（作成ジョブ管理部）
```

## ビルド・起動方法
- `output_system/` で `docker compose build` → `docker compose up -d`。`docker compose ps` が healthy ならよい
- 開発用コンテナから開く URL: `http://toshiakimamada-blip-mmd-output-system:3001`（`localhost` は使わない）
- テスト: `backend/` で `uv sync --locked && uv run pytest`、`frontend/` で `npm ci && npm test`。`npm ci` と `npm install` は Node 24 / npm 11.10 以上が要る。開発用コンテナ（Node 22 / npm 10）では `../tools/with-pinned-node.sh npm ci` のように付けて実行する
- 出荷するコンテナの WebSocket の確認: `docker compose up -d --build` のあと、`backend/` で `MMD_CONTAINER_URL=http://toshiakimamada-blip-mmd-output-system:3001 uv run pytest test/container --no-cov`
- ブラウザーでの WebSocket の確認（CSP のもとで接続できるか）: `backend/` で `uv run python test/browser/serve_websocket_check.py --port 3101 --app-hostname <開発用コンテナ名>` を起動し、`playwright-cli` で `http://localhost:3101/`・`http://127.0.0.1:3101/`・`http://<コンテナ名>:3101/` を開く。Edge は `--config` の `executablePath` で指す（README「ブラウザーでの WebSocket の確認」）
- 本物のモデルを使う確認（通常の `pytest` では飛ばす）: コンテナのボリュームから `docker cp <コンテナ>:/var/lib/mmd/models/whisper-small-en` と `.../fugumt-en-ja` を `/tmp/mmd-models/` へ取り出し、`MMD_TEST_MODELS_DIR=/tmp/mmd-models MMD_TEST_SPEECH_FILE=<英語の音声> uv run pytest test/meeting_interpreter/interpreter/test_interpreter_real_models.py --no-cov`。測定は `uv run python test/measure/measure_interpreter.py --models-dir /tmp/mmd-models --speech <音声> --repeat 6`（引数は docs/interpreter.md）
- 生成ワーカーの本物のモデルを使う確認: コンテナのボリュームから `docker cp <コンテナ>:/var/lib/mmd/models/qwen3-4b-q4-k-m` を `/tmp/mmd-models/` へ取り出し（約 2.5GB）、`MMD_TEST_MODELS_DIR=/tmp/mmd-models uv run pytest test/meeting_interpreter/generation/test_generation_real_model.py --no-cov`（約 4 分）。測定は `uv run python test/measure/measure_generation.py --models-dir /tmp/mmd-models --threads 4`（`--long-lines 1500` で 2 時間分、`--meeting --questions-only` で会議中の割り当ての質問だけ。引数は docs/generation.md）。2 時間分は約 45 分かかる。測っている間は、ほかの重い処理を動かさない
- ブラウザーの音声の取り込みの確認: `docs/audio-capture.md` の「実ブラウザーでの確認」。確認用のページを `vite build` し、`backend/test/browser/serve_audio_capture_check.py` を起動して、ADR-13 の起動設定（`--use-fake-ui-for-media-stream`・`--use-fake-device-for-media-stream`・`--use-file-for-fake-audio-capture`・`--unsafely-treat-insecure-origin-as-secure`）の `playwright-cli`（通常版の Chromium または Edge を `--headless=new`）で開く
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
- 調整値（`config/tuning.toml`）は仮置き。うち `generation_input_max_chars`（4000）、`generation_cpu_threads_during_meeting`（2）、`generation_nice_during_meeting`（10）は、設計書と要件に根拠が無く、十分には測っていない（`generation_cpu_threads_normal` の 8 は、この開発環境の物理 4 コアでは 4 より遅かった。docs/generation.md）。利用者の PC で測り直す。各項目に許す範囲（`tuning.py`）の外と、知らない項目名・`MMD_TUNING_` の知らない環境変数は、起動を止める
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
- 翻訳モデルの変換は `TranslationConversion`（`is_complete()` と `async convert(cancel)`）の形で管理部へつなぐ。本番は `build_model_manager` が `TranslationConverter` を渡す。変換が済み（変換後のファイルが控えと一致）かつ 3 つがそろって初めて準備完了。変換の失敗は `ModelFailure(CONVERSION_FAILED, exception_type=...)`（エラーコード `MODEL_CONVERSION_FAILED`）。入手のやり直しは確認から流れ直すので、変換前のファイルが一致していれば入手せず変換だけやり直す。`conversion_implemented` は変換を渡さない構成（自動テスト）で `false`、本番は `true`
- 変換は別のプロセス（`python -m meeting_interpreter.models.conversion`、Python は `MMD_CONVERSION_PYTHON`）。出力は `<モデルのフォルダー>/converted.tmp/output/` に書かせ、成功したら `converted/` へ名前を変え、控え `converted.receipt.json` を最後に書く。失敗と中止では `converted.tmp/`・`converted/`・控えを捨て、変換前のファイルは残す。変換の途中でアプリが止まると控えが無く、次の起動で変換し直す（実機で確認）。結果は標準出力の最後の 1 行の JSON で、親は形が外れたら変換の失敗にする（例外の文面は返さない）
- 控えには、変換後のファイルごとの大きさと SHA-256 に加え、モデル名・版・量子化・変換前の 5 ファイルの SHA-256 を入れる（量子化や版を変えたら変換し直すため）。変換の部品の版は記録だけで比べない（イメージの更新で変換し直さない）。控えはモデル置き場の JSON で、記録データベースには置かない
- 変換のために外へ通信しない備えは 3 段: 環境変数、Python の見張り（`network_guard.py`: ソケットの作成・接続・名前の解決を拒んで数え、試みがあれば成功でも失敗にする）、OS の仕組み（`sandbox.py`: seccomp で UNIX ドメイン以外のソケットと io_uring_setup を拒み、別の機械向けの呼び出し（32 ビット互換）と x32 の印がついた番号はすべて拒む。入れた直後に IPv4 のソケットを作ろうとして自己点検する）。OS の仕組みを入れられないとき、または入れても効かないときは変換しない。他のワーカーへ流用するときは、そのワーカーが UNIX ドメイン以外のソケットと 32 ビットのプログラムを使わないことを先に確かめる。重みは `safe_torch.py` が `weights_only=True` だけに限る
- 翻訳モデルの定義は、変換に要る 5 ファイル（`CONVERSION_SOURCE_FILES`）を持つこと（起動時に検査）。ファイル名に `converted`・`converted.tmp`・`converted.receipt.json` は使えない
- 失敗の原因は `FailureKind`（5 つ + 分類できない `unknown`）。エラーコードは `MODEL_*`。画面の文言は `frontend/src/errors/errorMessages.ts`。取得の応答は HTTP のエラーではなく準備状況の一部として返す。入手失敗でない状態のやり直しだけが業務エラー（409）
- テスト: 本物の入手はしない。`test/distribution_server.py` が手元の HTTPS の擬似の配布元（openssl で自己署名の証明書をテストごとに作る）を立てる。`conftest.py` の自動の見張りが、手元以外への接続と名前の解決をテスト全体で失敗にする。他のテストのアプリは、開始しても何もしない `IdleModelManager` で組み立てる（アプリの起動が入手を始めるため）
- 通訳ワーカーは、親が `python -P -s -m meeting_interpreter.interpreter --output-fd N` で起動する別のプロセス。入力は子の標準入力、出力は親が用意した別のパイプ（標準出力はログのために残す）。どちらも `[長さ 4 バイト][種類 1 バイト][本体]` の枠で運び、形に厳密に当てはまらない枠は `ProtocolError`。音声は 1 秒 32KB のため共有メモリは使わない。字幕の順番はワーカーが付けず、親が振る（データモデルの「字幕」）
- 通訳ワーカーは、入口のスレッド（音声を区切って待ち行列へ入れるだけ）・処理のスレッド（文字起こし→翻訳）・知らせのスレッド（処理を待つ音声の長さを 1 秒ごとに親へ）に分ける。処理が遅くても音声を読み続け、捨てない。区切りの音声は、字幕を出した時点で参照を外す。待つ音声が `interpreter_backlog_warning_seconds`（300 秒）を超えたら警告のログを 1 回出す。終了処理（Finish）は待ち行列を空にしてから、停止（Stop）は待ち行列を捨てて、停止完了を返す。どちらも最後は `os._exit` で終える（AI の部品が終わらないスレッドを作っても止まるように）
- 通訳ワーカーの外へ通信しない備えは変換プロセスと同じ 3 段（環境変数・`network_guard`・seccomp）。seccomp は numpy や AI の部品が作るスレッドより先に入れる（すでにあるスレッドには効かない）ため、`entry.py` は重い部品を入れたあとに取り込む。入れられないときは開始の失敗を返し、AI の部品を動かさない。数えた試みの回数は停止完了の `network_attempts` で親へ返す（0 でなければならない）
- 通訳ワーカーが標準入力を読まなくなっても（固まった、SIGSTOP）、`InterpreterClient.stop()` は戻る。`WorkerProcess.write(timeout=)` が鍵と書き込みの待ちを時間で諦め（4096 バイト以下向け）、停止の依頼を書けなければ省いて強制終了に進む。待つ時間は依頼と停止完了を合わせて `timeout_seconds` まで（強制終了の猶予 1 秒が別に付く）。`WorkerProcess.stop()` は先にプロセスのグループを止め、あとで標準入力を閉じる（先に閉じると、止まった書き込みを待って戻らない）
- 子の出力が壊れたときは、深い入れ子の JSON と巨大な整数も `ProtocolError` にする。読み取りスレッドは、想定外の例外でも `WorkerExited(protocol_error=True)` を届けて終わる（黙って死なない）。`finish()` のあとは音声を送らない（ワーカーが標準入力を読まなくなり、送信が止まる）
- 文字起こしは、前の区切りの文章を次の条件に使わない。無音や雑音で Whisper が出す幻覚（「you」など）は、声が無い確からしさが 0.6 を超える区間を除いて防ぐ。翻訳は文を分けず、原文を 1 件ずつ訳す（2 文でも欠けなかった）。翻訳が失敗したか空なら、日本語訳を `None` にして字幕を出す。文字起こしの失敗は、その区切りを飛ばして回復できるエラーを返す
- 声の検出は、faster-whisper に同梱の Silero VAD（ONNX）を使う（1 秒分の判定が約 2 ミリ秒）。文字起こしの所要時間は区切りの長さにほとんど依らない（0.5〜5 秒の区切りで約 2〜3.7 秒）ため、約 2 秒より短い間隔で発話が続くと処理が追いつかず、たまる。測定値と配分との比較は docs/interpreter.md
- 生成ワーカーの契約: 依頼（要約・質問）は届いた順に 1 件ずつ処理し、親は結果（要約・質問・中断・エラー）が返ってから次を送る。中断は要約だけで、次のブロックの区切りと統合の前に確かめる。中断で返った中間メモと次のブロックの番号を付けて送り直すと再開する（中間メモは親がメモリの上で持つ。ワーカーは保存しない）。1 ブロックの要約は中間メモを作らない。質問は中断できない。中断の要求は、作っている要約と、待ち行列にあってまだ始めていない要約に効く（依頼の直後に求めても失われない）。要約が無いときの要求は捨てる
- 文章生成モデルは llama.cpp の結び付けでワーカーの中に組み込む。付属のサーバーは TCP で待ち受けるため、seccomp（UNIX ドメイン以外のソケットを拒む）と両立しない。`use_mmap=False`（mmap だと準備完了で 4.93GB、しないと 3.27GB）。思考モードは、助手の発言の頭に空の思考を置いて飛ばす（GGUF の会話の形式の `enable_thinking=false` と同じ形）。会議の内容は `special=False` でトークンにし、区切りの記号と特別な記号に見える文字列を取り除く。ツールは与えない（docs/models.md、docs/generation.md）
- 要約の日本語は、指示だけでは英語の文が混ざる（会議のデータが英語のため）。system・書式の見本・念押しの 3 か所で日本語を指定すると 4 項目とも日本語になった。見本は具体的な内容を避け、欄の説明にした（内容を写されないため）。中間メモは英語、質問は英語と日本語の組
- 検証に失敗したら 1 回だけ作り直し、それでも合わなければ回復できるエラー（`summary_failed`・`questions_failed`）を返す。ワーカーは続ける。生成そのものの失敗（例外）は作り直さない。エラーに会議の内容と例外の文面を入れない
- CPU の割り当て（スレッド数と nice 値）は、`SetCpu` を覚えておき、処理のスレッドが次の生成の前に反映する。nice 値は、生成の呼び出しごとに新しいスレッドへ付ける（一般の利用者のプロセスは nice 値を上げられても下げられず、スレッドごとの値は子の計算のスレッドが引き継ぐため）。付けられないときは付けず、`CpuApplied` の読み直した値に現れる
- 生成ワーカーへのメッセージの文字数の上限（質問の字幕・ブロック。`generation/protocol.py`）は、調整値 `generation_input_max_chars` の許す最大（`tuning.GENERATION_INPUT_MAX_CHARS_LIMIT`、100 万）に合わせる。同じ定数が調整値の範囲の最大でもあるため、ずれない。上限を調整値より小さくすると、字幕が調整値いっぱいまで溜まった会議の質問で、ワーカーが `PROTOCOL_ERROR` で終わる。ただし 100 万は実モデルでは動かない大きさ（文脈 約 86 万トークン、KV キャッシュだけで約 118GB の計算）。測定で値を決めたら、定数を現実的な値に下げる判断が要る
- 質問の入力で長い要約を切るとき（`inputs._shorten_summary`）は、割り当てより長い項目だけを同じ長さに切り、「該当なし」のような短い項目は切らない。どの項目も空にしない（ワーカーは空の項目を `ProtocolError` で拒み、終了する）。比例して切る方式は、短い項目が空になったため不採用
- 記録データベースは、名前付きボリューム（`MMD_RECORDS_DIR`）の `records.sqlite3` 1 ファイル。接続は 1 つで、操作は専用の実行器（スレッド 1 本）で 1 つずつ動かし、書き込みは `BEGIN IMMEDIATE` で始める（字幕の順番 = その会議の最大 + 1 が重ならず、欠番も出ない）。ジャーナルは `DELETE`、同期は `FULL`。WAL は、更新前の控えを `-wal` と `-shm` を気にせず作れるよう不採用（読み書きの並行は、接続 1 つのこのアプリでは使わない）
- 日時は UTC のミリ秒の整数（ミリ秒未満は切り捨て）。時間帯の無い日時と 1970 年より前の日時は断る。会議番号は `uuid4` の 32 桁の 16 進数（連番だと、削除した会議の番号を再び使い、開いたままの画面が別の会議を表示する）
- 書き込みの関数は、保存の前に桁の上限・区分コード・日時の形を確かめ、合わなければ `RecordValueError`（`RECORD_VALUE_INVALID`）。表の CHECK 制約は最後の防ぎ。データベースの失敗は原因によらず `RecordSaveError`（`RECORD_SAVE_FAILED`）にそろえる。エラーログは記録保存部が 1 件出し（`RecordError.log_level` が無しで、例外の受け皿は重ねて出さない）、項目はエラーコード・コンポーネント・会議番号・例外の種類・発生場所だけ。読み出し（`get_meeting`・`list_subtitles`）は変換しない
- `update_meeting` は遷移規則を確かめない（会議管理部と作成ジョブ管理部の役目）。終了日時 `None` は「変えない」、`summary_failure_reason` の `None` は「空にする」。無い会議の更新は `RecordValueError`（項目 `meeting_id`）、無い会議への字幕の追加は外部キーの違反で `RecordSaveError`
- Node.js は 24.21.0 に固定（24.22.0 は公開から 7 日未満のため見送り）。SHA-256 も Dockerfile で固定
- コンテナは非 root（uid 10001 の app）。2 つのボリュームの置き場は、イメージの中で app が持ち主になる
- 音声の取り込みの部品は、ブラウザーの部品（WebSocket・AudioContext・AudioWorklet・画面共有）を `AudioCaptureEnvironment` から受け取る。テストは偽物に替える。アプリにテスト専用の入口は無い（ADR-13）
- AudioWorklet の処理部は `?worker&url` で別のファイルにする。`blob:` は CSP が止め、`new URL(..., import.meta.url)` は TypeScript のまま出て読めないため不採用。いまの画面は取り込みの部品を読み込まないので、`dist/` には処理部が出ない（画面が読み込むと出る。確認用ページのビルドでは出ることを確認した）
- 取り込みの状態は `capturing`・`interrupted`・`stopped`・`closed`。共有が止まった・マイクが外れたときは、残りの音声を送ったあとに `capture.stopped`（JSON のテキスト）を送る。接続は閉じず、`restart()` で同じ出どころ・同じ接続のまま取り込み直し、`capture.resumed` を送る。利用者の `stop()` では送らない。受ける側（会議管理部）は別の作業で作る
- 画面共有の音声には、エコー除去・雑音抑制・音量の自動調整を求めない（再生中の声を削るため）。マイクはブラウザーの既定に任せる。映像は受け取った直後に止めてストリームから外す
- 共有の取りやめと拒否は、ブラウザーが同じ `NotAllowedError` を返し、見分けがつかない。`AUDIO_PERMISSION_DENIED` の 1 つにそろえた
- 16kHz への変換は、ハン窓つきの sinc フィルター（片側 16 ゼロ交差、遮断は出力の上限の 0.95 倍）で間引く。出力の位置は整数の計算で求め、長く続けてもずれない。入力の分け方（128 サンプルずつ、など）によらず出力は同じ
- 会議のセッション（`meetings/session.py`）は、管理のタスク 1 つが、待ち行列 1 本（ワーカーのメッセージ・閉じる要求・音声を渡す側の失敗）を 1 つずつ処理する。ブロックする呼び出しは、出力を読む専用スレッド、音声を書く専用の実行器、`asyncio.to_thread` に分ける。ワーカーは起動し直すたびに世代の番号を付け、前の世代のメッセージは捨てる。会議の開始は、ワーカーの準備完了を待たず会議番号を返す（読み込みの間の音声はためる）
- 進行中の会議の判定は、メモリ上のセッションで行う（記録の `interpreting` で数えない）。起動時の復旧（PBI 7.1）が暫定版で未実装のため、止まったあとに残った通訳中の会議が、新しい開始を永久に断ってしまう。セッションが無い通訳中の会議への終了の要求は、字幕の件数から状態を決めて保存し直す（終了の保存に失敗した会議を終われるように）
- 終了と中断: 音声を渡し切る（`meeting_end_flush_timeout_seconds`）→ 終了処理の依頼（書けなければ即、強制終了）→ 字幕が届く間は待つ（字幕が `meeting_end_finish_idle_seconds` 届かなければ固まったとみなす）→ 最後の字幕のあとで終了日時と状態を 1 回の確定で保存 → ワーカーを止める → 要約を始める呼び出し（`on_meeting_ended`）。中断の終了日時は、接続が切れた日時。起動し直しは、失敗が `interpreter_max_failures` 回続いたらあきらめて中断（`interpreter_stable_seconds` 以上動いたワーカーが止まったら数え直す）。時間と回数は `config/tuning.toml`。開始から `audio_connect_timeout_seconds`（30 秒）のうちに音声の WebSocket が 1 本も張られなければ、接続切れと同じ手順で中断にする（タブを閉じた会議が、ワーカーを載せたまま新しい開始を断り続けるのを防ぐ）。頼んでいないのに止まったワーカーは、起動し直せる場合も、エラーログ `SYSTEM_INTERPRETER_EXITED`（`exception_type` に止まり方）を出す
- 遷移規則の表は `meetings/transitions.py` の 1 か所（15 行）。状態を変えるのは `meetings/` と将来の `jobs/` だけ（`update_meeting` の呼び出し元を `test_state_change_boundary.py` が構文木で検査）。要約の依頼・成功・失敗の遷移は作成ジョブ管理部（`jobs/summary_*.py`）が表で確かめて使う。削除・復旧の遷移は表にだけあり、使う部はまだ無い
- 通知は `{"type": "対象.出来事", ...}`: `subtitle.created`（保存が確定してから）、`meeting.state_changed`、`error.occurred`（`meeting_id`・`category`・`code`・`message`・`action`）、`model.status_changed`（モデル管理部の発行先は通知部）。送り待ちが 1,000 件を超えた画面だけ 1013 で閉じる。保存に失敗した字幕は通知せず順番も使わない
- `WorkerClient`: `get` が停止完了か `WorkerExited` を返したあとは、`stop` が停止完了を待たずに片付ける。壊れた出力のときの `WorkerExited` は、子の終了を待たずに届く（子を止めるのは受け取った側）。`finish(timeout_seconds=)` で依頼を書く待ちを区切れる。`WorkerProcess.write` は `select.poll`（記述子 1024 以上でも動く）
- 作成ジョブ管理部（`jobs/`）は、管理のタスク 1 つが、出来事（受け付け・会議の知らせ・ワーカーのメッセージ）を 1 つずつ処理し、そのたびに現在の状態（`Situation`）から、`policy.py` の純粋な関数（`pick_next`・`yield_reason`・`needs_worker`）でやるべきことを決め直す。実行順は確認事項 13 の仮置き。見直すときは `policy.py` だけを変える。ジョブはメモリの上だけ（ADR-12）で、アプリが止まれば消える。作成中は同時に 1 件。質問は要約より先。会議中は質問だけ実行し、要約は後回し
- 順番を譲る・後回しにする: 作成中の要約へ、ブロックの区切りで止まるよう頼み（`interrupt`）、止まったら中間メモと次のブロックの番号を覚えて `queued` へ戻す。再開は続きのブロックから依頼する。質問が待ち、通訳中の会議もあるときは、後回しの理由を優先する（質問のあとも、会議が終わるまで再開しないため）。止まったときの理由（処理ログの `job_postponed` か `job_yielded`）は、そのときの状況から決め直す（`yield_reason`）。状況に理由が無いときだけ、止まるよう頼んだときの理由（`_interrupt_reason`）を使う。たとえば、会議のために止めるよう頼んだあとで会議が終わった場合、質問が待っていれば `job_yielded`、待っていなければ `job_postponed` を残す。待った時間（`wait_ms`）は、待機中の間（譲った・後回しにした間を含む）から、生成ワーカーの起動の間を除いた時間。起動は待機中のまま行うため、起動の前後で待ちを締め直す（`JobTimeline.begin_worker_start`・`end_worker_start`）。起動の時間は `startup_ms` に分けて出す。ブロックの途中では打ち切らない（確認事項 13 で目標に届かないときの見直しの候補）
- 会議管理部は、`meeting_started` と `interpreter_stopped` を作成ジョブ管理部へ知らせる。`interpreter_stopped` は、通訳ワーカーを止めたあと、要約を始める呼び出し（`on_meeting_ended`）より先に呼ぶ。2 つのワーカーのメモリを重ねないため。会議中に起動する生成ワーカーは、CPU の割り当てを会議中にして始める
- 結果の保存と、会議の状態の変更は、`JobManager` 自身ではしない。`JobOutcomeHandler`（`summary_ready`・`questions_ready`・`summary_failed`）の受け取り先を `create_app(job_outcomes=...)` へ渡す。省略すると `SummaryOutcomes`（要約を保存し、状態を変える。質問は PBI 5.3 が足すまで捨てる）。会議を `summarizing` にしてから `submit_summary` を呼ぶのは依頼する側（`SummaryRequests`）。窓口が終わってから、ジョブを完了にして通知する（窓口が例外なら `*_SAVE_FAILED`）
- 生成ワーカーが止まったときは、理由を問わず（強制終了・出力の終わり・依頼を書けない例外）、作成中のジョブだけを失敗にする（`*_WORKER_STOPPED`）。待機中のジョブは、次のワーカーの起動で続く。ブラウザー（通知の接続）は持たないため、閉じても作成は続く。処理ログは `job_*`（本文なし。件数と文字数だけ）。メモリ使用量は、PBI 4.1 を後回しにしたため記録していない（`_log_job` の `job_finished` に足す。docs/jobs.md）

- 要約は 2 回に分けて確定する。1 回目は状態を `summarizing` に（利用者の操作は `SummaryRequests.request`、会議の終了は会議管理部の終了の保存）。2 回目は `SummaryOutcomes` が、要約の作成または置き換えと状態の `completed` を `RecordStore.save_summary` の 1 回の確定で行う（失敗理由も空になる）。失敗は `update_meeting(status=summary_failed, summary_failure_reason=エラーコード)` で、要約の表には触れない（作り直しの失敗で前の要約が残る）。依頼は、同じ会議の要約のジョブがあればそれを返す（遷移規則より先）。依頼は `asyncio.Lock` で 1 つずつ処理する。会議が無いときのコードは、依頼書の `RECORD_NOT_FOUND` ではなく、既存の `MEETING_NOT_FOUND`（404）にそろえた
- 待機中の理由 `wait_reason`（`other_job`・`meeting_in_progress`・`null`）は `policy.wait_reason` が決め、管理のタスクが出来事のたびに決め直す。変わったときだけ `job.status_changed` をもう一度出す（同じ `queued` が続くことがある。テストの `statuses` は連続する同じ状況を 1 つにまとめる）。`JobManager.active_jobs(会議番号)` が、画面を開いた時点の取得の元になる（Task 5.4.1 が会議の取得 API に含める）
- 通知の受信は `AppFrame` の `EventsProvider` が 1 本持つ。`useEvent(種類, 関数)` で購読し、型は `eventTypes.ts` に足す（いまは `subtitle.created` だけ）。切れるとお知らせ帯に offline を出し、1・2・4・8・10 秒の間隔でつなぎ直す。`useReloadOnConnect(関数)` は開通のたびに呼ぶ（登録した時点で開通済みなら呼ばない。開いた時点の読み込みは画面が行う）。通知の重複は、受け取る側が順番で除く（`mergeSubtitles`）
- 「会議中」は、開始の応答を `location.state.meeting` から受け取る。読めない・会議番号が違う・再読み込みで履歴に残った古い会議（`staleMeetingIdFrom`）なら、「会議の記録」へ置き換えて移す。#68 は `navigate(screenPaths.meetingLive(id), { replace: true, state: { meeting: 応答 } })` で移ること。**「会議を終了する」は終了の API を呼んで記録へ移るだけで、音声の取り込み（`AudioCaptureSession`）を止めない。#68 が、開始の流れの側で `stop()` と `close()` を呼ぶこと**。会議の中断（`meeting.state_changed` の `interrupted`）で記録へ移る動きは、まだ無い
- 取り込み中の音声（`AudioCaptureSession`）は、`MeetingCaptureProvider`（`AppFrame` が置く）が預かる。「会議を終了する」は `stop()` → `POST /api/meetings/{会議番号}/end` → `release()`（接続を閉じる）の順。先に閉じると、アプリサーバーが接続切れとみなして会議を中断にする。預けた会議の「会議中」から離れる（戻る操作など）と、接続を閉じる（実機で、約 4 秒で中断になることを確認）。接続が切れた取り込みは `forget` で預かりをやめるだけで、中断の表示は未実装（Epic 4）
- 開始の流れは `meetings/startMeeting.ts`: 取り込み → 会議を作る → 音声の接続（応答の直後）。結果は `StartOutcome`（started・capture_failed・rejected・invalid_input）。取り込めない失敗は案内の領域（失敗した出どころの手順を強調）、開始を断られた失敗（`MEETING_ALREADY_RUNNING`・`MODEL_NOT_READY` など）はお知らせ帯、アプリサーバーの入力の再検査は項目の下に出す。音声の接続を始められなければ（準備の間に接続が閉じて、`state` が `closed` の取り込みが返ったときも）、作った会議をすぐ終了して取り込みを止める。どの失敗でも入力は残る
- 準備状況（`useModelStatus`）は、取得・通知 `model.status_changed`・つなぎ直しの読み直しで更新する。取得の応答より新しい通知が先に届いたら、通知を優先する（古い応答で上書きしない）。案内の文言は `meetings/guidanceTexts.ts` の 1 か所
- 字幕の領域は、最下部から 24px 以内かで追いかけているかを決める。字幕の取得は `cache: 'no-store'`（会議の内容を HTTP キャッシュに残さない）。失敗は `ApiError` にそろえ、画面は `toDisplayError` で文言を引く。経過時間は、秒の切り替わりで更新し（`setInterval` だと秒がとぶ）、タブが見えたときに直す
- 「会議の記録」の取得は `GET /api/meetings/{会議番号}` の 1 回（会議・字幕・要約・質問のまとまり・作成ジョブ）。字幕の別取得は、再接続の抜けを埋める「会議中」の用。状態ごとの表示と操作は `records/recordView.ts` の純粋な関数 1 つ（表の全行を `test/records/recordView.test.ts` が検査）。質問を作るボタンと削除は、暫定版では画面に置かないが、関数は表のとおりに答える（ボタンを足すときに出し分けを作り直さない）。「質問を作る」ボタンを今作らないのは、PBI #108 のコメント（質問の作成を暫定版のあとに回した）による
- 「会議の記録」の更新は、通知で記録を読み直す（`meeting.state_changed`・`job.status_changed`）。`job.progress_changed` は読み直さず、処理したブロックの数だけ差し替える（ブロックごとに届くため）。読み込みは最後に送った 1 件の結果だけを使い、画面を離れたあとの応答は捨てる。要約の依頼は、読み直しを終えてからボタンを押せる状態へ戻す（先に戻すと、押せる状態が一瞬見える）
- 質問の作成（PBI #104）: `POST /api/meetings/{会議番号}/question-sets`（202）は会議の状態を変えず、字幕が 1 件以上あれば受け付ける。保存は質問のまとまり 1 件と質問 5 件を 1 回の確定で行い、5 組に満たない出力は 1 問も保存しない（`QuestionRequests`・`QuestionOutcomes`）。通訳中の会議があるときは、生成ワーカーを会議中の割り当て（2 スレッド・nice 10）で起動し、会議の開始・終了・中断で `SetCpu` を送って切り替える。作成が終わるとワーカーを止める（会議中の割り当てを残さない）。値は調整値で、仮置きのまま

## はまりポイント
- `.gitignore` の `tools/`（gaido 管理）が `output_system/tools/` にも当たる。`!/output_system/tools/` で打ち消している
- 環境変数 `COMPOSE_PROJECT_NAME` が設定済みのため、compose の `name:` は効かない。ボリュームは `name:` で固定名にしている
- `pkill -f` は、コマンドの文字列が一致して自分のシェルを落とすことがある。PID を指定する
- playwright-cli は root だと Chromium が起動しない。`--config` で `chromiumSandbox: false` と `--no-sandbox` を渡す（設定ファイルはリポジトリの外に置く）
- 開発用コンテナの Node 22 系に同梱の npm 10 系は、`min-release-age` を黙って無視する。`frontend/.npmrc` の `engine-strict=true` で、古い npm の `npm install`・`ci`・`update` は失敗する。依存を足すときは `frontend/` で `../tools/with-pinned-node.sh npm install <パッケージ>` を使う（Dockerfile と同じ Node 24.21.0 を SHA-256 の確認つきで `~/.cache/mmd-node` に入れる）。足したあとは公開日が 7 日以上前かを確かめる
- jsdom の既定のユーザーエージェントは Chrome でも Edge でもなく、そのままだと全画面に「Edge または Chrome で開いてください」の警告が出る。`frontend/test/setup.ts` が、各テストの前に Chrome のものへ差し替える。ブラウザーを切り替えるテストは `vi.spyOn(window.navigator, 'userAgent', 'get')` で上書きする
- `ConfirmDialog` のフォーカスの閉じ込めは、`inert` に加えて、document の keydown（Tab・Shift+Tab・Esc）でも行う。`inert` だけだと、端のボタンから先へ進んだ Tab が、ブラウザーのアドレス欄などへ出るため
- 画面のテストは全体で `WebSocket` を、つながらない偽物に置き換えている（`test/setup.ts`）。通知を確かめるテストは `test/events/fakeWebSocket.ts` の `FakeEventSocket` を `vi.stubGlobal` で渡す。「会議中」のテストは、`createMemoryRouter` の `initialEntries` に `{ pathname, state: { meeting } }` を渡す
- react-hooks の `set-state-in-effect` は、effect から呼ぶ関数が `await` のあとで setState すると報告する。`.then` に渡す形にすると通る。jsdom には画面の配置が無いので、スクロールの確認は `Element.prototype` の `scrollHeight`・`clientHeight`・`scrollTop` を置き換える
- 「会議の準備」が無い間は、実ブラウザーで `history.pushState({ usr: { meeting }, key, idx: 1 }, '', URL)` と `popstate` の発行で「会議中」を開く（docs/live-screen.md）。playwright-cli の Chromium が 1247 を求め、入っているのが 1248 のときは、設定の `executablePath` で指す
- `frontend/test/lint/noDangerousHtml.test.ts` は ESLint を起動するため、機械が忙しいと既定の 5 秒の制限に届かないことが 1 度あった（再実行で通る）
- pytest は、ルートのロガーに取り込み用の出力先を付ける。この出力先は、文の形と引数が合わない記録でテストを失敗させる。形の食い違いを確かめるテストは、`test_output.py` の `isolated_logger`（親へ流さないロガー）を使う
- uvicorn の `Config(...)` は、`log_config=None` を渡さないと、既定のログの設定をプロセス全体へ書き込む。ほかのテストの出力が変わるため、テストでも `log_config=None` を渡す
- uvicorn を更新すると、起動と終了の知らせの文の形が変わることがある。変わった記録は文面なしで出る。`backend/test/meeting_interpreter/test_entrypoint_process.py` が、起動と終了の知らせが読める形で出ることを確かめる
- Starlette の TestClient は uvicorn を通らない。WebSocket の部品が無くても、WebSocket のテストは通る。出荷するイメージの確認は `backend/test/container/` で行う
- starlette の TestClient は httpx2 を使う（httpx は非推奨の警告が出る）
- 出荷するアプリは、`/ws/audio/{会議番号}` と `/ws/events` を受け付ける。CSP のもとでの WebSocket の接続は、確認用のサーバー（`backend/test/browser/serve_websocket_check.py`）で確かめてある。画面から張るときは、実ブラウザーで CSP を通ることを確かめ直す（コンソールの文言が「handshake: Unexpected response code: 403」なら、CSP は通っていて、サーバーが断っている）
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
- `uv sync`（既定の依存グループ）に `interpreter` を入れていなかったため、素の `uv sync --locked` が faster-whisper・onnxruntime・av を外し、通訳ワーカーのテストが落ちた。`default-groups` に足した（`uv.lock` の `default-groups` の 1 行も変わる。`uv lock --check` で確かめる）
- `ruff` の E501 は、日本語の文字を幅 2 で数える。日本語の長い文字列は、100 桁に収まるよう分ける
- 本物のモデルでの測定は、約 90 秒かかり、CPU を使い切る。ほかの重い処理（pytest 全体など）と同時に走らせると、遅れの値が悪くなる。ファイルのキャッシュが効いた状態の値のため、初回の読み込みは別に測る
- slots の dataclass には `__dict__` が無い。コピーして一部を変えるときは `dataclasses.replace`
- 変換の自動テストの本物の変換（小さな翻訳モデル）は 1 回で数秒かかる。変換プロセスの結果をアプリサーバーが使う処理は、偽の起動（`FakeRunner`）に差し替えて確かめる
- CTranslate2 は CPU で、保存された `int8_float16` を `int8_float32` に自動で直し、警告を 1 行出す。翻訳の結果には影響しない
- イメージの大きさ: `docker image ls` の DISK USAGE は「展開後 + 圧縮後」の合計。増えた分を測るときは `docker history` の層（展開後）と `docker save | wc -c`（圧縮後）で見る
- `conftest.py` の自動の見張りは、手元以外への接続と名前の解決を失敗にする。本物の変換プロセスは子のプロセスなので、見張りの外で動く（外へ通信しないことは、変換プロセス自身の備えが守る）
- 文章生成の速さは、CPU とメモリの空きに強く左右される。同じ条件（4 スレッド、同じ入力）で、質問の作成が 110 秒の回と 189 秒の回があった。開発環境は 7.6GB で、モデルを読み込むと使えるメモリが約 3GB まで減り、スワップが使われた。測るときは、ほかの重い処理を止め、先にモデルを読み込んで約 700 トークンの入力を処理する短い測定で、速さが平常かを確かめる
- この環境では、測定の途中で時計（壁時計）が約 22 分飛んだ（ログの timestamp の差が実際の所要時間の 4 倍に見えた）。所要時間は単調増加時計で測る（測定ツールはそうしている）。ログの timestamp の差は信用しない。待機の `pgrep -f <名前>` は、待っているシェル自身のコマンド行に一致して終わらないので、プロセス番号で待つ
- ggml のスレッド数を物理コアの数より多くすると、生成が遅くなる（8 スレッド 5.2 トークン/秒、4 スレッド 7.8 トークン/秒。読み込み側はほぼ同じ）
- 文章生成の出力は、1 トークンずつ文法で制約するため、文法なしより遅い。測定は本物の入力と文法で行う
- 英語の字幕を日本語で要約させると、presence penalty（モデルの配布元は量子化したモデルに 1.5 を勧める）が高いと言語が混ざることがある、と README にある。今のところ、日本語の指定で足りている。混ざったら、まず値を下げて試す
- `ruff` の E501 を、英語の字幕の見本（`test/meeting_subtitles.py`）だけ許している（`pyproject.toml` の `per-file-ignores`）
- テストの日時の期待値（Unix 時刻のミリ秒）を手で計算しない。1 日（86,400,000）ずれた定数を書いて、正しい実装のテストが落ちた。`date -u -d "2026-11-04 12:00:00" +%s` で確かめる
- 保存の失敗の再現: ディスクがいっぱいは `PRAGMA max_page_count`（`database.run` の中で設定し、あとで戻す）、ロックは別の接続で `BEGIN IMMEDIATE` を持ち、`database.BUSY_TIMEOUT_MS` を `monkeypatch` で小さくしてから開く（既定は 5 秒待つ）。確定の取り消しは、本物の書き込みを済ませてから例外を起こす `RecordDatabase` の子で確かめる（`test_record_save_failure.py`）
- Python 3.12 のため、ジェネリクスは `def f[T](...)`（`TypeVar` を使うと ruff の UP047）
- `track.stop()` は `ended` のイベントを起こさない（共有の停止・マイクの取り外しとは別）。テストと確認用ページは、`dispatchEvent(new Event('ended'))` を足して再現する
- 疑似入力の Chromium は、共有の取りやめも「音声なしの共有」も再現できない（確認は常に通り、音声が付く）。この 2 つの分類は Vitest だけで確かめた。実際の PC の音声は、利用者の Windows の Edge で確認する（ADR-13）
- 前回の実行が止めた確認用サーバーが、ポート 3102 に残ることがある。`ps -eo pid,args | grep serve_audio_capture_check` で PID を調べて止める。残ったままだと、新しいサーバーは起動に失敗し、古い状態のサーバーが応答する
- 変換の長時間のテストは、カバレッジの計測つきだと既定の 5 秒に届かないことがある（1 サンプルあたり約 90 回の積和）。入力は 20 秒分にとどめ、制限を広げてある
- ONNX Runtime 1.31.0 の Linux 版は、Microsoft の利用状況の送信（1DS）を内蔵する。通訳ワーカーは seccomp でソケットを作れないため送れないが、起動のたびに標準出力へ警告が 1 行出る（JSON ではない行）。`onnxruntime.disable_telemetry_events()` を呼んでも残る（コンテナで確認）。更新で挙動が変わりうる
- `websockets.sync.client.connect` は `legacy=True, proxy=None` を付ける（付けないと非推奨の警告と、環境のプロキシの使用）。会議管理部のテストは、音声の接続を開いている間に記録の状態を読む（`with` を出て接続を閉じると、会議が中断になる）
- 共通の道具を `test/meeting_fakes.py` に集めた（`__init__.py` が無いため、テストのファイル同士の import はできない）。ワーカーを SIGSTOP で固める確認は、`os.killpg(pid, SIGSTOP)`。止めた後の後始末は SIGKILL
- コンテナを作り直した直後の最初の会議は、モデルの読み込みが冷えていて、最初の字幕の通知まで 36.7 秒かかった（2 回目は 6.2 秒）。遅れを測るときは、先に 1 回会議を流す
- コンテナの中で `docker exec ... python -c "<コード>"` からワーカーを探すとき、コードの文字列にワーカーの起動の引数（`--output-fd`）が入っていると、探した自分自身を数える。文字列を `+` で分けて書き、自分のプロセス番号を除く
- `docker compose build` の層の再利用が効くと、生成ワーカーの部品の組み立て（llama.cpp のコンパイル）は数秒で終わる。Dockerfile の `uv sync` の行か `pyproject.toml`・`uv.lock` を変えると、組み立て直しになる。出荷するコンテナで本物のモデルを動かす確認は、`docker exec -i <コンテナ> python - < <スクリプト>` で、`ProductionGenerationLauncher` と `JobManager` を直接動かす（アプリを通さない）
- 作成ジョブ管理部のテストで、ブロックの区切りで止まる位置は `BlockGate("block 1")` で決める（そのブロックの中間メモを作り始めたら `reached`、`release` を立てるまで待つ）。強制終了は `ProcessLauncher`（別のプロセスの偽のエンジン）へ `os.kill(pid, SIGKILL)`。同じプロセスの偽は `crash()`

- 画面のテストで「会議の準備」を開くと、準備状況の取得（`GET /api/models/status`）が走る。fetch の偽物が無いと、領域の中のエラーが出て、お知らせ帯の `alert` の数を数えるテストが崩れる（`AppFrame.test.tsx` に準備完了を返す fetch を置いた）。「会議の準備」のテストは `vi.mock('.../audio/browserEnvironment')` で偽の環境（`test/audio/fakes.ts`）に替える
- 経路の状態（`router.state`）が変わっても、画面の描画は少し遅れる。`waitFor(pathname)` の直後の `getByRole` は、3 回に 1 回ほど落ちた。`findByRole` で待つ
- 実ブラウザーの確認（疑似入力）: `curl` で `POST /api/meetings` を叩くときは `-H "Origin: <宛先と同じ>"` が要る。音声をつながない会議は 30 秒で中断になる。安全な接続として扱わせる起動設定を外すと `mediaDevices` が無く、`AUDIO_CAPTURE_FAILED` を再現できる（疑似入力では、共有の取りやめと音声なしは再現できない）。疑似の音声は PyAV で 48kHz・ステレオの WAV にする（`docs/prepare-screen.md`）
- 1 回目の会議で、約 25 秒間、画面の経過時間と字幕が更新されないように見えた（その後は追いついた）。同じ手順を 2 回繰り返しても再現しなかった。原因は未特定

- `make_app`（`test/meeting_fakes.py`）の既定は、会議の終了で要約を始めない。要約まで確かめるテストは `on_meeting_ended=None` を渡す（`setdefault` なので、None を渡すと本番の組み立てになる）。本番の組み立てで、結果の受け取り先も本物にすると、終了のあと状態が非同期で `completed` になる
- 実機の確認のスクリプトで、音声の WebSocket を `with` で先に閉じると、サーバーが接続切れとみなして会議を中断にする（終了の応答は `interrupted`）。終了の API を呼んでから閉じる。中断した会議の要約を確かめるときは、この動きを使える
- `test_audio_and_subtitles.py` の 2 つ（`test_should_pass_audio_to_the_worker_in_order_with_the_arrival_time`、`TestNotificationHub::test_should_not_let_a_screen_that_does_not_read_block_the_others`）は、この環境で 5 回に 1 回ほど落ちる（前者は時刻の比較、後者は websockets の `recv` が別のスレッドと重なる）。変更の前の main でも同じ失敗が出た。再実行で通る
- 要約の作成中（状態が要約作成中）にコンテナを再起動すると、作成ジョブ（メモリだけ）が消え、会議は要約作成中のまま残る（起動時の復旧 PBI 7.1 が暫定版で未実装）。実機の確認では、再起動しない。要約の作成は、この開発環境で約 2 分（字幕 3 件）。確認のスクリプトは、`POST /api/meetings` と `/ws/audio/…` に `Origin: http://<コンテナ名>:3001` を付ける（付けないと 403）。`playwright-cli` は root では `--config` で `channel: chrome`・`chromiumSandbox: false` を指定する
- 会議中の質問の測定（`test/measure/measure_question_during_meeting.py`）は、WSL で壁時計が飛ぶことがあり（2026-11-12 は +22 秒）、測定道具が検出して信頼できない旨を出す。2026-11-12 の 1 回の測定（参考値）では、質問の作成中に字幕の遅れが最大 75 秒まで伸び、完了から 216 秒たつまで 10 秒以内に戻らなかった。質問の作成は 228 秒（目標 3 分）。確認事項 13 で測り直して割り当てを見直す。測定には英語の音声が要る（`curl -fsSL -o /tmp/jfk.flac https://github.com/openai/whisper/raw/main/tests/jfk.flac`）

## 実装済み機能
- PBI #2: コンテナの起動、共通の枠と 4 画面の骨組み、`GET /api/health`、配信部、名前付きボリューム 2 つ、自動再起動
- PBI #7: 設定の読み込み（環境変数と調整値）、3 種類のログ（標準出力・許可リスト）、共通のエラーの形（サーバーと画面）、お知らせ帯・7 つのエラー表示パターン・確認の表示の部品、対応していないブラウザーの案内
- PBI #12: 接続元の制限（宛先 Host と送り元 Origin の判定、WebSocket の送り元必須、CORS なし、断った要求の接続ログ）、応答ヘッダー（CSP・nosniff・Referrer-Policy・X-Frame-Options）、ビルドの外部 URL 検査
- PBI #17: AI モデルの定義と検証、モデル置き場と一致の確認、HTTPS での入手（続きから・進み具合・失敗の原因 5 種）、準備状況の取得とやり直し（`/api/models/status`・`/retry`）、起動時の非同期の開始、外向きの通信を入手の部品だけに限る検査、ワーカー・変換プロセスの起動の入口。通知の送り先は未接続
- PBI #23: 翻訳モデルの変換（別のプロセス、外へ通信しない 3 段の備え、安全な重みの読み込み）、変換後のハッシュ値の控えと変換し直しの判定、準備状況の「変換中」と失敗（翻訳モデルを変換できない）、入手のやり直しで変換だけをやり直す流れ。測定値（イメージ +1.33GB、変換のメモリの最大値 約 625MB）は `output_system/docs/models.md`
- PBI #28: 通訳ワーカー（別のプロセス）。16kHz・モノラルの音声を無音と長さの上限（5 秒）で区切り、Whisper small.en で英語の文字に、FuguMT で日本語にして、段ごとの所要時間と先頭の音声が届いた日時つきの字幕を返す。失敗した区切りは飛ばし、翻訳の失敗は日本語訳なしで出す。処理が追いつかなくても音声を捨てず、届いた順に処理する。外へ通信しない備え 3 段。本物のモデルでの測定（字幕の遅れ 平均 5.3 秒・最大 7.6 秒、ワーカーのメモリ 最大 約 0.82GB）は `output_system/docs/interpreter.md`。会議管理部・画面・保存は含まない
- PBI #34: 生成ワーカー（別のプロセス）。字幕（英語の原文）のブロックから、4 項目の日本語の要約（1 ブロックは直接、複数ブロックは中間メモを作って統合）と、英語と日本語の質問 5 問を作る。出力は文法で制約し、検証に失敗したら 1 回作り直す。ブロックの区切りで中断・再開できる。質問の入力を 3 つの場面で組み立てる。CPU の割り当て（スレッド数と nice 値）を通常と会議中で切り替えられる。外へ通信しない備え 3 段。実モデル（Qwen3-4B）での測定（4 スレッド）: 質問は作成 93〜127 秒（起動を含めて 124〜166 秒。目標 180 秒以内）、要約は 2 時間分で 2,124 秒（35.4 分。**目標の 30 分を超えた**）、常駐メモリの最大は 2 時間分の要約のあとで 3,803MB（利用時の 4.5GB には収まるが、**自動テスト時の 3.5GB を 0.21GB 超えた**。質問だけなら 3,516MB）。詳細は `output_system/docs/generation.md`。作成ジョブ管理部・保存は含まない
- PBI #47: 記録データベース（`records.sqlite3`、構造の版 1、会議・字幕・要約・質問のまとまり・質問の 5 つの表）と記録保存部。会議の作成・字幕の追加（順番は同じ確定の中で振る）・状態と終了日時の更新・読み出し、保存の失敗の共通のエラー（`RECORD_SAVE_FAILED`・`RECORD_VALUE_INVALID`）と画面の文言、内容を含まないエラーログ。要約・質問のまとまり・質問の保存の関数、字幕の保存に失敗したときの会議の続け方、更新前の控え（暫定版では後回し）は含まない
- PBI #52: ブラウザーでの音声の取り込みと送信。画面共有（PC の音声。映像は破棄）とマイクから音声を得て、失敗の理由を 4 種類で返す。16kHz・モノラル・16 ビットの PCM に変換して、約 100 ミリ秒のフレームで `/ws/audio/{会議番号}` へ送る（詰まっても順序を保ち、欠落しない）。共有が止まったことを検知し、`capture.stopped` を送って呼び出し側へ通知し、同じ接続で取り込み直せる（`capture.resumed`）。Chromium 156 と Edge 155 の実ブラウザー（疑似入力）で確認済み。会議の開始の画面と、サーバー側で音声を受け取る経路は含まない。手順と結果は `output_system/docs/audio-capture.md`
- PBI #57: 会議の開始（`POST /api/meetings`）と終了（`POST /api/meetings/{会議番号}/end`）、音声の WebSocket（`/ws/audio/{会議番号}`。届いた日時を付けて順に通訳ワーカーへ）、通知部（`/ws/events`）、字幕の「保存してから通知」、接続切れでの中断、通訳ワーカーの起動し直し、遷移規則の表。出荷するコンテナで本物のモデルを動かして確認した（`docs/meetings.md`）。画面の接続、字幕の状況・遅れの測定・警告（Epic 4）、要約を始める本体（Epic 5）、起動時の復旧（PBI 7.1）は含まない
- PBI #64: 通知の受信と再接続（`/ws/events`、切断でお知らせ帯、開通のたびの読み直し）、「会議中」の画面（タイトル・経過時間・音声の出どころ・案内・終了の確認と終了）、字幕の一覧（通知で追加・順番で重複を除く・最新まで自動で進む・過去へ戻っている間は進まず「最新の字幕へ戻る」・再接続のあとの読み直し）、`GET /api/meetings/{会議番号}/subtitles`。出荷するコンテナで本物のモデルを動かして確認した（`docs/live-screen.md`）。質問の領域・字幕の状況・遅れの警告・取り込み停止の選択は含まない
- PBI #68: 「会議の準備」の画面（タイトルと音声の出どころの入力と検査、取り込み手順の案内と注意書き、AI モデルの準備状況・入手のやり直し、開始の流れ: 取り込み → 会議を作る → 音声の接続 → 「会議中」、取り込めない・開始できないときの案内とお知らせ、取りやめる）。「会議の一覧」に「新しい会議を始める」の入口。「会議中」の終了で取り込みを止める。出荷するコンテナを Chromium の疑似入力で確認した（`docs/prepare-screen.md`）。会議の一覧の表示（PBI 6.1）、取り込み停止の選択（PBI 4.3）、会議の中断の表示、ディスクの空きの警告は含まない
- PBI #92: 作成ジョブ管理部（要約と質問の作成を、メモリの上で受け付け、生成ワーカーで 1 件ずつ実行）。質問が要約より先、同じ種類は受け付けた順。同じ会議・同じ種類の待機中か作成中は 1 件まで（2 回目は既存のジョブを返す）。作成中の要約は、質問が待てばブロックの区切りで順番を譲り、通訳中の会議があれば後回しにして、続きのブロックから再開する。会議の終了の直後は通訳ワーカーを止めてから要約を始め、実行できるジョブが無くなったら生成ワーカーを止める。生成ワーカーが止まったら作成中のジョブを失敗にする。受け付け・開始・譲る・後回し・再開・終了を、待った時間つきで処理ログへ出す。生成ワーカーの部品を、出荷するイメージへ入れた。結果の保存と会議の状態の変更（PBI 5.2・5.3）、作成の依頼の API、画面への表示、終了時のメモリ使用量の記録（PBI 4.1 の後）は含まない
- PBI #99: 会議の終了から要約へのつなぎ。字幕が 1 件以上の会議は、終了で自動で要約のジョブが登録され、4 項目の日本語の要約が保存されて、状態が完了になる（字幕 0 件は完了のまま、中断は自動では作らない）。`POST /api/meetings/{会議番号}/summary` が、中断・要約失敗・完了の会議の要約を作る・作り直す（202。同じ会議の要約のジョブがあれば、それを返す。字幕 0 件は `SUMMARY_NO_CONTENT`）。失敗では、状態が要約失敗になり、失敗理由（エラーコード）が保存され、作り直しの失敗では前の要約が残る。`job.status_changed` に待機中の理由 `wait_reason`、ブロックごとの `job.progress_changed`。出荷するコンテナで本物のモデルを動かして確かめた（`docs/jobs.md`「要約の確認」）。画面（要約を作るボタン・進み具合の表示）、会議の取得 API、起動時の復旧（PBI 7.1）、質問の保存（PBI 5.3）は含まない
- PBI #108: 「会議の記録」の画面（`GET /api/meetings/{会議番号}`、S-04）。タイトル・開始日時・会議の長さ・状態、要約の 4 項目（1 行ずつ箇条書き・文字として表示・「該当なし」）、作成状況と進み具合（待機中の添え書き）、失敗の表示（前の要約も併せて）、「要約を作る」「要約を作り直す」、質問のまとまり（新しい順・英日・作成した時点。無いときは「質問はまだありません。」）、字幕、通知での更新、記録が無いときは一覧へ移ってお知らせ帯。出荷するコンテナで確かめた（docs/record-screen.md）。質問を作るボタン・削除・書き出し・コピーは含まない
- PBI #104: 質問を作れる。質問の作成の依頼と保存（`POST /api/meetings/{会議番号}/question-sets`、5 組を 1 回の確定で保存）、会議中の生成ワーカーの CPU 割り当て（会議の状態で切り替え、作成中の状況は `job.status_changed` で通知）、会議中の質問の測定道具。画面の「字幕が遅れることがあります」の表示は会議中の画面の作業
