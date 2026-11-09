# 通知の受信と再接続

アプリサーバーが `/ws/events` で送る通知を、画面のどこからでも購読するための部品です。部品は `frontend/src/events/` にあります。通知の種類と項目は [会議の開始と終了、字幕の保存と通知](meetings.md#通知wsevents) にあります。

## 使い方

`AppFrame` が `EventsProvider` を 1 つだけ置きます。画面を切り替えても枠は作り直されないため、接続は 1 本を使い回します。画面の部品は、次の 2 つの hook を使います。

```tsx
// 通知を購読する。部品が画面にある間だけ受け取る。型は通知の種類で決まる
useEvent('subtitle.created', (event) => {
  if (event.meetingId === meetingId) add(event.subtitle);
});

// アプリサーバーとの接続が開通するたびに、表示中の内容を読み直す
useReloadOnConnect(async () => {
  await loadSubtitles();
});
```

- `useReloadOnConnect` は、登録した時点ですでに開通しているときは呼びません。画面を開いた時点の読み込みは、画面が自分で行います。登録したあとに開通する（開通前に登録した、または切れてつなぎ直した）ときに呼びます。
- 通知は重複して届くことがあります。呼び出し側は、同じ通知が 2 回届いても状態が壊れないように作ります（字幕の一覧は `mergeSubtitles` が、順番が同じ字幕を 1 回だけ持ちます）。
- 通知の種類を足すときは、`eventTypes.ts` の `AppEvent` に型を足し、`parseAppEvent` に読み替えを足します。形が合わない通知と知らない種類の通知は読み捨てます。

## 再接続

接続が切れたら、間隔を空けて自動でつなぎ直します。間隔は 1 秒、2 秒、4 秒、8 秒、10 秒で、開通しないまま切れ続けると 1 つずつ進み、10 秒で頭打ちです。開通すると 1 秒に戻ります（`EventConnection.ts` の `DEFAULT_RETRY_DELAYS_MS`）。

接続が切れている間は、お知らせ帯に「アプリとつながっていません」を出し、つながると消します。接続を始めただけの間（まだ開通も切断も起きていない間）は出しません。

## 接続元の制限との関係

接続先は、画面を開いているページと同じ宛先とポートの `/ws/events` です（`buildEventsUrl`）。アプリサーバーの接続元の制限は、WebSocket に送り元（Origin）を求めます。Origin はブラウザーが自動で付けます。画面では付けず、偽装もしません。

## テスト

`test/setup.ts` が、全テストで `WebSocket` を、開通も切断もしない偽物に置き換えます。通知を確かめるテストは、`test/events/fakeWebSocket.ts` の `FakeEventSocket` を `vi.stubGlobal('WebSocket', FakeEventSocket)` で渡し、`open()`・`receive()`・`drop()` でアプリサーバーの動きを再現します。
