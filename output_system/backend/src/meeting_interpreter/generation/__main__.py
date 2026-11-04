"""`python -m meeting_interpreter.generation` で生成ワーカーを起動する。"""

import os
import sys

from .entry import main

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # AI の部品が作った、終わらないスレッドがあっても、確実に止める（止まると、メモリが戻る）。
    # 生成の途中で停止を受けたときも、ここでプロセスごと終わる。
    os._exit(code)
