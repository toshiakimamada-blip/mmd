"""AI モデルの入手に失敗した原因の種類。

機能設計書 F-16 が定める原因の種類の 6 つ（ネットワークにつながらない・証明書を確かめられない・
配布元が応答しない・配布元のファイルと一致しない・ディスクの空きが足りない・
翻訳モデルを変換できない）と、分類できなかった失敗を持つ。

原因の種類は、画面へ返す準備状況に入る。種類ごとにエラーコード（MODEL_*）を持ち、
画面は、エラーコードごとの文言の一覧から表示する文を引く。
"""

import errno
from enum import StrEnum


class FailureKind(StrEnum):
    """入手に失敗した原因の種類。値は、画面とのやり取りに使うコード値（小文字のスネークケース）。"""

    # ネットワークにつながらない（名前が引けない、接続を断られた、経路が無い）。
    NETWORK_UNREACHABLE = "network_unreachable"
    # 配布元の証明書を確かめられない。社内のネットワークの証明書が未登録の場合などに起きる。
    CERTIFICATE_INVALID = "certificate_invalid"
    # 配布元が応答しない（時間切れ、エラーの応答、途中で切れた）。
    SOURCE_UNRESPONSIVE = "source_unresponsive"
    # 入手したファイルが、定義の SHA-256（配布元のもの）と一致しない。
    HASH_MISMATCH = "hash_mismatch"
    # ディスクの空きが足りない。入手の前と、書き込み中のほか、翻訳モデルの変換でも起きる。
    DISK_INSUFFICIENT = "disk_insufficient"
    # 翻訳モデルを、実行基盤の形式へ変換できない（変換プロセスが失敗した。時間切れを含む）。
    CONVERSION_FAILED = "conversion_failed"
    # 上のどれにも分類できない。想定外の失敗で、原因はログの発生場所から調べる。
    UNKNOWN = "unknown"

    @property
    def error_code(self) -> str:
        """この原因に対応するエラーコード（領域_出来事）。画面は、このコードから文言を引く。"""
        return _ERROR_CODES[self]


_ERROR_CODES: dict[FailureKind, str] = {
    FailureKind.NETWORK_UNREACHABLE: "MODEL_NETWORK_UNREACHABLE",
    FailureKind.CERTIFICATE_INVALID: "MODEL_CERTIFICATE_INVALID",
    FailureKind.SOURCE_UNRESPONSIVE: "MODEL_SOURCE_UNRESPONSIVE",
    FailureKind.HASH_MISMATCH: "MODEL_HASH_MISMATCH",
    FailureKind.DISK_INSUFFICIENT: "MODEL_DISK_INSUFFICIENT",
    FailureKind.CONVERSION_FAILED: "MODEL_CONVERSION_FAILED",
    FailureKind.UNKNOWN: "MODEL_DOWNLOAD_FAILED",
}


class ModelFailure(Exception):
    """AI モデルの入手・確認の失敗。原因の種類を持つ。

    例外の文面は種類の値だけにする。配布元の URL や OS の文面を、そのまま外へ出さないため
    （ログにも画面にも、種類とエラーコードだけを出す）。
    """

    def __init__(self, kind: FailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


# 空きが足りないことを表す OS のエラー番号。ENOSPC は空き容量の不足、EDQUOT は割り当て量の超過。
_DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


def failure_from_os_error(error: OSError) -> ModelFailure:
    """ファイルの書き込みなどの OSError を、原因の種類に分類する。

    空きの不足だけを、ディスクの空きが足りない失敗にする。権限や壊れたファイルシステムなど、
    ほかの OSError は、原因を取り違えて表示しないよう、分類できない失敗にする。
    """
    if error.errno in _DISK_FULL_ERRNOS:
        return ModelFailure(FailureKind.DISK_INSUFFICIENT)
    return ModelFailure(FailureKind.UNKNOWN)


class DownloadCancelled(Exception):
    """入手の中止。アプリの停止などで、入手の途中で止めたことを表す。

    失敗ではない。それまでに受け取った分は一時名のファイルに残し、次の起動で続きから入手する。
    """
