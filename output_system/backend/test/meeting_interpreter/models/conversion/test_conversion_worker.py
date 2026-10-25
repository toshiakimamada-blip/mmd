"""変換プロセス（python -m meeting_interpreter.models.conversion）のテスト。

本物の子のプロセスを起動して、小さな翻訳モデル（ランダムな重み）を変換する。
本物の FuguMT の変換は、コンテナでの動作確認で行う（docs/models.md）。
変換の部品（PyTorch など）が入っていない環境では、このファイルのうち、変換を動かすテストを飛ばす。
"""

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from meeting_interpreter.child_process import ChildOutcome, run_child_process
from meeting_interpreter.models.conversion import network_guard, worker
from meeting_interpreter.models.conversion.network_guard import ExternalCommunicationBlocked
from meeting_interpreter.models.conversion.protocol import ConversionResult, decode_result
from meeting_interpreter.models.failure import FailureKind

# このファイルは backend/test/meeting_interpreter/models/conversion/ にある。4 つ上が backend/。
SOURCE_ROOT = Path(__file__).resolve().parents[4] / "src"

# 変換の出力に要るファイル（ctranslate2 のモデルのフォルダーと、複製した SentencePiece の 2 つ）。
EXPECTED_OUTPUT = {"model.bin", "config.json", "shared_vocabulary.json", "source.spm", "target.spm"}


def _command(source: Path, output: Path, *, quantization: str = "int8") -> list[str]:
    """変換プロセスを起動するコマンド。"""
    return [
        sys.executable,
        "-P",
        "-s",
        "-m",
        "meeting_interpreter.models.conversion",
        "--source-dir",
        str(source),
        "--output-dir",
        str(output),
        "--quantization",
        quantization,
        "--copy-file",
        "source.spm",
        "--copy-file",
        "target.spm",
    ]


def run_conversion(
    source: Path,
    output: Path,
    *,
    quantization: str = "int8",
    prefix: list[str] | None = None,
    timeout: float = 300,
) -> tuple[ChildOutcome, ConversionResult | None]:
    """変換プロセスを動かして、結果を読む。"""
    command = [*(prefix or []), *_command(source, output, quantization=quantization)]
    outcome = run_child_process(
        command, env_extra={"PYTHONPATH": str(SOURCE_ROOT)}, timeout=timeout
    )
    return outcome, decode_result(outcome.stdout)


def _processes_mentioning(text: str) -> list[int]:
    """コマンドラインに、ある文字列を含むプロセスの番号を返す（このテストのプロセスを除く）。"""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command_line = (
                (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            )
        except OSError:
            continue
        if text in command_line:
            found.append(int(entry.name))
    return found


class TestConversion:
    """小さな翻訳モデルを変換する（変換の部品が要る）。"""

    def test_should_convert_the_model_and_translate_one_sentence(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】変換プロセス（正常な変換）
        【テスト内容】小さな翻訳モデルを、8 ビット整数の量子化で変換する。出力を、実行基盤
                      （CTranslate2）で読み込み、1 文を訳す
        【期待結果】終了コード 0 で成功の結果を返し、出力に model.bin など 5 つのファイルが
                    あること。出力のモデルを読み込めて、訳の結果が返ること（翻訳 1 文が通る）。
                    外への通信の試みは 0
        """
        ctranslate2 = pytest.importorskip("ctranslate2")
        sentencepiece = pytest.importorskip("sentencepiece")
        output = tmp_path / "converted"

        outcome, result = run_conversion(tiny_translation_model, output)

        assert outcome.returncode == 0, outcome.stderr_tail.decode(errors="replace")
        assert result is not None and result.ok is True
        assert result.network_attempts == 0
        assert result.peak_rss_bytes is not None and result.peak_rss_bytes > 0
        assert set(result.versions) == {"torch", "transformers", "ctranslate2", "sentencepiece"}
        assert {path.name for path in output.iterdir()} == EXPECTED_OUTPUT
        translator = ctranslate2.Translator(str(output), device="cpu")
        pieces = sentencepiece.SentencePieceProcessor(str(output / "source.spm")).encode(
            "this is a cat", out_type=str
        )
        translated = translator.translate_batch([[*pieces, "</s>"]], max_decoding_length=8)
        assert len(translated[0].hypotheses) == 1

    def test_should_stop_after_the_conversion(self, tiny_translation_model, tmp_path):
        """
        【テスト対象】変換プロセス（終了）
        【テスト内容】変換を終えたあと、その変換プロセスが残っているかを、プロセスの一覧で調べる
        【期待結果】残っていないこと（成功しても、変換プロセスは止まる。メモリが戻る）
        """
        output = tmp_path / "unique-output-for-the-process-check"

        outcome, result = run_conversion(tiny_translation_model, output)

        assert result is not None and result.ok is True, outcome.stderr_tail.decode(
            errors="replace"
        )
        assert _processes_mentioning(str(output)) == []

    def test_should_fail_and_leave_no_output_for_a_broken_weights_file(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】変換プロセス（壊れた重み）
        【テスト内容】重みのファイルを、でたらめなバイト列に置き換えて変換する
        【期待結果】終了コード 1 で、変換の失敗（conversion_failed）と例外の種類を返すこと。
                    出力のフォルダーは残らない
        """
        (tiny_translation_model / "pytorch_model.bin").write_bytes(b"not a checkpoint" * 100)
        output = tmp_path / "converted"

        outcome, result = run_conversion(tiny_translation_model, output)

        assert outcome.returncode == 1
        assert result is not None
        assert result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert result.exception_type is not None
        assert not output.exists()

    def test_should_not_run_the_code_inside_a_malicious_pickle(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】変換プロセス（悪意のある pickle の重み）
        【テスト内容】重みを、読み込むと印のファイルを作るコードを含む pickle に置き換えて変換する
        【期待結果】変換が失敗し、印のファイルが作られないこと（コードが実行されない）。
                    出力のフォルダーも残らない
        """
        torch = pytest.importorskip("torch")
        marker = tmp_path / "executed.marker"

        class Malicious:
            def __reduce__(self) -> tuple[object, tuple[str]]:
                return (os.system, (f"touch {marker}",))

        torch.save(
            {"model.shared.weight": Malicious()}, tiny_translation_model / "pytorch_model.bin"
        )
        output = tmp_path / "converted"

        outcome, result = run_conversion(tiny_translation_model, output)

        assert not marker.exists()
        assert outcome.returncode == 1
        assert result is not None and result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert result.exception_type is not None and "Unpickling" in result.exception_type
        assert not output.exists()

    @pytest.mark.parametrize("missing", ["vocab.json", "source.spm"])
    def test_should_fail_when_a_required_file_is_missing(
        self, tiny_translation_model, tmp_path, missing
    ):
        """
        【テスト対象】変換プロセス（足りないファイル）
        【テスト内容】変換に要るファイルを 1 つ消して変換する
        【期待結果】変換の失敗になり、出力のフォルダーは残らないこと
        入力例: vocab.json・source.spm
        """
        (tiny_translation_model / missing).unlink()
        output = tmp_path / "converted"

        _, result = run_conversion(tiny_translation_model, output)

        assert result is not None and result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert not output.exists()

    def test_should_refuse_to_write_into_an_existing_output_folder(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】変換プロセス（出力先が既にある）
        【テスト内容】出力先のフォルダーが既にある状態で、変換する
        【期待結果】変換の失敗になり、既にあるフォルダーの中身は変わらないこと
                    （前の変換の途中のファイルに、混ぜて書かない）
        """
        output = tmp_path / "converted"
        output.mkdir()
        (output / "keep.txt").write_text("keep")

        _, result = run_conversion(tiny_translation_model, output)

        assert result is not None and result.ok is False
        assert (output / "keep.txt").read_text() == "keep"
        assert [path.name for path in output.iterdir()] == ["keep.txt"]

    def test_should_apply_the_requested_quantization(self, tiny_translation_model, tmp_path):
        """
        【テスト対象】変換プロセス（量子化の指定）
        【テスト内容】量子化を int8 と float32 にして変換し、model.bin の大きさを比べる
        【期待結果】どちらも成功し、int8 のほうが小さいこと（指定が変換に効いている）
        """
        pytest.importorskip("ctranslate2")
        sizes: dict[str, int] = {}
        for name in ("int8", "float32"):
            output = tmp_path / f"converted-{name}"
            _, result = run_conversion(tiny_translation_model, output, quantization=name)
            assert result is not None and result.ok is True, name
            sizes[name] = (output / "model.bin").stat().st_size

        assert sizes["int8"] < sizes["float32"]


class TestArguments:
    """引数の検証。変換の部品を取り込む前に、誤りを断る。"""

    def test_should_exit_with_usage_error_for_an_unknown_quantization(self, tmp_path):
        """
        【テスト対象】変換プロセス（量子化の値）
        【テスト内容】選べる値に無い量子化を渡す
        【期待結果】終了コード 2 で止まり、結果の行を出さないこと
                    （値は引数になるため、決めた値以外を通さない）
        """
        outcome, result = run_conversion(tmp_path, tmp_path / "out", quantization="int4")

        assert outcome.returncode == 2
        assert result is None

    def test_should_refuse_a_copy_file_name_that_leaves_the_folder(self, tmp_path):
        """
        【テスト対象】変換プロセス（複製するファイル名）
        【テスト内容】フォルダーの外を指すファイル名（../x）を、複製するファイルとして渡す
        【期待結果】終了コード 2 で止まること
        """
        command = _command(tmp_path, tmp_path / "out")
        command[command.index("source.spm")] = "../x"

        outcome = run_child_process(command, env_extra={"PYTHONPATH": str(SOURCE_ROOT)})

        assert outcome.returncode == 2


class TestFailureClassification:
    """失敗の原因の種類の判定（変換の部品を使わない）。"""

    def test_should_classify_a_full_disk_as_disk_insufficient(self):
        """
        【テスト対象】worker.classify_failure（ディスクの空き不足）
        【テスト内容】ENOSPC の OSError を渡す
        【期待結果】ディスクの空きが足りない（disk_insufficient）になること
        """
        kind = worker.classify_failure(OSError(errno.ENOSPC, "No space left on device"))

        assert kind is FailureKind.DISK_INSUFFICIENT

    def test_should_find_a_full_disk_inside_a_chain_of_exceptions(self):
        """
        【テスト対象】worker.classify_failure（例外の連鎖）
        【テスト内容】部品が ENOSPC を別の例外で包み直した連鎖（RuntimeError ← OSError）を渡す
        【期待結果】連鎖をたどり、ディスクの空きが足りない（disk_insufficient）になること
        """
        try:
            try:
                raise OSError(errno.EDQUOT, "Disk quota exceeded")
            except OSError as inner:
                raise RuntimeError("saving failed") from inner
        except RuntimeError as error:
            kind = worker.classify_failure(error)

        assert kind is FailureKind.DISK_INSUFFICIENT

    @pytest.mark.parametrize(
        "error",
        [ValueError("x"), OSError(errno.EACCES, "denied"), MemoryError(), KeyError("k")],
    )
    def test_should_classify_everything_else_as_conversion_failed(self, error):
        """
        【テスト対象】worker.classify_failure（そのほか）
        【テスト内容】ディスクの空き不足でない例外を渡す
        【期待結果】変換の失敗（conversion_failed）になること
        """
        assert worker.classify_failure(error) is FailureKind.CONVERSION_FAILED

    def test_should_name_the_exception_type_without_its_message(self):
        """
        【テスト対象】worker.exception_type_name
        【テスト内容】文面に秘密らしい内容を持つ例外から、種類の名前を求める
        【期待結果】クラスの名前だけで、文面は含まないこと。標準の例外はモジュール名を付けない
        """
        name = worker.exception_type_name(ValueError("/secret/path https://host?token=1"))

        assert name == "ValueError"

    def test_should_fall_back_when_the_type_name_has_an_unusable_form(self):
        """
        【テスト対象】worker.exception_type_name（関数の中で定義したクラス）
        【テスト内容】名前に <locals> を含むクラスの例外を渡す
        【期待結果】ログに書ける形の名前（クラス名だけ）になること
        """

        class LocalError(Exception):
            pass

        assert worker.exception_type_name(LocalError()) == "LocalError"


class TestNetworkPolicy:
    """外への通信の試みは、変換が済んでいても失敗にする。"""

    def test_should_fail_even_when_a_component_swallowed_the_blocked_attempt(self, tmp_path):
        """
        【テスト対象】worker.run（外への通信の試み）
        【テスト内容】変換の中の部品が外へ接続しようとして、拒まれた例外を握りつぶし、
                      変換は最後まで済む状況を作る
        【期待結果】結果は失敗（conversion_failed）になり、試みの数が結果に入ること。
                    例外の種類は ExternalCommunicationAttempted
        """

        def convert_that_tries_to_connect(*_: object) -> dict[str, str]:
            try:
                socket.getaddrinfo("example.com", 443)
            except ExternalCommunicationBlocked:
                pass  # 部品が、ネットワークの失敗として握りつぶした。
            return {}

        arguments = worker.parse_arguments(_command(tmp_path, tmp_path / "out")[5:])
        guard = network_guard.install()
        try:
            result = worker.run(arguments, guard, convert=convert_that_tries_to_connect)
        finally:
            guard.uninstall()

        assert result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert result.exception_type == "ExternalCommunicationAttempted"
        assert result.network_attempts == 1


class TestGuardsInAFreshProcess:
    """仕組みが、新しいプロセスでも働くこと（テストの差し替えに頼らない確認）。"""

    def test_should_block_a_connection_attempt_in_a_fresh_process(self):
        """
        【テスト対象】network_guard（新しいプロセスでの働き）
        【テスト内容】仕組みを入れた新しいプロセスで、手元の待ち受けへ urllib で接続しようとする
        【期待結果】接続は拒まれ、待ち受け側には何も届かず、試みが数えられること
                    （外へ通信しないことを「試みの数が 0」で確かめる値が、実際に増えること）
        """
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(0.5)
        port = server.getsockname()[1]
        code = (
            "import urllib.request, urllib.error, json\n"
            "from meeting_interpreter.models.conversion import network_guard\n"
            "guard = network_guard.install()\n"
            "try:\n"
            f"    urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=2)\n"
            "except urllib.error.URLError as error:\n"
            "    print(json.dumps({'blocked': True, 'attempts': guard.attempts}))\n"
        )
        try:
            outcome = run_child_process(
                [sys.executable, "-P", "-c", code], env_extra={"PYTHONPATH": str(SOURCE_ROOT)}
            )
            with pytest.raises(TimeoutError):
                server.accept()
        finally:
            server.close()

        reported = json.loads(outcome.stdout.decode().strip().splitlines()[-1])
        assert reported["blocked"] is True
        assert reported["attempts"] >= 1


@pytest.mark.skipif(
    subprocess.run(["unshare", "-n", "true"], capture_output=True).returncode != 0
    if shutil.which("unshare")
    else True,
    reason="ネットワーク名前空間を作れない環境",
)
class TestWithoutAnyNetworkInterface:
    """ネットワークの口が 1 つも無い環境でも、変換が成功すること。"""

    def test_should_convert_inside_an_empty_network_namespace(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】変換プロセス（ネットワーク名前空間の中）
        【テスト内容】ネットワークの口が 1 つも無い名前空間（unshare -n）の中で、変換する
        【期待結果】成功し、外への通信の試みが 0 であること。変換がネットワークを必要としない
                    ことを、OS の仕組みで確かめる（C や Rust の部品の通信も、この環境では届かない）
        """
        output = tmp_path / "converted"

        outcome, result = run_conversion(tiny_translation_model, output, prefix=["unshare", "-n"])

        assert outcome.returncode == 0, outcome.stderr_tail.decode(errors="replace")
        assert result is not None and result.ok is True
        assert result.network_attempts == 0
        assert {path.name for path in output.iterdir()} == EXPECTED_OUTPUT


class TestParentWatchdog:
    """親のプロセスが消えたら、変換プロセスも終わる。"""

    def test_should_exit_when_the_parent_process_disappears(self, tmp_path):
        """
        【テスト対象】worker.start_parent_watchdog
        【テスト内容】見張りを入れたプロセスを、親が先に終わる形で起動する
                      （親 → 見張りのプロセス。親は子を起動して、すぐ終わる）
        【期待結果】親が消えたあと、数秒のうちに、見張りのプロセスが終わること
                    （アプリサーバーが突然止まっても、変換プロセスが残って、メモリを使い続けない）
        """
        pid_file = tmp_path / "child.pid"
        child_code = (
            "import os, sys, time;"
            "from meeting_interpreter.models.conversion import worker;"
            "worker.start_parent_watchdog(interval_seconds=0.1);"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()));"
            "time.sleep(60)"
        )
        parent_code = (
            "import subprocess, sys, time;"
            f"subprocess.Popen([sys.executable, '-P', '-c', {child_code!r}]);"
            f"time.sleep(1.0)"
        )

        subprocess.run(
            [sys.executable, "-P", "-c", parent_code],
            env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT)},
            check=True,
            timeout=30,
        )
        pid = int(pid_file.read_text())
        deadline = threading.Event()
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists() or "State:\tZ" in _status(pid):
                break
            deadline.wait(0.1)

        assert not Path(f"/proc/{pid}").exists() or "State:\tZ" in _status(pid)


def _status(pid: int) -> str:
    """プロセスの状態の文字列。読めなければ空。"""
    try:
        return Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return ""
