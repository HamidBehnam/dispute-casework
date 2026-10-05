import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import tiktoken

from dispute_casework import corpus_build
from dispute_casework.corpus import verified_manifest
from dispute_casework.corpus_build import (
    TOKENS_PER_REQUEST,
    embed_corpus,
    token_batches,
)
from dispute_casework.foundry import FoundrySettings


@pytest.fixture
def one_token_per_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real encoder downloads its vocabulary on first use."""
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: Mock(encode=str.split))


def words(count: int) -> str:
    return " ".join(["word"] * count)


@pytest.mark.usefixtures("one_token_per_word")
def test_token_batches_keep_order_and_stay_under_the_request_cap() -> None:
    texts = [words(5000), words(3000), words(1), words(2), words(TOKENS_PER_REQUEST)]
    batches = list(token_batches(texts))
    assert [text for batch, _ in batches for text in batch] == texts
    assert all(
        tokens == sum(len(text.split()) for text in batch) for batch, tokens in batches
    )
    assert [tokens for _, tokens in batches] == [
        TOKENS_PER_REQUEST,
        3,
        TOKENS_PER_REQUEST,
    ]


@pytest.mark.usefixtures("one_token_per_word")
def test_a_text_over_the_request_cap_goes_out_alone() -> None:
    over_the_cap = words(TOKENS_PER_REQUEST + 1000)
    batches = list(token_batches([words(1), over_the_cap, words(2)]))
    assert batches == [
        ([words(1)], 1),
        ([over_the_cap], TOKENS_PER_REQUEST + 1000),
        ([words(2)], 2),
    ]


@pytest.mark.usefixtures("one_token_per_word")
def test_interrupted_embed_leaves_the_snapshot_matching_its_manifest(
    snapshot_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, directory)
    monkeypatch.setattr(corpus_build, "chunk_content", lambda paragraph: paragraph.text)
    monkeypatch.setattr(
        corpus_build, "embed", Mock(side_effect=RuntimeError("interrupted"))
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        embed_corpus(directory, FoundrySettings(endpoint="https://unused", key="k"))
    verified_manifest(directory)
