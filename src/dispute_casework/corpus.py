"""The corpus files of one eCFR snapshot and their load into the chunk store.

A snapshot directory holds the part as XML and as rendered HTML, chunks.jsonl,
embeddings.npy (float32, row-aligned with chunks.jsonl) and manifest.json,
which records the sha256 of the other four.
"""

import asyncio
import hashlib
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from dispute_casework.database import LOADER, DatabaseSettings, connect, engine
from dispute_casework.ecfr import Paragraph
from dispute_casework.foundry import FoundrySettings
from dispute_casework.retrieval import METADATA_COLUMNS, SCHEMA, TABLE, open_store

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
    manifest: dict[str, Any] = json.loads(
        (directory / MANIFEST).read_text(encoding="utf-8")
    )
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
        json.loads(line)
        for line in (directory / CHUNKS).read_text(encoding="utf-8").splitlines()
    ]
    return manifest, chunks, np.load(directory / EMBEDDINGS)


def load(
    database: DatabaseSettings, directory: Path, settings: FoundrySettings
) -> None:
    """Replaces the rows of the chunk table with the snapshot's, as the loader role."""
    manifest, chunks, vectors = read_corpus(directory)
    snapshot = manifest["snapshot"]
    with connect(database, options=f"-c role={LOADER}", autocommit=True) as connection:
        connection.execute(f"TRUNCATE {SCHEMA}.{TABLE}")
    loader = engine(database, pool_size=1, max_overflow=0, options=f"-c role={LOADER}")
    try:
        open_store(loader, settings).add_embeddings(
            texts=[chunk["content"] for chunk in chunks],
            embeddings=vectors.tolist(),
            metadatas=[
                {name: chunk[name] for name in METADATA_COLUMNS if name in chunk}
                | {"snapshot": date.fromisoformat(snapshot), "access_label": "public"}
                for chunk in chunks
            ],
            ids=[
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{snapshot}:{chunk['paragraph_id']}"
                    )
                )
                for chunk in chunks
            ],
        )
    finally:
        asyncio.run(loader.close())
