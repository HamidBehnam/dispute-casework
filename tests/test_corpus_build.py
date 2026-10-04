from unittest.mock import Mock

import pytest
import tiktoken

from dispute_casework.corpus_build import TOKENS_PER_REQUEST, token_batches


@pytest.fixture
def one_token_per_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real encoder downloads its vocabulary on first use."""
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: Mock(encode=str.split))


def words(count: int) -> str:
    return " ".join(["word"] * count)


@pytest.mark.usefixtures("one_token_per_word")
def test_token_batches_keep_order_and_stay_under_the_request_cap() -> None:
    over_the_cap = words(TOKENS_PER_REQUEST + 1000)
    texts = [
        words(5000),
        words(3000),
        words(1),
        over_the_cap,
        words(2),
        words(TOKENS_PER_REQUEST),
    ]
    batches = list(token_batches(texts))
    assert [text for batch, _ in batches for text in batch] == texts
    assert all(
        tokens == sum(len(text.split()) for text in batch) for batch, tokens in batches
    )
    assert [tokens for _, tokens in batches] == [
        TOKENS_PER_REQUEST,
        1,
        TOKENS_PER_REQUEST + 1000,
        2,
        TOKENS_PER_REQUEST,
    ]
    assert [over_the_cap] in [batch for batch, _ in batches]
