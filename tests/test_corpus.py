import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import numpy as np
import pytest
from langchain_postgres import PGVectorStore

from dispute_casework import corpus
from dispute_casework.corpus import (
    SNAPSHOT,
    SNAPSHOT_DIR,
    chunk_content,
    load,
    read_corpus,
    verified_manifest,
)
from dispute_casework.ecfr import Paragraph, parse_part
from dispute_casework.foundry import FoundrySettings


def test_chunk_content_is_id_then_heading_path_then_text() -> None:
    paragraph = Paragraph(
        paragraph_id="1005.11(c)(1)",
        part="1005",
        section="1005.11",
        source="regulation",
        heading_path=("§ 1005.11 Title.", "(c) Time limits"),
        text="(1) Ten-day period. Text.",
    )
    assert chunk_content(paragraph) == (
        "1005.11(c)(1)\n§ 1005.11 Title. > (c) Time limits\n(1) Ten-day period. Text."
    )


def test_snapshot_manifest_pins_the_four_files() -> None:
    manifest = verified_manifest(SNAPSHOT_DIR)
    assert set(manifest["files"]) == {
        "part-1005.xml",
        "part-1005.html",
        "chunks.jsonl",
        "embeddings.npy",
    }
    assert set(manifest["sources"]) == {"part-1005.xml", "part-1005.html"}
    assert manifest["snapshot"] == SNAPSHOT == manifest["latest_amendment_date"]
    assert manifest["pin"]["reason"] and manifest["pin"]["checked"]
    assert manifest["label"] == "unofficial eCFR snapshot"
    assert manifest["chunks"] == {"regulation": 741, "commentary": 1001}
    assert manifest["embedding"]["dimensions"] == 1536


def test_chunks_file_is_what_the_parser_yields_from_the_snapshot() -> None:
    _, chunks, _ = read_corpus(SNAPSHOT_DIR)
    paragraphs = parse_part(
        (SNAPSHOT_DIR / "part-1005.xml").read_bytes(),
        (SNAPSHOT_DIR / "part-1005.html").read_bytes(),
    )
    assert [chunk["content"] for chunk in chunks] == [
        chunk_content(paragraph) for paragraph in paragraphs
    ]


def test_embeddings_are_float32_rows_aligned_with_chunks() -> None:
    _, chunks, vectors = read_corpus(SNAPSHOT_DIR)
    assert vectors.shape == (len(chunks), 1536)
    assert vectors.dtype == np.float32


def test_load_rejects_manifest_mismatch(tmp_path: Path) -> None:
    directory = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT_DIR, directory)
    with (directory / "embeddings.npy").open("ab") as embeddings:
        embeddings.write(b"\0")
    database = Mock()
    with pytest.raises(ValueError, match="embeddings.npy does not match"):
        load(database, directory, FoundrySettings(endpoint="https://unused", key="k"))
    assert database.mock_calls == []


@pytest.mark.parametrize(
    "unlisted", ["part-1005.xml", "part-1005.html", "chunks.jsonl", "embeddings.npy"]
)
def test_load_rejects_a_file_the_manifest_does_not_list(
    tmp_path: Path, unlisted: str
) -> None:
    directory = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT_DIR, directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"][unlisted]
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    database = Mock()
    with pytest.raises(ValueError, match=f"{unlisted} has no sha256"):
        load(database, directory, FoundrySettings(endpoint="https://unused", key="k"))
    assert database.mock_calls == []


@pytest.mark.parametrize(
    "missing", ["part-1005.xml", "part-1005.html", "chunks.jsonl", "embeddings.npy"]
)
def test_load_rejects_a_listed_file_missing_on_disk(
    tmp_path: Path, missing: str
) -> None:
    directory = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT_DIR, directory)
    (directory / missing).unlink()
    database = Mock()
    with pytest.raises(FileNotFoundError, match=missing):
        load(database, directory, FoundrySettings(endpoint="https://unused", key="k"))
    assert database.mock_calls == []


def test_load_closes_the_loader_engine_when_the_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = Mock(close=AsyncMock())
    monkeypatch.setattr(corpus, "connect", MagicMock())
    monkeypatch.setattr(corpus, "engine", Mock(return_value=loader))
    monkeypatch.setattr(
        corpus,
        "open_store",
        Mock(return_value=Mock(add_embeddings=Mock(side_effect=RuntimeError("lost")))),
    )
    with pytest.raises(RuntimeError, match="lost"):
        load(Mock(), SNAPSHOT_DIR, FoundrySettings(endpoint="https://unused", key="k"))
    loader.close.assert_awaited_once()


def test_loaded_table_holds_every_chunk_with_its_metadata(store: PGVectorStore) -> None:
    manifest, chunks, _ = read_corpus(SNAPSHOT_DIR)
    rows = store.get(where={"paragraph_id": "1005.11(c)-3"})
    assert len(store.get()["ids"]) == len(chunks)
    assert rows["documents"] == [
        chunk["content"] for chunk in chunks if chunk["paragraph_id"] == "1005.11(c)-3"
    ]
    (metadata,) = rows["metadatas"]
    assert metadata["source"] == "commentary"
    assert metadata["legal_status"] == "official_interpretation"
    assert metadata["section"] == "1005.11"
    assert metadata["snapshot"].isoformat() == manifest["snapshot"]
    assert metadata["access_label"] == "public"
