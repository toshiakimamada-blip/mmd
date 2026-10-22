# お知らせ帯とエラー表示の部品

画面にエラーと警告を出す部品の使い方です。パターンの定義は、基本設計書の画面設計書「エラー表示パターン」と「画面共通の枠」にあります。部品は `frontend/src/components/notices/` にあり、見た目は `frontend/src/styles/notices.css` です。

## 7 つのパターンと部品

| パターン | 部品 | 使い方 |
|---|---|---|
| 項目のすぐ下 | `FieldError` | 入力項目の直後に置く。項目の `aria-describedby` に、渡した `id` を指定する |
| お知らせ帯のエラー | `useNoticeActions().post({ kind: 'error', ... })` | 利用者が閉じると消える。同じ操作が成功したら `clear(id)` で消す |
| お知らせ帯の警告 | `useWarning(id, 条件, 文)` | 条件が成り立つ間だけ出る。解消すると消える |
| 選択を求めるお知らせ | `post({ kind: 'choice', ... })` | ボタンを 2 つ添える。どちらかを選ぶと消える |
| 領域の中 | `RegionError` | 結果を表示するはずだった領域の中に置く。やり直しのボタンを 1 つ添える |
| つながらないことの表示 | `post({ kind: 'offline', id })` | つながったら `clear(id)` で消す。検出と再接続は別の作業で作る |
| 画面全体 | `FullScreenError` | アプリを開始できないとき、どの画面も出さずに表示する |

確認の表示（削除の確認・終了の確認）は `ConfirmDialog` です。エラーではないため、7 つのパターンには含みません。

## お知らせ帯

`AppFrame` が `NoticeProvider` と `NoticeBand` を 1 つずつ持ちます。画面の部品は、`NoticeProvider` の中で `useNoticeActions` を呼んで、お知らせを出したり消したりします。

```tsx
const { post, clear } = useNoticeActions();

// エラーを出す。文言は、エラーコードから errorMessages.ts の toDisplayError で引く
post({ kind: 'error', id: 'start-meeting', error: toDisplayError(body) });
// 同じ操作が成功したら消す
clear('start-meeting');

// 条件が成り立つ間だけ警告を出す（解消するか、部品が消えると、警告も消える）
useWarning('long-meeting', elapsedHours >= 2, '会議が 2 時間を超えました。');
```

- 同じ `id` で出し直すと、お知らせは増えずに内容が置き換わります。
- エラーは警告より上に並びます。出した順ではありません。
- エラーは `role="alert"` で、警告は `aria-live="polite"` で、読み上げソフトへ伝えます。
- 警告に閉じるボタンが付くのは、`dismissible` を指定したときだけです（`useWarning` の第 4 引数、または `post` の項目）。
- 対応していないブラウザー（Edge と Chrome 以外）で開くと、`AppFrame` の `BrowserSupportNotice` が、すべての画面で警告を出します。判定は `isSupportedBrowser`（ユーザーエージェントの文字列）です。

## エラーと警告の区別

色だけに頼らず、3 つを重ねて区別します。

- 文字: エラーは塗りつぶしの札「エラー」、警告は枠だけの札「警告」
- 形: エラーは丸、警告は三角のアイコン（読み上げソフトには伝えません）
- 色: エラーは赤、警告は琥珀色

エラーの文面は、「エラー」の札・何が起きたか・次に行う操作・エラーコードの順で、すべてのパターンが同じ組み立て（`ErrorText`）です。つながらないことの表示は、サーバーがエラーコードを返せない状況のため、エラーコードを付けません。

## 確認の表示

```tsx
<ConfirmDialog
  open={confirming}
  title="この記録を削除しますか"
  description="削除した記録は、元に戻せません。"
  confirmLabel="削除する"
  initialFocus="cancel"
  onConfirm={() => { setConfirming(false); remove(); }}
  onCancel={() => setConfirming(false)}
/>
```

- 開くと、フォーカスが「取りやめる」（`initialFocus="confirm"` なら実行）へ移ります。Tab と Shift+Tab は、ダイアログの中だけを巡ります。
- Esc で取りやめます。暗い背景を押しても、実行にも取りやめにもなりません。
- 開いている間、背景（`body` 直下のほかの要素）を `inert` にします。閉じると外し、フォーカスを開く前の場所へ戻します。
- 閉じる（`open` を false にする）のは、呼び出し側です。
