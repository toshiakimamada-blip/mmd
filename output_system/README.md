# 会議通訳アプリ（output_system）

英語の会議を日本語の字幕で追い、会議後に要約と質問を得るための Web アプリです。要件と基本設計は、リポジトリ直下の `ai_generated/` にあります。

## 配置

| フォルダ | 中身 |
|---|---|
| `backend/` | アプリサーバー。Python 3.12 と FastAPI で作ります。ロックファイルは `uv.lock` です |
| `frontend/` | 画面。TypeScript と React を Vite でビルドします。ロックファイルは `package-lock.json` です |
| `config/` | アプリに同梱する設定ファイル。調整値（`tuning.toml`）と、AI モデルの定義（`models.toml`。翻訳モデルの代わりの候補は `translation_alternatives.md`） |
| `tools/` | 補助の道具。画面の依存を足すときに、固定した Node.js で実行する `with-pinned-node.sh` など |
| `docs/` | 実装に付ける文書。エラーの扱いと、エラーコードの一覧は `docs/errors.md`。お知らせ帯とエラー表示の部品の使い方は `docs/notices.md`。AI モデルの入手と、翻訳モデルの変換（確かめた結果、部品、測定値）は `docs/models.md` |

テストは、各フォルダの `src/` と同じ階層の `test/` に置き、`src/` と同じ構造にします。起動したコンテナを相手にするテストだけは、`backend/test/container/` に置きます。ブラウザーで手動で確かめるための確認用のサーバーは、`backend/test/browser/` に置きます（pytest は集めません）。

## 画面の URL

1 つのページの中で、4 つの画面を切り替えます。URL で画面を指せるため、再読み込みしても同じ画面が開きます。URL の形は `frontend/src/routes.ts` で定義します。

| 画面 | URL |
|---|---|
| 会議の一覧（S-01） | `/` |
| 会議の準備（S-02） | `/meetings/new` |
| 会議中（S-03） | `/meetings/<会議番号>/live` |
| 会議の記録（S-04） | `/meetings/<会議番号>` |

どの画面にも当たらない URL は、会議の一覧へ移ります。

## 画面の実装の規約

画面は、会議の内容（字幕・要約・質問・タイトル）を、次のように扱います。基本設計書の非機能要件設計書「セキュリティ」の「画面に残す情報」「画面からの送信の防止」に従います。

- **会議の内容を、ブラウザーの保存領域へ書きません。** 保存領域は、`localStorage`・`sessionStorage`・`IndexedDB`・Cache API・Cookie・`window.name` です。画面を開くたびに、アプリサーバーから読み直します。保存領域の中身は、ブラウザーを閉じたあとも、利用者のほかの操作から見える場所に残るためです。
- **外部の資源を読み込みません。** 外部の CDN・Web フォント・解析サービスを使いません。必要なフォントや画像は、`frontend/` の中に置き、アプリに同梱します。ビルドの最後に、`scripts/check-no-external-urls.ts` が、ビルドした画面のファイルに外部の URL への参照が無いことを確かめます。参照があると、`npm run build` が失敗します。
- **小さな画像やフォントを、`data:` の URL として埋め込みません。** 応答ヘッダー（CSP）が `data:` を止めるためです。ビルドの設定（`vite.config.ts` の `assetsInlineLimit: 0`）で埋め込みを止めています。
- **スクリプトとスタイルを、HTML の中に直接書きません（インラインにしません）。** 同じ理由で、ブラウザーが実行を止めます。Worker とワークレットは、`blob:` の URL ではなく、アプリが配信するファイルから読み込みます。
- 会議の内容を、URL（経路と問い合わせの文字列）に載せません。URL はログと履歴に残るためです。
- 会議の内容は、常に文字として表示します（`dangerouslySetInnerHTML` と `innerHTML` への代入は、ESLint が禁止しています）。

## アプリサーバーの経路と設定

アプリサーバーは、1 つのポート（既定は 3001）で、画面のファイル・HTTP・WebSocket を受けます。経路は次の 3 つに分けます。

| 経路 | 役割 |
|---|---|
| `/api` 以下 | HTTP（JSON）。動作の確認は `GET /api/health` です。存在しない経路は、画面のファイルではなく、404 の JSON のエラーを返します |
| `/ws` 以下 | WebSocket。存在しない経路は、HTTP の要求には 404 の JSON を返します。WebSocket の接続要求は、接続を受け付けず、403 で断ります（アプリの中では、コード 1008 で閉じる指示を出します） |
| それ以外 | 画面のファイル。画面の URL にはトップのファイル（`index.html`）を返します。拡張子を持つ名前（`/assets/app.js` など）が存在しなければ 404 です。そのため、会議番号に「.」を使いません |

ミドルウェアと経路の登録は、`backend/src/meeting_interpreter/app.py` の `create_app` にまとめます。

### WebSocket の部品

uvicorn は、WebSocket の部品（`websockets`）が無いと、WebSocket の接続要求を、ふつうの HTTP の要求として扱います。`/ws` は WebSocket として動かず、`/ws` 以外の経路には、接続要求に対して画面のファイルが返ります。そのため、次の 2 つを行っています。

- `websockets` を `backend/pyproject.toml` の依存に入れます。
- 起動のときに、uvicorn へ WebSocket の実装の名前（`websockets-sansio`）を指定します（`backend/src/meeting_interpreter/__main__.py` の `WEBSOCKET_IMPLEMENTATION`）。部品が無いイメージは、起動に失敗し、コンテナが healthy になりません。名前を指定しない既定（`auto`）は、部品が無くても黙って起動します。

Starlette の `TestClient` は uvicorn を通らないため、部品が無くても WebSocket のテストは通ります。本物の uvicorn で確かめるテストは `backend/test/meeting_interpreter/test_websocket_server.py` にあります。出荷するイメージで確かめる手順は、[出荷するコンテナの WebSocket の確認](#出荷するコンテナの-websocket-の確認)にあります。

動かす場所で変わる値は、環境変数で渡します。コンテナでは `docker-compose.yml` で指定します。名前は、アプリの共通の接頭辞 `MMD_` を付けた大文字です。

| 環境変数 | 意味 | 必須 | 既定値 |
|---|---|---|---|
| `MMD_PORT` | 待ち受けるポート | いいえ | `3001` |
| `MMD_APP_HOSTNAME` | 要求の宛先として常に通す、アプリのコンテナ名 | いいえ | 未設定 |
| `MMD_RECORDS_DIR` | 記録データベースを置く場所 | はい | なし（コンテナのイメージは `/var/lib/mmd/records` を渡します） |
| `MMD_MODELS_DIR` | AI モデルを置く場所 | はい | なし（コンテナのイメージは `/var/lib/mmd/models` を渡します） |
| `MMD_STATIC_DIR` | ビルドした画面のファイルの場所 | いいえ | `/app/static` |
| `MMD_CONFIG_DIR` | アプリに同梱する設定ファイル（`tuning.toml`、`models.toml`）の場所 | いいえ | `/app/config` |
| `MMD_CONVERSION_PYTHON` | 翻訳モデルの変換プロセスを動かす Python（変換の部品を入れた環境のもの）。実行できるファイルであること | いいえ | アプリサーバーを動かしている Python（コンテナのイメージは `/opt/conversion-venv/bin/python` を渡します） |
| `MMD_LOG_LEVEL` | ログのレベルの下限。`info`・`warning`・`error` のいずれか（大文字小文字は問いません） | いいえ | `info` |

- 空（または空白だけ）の環境変数は、未設定として扱います。
- 記録とモデルの置き場は、既定値を持たせません。名前付きボリュームの置き場と食い違ったまま、別の場所へ黙って書き込む事故を防ぐためです。
- 誤りは起動の時点で見つけます。必須の環境変数が無いとき、値が誤っているとき、調整値のファイルが読めないときは、サーバーを起動せず、終了コード 2 で止まります。誤りは見つけたものをすべて、標準出力に 1 行 1 件で出します。
- 秘密の情報（パスワード・鍵）は持ちません。設定を画面から変える機能もありません。

### 接続制限

アプリはログインを求めません。代わりに、接続元を「動かしている PC の中」に限ります。判定は `backend/src/meeting_interpreter/access.py` にあり、HTTP の要求も WebSocket の接続も、同じ関数（`judge_access`）を使います。

| 確かめる対象 | 通す条件 | 守っている場所 |
|---|---|---|
| 公開するポート | PC のループバック（`127.0.0.1`）にだけ公開します | `docker-compose.yml` の `ports` |
| 要求の宛先（Host ヘッダー） | `localhost`・`127.0.0.1`・アプリのコンテナ名（`MMD_APP_HOSTNAME`）のどれかです。ポートは確かめません | `access.py` |
| 要求の送り元（Origin ヘッダー） | 付いていれば、宛先と同じ接続先（スキーム・ホスト・ポート）です。読み取りだけのメソッド（`GET`・`HEAD`・`OPTIONS`）以外では、必ず付いている必要があります。WebSocket の接続では、常に必須です | `access.py` |
| ほかのサイトへの読み取りの許可 | 出しません（`Access-Control-*` のヘッダーを付けません） | ― |

- ミドルウェア（`AccessControlMiddleware`）が、`create_app` で配信部を含むすべての経路を包みます。経路をあとから足しても、この確認を通らない経路は作れません。
- 断った HTTP の要求は、本文を読まずに、`403` と `ACCESS_HOST_NOT_ALLOWED`・`ACCESS_ORIGIN_MISSING`・`ACCESS_ORIGIN_MISMATCH` のどれかの共通の形で返します。受け取った宛先と送り元の値は、応答に含めません。
- 断った WebSocket の接続は、受け付けずに閉じます（符号 1008）。サーバー（uvicorn）は、接続要求に `403` を返します。ブラウザーは、ほかのサイトからの WebSocket の接続を止めません。そのため、アプリが確かめる送り元が唯一の守りです。
- 断った記録は、接続ログ（警告）に出ます。項目は、断った理由の種類（`host_not_allowed`・`origin_missing`・`origin_mismatch`）と、宛先・送り元の名前だけです。経路・問い合わせ・本文は出しません。
- Host と Origin は、1 件ずつだけ読みます。同じヘッダーが複数あるときは断ります。
- コンテナの死活の確認（`HEALTHCHECK`）は `127.0.0.1` 宛てに送るため、そのまま通ります。
- 出荷するコンテナで確かめるテストは、`backend/test/container/` にあります（[出荷するコンテナの WebSocket の確認](#出荷するコンテナの-websocket-の確認)と同じ手順で動きます）。
- 通す条件は、利用時も自動テスト時も同じです。テストのために切り替える設定はありません。自動テストのブラウザーは、コンテナ名（`http://toshiakimamada-blip-mmd-output-system:3001`）でアプリへ接続し、宛先と送り元の両方がコンテナ名になるため、そのまま通ります。
- サーバーのテストは、`backend/test/conftest.py` の `local_client` で作るクライアントを使います。利用者が `http://localhost:3001` を開いたときと同じ宛先と送り元を付けます。別の宛先や送り元を試すときは、要求ごとに `headers` を渡します。

### 応答ヘッダー（画面の読み込み先と通信先の制限）

アプリサーバーは、HTTP の応答のすべてに、次のヘッダーを付けます（`backend/src/meeting_interpreter/security_headers.py`）。画面のファイル、API、エラー、接続制限が断った応答にも付きます。経路が同じ名前のヘッダーを付けても、決めた値に置き換えます。

| ヘッダー | 値 | 目的 |
|---|---|---|
| `Content-Security-Policy` | 既定は `default-src 'none'`。スクリプト・スタイル・画像・フォント・通信先は `'self'`（アプリ自身）だけ。`base-uri`・`form-action`・`object-src`・`frame-ancestors` は `'none'` | 外部の資源の読み込みと、外への送信を、ブラウザーに止めさせる |
| `X-Content-Type-Options` | `nosniff` | 宣言した種類と違う種類として、中身を解釈させない |
| `Referrer-Policy` | `no-referrer` | 外へ出る要求に、アプリの URL（会議番号を含む）を送らない |
| `X-Frame-Options` | `DENY` | ほかのサイトが、画面をフレームに埋め込めない。`frame-ancestors 'none'` の重ね書き |

- 通信先の `'self'` は、同じホストとポートの WebSocket（`ws:`）を含みます。Chrome（156）と Edge（155）で、`localhost`・`127.0.0.1`・ホスト名の 3 通りの宛先から WebSocket が接続できることを確かめました。手順は、[ブラウザーでの WebSocket の確認](#ブラウザーでの-websocket-の確認)にあります。`'self'` が WebSocket を含まないブラウザーが出たら、`CONTENT_SECURITY_POLICY` の `connect-src` に `ws://localhost:3001` などの接続先を明示します。
- `upgrade-insecure-requests` は付けません。アプリは `http://localhost` で開くためです。
- WebSocket の接続には、ヘッダーを足しません。ヘッダーを付けるのは、HTTP の応答だけです。

### 調整値

発話の区切りの長さの上限、警告を出すしきい値、文章生成モデルへ一度に渡す量、CPU の割り当てなどは、アプリに同梱するファイル `config/tuning.toml` で持ちます。値はすべて仮置きで、利用者の PC で測った結果をもとに見直します。環境変数でも上書きできます。名前は、項目名を大文字にして `MMD_TUNING_` を付けます（例: `utterance_max_seconds` は `MMD_TUNING_UTTERANCE_MAX_SECONDS`）。項目の一覧と意味は `config/tuning.toml` のコメントにあります。

## AI モデルの入手

3 つの AI モデル（文字起こし・翻訳・文章生成）は、コンテナのイメージに含めません。アプリの起動のたびに、モデル管理部（`backend/src/meeting_interpreter/models/`）が、置き場の確認と、足りないファイルの入手を非同期で始めます。アプリの起動は、入手の完了を待ちません。

### 流れ

1. **確認**: 同梱の定義（`config/models.toml`）の全ファイルについて、モデル置き場のファイルが定義の SHA-256 と一致するかを確かめます。一致しないファイルは捨てます。一致を確認できたファイルは入手し直しません。
2. **ディスクの空きの確認**: 入手するファイルがあれば、先に空きを確かめます。必要な量は「入手する分 + 予備 2GiB」です（`fetch.py` の `DISK_RESERVE_BYTES`。非機能要件設計書の「約 5GB」は仮置きの見込みで、入手するファイルの大きさが決まったため、この式にしました）。足りなければ、入手を始めずに失敗にします。
3. **入手**: 一時名（`<ファイル名>.part`）で入手し、定義の SHA-256 と一致してから、正しい名前へ移します。途中で止まったときは、次の起動で、一時名のファイルの続きから入手します（Range 要求）。
4. **変換**: 翻訳モデルを、実行基盤（CTranslate2）の形式へ変換します（`backend/src/meeting_interpreter/models/conversion/`）。変換後のファイルが、変換後のハッシュ値の控えと一致していれば、変換も入手もしません。変換後のファイルが無い・控えが無い・一致しないときは、入手済みのファイルから変換し直します（入手はやり直しません）。変換の間、準備状況は「変換中」（進み具合は出ません）で、3 つの AI モデルがそろって変換が済むと「準備完了」になります。変換に失敗すると、途中のファイルを捨てて入手失敗（原因: 翻訳モデルを変換できない）になり、入手のやり直しで、変換だけをやり直します。

モデル置き場（名前付きボリューム `MMD_MODELS_DIR`）の配置は `<モデル名>/<固定した版>/<ファイル名>` です。版を上げても、古い版のファイルを上書きしません。

### 準備状況の取得とやり直し

| 経路 | 内容 |
|---|---|
| `GET /api/models/status` | 準備状況（`checking`・`downloading`・`converting`・`ready`・`failed`）。入手中は進み具合（`progress.downloaded_bytes` と `progress.total_bytes`）。入手失敗のときは原因の種類（`failure_kind`）とエラーコード（`error_code`） |
| `POST /api/models/retry` | 入手のやり直し。入手失敗のときだけ受け付け（202）、それ以外は業務エラー `MODEL_RETRY_NOT_ALLOWED`（409）。入手は非同期で始まります |

準備状況は、アプリサーバーのメモリの上だけで持ちます（記録データベースには置きません）。変化は、通知 `model.status_changed` として発行します。通知の送り先（画面への WebSocket）は別の作業で作るため、いまは `ModelStatusNotifier` の形の受け口だけがあります。原因の種類とエラーコードは [docs/errors.md](docs/errors.md) にあります。

入手の自動の再試行はしません。失敗したら止まり、利用者の「入手をやり直す」で再開します。

### 外向きの通信を行う箇所は 1 つだけ

アプリが外向きに通信するのは、**`backend/src/meeting_interpreter/models/download.py`（`HttpsDownloader`）だけ**です。準備完了のあとは、アプリが外部と通信しません（ADR-14）。

- 通信の部品（`socket`・`ssl`・`http.client`・`urllib.request`・`requests`・`httpx`・`huggingface_hub` など）を取り込んでよいモジュールは、`download.py` だけです。例外は `healthcheck.py` の 1 つで、同じコンテナの中のアプリサーバー（`127.0.0.1`）への死活の確認に `urllib.request` を使います。
- この決まりは、自動テスト `backend/test/meeting_interpreter/test_network_boundary.py` が守ります。ソースコード全体を構文木で調べる**依存の向きの検査**（違反するとテストが失敗します）と、ソケットの接続を監視する**実行時の検査**（入手の部品以外のアプリのコードが接続すると失敗します。準備完了のあと、接続も名前の解決も起きないことを確かめます）です。
- 新しい処理が通信を必要とするときは、`download.py` に足します。ほかのモジュールは、通信しない形の入口（`fetch.py` の `FileDownloader`）を介して使います。
- AI の実行基盤の自動入手と利用状況の送信は使いません。入手は、標準ライブラリの HTTPS クライアントで自前に行います。
- 通信の相手は、許したホストだけです。入手先（`config/models.toml` の `repository_url`）は `huggingface.co` に限り、入手の要求と転送先は `huggingface.co` と `*.hf.co`（ファイルの実体の置き場。CDN）に限ります。許可リストは、設定ファイルではなく、`backend/src/meeting_interpreter/models/hosts.py` の定数です（許可を広げる変更が、コードの変更として見えるように）。転送先が許したホストでないときは、転送に従わず、入手失敗（原因の種類は「配布元が応答しない」）にします。CDN のホスト名が変わって入手できなくなったときは、`hosts.py` の `TRANSFER_HOSTS` を、確かめたうえで直します。

### ワーカーと変換プロセスの起動

実行基盤（Hugging Face の部品、llama.cpp など）は、モデルを自動で入手する機能と、利用状況を送る機能を持つことがあります。ワーカーと変換プロセスは、必ず `backend/src/meeting_interpreter/child_process.py` の `start_child_process` で起動します。次の環境変数が必ず付き、呼び出し側から外せません。アプリサーバー自身にも、起動のときに反映します（子のプロセスは引き継ぎます）。

| 環境変数 | 止めるもの |
|---|---|
| `HF_HUB_OFFLINE`・`TRANSFORMERS_OFFLINE`・`HF_DATASETS_OFFLINE` | Hugging Face の部品の、モデルとデータの自動入手 |
| `HF_HUB_DISABLE_TELEMETRY`・`DO_NOT_TRACK` | 利用状況の送信 |
| `LLAMA_ARG_OFFLINE` | llama.cpp の通信（キャッシュだけを使う） |

`subprocess` と `multiprocessing` を取り込んでよいのは `child_process.py` だけです（上のテストが守ります）。

### 証明書

HTTPS の証明書は、必ず確かめます（確認を無効にする設定は持ちません。自動テストが、そのような書き方を探します）。社内のネットワークが通信を検査する場合の証明書は、イメージのビルドでシステムの証明書の保管先に登録します（[社内の証明書](#社内の証明書)）。入手の部品の TLS の文脈（`ssl.create_default_context`）は、その保管先と、環境変数 `SSL_CERT_FILE`・`SSL_CERT_DIR` を読みます（`Dockerfile` が指します）。certifi の証明書の一覧は、使わず、書き換えもしません。プロキシは、環境変数（`https_proxy`・`no_proxy`）に従います。

### 自動テストでの扱い

数 GB の本物の入手を避けるため、テストは手元の擬似の配布元（`backend/test/distribution_server.py`。HTTPS・自己署名の証明書・Range 対応）を使います。`backend/test/conftest.py` は、テスト全体で、手元（ループバック）以外への接続と名前の解決を失敗にします。アプリの起動で入手が始まらないよう、他のテストのアプリは、開始しても何もしないモデル管理部で組み立てます。

## ログ

ログは、コンテナの標準出力へ 1 行 1 件の JSON で出します（標準エラー出力には出しません）。コンテナのログは `docker compose logs` で読めます。種別と項目は、基本設計書の横断設計規約「ログの種別と項目」に従います。

| 種別 | `log_type` | レベル | 項目 |
|---|---|---|---|
| 処理ログ | `processing` | 情報(`info`) | `process_type`（処理の種類）、`meeting_id`（会議番号）、`trace_id`（処理の識別子）、`duration_ms`（かかった時間）、`item_count`（件数）、`char_count`（文字数） |
| エラーログ | `error` | 業務エラーは警告(`warning`)、システムエラーは異常(`error`) | `error_code`、`component`（発生したコンポーネント）、`meeting_id`、`trace_id`。想定外の例外は、`exception_type`（種類）と `location`（発生場所）も出す |
| 接続ログ | `connection` | 警告(`warning`) | `reject_reason`（断った理由の種類）、`destination_name`（宛先の名前）、`source_name`（送り元の名前） |

- どの行にも、`timestamp`（UTC・ミリ秒）、`log_type`、`level` が付きます。
- 「必ず含める項目」は、値が無いときも `null` で出します。
- 利用者の項目は持ちません。ログインが無いためです。
- 会議の内容は、ログに書けません。字幕・要約・質問の本文、会議のタイトル、文章生成モデルへ渡す指示と受け取った出力、音声は、渡しても出力されません。書ける項目は、`backend/src/meeting_interpreter/logs/fields.py` の許可リストで決めています。許可リストに無い項目と、決めた形に当てはまらない値は捨てます。捨てた数は `dropped_field_count` に出します。名前も値も出しません。
- 項目を足すときは、許可リストに `FieldSpec` を 1 つ足します（性能の測定の値を足す作業で使います）。値の形は、会議の内容が入り込めない狭い形にします。
- 処理の識別子は、1 回の要求に 1 つ振ります（ミドルウェアが振ります）。ワーカーへ渡すメッセージには、`attach_trace_id` で付けます。受け取った側は、`trace_scope_from_message` で同じ識別子の範囲に入ります。
- 標準のロガー（`logging`）を使う部品（uvicorn など）のログも、標準出力へ出します。`log_type` は `runtime` で、項目は `logger`（ロガーの名前）と `message`（文面）です。文面は、出してよいと決めた記録だけに付けます。それ以外は、`logger`・`level`・`exception_type`（例外の種類）だけを出し、文面と引数は出しません（出さなかった数は `dropped_field_count` に出します）。例外の文面とスタックトレースも出しません。
  - 文面を出す記録は、2 つだけです。起動の知らせ用のロガー `STARTUP_LOGGER`（`meeting_interpreter.startup`）の記録と、uvicorn の起動・終了・接続の開始と終了の知らせです。後者は、文の形が `logs/output.py` の `_UVICORN_FIXED_MESSAGES` にあるものに限ります。
  - 要求の経路と問い合わせの文字列を含む uvicorn の記録（アクセスの記録、接続の受け付けと拒否）は、文面を出しません。アクセスの記録は、起動の引数（`access_log=False`）でも止めています。
  - `STARTUP_LOGGER` には、環境変数の名前・設定の値・ファイルの置き場などの、設定に関わる固定の文だけを渡します。会議の最中の記録には使いません。
  - 会議に関わる記録には、標準のロガーを使わず、`log_processing`・`log_error`・`log_connection` を使います。標準のロガーに会議の内容を渡しても、文面は出ませんが、調べる手がかり（項目）も残らないためです。
  - 標準のロガーを通らない出力（`print`、`sys.stdout` への直接の書き込み、`warnings`、スレッドの中の未処理の例外の表示）は、この仕組みの外です。`print` は ruff で禁止しています。ワーカーなど別のプロセスを足すときは、そのプロセスの最初で `configure_logging` を呼びます。
- `runtime` は、基本設計書の「ログの種別と項目」にある 3 種別（処理ログ・エラーログ・接続ログ）の外です。設定の誤りで起動できない理由を、環境変数の名前つきの日本語の文で出す必要があり、3 種別の項目には文を載せる欄が無いために設けています。
- ログのレベルの下限は、環境変数 `MMD_LOG_LEVEL` で決めます。性能の測定は情報のレベルの処理ログを使うため、既定は `info` です。

### コンテナを使わずにアプリサーバーを動かす（開発用）

先に `frontend/` で `npm run build` を実行してから、`backend/` で次を実行します。

```bash
MMD_STATIC_DIR=../frontend/dist MMD_CONFIG_DIR=../config \
MMD_RECORDS_DIR=/tmp/mmd/records MMD_MODELS_DIR=/tmp/mmd/models \
PYTHONPATH=src uv run --no-default-groups python -m meeting_interpreter
```

## 必要な道具

| 道具 | 版 | 用途 |
|---|---|---|
| Python | 3.12 系（Ubuntu 24.04 の標準） | アプリサーバーの実行 |
| uv | 0.12 以降 | Python の依存の導入とロック |
| Node.js | 24 系（LTS） | 画面のビルドとテスト |
| npm | 11.10 以上（Node.js 24 系に同梱） | 画面の依存の導入とロック。古い npm では、導入が止まります（[依存の追加と更新](#依存の追加と更新)） |
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

`npm ci` は、npm 11.10 以上と Node.js 24 系が要ります。開発用のコンテナの Node.js が 22 系のときは、`npm ci` の前に `../tools/with-pinned-node.sh` を付けます。ほかのコマンドは、そのまま実行できます。

```bash
npm ci                    # 依存を入れる（ロックファイルの版をそのまま入れる）。22 系のときは ../tools/with-pinned-node.sh npm ci
npm test                  # テスト（Vitest）
npm run test:coverage     # カバレッジ付きのテスト
npm run lint              # lint（ESLint）
npm run typecheck         # 型チェック
npm run build             # 本番用のビルド（dist/ に出る）。最後に、外部の URL への参照が無いことも確かめる
npm run check:dist        # ビルドした dist/ だけを、外部の URL への参照が無いか確かめる
```

## 依存の追加と更新

依存の版は、ロックファイルで固定します。追加と更新は、次のとおり行います。

- Python: `backend/` で `uv add <パッケージ>` を実行し、`uv.lock` も一緒にコミットします。`uv.lock` の形式の版（`revision`）は、書いた uv の版で決まります。Dockerfile の `UV_VERSION` より新しい uv で書くと、`revision` が上がります。ロックし直すときは、`uvx --from uv==<UV_VERSION の値> uv lock` のように、同じ版の uv を使います
- Python の依存グループ: `dev`（テスト用）と `conversion`（翻訳モデルの変換の部品。PyTorch など）は、開発環境（`uv sync`）には入りますが、イメージのアプリサーバーの環境には入れません（`--no-default-groups`）。変換の部品は、イメージの別の環境に入れます（[docs/models.md](docs/models.md)）
- 画面: `frontend/` で `../tools/with-pinned-node.sh npm install <パッケージ>` を実行し、`package-lock.json` も一緒にコミットします。素の `npm install` は、npm が 11.10 未満だと止まります（下の「公開から 7 日未満の版を使わない設定」を参照）
- 依存は公式の配布元（PyPI と npm）から入手します。既知の脆弱性がある版は選びません

### 公開から 7 日未満の版を使わない設定

サプライチェーン攻撃は、公開から 1 週間以内に見つかって取り下げられることが大半です。そのため、版を新しく決めるときは、公開から 7 日未満の版を対象にしません。

| 道具 | 設定 | 置き場所 |
|---|---|---|
| uv | `exclude-newer = "7 days"` | `backend/pyproject.toml` の `[tool.uv]` |
| npm | `min-release-age=7` | `frontend/.npmrc` |

- どちらの設定も、ロックファイルに記録済みの版には影響しません。版が新しく決まる `uv add`・`uv lock`・`npm install` のときに効きます。
- `min-release-age` は npm 11.10 以上で効きます。古い npm は、この設定を黙って無視します（npm 10.9.9 は、`--min-release-age=36500` を付けても版を選んで導入します。npm 11 は「版が無い」と失敗します）。そのため、次の 3 つで npm の版をそろえます。
  - `frontend/package.json` の `engines` で、Node.js 24 以上・npm 11.10 以上を求めます。
  - `frontend/.npmrc` の `engine-strict=true` で、`engines` に合わない環境の `npm install`・`npm ci`・`npm update` を、警告ではなく失敗にします。
  - 失敗したときは、`tools/with-pinned-node.sh` で実行し直します。イメージのビルドと同じ版の Node.js（`Dockerfile` の `NODE_VERSION`、SHA-256 を確認してから使う）を `~/.cache/mmd-node` に入れ、そこに同梱の npm で実行します。
- 依存を足したあとは、足した版の公開日が 7 日以上前であることを確かめます。画面は `npm view <パッケージ> time --json` の出力で版の行を、サーバーは `backend/uv.lock` の `upload-time` を見ます。
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

### 出荷するコンテナの WebSocket の確認

起動したコンテナへ実際に接続して、`/ws` が WebSocket として扱われることを確かめます。`backend/` で実行します。

```bash
docker compose up -d --build        # output_system/ で実行する
cd backend
MMD_CONTAINER_URL=http://toshiakimamada-blip-mmd-output-system:3001 uv run pytest test/container --no-cov
```

- 存在しない `/ws` への WebSocket の接続要求を、コンテナが 403 で断れば合格です。WebSocket の部品が無いイメージでは、404 の JSON が返り、失敗します。
- 接続制限も同じ手順で確かめます。宛先を変えた HTTP の要求（`test_container_access.py`）と、送り元が無い・違う WebSocket の接続要求（`test_container_websocket.py`）が、コンテナの設定（`MMD_APP_HOSTNAME` とコンテナ名の対応）まで含めて通ることを見ます。
- 応答ヘッダー（CSP など）も同じ手順で確かめます。画面のファイル・API・エラー・接続制限が断った応答のすべてに、決めたヘッダーが付くことを見ます（`test_container_access.py`）。古いイメージを起動していると、ここで失敗します。
- `MMD_CONTAINER_URL` が未設定のときは、この確認を飛ばします（`uv run pytest` の通常の実行では skipped になります）。
- 受け付ける経路ができたあとは、その経路の接続と往復を、このフォルダーのテストに足します。

### ブラウザーでの WebSocket の確認

画面の読み込み先と通信先の制限（CSP）のもとで、ブラウザーが WebSocket を接続できることを確かめます。`connect-src 'self'` が WebSocket（`ws:`）を含むかどうかは、ブラウザーごとに差が出うるため、Edge と Chrome の両方で確かめます。自動テスト（pytest）はブラウザーを使いません。

出荷するアプリには、まだ接続を受け付ける `/ws` の経路がありません。そこで、確認用のサーバーを使います（`backend/test/browser/serve_websocket_check.py`）。本物のアプリ（`create_app`）に、確認用の経路（`/ws/probe`）を 1 つ足したものです。接続制限と応答ヘッダーはアプリのままで、確認のために緩める設定はありません。

```bash
cd backend
uv run python test/browser/serve_websocket_check.py --port 3101 --app-hostname <この PC のコンテナ名>
```

ブラウザーで、次の URL を順に開きます。コンテナ名の URL は、`--app-hostname` に渡した名前を使います。

- `http://localhost:3101/`
- `http://127.0.0.1:3101/`
- `http://<コンテナ名>:3101/`

- 画面に「接続できました」と出れば、接続できています。
- ブラウザーのコンソールに、CSP の違反（`violates the following Content Security Policy directive`）が出ないことも見ます。
- 出荷するアプリの 4 つの画面を開き、コンソールに何も出ないことも見ます（外部の資源を読み込まない確認）。
- Ctrl+C で止めます。確認用の画面は一時のフォルダーに作り、止めると消えます。

### 社内の証明書

社内のネットワークが通信を検査する場合は、証明書をイメージのビルドで登録します。証明書がある環境と無い環境で、同じ手順でビルドできます。

- 既定では、リポジトリ直下の `ssl-certificates/` の `.cer`・`.crt`・`.pem` を登録します。PEM 形式と DER 形式の両方に対応します。フォルダーが空なら、何も登録しません。
- このフォルダーが存在しない環境では、空のフォルダーを作り、`SSL_CERTS_DIR=<空のフォルダー> docker compose build` でビルドします。
- `docker compose` を使わずにビルドする場合は、証明書がある環境で `--build-context certs=<証明書のフォルダー>` を付けます。

### イメージの版の固定

- Node.js は、`Dockerfile` の `NODE_VERSION` と、アーカイブの SHA-256（`NODE_SHA256_X64` と `NODE_SHA256_ARM64`）で固定します。版を上げるときは、公開から 7 日以上たった版を選び、配布元の `SHASUMS256.txt` の値へ更新します。
- uv は、`Dockerfile` の `UV_VERSION` で固定します。
- 画面の依存は `npm ci`、サーバーの依存は `uv sync --locked` で、ロックファイルの版だけを入れます。
