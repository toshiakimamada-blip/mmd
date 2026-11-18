"""処理ログの集計コマンド（output_system/tools/summarize_processing_log.py）のテスト。

コマンドは標準ライブラリだけで動く 1 つのファイルで、アプリのパッケージの外にある。
ここでは、ファイルの場所から読み込んで確かめる。
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

TOOL_PATH = Path(__file__).resolve().parents[3] / "tools" / "summarize_processing_log.py"


def load_tool():
    """コマンドのファイルを、モジュールとして読み込む。"""
    spec = importlib.util.spec_from_file_location("summarize_processing_log", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()

MEETING_A = "a" * 32
MEETING_B = "b" * 32


def log(process_type: str, meeting_id: str | None = None, **fields: object) -> str:
    """処理ログ 1 行の見本。アプリが出す形（JSON 1 行）。"""
    entry = {
        "timestamp": "2026-11-18T01:00:00.000Z",
        "log_type": "processing",
        "level": "info",
        "process_type": process_type,
        "meeting_id": meeting_id,
        **fields,
    }
    return json.dumps(entry, separators=(",", ":"))


def subtitle(meeting_id: str, delay_ms: float, **fields: object) -> str:
    """字幕を画面へ送った記録（subtitle_sent）の見本。"""
    return log("subtitle_sent", meeting_id, duration_ms=delay_ms, **fields)


def sample_lines() -> list[str]:
    """2 つの会議と、読み飛ばす行を混ぜたログの見本。

    会議 A: 字幕 20 件（遅れ 1〜20 秒を 1 秒刻み）。最初の字幕まで 6.5 秒。終了。
    会議 B: 字幕 2 件（遅れ 2 秒と 4 秒）。中断。メモリの記録なし。
    """
    lines = [
        "INFO: 起動しました（JSON ではない行）",
        json.dumps({"timestamp": "x", "log_type": "runtime", "level": "info", "logger": "uvicorn"}),
        json.dumps({"timestamp": "x", "log_type": "error", "level": "error", "meeting_id": "zzz"}),
        "[1, 2, 3]",
        "{壊れた JSON",
        log("meeting_started", MEETING_A, timestamp="2026-11-18T01:00:00.000Z"),
        log("meeting_started", MEETING_B, timestamp="2026-11-18T02:00:00.000Z"),
    ]
    for index in range(1, 21):
        extra = {"first_subtitle_ms": 6500} if index == 1 else {}
        lines.append(
            subtitle(
                MEETING_A,
                index * 1000,
                wait_ms=100 * index,
                transcribe_ms=2000,
                translate_ms=500,
                save_notify_ms=10,
                audio_ms=3000,
                **extra,
            )
        )
    lines.append(subtitle(MEETING_B, 2000, first_subtitle_ms=9000))
    lines.append(subtitle(MEETING_B, 4000))
    lines.append(log("meeting_ended", MEETING_A, duration_ms=1_800_000, item_count=20))
    lines.append(log("meeting_interrupted", MEETING_B, duration_ms=60_000, item_count=2))
    for role, resident, peak in (
        ("app_server", 300.0, 310.0),
        ("interpreter_worker", 1500.5, 1700.0),
        ("container", 4000.0, 4100.0),
    ):
        lines.append(
            log(
                "process_memory",
                MEETING_A,
                trace_id="t1",
                memory_trigger="meeting_ended",
                process_role=role,
                memory_mb=resident,
                peak_memory_mb=peak,
            )
        )
    lines.append(
        log(
            "process_memory",
            MEETING_A,
            trace_id="t2",
            memory_trigger="job_finished",
            job_type="summary",
            process_role="generation_worker",
            memory_mb=3500.0,
        )
    )
    return lines


class TestParseLine:
    """1 行の読み取り。"""

    def test_should_read_a_processing_log_line(self):
        """
        【テスト対象】parse_line
        【テスト内容】処理ログの JSON 1 行を渡す
        【期待結果】項目の辞書が返ること
        """
        entry = tool.parse_line(log("meeting_started", MEETING_A))

        assert entry["process_type"] == "meeting_started"

    def test_should_read_a_line_that_docker_logs_prefixes_with_a_timestamp(self):
        """
        【テスト対象】parse_line（docker logs -t の行頭の日時）
        【テスト内容】行頭に日時が付いた処理ログの行を渡す
        【期待結果】日時を飛ばして、処理ログとして読めること
        """
        line = "2026-11-18T01:23:45.123456789Z " + log("meeting_started", MEETING_A)

        assert tool.parse_line(line)["meeting_id"] == MEETING_A

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "INFO: Uvicorn running on http://0.0.0.0:3001",
            "{壊れた",
            "[1, 2]",
            '"text"',
            "123",
            '{"log_type":"error","error_code":"MEETING_ALREADY_RUNNING"}',
            '{"log_type":"runtime","logger":"uvicorn.error"}',
            '{"no_log_type":true}',
            "prefix {壊れた",
        ],
    )
    def test_should_skip_lines_that_are_not_processing_logs(self, line):
        """
        【テスト対象】parse_line（読み飛ばす行）
        【テスト内容】空行・JSON でない行・壊れた JSON・辞書でない JSON・エラーログ・runtime・
                    log_type が無い JSON を渡す
        【期待結果】どれも例外にならず、None が返ること
        """
        assert tool.parse_line(line) is None


class TestBrokenLinesDoNotStopTheTool:
    """壊れた行（桁数の多い整数・深く入れ子になった JSON）で止まらないこと。"""

    def test_should_skip_a_number_that_is_too_large_for_a_float(self):
        """
        【テスト対象】read_logs（400 桁の整数の duration_ms）
        【テスト内容】duration_ms が 400 桁の整数の字幕の行と、ふつうの字幕の行を読む。
                    400 桁の整数は float に直せず、OverflowError になる値
        【期待結果】例外にならず、字幕の件数には数えるが、遅れの値には入れないこと。
                    ふつうの行の値は集計されること
        """
        huge = "1" + "0" * 399
        broken = (
            '{"log_type":"processing","process_type":"subtitle_sent",'
            f'"meeting_id":"{MEETING_A}","duration_ms":{huge}}}'
        )

        result = tool.read_logs([broken, subtitle(MEETING_A, 2500)])

        meeting = result.meetings[MEETING_A]
        assert meeting.subtitle_count == 2
        assert meeting.delays_ms == [2500.0]

    def test_should_skip_a_deeply_nested_json_line(self):
        """
        【テスト対象】parse_line と read_logs（深く入れ子になった配列を持つ行）
        【テスト内容】「{」で始まり、配列を 200,000 段に入れ子にした行を読む。
                    JSON の読み取りが、再帰の深さの上限で RecursionError になる行。
                    「docker logs -t」の行頭の日時が付いた同じ行も読む
        【期待結果】どちらも例外にならず None になり、あとに続くふつうの行は集計されること
        """
        depth = 200_000
        nested = '{"log_type":"processing","x":' + "[" * depth + "]" * depth + "}"

        assert tool.parse_line(nested) is None
        assert tool.parse_line("2026-11-18T01:23:45.123456789Z " + nested) is None
        result = tool.read_logs([nested, subtitle(MEETING_A, 1000)])
        assert result.meetings[MEETING_A].subtitle_count == 1


class TestPercentile:
    """パーセンタイル（最近順位法）。"""

    @pytest.mark.parametrize(
        ("count", "expected_rank"),
        [(1, 1), (2, 2), (19, 19), (20, 19), (21, 20), (60, 57), (100, 95), (101, 96)],
    )
    def test_should_pick_the_value_at_the_ceiling_of_the_rank(self, count, expected_rank):
        """
        【テスト対象】percentile
        【テスト内容】1〜101 件の値（1, 2, 3, ...）の 95 パーセンタイルを求める
        【期待結果】小さい順の ceil(0.95 × 件数) 番目の値になること
                    （60 件は 57 番目。小数の掛け算の誤差で 58 番目にならない）
        """
        values = list(range(count, 0, -1))  # 並びが逆でも、並べ替えて求める。

        assert tool.percentile(values, 95) == expected_rank

    def test_should_return_none_for_no_values(self):
        """
        【テスト対象】percentile（値が無い）
        【テスト内容】空の一覧を渡す
        【期待結果】None になること
        """
        assert tool.percentile([], 95) is None


class TestReadLogs:
    """ログの読み込みと集計の元になる値。"""

    def test_should_collect_the_values_of_each_meeting(self):
        """
        【テスト対象】read_logs
        【テスト内容】2 つの会議と読み飛ばす行を混ぜたログを読む
        【期待結果】会議ごとに、字幕の件数・遅れ・最初の字幕までの時間・終わり方・長さが集まること。
                    読んだ行と処理ログの行の数が合うこと
        """
        result = tool.read_logs(sample_lines())

        assert list(result.meetings) == [MEETING_A, MEETING_B]
        a, b = result.meetings[MEETING_A], result.meetings[MEETING_B]
        assert (a.subtitle_count, a.first_subtitle_ms, a.outcome) == (20, 6500, "meeting_ended")
        assert a.delays_ms == [index * 1000 for index in range(1, 21)]
        assert a.duration_ms == 1_800_000
        assert a.started_at == "2026-11-18T01:00:00.000Z"
        assert (b.subtitle_count, b.first_subtitle_ms, b.outcome) == (
            2,
            9000,
            "meeting_interrupted",
        )
        assert result.lines == len(sample_lines())
        # 読み飛ばしたのは、先頭の 5 行（JSON でない行・runtime・エラーログ・辞書でない・壊れた）。
        assert result.processing_lines == len(sample_lines()) - 5

    def test_should_filter_by_meeting_id(self):
        """
        【テスト対象】read_logs（会議番号で絞り込む）
        【テスト内容】会議 B の番号だけを指定して読む
        【期待結果】会議 B だけが集計され、会議 A は含まれないこと
        """
        result = tool.read_logs(sample_lines(), frozenset({MEETING_B}))

        assert list(result.meetings) == [MEETING_B]

    def test_should_group_memory_rows_of_one_moment_and_split_other_moments(self):
        """
        【テスト対象】read_logs（メモリの記録のまとまり）
        【テスト内容】会議の終了の時点の 3 行と、作成ジョブの終了の時点の 1 行を読む
        【期待結果】2 つのまとまりになり、1 つ目は 3 行（時点 meeting_ended）、
                    2 つ目は 1 行（時点 job_finished、種類 summary）であること
        """
        meeting = tool.read_logs(sample_lines()).meetings[MEETING_A]

        first, second = meeting.snapshots
        assert (first.trigger, [row.role for row in first.rows]) == (
            "meeting_ended",
            ["app_server", "interpreter_worker", "container"],
        )
        assert (second.trigger, second.job_type) == ("job_finished", "summary")
        assert second.rows[0].peak_mb is None

    def test_should_start_a_new_group_when_the_same_role_appears_twice(self):
        """
        【テスト対象】read_logs（識別子の無いメモリの記録）
        【テスト内容】処理の識別子が無く、同じ時点・同じ会議の記録が 2 回続けて出たログを読む
        【期待結果】同じ役割の行が重なった所で、新しいまとまりに分かれること
        """
        row = dict(memory_trigger="meeting_ended", process_role="app_server", memory_mb=1.0)
        lines = [log("process_memory", MEETING_A, **row), log("process_memory", MEETING_A, **row)]

        meeting = tool.read_logs(lines).meetings[MEETING_A]

        assert len(meeting.snapshots) == 2

    def test_should_ignore_invalid_numbers_and_lines_without_a_meeting(self):
        """
        【テスト対象】read_logs（値が不正な記録、会議に結びつかない記録）
        【テスト内容】遅れが文字列・真偽値・NaN の字幕と、会議番号が無い処理ログを読む
        【期待結果】字幕の件数には数えるが、遅れの値には入れないこと。会議番号が無い記録は集計しないこと
        """
        lines = [
            log("subtitle_sent", MEETING_A, duration_ms="3000"),
            log("subtitle_sent", MEETING_A, duration_ms=True),
            '{"log_type":"processing","process_type":"subtitle_sent",'
            f'"meeting_id":"{MEETING_A}","duration_ms":NaN}}',
            log("model_conversion_finished", None, duration_ms=9000),
            log("subtitle_sent", MEETING_A, duration_ms=2500),
        ]

        result = tool.read_logs(lines)

        assert list(result.meetings) == [MEETING_A]
        assert result.meetings[MEETING_A].subtitle_count == 4
        assert result.meetings[MEETING_A].delays_ms == [2500.0]


class TestRender:
    """表の文面。"""

    def test_should_state_that_the_interval_rule_is_not_applied(self):
        """
        【テスト対象】render（区間の規則を適用していない旨）
        【テスト内容】サンプルのログから表を作る
        【期待結果】先頭に、区間の規則を適用していないこと、作成時間を集計していないこと、
                    目標の判定には使えないことが書かれていること
        """
        text = tool.render(tool.read_logs(sample_lines()))

        head = text.split("##", 1)[0]
        assert "区間の規則" in head
        assert "適用していません" in head
        assert "作成ジョブの作成時間も集計していません" in head
        assert "目標を満たしたかの判定には使えません" in head

    def test_should_show_the_delay_statistics_of_each_meeting(self):
        """
        【テスト対象】render（字幕の遅れの表）
        【テスト内容】遅れが 1〜20 秒の会議 A と、2 秒・4 秒の会議 B の表を作る
        【期待結果】会議 A は、件数 20・中央値 10.50・95 パーセンタイル 19.00・最大 20.00・
                    10 秒を超えた件数 10・最初の字幕まで 6.50 になること。ちょうど 10 秒は数えない。
                    会議 B は、件数 2・中央値 3.00・95 パーセンタイル 4.00・最大 4.00・超過 0・
                    終わり方が中断になること
        """
        text = tool.render(tool.read_logs(sample_lines()))

        delay_rows = {
            cells[0].strip("`"): cells
            for cells in (
                [cell.strip() for cell in row.strip("|").split("|")]
                for row in text.splitlines()
                if row.startswith(f"| `{MEETING_A}`") or row.startswith(f"| `{MEETING_B}`")
            )
            if "終了" in cells[2] or "中断" in cells[2]
        }
        a, b = delay_rows[MEETING_A], delay_rows[MEETING_B]
        # 列: 会議番号・開始・終わり方・会議の長さ・件数・中央値・95%・最大・超過・最初の字幕まで
        assert a[2:] == ["終了", "1800.00", "20", "10.50", "19.00", "20.00", "10", "6.50"]
        assert b[2:] == ["中断", "60.00", "2", "3.00", "4.00", "4.00", "0", "9.00"]
        assert a[1] == "2026-11-18T01:00:00.000Z"

    def test_should_use_the_given_limit_for_the_count_over_the_limit(self):
        """
        【テスト対象】render（目標の秒数の指定）
        【テスト内容】目標を 5 秒にして表を作る
        【期待結果】見出しが「5 秒を超えた件数」になり、会議 A は 15 件、会議 B は 0 件になること
        """
        text = tool.render(tool.read_logs(sample_lines()), limit_seconds=5)

        assert "5 秒を超えた件数" in text
        row_a = next(
            r for r in text.splitlines() if r.startswith(f"| `{MEETING_A}`") and "終了" in r
        )
        assert row_a.split("|")[9].strip() == "15"

    def test_should_show_the_stage_times_as_median_and_maximum(self):
        """
        【テスト対象】render（段ごとの所要時間の表）
        【テスト内容】会議 A（待ち 0.1〜2.0 秒、文字起こし 2 秒、翻訳 0.5 秒、保存と通知 0.01 秒、
                    区切りの長さ 3 秒）と、段の値が無い会議 B の表を作る
        【期待結果】会議 A の欄が「中央値 / 最大」になり、値が無い会議 B の欄は「—」になること
        """
        text = tool.render(tool.read_logs(sample_lines()))

        rows = [row for row in text.splitlines() if "区切りの長さ" not in row]
        stage_a = next(r for r in rows if r.startswith(f"| `{MEETING_A}`") and "3.00 / 3.00" in r)
        assert "1.05 / 2.00" in stage_a  # 待ち: 0.1〜2.0 秒の中央値は 1.05、最大は 2.00
        assert "2.00 / 2.00" in stage_a  # 文字起こし
        assert "0.50 / 0.50" in stage_a  # 翻訳
        assert "0.01 / 0.01" in stage_a  # 保存と通知
        stage_b = next(
            r for r in rows if r.startswith(f"| `{MEETING_B}`") and "—" in r and "|" in r
        )
        assert stage_b.count("—") >= 5

    def test_should_list_the_memory_of_each_process_and_the_total_of_four_processes(self):
        """
        【テスト対象】render（メモリの表）
        【テスト内容】会議の終了の時点（アプリサーバー・通訳ワーカー・コンテナ全体）と、
                    作成ジョブの終了の時点（生成ワーカーだけ）の表を作る
        【期待結果】動いていたプロセスの行だけが並び、動いていない生成ワーカーは会議の終了の時点に出ないこと。
                    合計は 4 つのプロセスだけの和で、コンテナ全体を含めない
                    （300.0 + 1500.5 = 1800.5）
        """
        text = tool.render(tool.read_logs(sample_lines()))

        memory = text.split("## メモリの使用量（MB）", 1)[1]
        assert "| `" + MEETING_A + "` | 会議の終了 | アプリサーバー | 300.0 | 310.0 |" in memory
        assert "| 会議の終了 | 通訳ワーカー | 1500.5 | 1700.0 |" in memory
        assert "| 会議の終了 | コンテナ全体（cgroup） | 4000.0 | 4100.0 |" in memory
        assert "| 会議の終了 | **合計（4 プロセス）** | **1800.5** |" in memory
        assert "| 作成ジョブの終了（要約） | 生成ワーカー | 3500.0 | — |" in memory
        assert "| 作成ジョブの終了（要約） | **合計（4 プロセス）** | **3500.0** |" in memory
        meeting_end_rows = [row for row in memory.splitlines() if "会議の終了 |" in row]
        assert not any("生成ワーカー" in row for row in meeting_end_rows)

    def test_should_say_so_when_there_is_no_memory_record(self):
        """
        【テスト対象】render（メモリの記録が無いログ）
        【テスト内容】字幕だけのログから表を作る
        【期待結果】メモリの表の代わりに、記録が無いことが書かれること
        """
        text = tool.render(tool.read_logs([subtitle(MEETING_A, 1000)]))

        assert "メモリの記録（process_memory）はありません。" in text


class TestMain:
    """コマンドの入口。"""

    def test_should_read_a_file_and_print_the_tables(self, tmp_path, capsys):
        """
        【テスト対象】main（ファイルの読み込み）
        【テスト内容】ログのファイルを渡して実行する
        【期待結果】終了コード 0 で、標準出力に表が出ること
        """
        path = tmp_path / "app.log"
        path.write_text("\n".join(sample_lines()) + "\n", encoding="utf-8")

        code = tool.main([str(path)])

        assert code == 0
        out = capsys.readouterr().out
        assert "# 処理ログの集計" in out
        assert MEETING_A in out and MEETING_B in out

    def test_should_read_standard_input_when_no_file_is_given(self, monkeypatch, capsys):
        """
        【テスト対象】main（標準入力）
        【テスト内容】ファイルを渡さず、標準入力にログを流して実行する
        【期待結果】終了コード 0 で、会議 A の行が出ること
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(sample_lines())))

        code = tool.main([])

        assert code == 0
        assert MEETING_A in capsys.readouterr().out

    def test_should_narrow_down_to_the_given_meeting(self, tmp_path, capsys):
        """
        【テスト対象】main（--meeting）
        【テスト内容】会議 B の番号を --meeting に渡して実行する
        【期待結果】会議 B だけが出て、会議 A は出ないこと
        """
        path = tmp_path / "app.log"
        path.write_text("\n".join(sample_lines()) + "\n", encoding="utf-8")

        code = tool.main([str(path), "--meeting", MEETING_B])

        assert code == 0
        out = capsys.readouterr().out
        assert MEETING_B in out
        assert MEETING_A not in out

    def test_should_not_fail_on_a_log_full_of_non_json_lines(self, tmp_path, capsys):
        """
        【テスト対象】main（JSON ではない行だらけのログ）
        【テスト内容】JSON でない行と壊れた行だけのファイルを渡して実行する
        【期待結果】例外にならず、終了コード 1 で、会議が見つからなかったことを
                    標準エラー出力へ出すこと
        """
        path = tmp_path / "app.log"
        path.write_text("INFO: a\n{壊れた\n\n\xff\n", encoding="utf-8", errors="replace")

        code = tool.main([str(path)])

        captured = capsys.readouterr()
        assert code == 1
        assert "会議の記録が見つかりませんでした" in captured.err
        assert "区間の規則" in captured.out

    def test_should_read_a_file_with_invalid_utf8_bytes(self, tmp_path, capsys):
        """
        【テスト対象】main（文字コードの壊れたログ）
        【テスト内容】UTF-8 として読めないバイトを含むファイルを渡して実行する
        【期待結果】例外にならず、読める行から集計されること
        """
        path = tmp_path / "app.log"
        path.write_bytes(b"\xff\xfe broken\n" + subtitle(MEETING_A, 1000).encode() + b"\n")

        code = tool.main([str(path)])

        assert code == 0
        assert MEETING_A in capsys.readouterr().out
