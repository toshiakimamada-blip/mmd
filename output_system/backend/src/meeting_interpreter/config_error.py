"""設定の誤りを表す例外。

設定（環境変数と、アプリに同梱する調整値のファイル）の誤りは、起動の時点で見つける。
誤りが複数あるときも、1 回の例外にまとめる。
1 つ直すたびに次の誤りが見つかる、という手戻りを防ぐため。
"""


class ConfigError(ValueError):
    """設定に誤りがあり、アプリを起動できないことを表す。

    ValueError の一種にする。設定の誤りを「値の誤り」として扱う呼び出し側からも捕まえられる。
    """

    def __init__(self, problems: list[str]) -> None:
        """
        Args:
            problems: 誤りの内容。1 件につき 1 つの文。どの環境変数またはどの項目が誤りか、
                どう直すかを文に入れる。

        Raises:
            ValueError: problems が空のとき（誤りが無いのに例外を作る呼び出しの誤り）。
        """
        if not problems:
            raise ValueError("ConfigError には、誤りを 1 件以上渡してください")
        self.problems = list(problems)
        super().__init__(self._format(self.problems))

    @staticmethod
    def _format(problems: list[str]) -> str:
        """誤りの一覧を、起動の失敗の文面にする。1 行 1 件にして、見落とさないようにする。"""
        lines = ["設定に誤りがあるため、アプリを起動できません。"]
        lines.extend(f"- {problem}" for problem in problems)
        return "\n".join(lines)
