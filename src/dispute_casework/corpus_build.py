"""Builds the corpus files of the pinned eCFR snapshot, one step per command.

fetch    download the part XML and start manifest.json
measure  report the size of Part 1026 without storing it
embed    parse the XML, write chunks.jsonl and embeddings.npy, complete the manifest
load     rebuild the chunk table in the database named by DATABASE_URL

embed and load need FOUNDRY_ENDPOINT. embed sends one request a minute to stay
under the embedding deployment's tokens-per-minute limit.
"""

import argparse
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx
import numpy as np
import tiktoken
from langchain_postgres import PGEngine

from dispute_casework.corpus import (
    CHUNKS,
    CONTENT_FORMAT_VERSION,
    EMBEDDINGS,
    MANIFEST,
    PART,
    SNAPSHOT,
    SNAPSHOT_DIR,
    chunk_content,
    load,
    sha256,
    verified_manifest,
)
from dispute_casework.ecfr import PARSER_VERSION, parse_part
from dispute_casework.foundry import (
    EMBEDDING_DEPLOYMENT,
    EMBEDDING_DIMENSIONS,
    FoundrySettings,
)
from dispute_casework.retrieval import embed

API = "https://www.ecfr.gov/api/versioner/v1"
XML = f"part-{PART}.xml"
EMBEDDING_DEPLOYMENT_VERSION = "1"
TOKENS_PER_REQUEST = 8000
SECONDS_BETWEEN_REQUESTS = 60


def part_xml(part: str) -> httpx.Response:
    response = httpx.get(
        f"{API}/full/{SNAPSHOT}/title-12.xml", params={"part": part}, timeout=60
    )
    response.raise_for_status()
    return response


def fetch(directory: Path) -> None:
    response = part_xml(PART)
    versions = httpx.get(
        f"{API}/versions/title-12.json",
        params={"part": PART, "issue_date[lte]": SNAPSHOT},
        timeout=60,
    )
    versions.raise_for_status()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / XML).write_bytes(response.content)
    write_manifest(
        directory,
        {
            "snapshot": SNAPSHOT,
            "label": "unofficial eCFR snapshot",
            "source_url": str(response.url),
            "latest_amendment_date": versions.json()["meta"]["latest_amendment_date"],
            "files": {XML: sha256(directory / XML)},
        },
    )


def measure() -> None:
    content = part_xml("1026").content
    root = ET.fromstring(content)
    text = " ".join("".join(root.itertext()).split())
    print(f"bytes       {len(content)}")
    print(f"sha256      {hashlib.sha256(content).hexdigest()}")
    print(f"paragraphs  {sum(1 for _ in root.iter('P'))}")
    print(f"tokens      {len(tiktoken.get_encoding('cl100k_base').encode(text))}")


def token_batches(texts: list[str]) -> Iterator[tuple[list[str], int]]:
    encoding = tiktoken.get_encoding("cl100k_base")
    batch: list[str] = []
    used = 0
    for text in texts:
        tokens = len(encoding.encode(text))
        if batch and used + tokens > TOKENS_PER_REQUEST:
            yield batch, used
            batch, used = [], 0
        batch.append(text)
        used += tokens
    yield batch, used


def embed_corpus(directory: Path, settings: FoundrySettings) -> None:
    manifest = verified_manifest(directory)
    paragraphs = parse_part((directory / XML).read_bytes())
    chunks = [
        {
            "paragraph_id": paragraph.paragraph_id,
            "part": paragraph.part,
            "section": paragraph.section,
            "source": paragraph.source,
            "legal_status": paragraph.legal_status,
            "content": chunk_content(paragraph),
        }
        for paragraph in paragraphs
    ]
    vectors: list[list[float]] = []
    started = time.monotonic()
    batches = token_batches([chunk["content"] for chunk in chunks])
    for request, (batch, tokens) in enumerate(batches, start=1):
        if request > 1:
            time.sleep(SECONDS_BETWEEN_REQUESTS)
        vectors.extend(embed(settings, batch))
        minutes = (time.monotonic() - started) / 60
        print(
            f"request {request}: {len(batch)} chunks, {tokens} tokens, "
            f"{len(vectors)}/{len(chunks)} done after {minutes:.1f} min",
            flush=True,
        )
    (directory / CHUNKS).write_text(
        "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks)
    )
    np.save(directory / EMBEDDINGS, np.asarray(vectors, dtype=np.float32))
    write_manifest(
        directory,
        manifest
        | {
            "files": manifest["files"]
            | {name: sha256(directory / name) for name in (CHUNKS, EMBEDDINGS)},
            "chunks": dict(Counter(chunk["source"] for chunk in chunks)),
            "parser_version": PARSER_VERSION,
            "content_format_version": CONTENT_FORMAT_VERSION,
            "embedding": {
                "model": EMBEDDING_DEPLOYMENT,
                "deployment_version": EMBEDDING_DEPLOYMENT_VERSION,
                "dimensions": EMBEDDING_DIMENSIONS,
                "dtype": "float32",
            },
        },
    )


def write_manifest(directory: Path, manifest: dict[str, object]) -> None:
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["fetch", "measure", "embed", "load"])
    match parser.parse_args().command:
        case "fetch":
            fetch(SNAPSHOT_DIR)
        case "measure":
            measure()
        case "embed":
            embed_corpus(SNAPSHOT_DIR, FoundrySettings.from_env())
        case "load":
            engine = PGEngine.from_connection_string(os.environ["DATABASE_URL"])
            load(engine, SNAPSHOT_DIR, FoundrySettings.from_env())


if __name__ == "__main__":
    main()
