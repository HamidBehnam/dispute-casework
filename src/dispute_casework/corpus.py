"""The corpus files of one eCFR snapshot and their load into the chunk store.

A snapshot directory holds the part as XML and as rendered HTML, chunks.jsonl,
embeddings.npy (float32, row-aligned with chunks.jsonl) and manifest.json,
which records the sha256 of the other four.
"""

import hashlib
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from langchain_postgres import Column, PGEngine, PGVectorStore

from dispute_casework.ecfr import Paragraph
from dispute_casework.foundry import EMBEDDING_DIMENSIONS, FoundrySettings
from dispute_casework.retrieval import (
    ID_COLUMN,
    METADATA_COLUMNS,
    TABLE,
    hybrid_config,
    open_store,
)

SNAPSHOT = "2023-04-19"
PART = "1005"
SNAPSHOT_DIR = Path("corpus/ecfr") / SNAPSHOT
MANIFEST = "manifest.json"
XML = f"part-{PART}.xml"
HTML = f"part-{PART}.html"
CHUNKS = "chunks.jsonl"
EMBEDDINGS = "embeddings.npy"
CONTENT_FORMAT_VERSION = "1"


def chunk_content(paragraph: Paragraph) -> str:
    """The one string that is embedded, keyword-indexed and reranked."""
    return "\n".join(
        [paragraph.paragraph_id, " > ".join(paragraph.heading_path), paragraph.text]
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_manifest(directory: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((directory / MANIFEST).read_text())
    for name, recorded in manifest["files"].items():
        if sha256(directory / name) != recorded:
            raise ValueError(f"{name} does not match the sha256 in {MANIFEST}")
    return manifest


def read_corpus(
    directory: Path,
) -> tuple[dict[str, Any], list[dict[str, str]], npt.NDArray[np.float32]]:
    manifest = verified_manifest(directory)
    for name in (XML, HTML, CHUNKS, EMBEDDINGS):
        if name not in manifest["files"]:
            raise ValueError(f"{name} has no sha256 in {MANIFEST}")
    chunks = [
        json.loads(line) for line in (directory / CHUNKS).read_text().splitlines()
    ]
    return manifest, chunks, np.load(directory / EMBEDDINGS)


def load(engine: PGEngine, directory: Path, settings: FoundrySettings) -> PGVectorStore:
    """Rebuilds the chunk table from the snapshot files."""
    manifest, chunks, vectors = read_corpus(directory)
    snapshot = manifest["snapshot"]
    engine.init_vectorstore_table(
        TABLE,
        EMBEDDING_DIMENSIONS,
        id_column=ID_COLUMN,
        metadata_columns=[
            Column(name, "DATE" if name == "snapshot" else "TEXT", nullable=False)
            for name in METADATA_COLUMNS
        ],
        store_metadata=False,
        overwrite_existing=True,
        hybrid_search_config=hybrid_config(""),
    )
    store = open_store(engine, settings)
    store.add_embeddings(
        texts=[chunk["content"] for chunk in chunks],
        embeddings=vectors.tolist(),
        metadatas=[
            {name: chunk[name] for name in METADATA_COLUMNS if name != "snapshot"}
            | {"snapshot": date.fromisoformat(snapshot)}
            for chunk in chunks
        ],
        ids=[
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot}:{chunk['paragraph_id']}"))
            for chunk in chunks
        ],
    )
    return store
