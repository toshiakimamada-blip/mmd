"""変換のテストが使う、小さな翻訳モデル（ランダムな重み）。

本物の FuguMT（約 120MB）は、テストでは使わない。同じ形式（Marian の構造・pickle 形式の重み・
SentencePiece の分割・vocab.json）で、数百 KB の小さなモデルを、テストの開始のときに作る。
訳は意味を成さないが、変換と読み込みの手順は、本物と同じ部品を通る。

作るのに PyTorch・transformers・SentencePiece が要る（依存グループ conversion）。
入っていない環境では、このモデルを使うテストを飛ばす。
"""

import json
import shutil
from pathlib import Path

import pytest

# 分割を学習させる、小さな文章。意味は問わない。
_CORPUS = [
    "this is a cat",
    "the meeting will start soon",
    "hello world",
    "could you share your screen",
    "これは猫です",
    "会議はまもなく始まります",
    "こんにちは世界",
    "画面を共有してください",
] * 20

# 分割の語彙の大きさ。小さいほど、モデルが小さくなる。
_VOCAB_SIZE = 60


def build_tiny_translation_model(destination: Path) -> Path:
    """小さな翻訳モデルを destination に作る。

    作るファイル: config.json・pytorch_model.bin・vocab.json・source.spm・target.spm
    （FuguMT の定義のうち、変換に要る 5 つと同じ名前）。

    Args:
        destination: 作る先のフォルダー（無ければ作る）。

    Returns:
        destination。
    """
    import sentencepiece
    import torch
    from transformers import MarianConfig, MarianMTModel

    destination.mkdir(parents=True, exist_ok=True)
    corpus = destination / "corpus.txt"
    corpus.write_text("\n".join(_CORPUS), encoding="utf-8")
    prefix = destination / "tiny"
    sentencepiece.SentencePieceTrainer.train(
        input=str(corpus),
        model_prefix=str(prefix),
        vocab_size=_VOCAB_SIZE,
        model_type="unigram",
        # Marian の語彙の決まり: 文末の印 </s> が 0 番、未知語が 1 番。
        eos_id=0,
        unk_id=1,
        bos_id=-1,
        pad_id=-1,
        character_coverage=1.0,
        minloglevel=2,
    )
    (destination / "tiny.model").rename(destination / "source.spm")
    shutil.copy(destination / "source.spm", destination / "target.spm")
    (destination / "tiny.vocab").unlink()
    corpus.unlink()

    processor = sentencepiece.SentencePieceProcessor(str(destination / "source.spm"))
    vocabulary = {processor.id_to_piece(i): i for i in range(processor.get_piece_size())}
    # Marian は、語彙の最後に <pad> を足す（デコーダーの開始にも使う）。
    vocabulary["<pad>"] = len(vocabulary)
    (destination / "vocab.json").write_text(json.dumps(vocabulary, ensure_ascii=False))

    size = len(vocabulary)
    config = MarianConfig(
        vocab_size=size,
        decoder_vocab_size=size,
        d_model=16,
        encoder_layers=1,
        decoder_layers=1,
        encoder_attention_heads=2,
        decoder_attention_heads=2,
        encoder_ffn_dim=32,
        decoder_ffn_dim=32,
        max_position_embeddings=64,
        pad_token_id=size - 1,
        eos_token_id=0,
        bos_token_id=0,
        decoder_start_token_id=size - 1,
        forced_eos_token_id=0,
        share_encoder_decoder_embeddings=True,
        scale_embedding=True,
        static_position_embeddings=True,
        activation_function="swish",
        normalize_embedding=False,
        bad_words_ids=[[size - 1]],
        max_length=64,
    )
    config.save_pretrained(destination)
    torch.manual_seed(0)
    model = MarianMTModel(config)
    # 配布元と同じ、pickle 形式の重み（テンソルだけの state_dict）。
    torch.save(model.state_dict(), destination / "pytorch_model.bin")
    return destination


@pytest.fixture(scope="session")
def tiny_translation_model_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """小さな翻訳モデルの原本（テスト全体で 1 回だけ作る）。変えずに、複製して使う。"""
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    pytest.importorskip("sentencepiece")
    return build_tiny_translation_model(tmp_path_factory.mktemp("tiny-translation-model"))


@pytest.fixture
def tiny_translation_model(tiny_translation_model_template: Path, tmp_path: Path) -> Path:
    """小さな翻訳モデルの複製（テストごと）。重みを差し替えるテストが、原本を汚さない。"""
    destination = tmp_path / "source-model"
    shutil.copytree(tiny_translation_model_template, destination)
    return destination
