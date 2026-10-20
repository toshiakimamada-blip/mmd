# エラーの扱い

画面へ返すエラーの共通の形と、エラーコードの一覧です。分類と見せ方は、基本設計書の横断設計規約「エラーの分類と見せ方」と、画面設計書「エラー表示パターン」にあります。

## 共通の形

サーバーが画面へ返すエラーは、次の 4 項目だけを持つ JSON です。

```json
{
  "category": "business_error",
  "code": "MEETING_ALREADY_RUNNING",
  "message": "進行中の会議があります。",
  "action": "進行中の会議を終了してから、新しい会議を開始してください。"
}
```

| 項目 | 内容 |
|---|---|
| `category` | 分類。`input_error`（入力エラー）・`business_error`（業務エラー）・`system_error`（システムエラー） |
| `code` | エラーコード。`領域_出来事`（大文字・アンダースコア）。領域は `AUDIO`・`MEETING`・`SUBTITLE`・`SUMMARY`・`QUESTION`・`RECORD`・`MODEL`・`ACCESS`・`SYSTEM` の 9 つ |
| `message` | 何が起きたか。固定の文。会議の内容を入れない |
| `action` | 利用者が次に行う操作。固定の文。会議の内容を入れない |

プログラムの例外の文面とスタックトレースは、画面にもログにも出しません。

## サーバーでの起こし方

分類ごとの例外を起こします（`backend/src/meeting_interpreter/errors.py`）。

| 例外 | 分類 | HTTP の状態（既定） | ログ |
|---|---|---|---|
| `InputAppError` | 入力エラー | 422 | 出さない |
| `BusinessAppError` | 業務エラー | 409 | エラーログ・警告 |
| `SystemAppError` | システムエラー | 500 | エラーログ・異常 |

```python
raise BusinessAppError(
    "MEETING_ALREADY_RUNNING",
    "進行中の会議があります。",
    "進行中の会議を終了してから、新しい会議を開始してください。",
    meeting_id=meeting_id,
)
```

- コードの形が誤っていると、例外を作った時点で失敗します。
- `status_code` で HTTP の状態を変えられます（例: 存在しない会議は 404）。
- `component` を省くと、例外を起こしたモジュールの名前が入ります。

### 想定外の例外

どの分類にも当てはまらない例外は、`UnhandledErrorMiddleware` が `SYSTEM_UNEXPECTED_ERROR`（HTTP 500）に変えます。

- 応答の本文に、例外の文面は入りません。
- エラーログ（異常）に、例外の種類（`exception_type`）と発生場所（`location`）だけを出します。
- 例外は外へ伝えません。外へ伝えると、サーバー（uvicorn）が文面つきのスタックトレースを記録します。
- WebSocket では、接続を内部エラーの符号（1011）で閉じます。
- 応答が始まったあとの例外は、本文を変えられないため、エラーログだけを出します。

FastAPI が自動で返す応答も、共通の形に変えています。

| 場面 | コード | 分類 |
|---|---|---|
| 要求の内容が決めた形でない（FastAPI の検証の失敗） | `SYSTEM_INVALID_REQUEST` | 入力エラー（422）。送られた値は返しません |
| 存在しない経路・存在しないファイル | `SYSTEM_ROUTE_NOT_FOUND` | システムエラー（404） |
| 経路で許していないメソッド | `SYSTEM_METHOD_NOT_ALLOWED` | システムエラー（405） |
| そのほかの HTTP の例外 | `SYSTEM_REQUEST_FAILED` | システムエラー（元の状態） |

## 画面での扱い

画面は、サーバーが返した `message` と `action` を表示しません。エラーコードごとの文言を、画面側の一覧から引きます（`frontend/src/errors/errorMessages.ts`）。

- 一覧に無いコードには、分類ごとの汎用の文言を出します。コードは必ず添えて表示します。
- 応答の本文が共通の形でないとき（プロキシの HTML のエラーなど）は、`SYSTEM_UNEXPECTED_ERROR` として扱います（`readErrorResponse`）。

## エラーコードを足す手順

1. サーバーで、分類に合う例外を起こします。コードは `領域_出来事` の形にします。
2. 画面の `ERROR_WORDINGS` に、同じコードの文言（何が起きたか・次に行う操作）を足します。
3. 文言は、業務用語辞書（データモデル設計書）の用語にそろえ、会議の内容を入れません。

## 登録済みのコード

| コード | 分類 | 何が起きたか |
|---|---|---|
| `SYSTEM_UNEXPECTED_ERROR` | システムエラー | 予期しない問題が起きました |
| `SYSTEM_ROUTE_NOT_FOUND` | システムエラー | 要求された経路がありません |
| `SYSTEM_INVALID_REQUEST` | 入力エラー | 送った内容に誤りがあります |
| `SYSTEM_METHOD_NOT_ALLOWED` | システムエラー | この操作は受け付けられません |
| `SYSTEM_REQUEST_FAILED` | システムエラー | 要求を処理できませんでした |
| `SYSTEM_CONFIG_INVALID` | システムエラー | 設定に誤りがあり、起動できません（起動の失敗のエラーログにだけ出ます。画面には出ません） |

各機能のコードは、その機能の作業で足します。
