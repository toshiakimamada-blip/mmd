"""変換プロセスの起動（python -m meeting_interpreter.models.conversion）。"""

import os
import sys

from .worker import main

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # 部品が作った、終わらないスレッドがあっても、確実に止める（止まると、メモリが戻る）。
    os._exit(code)
