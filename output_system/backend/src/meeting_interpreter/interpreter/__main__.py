"""`python -m meeting_interpreter.interpreter` で通訳ワーカーを起動する。"""

import os
import sys

from .entry import main

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # AI の部品が作った、終わらないスレッドがあっても、確実に止める（止まると、メモリが戻る）。
    os._exit(code)
