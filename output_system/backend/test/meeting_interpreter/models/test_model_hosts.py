"""入手の通信の相手として許すホストの判定のテスト。

「通信の相手は AI モデルの配布元だけ」（非機能要件設計書の外部への送信の防止）を、コードで守る。
"""

import pytest

from meeting_interpreter.models.hosts import SOURCE_HOSTS, TRANSFER_HOSTS, host_matches


class TestHostMatches:
    """ホスト名が、許可の一覧に当てはまるかの判定。"""

    @pytest.mark.parametrize(
        "host",
        [
            "huggingface.co",
            "HUGGINGFACE.CO",
            "us.aws.cdn.hf.co",
            "cas-bridge.xethub.hf.co",
            "a.hf.co",
            "A.B.HF.CO",
        ],
    )
    def test_should_allow_the_distribution_hosts(self, host):
        """
        【テスト対象】host_matches（配布元とその CDN）
        【テスト内容】Hugging Face の配布元と、ファイルの実体の置き場（hf.co 以下）のホストを渡す
        【期待結果】許可されること（大文字小文字は区別しない）
        """
        assert host_matches(host, TRANSFER_HOSTS) is True

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example",
            "huggingface.co.evil.example",
            "evilhuggingface.co",
            "www.huggingface.co",
            "hf.co",
            "evilhf.co",
            "hf.co.evil.example",
            "us.aws.cdn.hf.co.evil.example",
            "us.aws.cdn.hf.co.",
            "huggingface.co.",
            "127.0.0.1",
            "localhost",
            "",
            None,
        ],
    )
    def test_should_refuse_a_host_outside_the_distribution(self, host):
        """
        【テスト対象】host_matches（配布元以外）
        【テスト内容】名前の後ろや前に別の文字を足したホスト、hf.co そのもの、数値の宛先、空を渡す
        【期待結果】許可されないこと（部分一致や、末尾のドットで逃れられない）
        """
        assert host_matches(host, TRANSFER_HOSTS) is False

    def test_should_match_a_wildcard_only_below_the_domain(self):
        """
        【テスト対象】host_matches（ワイルドカード）
        【テスト内容】`*.example.test` に、サブドメインとドメインそのものを渡す
        【期待結果】サブドメインだけが当てはまること
        """
        assert host_matches("a.example.test", ("*.example.test",)) is True
        assert host_matches("example.test", ("*.example.test",)) is False

    def test_should_limit_the_repository_host_to_the_source(self):
        """
        【テスト対象】SOURCE_HOSTS
        【テスト内容】配布元のリポジトリとして許すホストを読む
        【期待結果】huggingface.co だけであること（CDN のホストは、転送先としてだけ許す）
        """
        assert SOURCE_HOSTS == ("huggingface.co",)
        assert set(SOURCE_HOSTS) <= set(TRANSFER_HOSTS)
