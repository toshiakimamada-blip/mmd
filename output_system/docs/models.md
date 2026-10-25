# AI モデルの入手と変換

3 つの AI モデルのうち、翻訳モデル（FuguMT 英語から日本語）の変換について、確かめた結果と、決めた内容を書きます。AI モデルの定義は `config/models.toml`、入手の流れは `README.md` の「AI モデルの入手」にあります。

## 翻訳モデルの変換

### 配布形式の確認結果（2026-10-25）

開発元（staka 氏）が、実行基盤（CTranslate2）の形式でファイルを配布しているかを、公式の配布元で確かめました。

| 確かめた場所 | 確かめた内容 | 結果 |
|---|---|---|
| https://huggingface.co/staka/fugumt-en-ja（固定した版 `fbb91252110cf97d90291eb808629ab0b10f7928`。最終更新 2023-08-15） | リポジトリのファイル一覧と、タグ | `pytorch_model.bin`（PyTorch の重み）、`config.json`、`vocab.json`、`source.spm`、`target.spm` などだけ。`model.bin` や `shared_vocabulary.json` など、CTranslate2 の形式のファイルは無い。タグは `transformers`・`pytorch`・`marian` |
| https://huggingface.co/staka（開発元の組織。API で `author=staka` を検索） | 開発元が公開しているモデル | `staka/fugumt-en-ja`、`staka/fugumt-ja-en`、`staka/takomt` の 3 つ。どれも PyTorch の形式 |
| https://github.com/s-taka/fugumt（開発元のリポジトリ。最終の更新は 2021-02-28） | README、リリース、ファイル一覧 | リリースは 0 件。モデルの配布は https://fugumt.com/FuguMT_ver.202011.1.zip で、Marian の元の形式（`model.npz`）。CTranslate2 の形式ではない |

結論: **開発元は、CTranslate2 の形式を配布していません。** ADR-16 のとおり、入手したあとに変換する方式で進めます。Task 2.2.2 から 2.2.4 は、この方式で作ります。設計の変更はありません。

開発元以外が変換したファイルは、Hugging Face にあります（`jkawamoto/fugumt-en-ja-ct2`、`shift-ykurami/fugumt-en-ja-ct2-f32` など）。要件の「公式の配布元から入手する」に沿わず、変換前のファイルと同じ中身であることをアプリが確かめられないため、使いません（ADR-16 の却下案）。

### 変換の方式

CTranslate2 が提供する変換機能 `ctranslate2.converters.TransformersConverter`（公開されている API）を使います。変換前の翻訳モデルは Hugging Face の形式（`MarianMTModel`）で、この機能が対応する構造です。

| 項目 | 内容 |
|---|---|
| 変換元 | モデル置き場の `pytorch_model.bin`、`config.json`、`vocab.json`、`source.spm`、`target.spm` |
| 変換後（出力） | `model.bin`（重み）、`config.json`、`shared_vocabulary.json`（語彙）。あわせて `source.spm` と `target.spm` を出力先へ複製する（翻訳の実行時に、1 つのフォルダーで足りるようにするため） |
| 量子化 | 8 ビット整数（`int8`）を既定にする。調整値 `translation_quantization`（`config/tuning.toml`）で変える |
| 変換が確定的か | 同じ入力を 2 回変換した `model.bin` は、SHA-256 まで一致した |
| 実行する場所 | 変換プロセス（アプリサーバーとは別のプロセス）。アプリサーバーの中では変換しない |

### 変換に使う部品

名前と版は `backend/uv.lock` で固定します（依存の宣言は `backend/pyproject.toml` の依存グループ `conversion`）。公開から 7 日以上たった版だけを選びます。

| 部品 | 版 | 役目 | ライセンス（配布物の表示） |
|---|---|---|---|
| ctranslate2 | 4.8.3 | 実行基盤の形式への変換 | MIT |
| torch | 2.14.1（CPU 版の wheel） | 重みの読み込み | BSD-3-Clause ほか（複数の表示） |
| transformers | 5.20.1 | 変換元の読み込み | Apache-2.0 |
| sentencepiece | 0.2.2 | 文字の分割 | Apache-2.0 |

- この 4 つと、その依存を合わせて、Linux では 36 個の部品が入ります。配布物のライセンス表示を確かめた結果、NVIDIA 独自のライセンスの部品はありません。tqdm だけが、弱いコピーレフトの `MPL-2.0` を含みます（`MPL-2.0 AND MIT`）。表示の転記で、法的な判断ではありません。ライセンスの一覧は Task 8.3.1 で作ります。
- ctranslate2 4.8.4 は 2026-10-25 の公開のため、7 日以上たった 4.8.3 を選びました。
- torch は、CPU 版の wheel の配布元（https://download.pytorch.org/whl/cpu）から入れます。Linux 向けの標準の wheel は、NVIDIA 独自のライセンスの CUDA 部品（`nvidia-*`・`triton`）を取り込みます。CPU 版なら取り込まず、イメージも小さくなります。`backend/uv.lock` に `nvidia-*` と `triton` が無いことは、自動テスト（`backend/test/deployment/test_conversion_dependencies.py`）が確かめます。
- torch の下限は 2.6.0 です。2.5.1 以前には、`torch.load` の `weights_only=True` を回避できる脆弱性（CVE-2025-32434）があります。
- 変換に使う部品は、アプリサーバーの環境に入れません。イメージの別の環境（`/opt/conversion-venv`）に入れ、変換プロセスだけが使います。

### 変換に要るファイル

`config/models.toml` には、次の 5 つだけを書きます。

| ファイル | 要否 | 確かめた内容 |
|---|---|---|
| `pytorch_model.bin` | 要る | 重み |
| `config.json` | 要る | モデルの構造 |
| `vocab.json` | 要る | 外すと変換が失敗する（トークンと番号の対応を読めない） |
| `source.spm` | 要る | 外すと変換が失敗する（文字の分割を読めない）。翻訳の実行時にも要る |
| `target.spm` | 要る | 外すと変換が失敗する。翻訳の結果を文字に戻すのに、実行時にも要る |
| `generation_config.json` | 要らない | 外しても、変換の結果が 1 バイトも変わらない |
| `special_tokens_map.json` | 要らない | 同上 |
| `tokenizer_config.json` | 要らない | 同上 |

確かめた方法: 5 つだけを置いたフォルダーと、リポジトリの全ファイルを置いたフォルダーを、同じ設定で変換し、出力の 5 ファイル（`model.bin`・`config.json`・`shared_vocabulary.json`・`source.spm`・`target.spm`）の SHA-256 が一致することを見ました。

### 文字の分割（SentencePiece）

**翻訳の実行時にも、SentencePiece が要ります。** CTranslate2 の翻訳は、文字の分割と、分割した結果を文字へ戻す処理を持ちません。実行時の部品は `sentencepiece`（Python の部品）で、使うファイルは `source.spm`（英語の分割）と `target.spm`（日本語へ戻す）です。2 つとも `config/models.toml` に定義済みで、変換の出力先にも複製します。

翻訳 1 文の通し方を、変換した実物で確かめました。

```python
import ctranslate2
import sentencepiece

translator = ctranslate2.Translator(converted_dir)          # 変換後のフォルダー
source = sentencepiece.SentencePieceProcessor(f"{converted_dir}/source.spm")
target = sentencepiece.SentencePieceProcessor(f"{converted_dir}/target.spm")

pieces = source.encode("This is a cat.", out_type=str) + ["</s>"]   # 末尾に文末の印 </s> を足す
result = translator.translate_batch([pieces], beam_size=4)
print(target.decode(result[0].hypotheses[0]))                        # これは猫です。
```

- 原文の分割の末尾に、文末の印 `</s>` を足します。足さないと、同じ語が延々と繰り返される結果になります（実際にそうなりました）。
- 実物（FuguMT、`int8`）の訳の例: 「This is a cat.」は「これは猫です。」、「The meeting will start in five minutes.」は「会議は5分で始まります。」

### 重みの読み込み

`pytorch_model.bin` は pickle 形式です。SHA-256 の一致は「固定した版と同じファイル」を示すだけで、ファイルが安全であることは示しません。pickle は、読み込むときに任意のコードを実行できます。

- 変換プロセスは、重みを `torch.load(..., weights_only=True)` だけで読みます。この読み込みは、テンソルを組み立てる決まった名前以外の pickle を拒みます。
- `weights_only=True` でない読み込みは、変換プロセスの中で拒みます。拒む仕組みとテストは、変換プロセスを作る作業（Task 2.2.2）で足します。
- 固定した版のファイルを、実行せずに点検しました（2026-10-25）。pickle が取り込む名前は、`collections.OrderedDict`、`torch.HalfStorage`、`torch._utils._rebuild_tensor_v2` の 3 つだけです。版を上げるときは、同じ点検をします。
