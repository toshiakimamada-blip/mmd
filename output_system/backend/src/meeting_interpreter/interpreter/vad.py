"""本物の声の検出。Silero VAD（MIT ライセンス）の ONNX 形式を、ONNX Runtime で動かす。

Silero VAD のモデル（ONNX 形式）は、faster-whisper の配布物に同梱されている。同梱のモデルを使えば、
PyTorch のような重い部品を足さず、モデルをネットワークから入手する必要もない（ADR-04）。

モデルは 512 標本（16kHz で 32 ミリ秒）の窓ごとに、声が含まれる確からしさを返す。
1 秒分の音声の判定は、数ミリ秒で終わる。
"""

from collections.abc import Sequence

import numpy as np


class SileroSpeechDetector:
    """Silero VAD による声の検出。"""

    def __init__(self) -> None:
        """同梱のモデルを読み込む。

        faster-whisper は、使うときまで取り込まない
        （取り込むと、ONNX Runtime などが読み込まれるため）。

        ONNX Runtime の Linux 版は、利用状況を送る部品（Microsoft の 1DS。送り先は
        mobile.events.data.microsoft.com）を内蔵している。通訳ワーカーは、OS の仕組み（seccomp）で
        外への通信のソケットを作れないため、送れない。そのうえで、利用状況の収集を止める呼び出しも
        行う（モデルを読み込む前に呼ぶ）。この呼び出しをしても、起動のときの警告
        （device ID を保存できない、と標準出力に 1 行出る）は残る。コンテナの中で確かめた。
        """
        import onnxruntime

        onnxruntime.disable_telemetry_events()

        from faster_whisper.vad import get_vad_model

        self._model = get_vad_model()

    def probabilities(self, samples: "np.ndarray") -> Sequence[float]:
        """窓ごとの、声が含まれる確からしさを返す。

        Args:
            samples: 16kHz・モノラルの音声（float32、-1 から 1）。長さは 512 標本の倍数。

        Returns:
            窓ごとの値（0 から 1）。
        """
        # モデルの部品は、渡された配列の末尾の 64 標本を書き換える。
        # 呼び出し側の配列を守るため、複製を渡す。
        output = self._model(samples.copy())
        return [float(value) for value in np.asarray(output).reshape(-1)]
