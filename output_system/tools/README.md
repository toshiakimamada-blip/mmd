# tools

開発と運用で使う補助の道具の置き場です。アプリ本体の実行には使いません。

| 道具 | 用途 |
|---|---|
| `with-pinned-node.sh` | 固定した版の Node.js 24 系（npm 11.10 以上を同梱）で、渡されたコマンドを実行します。画面の依存を足すときに使います。使い方と理由は、スクリプトの先頭のコメントと、`../README.md` の「公開から 7 日未満の版を使わない設定」にあります |

| `summarize_processing_log.py` | コンテナのログから処理ログを読み、会議ごとに、字幕の遅れ（中央値・95 パーセンタイル・最大・10 秒を超えた件数）・最初の字幕までの時間・段ごとの所要時間・メモリの使用量を、Markdown の表で出します。標準ライブラリだけで動きます。字幕の遅れの判定から除く区間の規則は適用しません（出力の先頭に書きます）。使い方と数え方は、`../docs/measurement.md` にあります |

```bash
# output_system/ で実行する
docker logs toshiakimamada-blip-mmd-output-system 2>&1 | python3 -I tools/summarize_processing_log.py
```

区間の規則の適用と作成時間の集計は、性能の測定を扱う後続の作業で、この道具に足します。
