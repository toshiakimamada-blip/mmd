"""変換プロセスを起動して見届ける部分（process.py）のテスト。

多くのテストは、本物の Python の代わりに、結果を決めて返す小さな実行ファイル（偽の Python）を使う。
アプリサーバー側が、子のプロセスの結果・異常・時間切れ・中止を正しく扱うことを確かめる。
本物の変換プロセスを通す確認も 1 つ含める。
"""

import stat
import sys
import threading
from pathlib import Path

import pytest

from meeting_interpreter.models.conversion.process import (
    ConversionJob,
    run_conversion_process,
)
from meeting_interpreter.models.conversion.protocol import ConversionResult, encode_result
from meeting_interpreter.models.failure import DownloadCancelled, FailureKind

# このファイルは backend/test/meeting_interpreter/models/conversion/ にある。4 つ上が backend/。
SOURCE_ROOT = Path(__file__).resolve().parents[4] / "src"


def _fake_python(directory: Path, body: str) -> Path:
    """決めた動きをする、偽の Python（シェルスクリプト）を作る。引数は body から $@ で読める。"""
    path = directory / "fake-python"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _job(tmp_path: Path, **overrides: object) -> ConversionJob:
    """テスト用の変換の仕事。フォルダーは、一時のフォルダーの中に作る。"""
    values: dict[str, object] = {
        "source_dir": tmp_path / "source",
        "output_dir": tmp_path / "work" / "output",
        "scratch_dir": tmp_path / "work" / "scratch",
        "quantization": "int8",
        "copy_files": ("source.spm", "target.spm"),
    }
    values.update(overrides)
    return ConversionJob(**values)  # type: ignore[arg-type]


def _ok_line(**overrides: object) -> str:
    """成功の結果の行。"""
    values: dict[str, object] = {
        "ok": True,
        "peak_rss_bytes": 1000,
        "versions": {"torch": "2.14.1"},
    }
    values.update(overrides)
    return encode_result(ConversionResult(**values))  # type: ignore[arg-type]


class TestResults:
    """子の結果の扱い。"""

    def test_should_return_the_successful_result_of_the_child(self, tmp_path):
        """
        【テスト対象】run_conversion_process（成功）
        【テスト内容】成功の結果を返して、終了コード 0 で終わる偽の Python を動かす
        【期待結果】その結果がそのまま返ること
        """
        python = _fake_python(tmp_path, f"echo '{_ok_line()}'\n")

        result = run_conversion_process(python, _job(tmp_path))

        assert result.ok is True
        assert result.peak_rss_bytes == 1000

    def test_should_return_the_failure_reported_by_the_child(self, tmp_path):
        """
        【テスト対象】run_conversion_process（子が報告した失敗）
        【テスト内容】ディスクの空き不足の失敗を返して、終了コード 1 で終わる偽の Python を動かす
        【期待結果】原因の種類（disk_insufficient）と例外の種類が、そのまま返ること
        """
        line = encode_result(
            ConversionResult(
                ok=False,
                failure_kind=FailureKind.DISK_INSUFFICIENT,
                exception_type="OSError",
            )
        )
        python = _fake_python(tmp_path, f"echo '{line}'\nexit 1\n")

        result = run_conversion_process(python, _job(tmp_path))

        assert result.ok is False
        assert result.failure_kind is FailureKind.DISK_INSUFFICIENT
        assert result.exception_type == "OSError"

    @pytest.mark.parametrize(
        "body",
        [
            "echo 'garbage'\n",
            "exit 0\n",
            "echo '{\"ok\": true}'\n",
            "exit 139\n",
            "kill -9 $$\n",
        ],
        ids=["garbage", "no-output", "partial-json", "crash-code", "killed-by-signal"],
    )
    def test_should_treat_a_child_without_a_valid_result_as_a_conversion_failure(
        self, tmp_path, body
    ):
        """
        【テスト対象】run_conversion_process（結果が読めない）
        【テスト内容】結果を返さずに終わる子（でたらめな出力・出力なし・形の足りない出力・
                      異常終了・信号で終了）を動かす
        【期待結果】変換の失敗（conversion_failed）になること。成功と見なさない
        """
        python = _fake_python(tmp_path, body)

        result = run_conversion_process(python, _job(tmp_path))

        assert result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert result.exception_type == "InvalidChildResult"

    def test_should_not_accept_a_success_with_a_nonzero_exit_code(self, tmp_path):
        """
        【テスト対象】run_conversion_process（結果と終了コードの食い違い）
        【テスト内容】成功の結果を返しながら、終了コード 1 で終わる子を動かす
        【期待結果】変換の失敗になること（結果の行だけを信じない）
        """
        python = _fake_python(tmp_path, f"echo '{_ok_line()}'\nexit 1\n")

        result = run_conversion_process(python, _job(tmp_path))

        assert result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED

    def test_should_not_accept_a_success_that_attempted_to_communicate(self, tmp_path):
        """
        【テスト対象】run_conversion_process（外への通信の試みがある成功）
        【テスト内容】外への通信の試みの数が 1 の、成功の結果を返す子を動かす
        【期待結果】変換の失敗になること。例外の種類は ExternalCommunicationAttempted
                    （子の判定に頼らず、アプリサーバーでも確かめる）
        """
        python = _fake_python(tmp_path, f"echo '{_ok_line(network_attempts=1)}'\n")

        result = run_conversion_process(python, _job(tmp_path))

        assert result.ok is False
        assert result.exception_type == "ExternalCommunicationAttempted"
        assert result.network_attempts == 1

    def test_should_fail_when_the_python_does_not_exist(self, tmp_path):
        """
        【テスト対象】run_conversion_process（Python が無い）
        【テスト内容】存在しない Python のパスで動かす
        【期待結果】例外にならず、変換の失敗になること。例外の種類は起動の失敗の種類
        """
        result = run_conversion_process(tmp_path / "no-such-python", _job(tmp_path))

        assert result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert result.exception_type == "FileNotFoundError"


class TestTimeoutAndCancel:
    """時間切れと中止。"""

    def test_should_stop_a_child_that_takes_too_long(self, tmp_path):
        """
        【テスト対象】run_conversion_process（時間切れ）
        【テスト内容】終わらない子を、時間切れ 1 秒で動かす
        【期待結果】変換の失敗になり、例外の種類は ConversionTimeout。子は残らないこと
        """
        python = _fake_python(tmp_path, f"echo $$ > {tmp_path}/pid\nexec sleep 60\n")

        result = run_conversion_process(python, _job(tmp_path), timeout_seconds=1.0)

        assert result.ok is False
        assert result.failure_kind is FailureKind.CONVERSION_FAILED
        assert result.exception_type == "ConversionTimeout"
        assert not Path(f"/proc/{(tmp_path / 'pid').read_text().strip()}").exists()

    def test_should_stop_the_child_and_raise_when_cancelled(self, tmp_path):
        """
        【テスト対象】run_conversion_process（中止）
        【テスト内容】終わらない子を動かし、別のスレッドから中止の合図を立てる
        【期待結果】DownloadCancelled を起こして戻ること（アプリの停止は、失敗ではない）。
                    子は残らない
        """
        python = _fake_python(tmp_path, f"echo $$ > {tmp_path}/pid\nexec sleep 60\n")
        cancel = threading.Event()
        threading.Timer(0.7, cancel.set).start()

        with pytest.raises(DownloadCancelled):
            run_conversion_process(python, _job(tmp_path), cancel=cancel)

        assert not Path(f"/proc/{(tmp_path / 'pid').read_text().strip()}").exists()


class TestStartingTheChild:
    """子のプロセスの起動のしかた。"""

    def test_should_pass_the_arguments_and_a_closed_environment(self, tmp_path):
        """
        【テスト対象】run_conversion_process（コマンドと環境変数）
        【テスト内容】引数と環境変数を記録する偽の Python を動かす
        【期待結果】-P -s -m でパッケージを指定し、変換前・出力先・量子化・複製するファイルを
                    引数で渡すこと。止める環境変数が付き、HOME・キャッシュ・一時ファイルの場所は、
                    作業用のフォルダーの中を指すこと。PYTHONPATH はアプリのソースを指す
        """
        python = _fake_python(
            tmp_path,
            f'printf "%s\\n" "$@" > {tmp_path}/args\nenv > {tmp_path}/env\necho \'{_ok_line()}\'\n',
        )
        job = _job(tmp_path)

        run_conversion_process(python, job)

        args = (tmp_path / "args").read_text().splitlines()
        assert args[:4] == ["-P", "-s", "-m", "meeting_interpreter.models.conversion"]
        assert args[args.index("--source-dir") + 1] == str(job.source_dir)
        assert args[args.index("--output-dir") + 1] == str(job.output_dir)
        assert args[args.index("--quantization") + 1] == "int8"
        assert [args[i + 1] for i, a in enumerate(args) if a == "--copy-file"] == [
            "source.spm",
            "target.spm",
        ]
        env = dict(
            line.split("=", 1)
            for line in (tmp_path / "env").read_text().splitlines()
            if "=" in line
        )
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["TRANSFORMERS_OFFLINE"] == "1"
        assert env["PYTHONPATH"] == str(SOURCE_ROOT)
        for name in ("HOME", "XDG_CACHE_HOME", "HF_HOME", "TORCH_HOME", "TMPDIR"):
            assert env[name].startswith(str(job.scratch_dir)), name

    def test_should_create_the_scratch_folders_before_starting(self, tmp_path):
        """
        【テスト対象】run_conversion_process（作業用のフォルダー）
        【テスト内容】作業用のフォルダーが無い状態で、起動直後の状態を記録する偽の Python を動かす
        【期待結果】子が動く時点で、作業用のフォルダーと一時ファイルのフォルダーが作られていること
                    （出力先は、子が作るので、まだ無い）
        """
        job = _job(tmp_path)
        python = _fake_python(
            tmp_path,
            f'ls -d "{job.scratch_dir}" "{job.scratch_dir}/tmp" > {tmp_path}/ls 2>&1\n'
            f'[ -e "{job.output_dir}" ] && echo exists > {tmp_path}/output-state\n'
            f"echo '{_ok_line()}'\n",
        )

        run_conversion_process(python, _job(tmp_path))

        assert (tmp_path / "ls").read_text().split() == [
            str(job.scratch_dir),
            f"{job.scratch_dir}/tmp",
        ]
        assert not (tmp_path / "output-state").exists()

    def test_should_run_the_child_in_the_scratch_folder(self, tmp_path):
        """
        【テスト対象】run_conversion_process（作業フォルダー）
        【テスト内容】子の作業フォルダーを記録する偽の Python を動かす
        【期待結果】作業用のフォルダー（scratch）であること
                    （カレントのフォルダーに置かれたファイルを、読み込まない）
        """
        job = _job(tmp_path)
        python = _fake_python(tmp_path, f"pwd > {tmp_path}/cwd\necho '{_ok_line()}'\n")

        run_conversion_process(python, job)

        assert Path((tmp_path / "cwd").read_text().strip()).resolve() == job.scratch_dir.resolve()


class TestWithTheRealWorker:
    """本物の変換プロセスを通す。"""

    def test_should_convert_a_tiny_model_through_the_real_child(
        self, tiny_translation_model, tmp_path
    ):
        """
        【テスト対象】run_conversion_process（本物の変換プロセス）
        【テスト内容】小さな翻訳モデルを、本物の変換プロセスで変換する
        【期待結果】成功し、出力に model.bin があり、メモリの最大値と部品の版が結果に入ること
        """
        job = _job(tmp_path, source_dir=tiny_translation_model)

        result = run_conversion_process(Path(sys.executable), job)

        assert result.ok is True, result
        assert (job.output_dir / "model.bin").is_file()
        assert result.peak_rss_bytes and result.peak_rss_bytes > 0
        assert "ctranslate2" in result.versions
