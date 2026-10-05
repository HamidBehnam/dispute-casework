import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from langchain_postgres import PGVectorStore

from dispute_casework.corpus import (
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


def test_snapshot_manifest_pins_the_three_files(snapshot_dir: Path) -> None:
    manifest = verified_manifest(snapshot_dir)
    assert set(manifest["files"]) == {
        "part-1005.xml",
        "chunks.jsonl",
        "embeddings.npy",
    }
    assert manifest["snapshot"] == "2026-09-29"
    assert manifest["label"] == "unofficial eCFR snapshot"
    assert manifest["chunks"] == {"regulation": 741, "commentary": 1001}
    assert manifest["embedding"]["dimensions"] == 1536


def test_chunks_file_is_what_the_parser_yields_from_the_snapshot(
    snapshot_dir: Path,
) -> None:
    _, chunks, _ = read_corpus(snapshot_dir)
    paragraphs = parse_part((snapshot_dir / "part-1005.xml").read_bytes())
    assert [chunk["content"] for chunk in chunks] == [
        chunk_content(paragraph) for paragraph in paragraphs
    ]


def test_embeddings_are_float32_rows_aligned_with_chunks(snapshot_dir: Path) -> None:
    _, chunks, vectors = read_corpus(snapshot_dir)
    assert vectors.shape == (len(chunks), 1536)
    assert vectors.dtype == np.float32


def test_load_rejects_manifest_mismatch(snapshot_dir: Path, tmp_path: Path) -> None:
    directory = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, directory)
    with (directory / "embeddings.npy").open("ab") as embeddings:
        embeddings.write(b"\0")
    engine = Mock()
    with pytest.raises(ValueError, match="embeddings.npy does not match"):
        load(engine, directory, FoundrySettings(endpoint="https://unused", key="k"))
    assert engine.mock_calls == []


@pytest.mark.parametrize("unlisted", ["chunks.jsonl", "embeddings.npy"])
def test_load_rejects_a_file_the_manifest_does_not_list(
    snapshot_dir: Path, tmp_path: Path, unlisted: str
) -> None:
    directory = tmp_path / "snapshot"
    shutil.copytree(snapshot_dir, directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    del manifest["files"][unlisted]
    (directory / "manifest.json").write_text(json.dumps(manifest))
    engine = Mock()
    with pytest.raises(ValueError, match=f"{unlisted} has no sha256"):
        load(engine, directory, FoundrySettings(endpoint="https://unused", key="k"))
    assert engine.mock_calls == []


def test_loaded_table_holds_every_chunk_with_its_metadata(
    store: PGVectorStore, snapshot_dir: Path
) -> None:
    manifest, chunks, _ = read_corpus(snapshot_dir)
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
