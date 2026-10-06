"""Builds the corpus files of the pinned eCFR snapshot, one step per command.

fetch    download the part as XML and as rendered HTML and start manifest.json
embed    parse both, write chunks.jsonl and embeddings.npy, complete the manifest
load     rebuild the chunk table in the database named by DATABASE_URL

embed and load need FOUNDRY_ENDPOINT.
"""

import argparse
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path

import httpx
import numpy as np
from langchain_postgres import PGEngine

from dispute_casework.corpus import (
    CHUNKS,
    CONTENT_FORMAT_VERSION,
    EMBEDDINGS,
    HTML,
    MANIFEST,
    PART,
    SNAPSHOT,
    SNAPSHOT_DIR,
    XML,
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

ECFR = "https://www.ecfr.gov/api"
PIN_REASON = (
    "2023-04-19 is the date of the last Part 1005 amendment in force. Later eCFR "
    "versions carry the amendments of 89 FR 106768, which Public Law 119-10 "
    "(May 9, 2025) disapproved before they took effect."
)
EMBEDDING_DEPLOYMENT_VERSION = "1"


def fetch(directory: Path) -> None:
    # The renderer answers with a redirect to the same content addressed by chapter.
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        xml = client.get(
            f"{ECFR}/versioner/v1/full/{SNAPSHOT}/title-12.xml", params={"part": PART}
        )
        html = client.get(
            f"{ECFR}/renderer/v1/content/enhanced/{SNAPSHOT}/title-12",
            params={"part": PART},
        )
        versions = client.get(
            f"{ECFR}/versioner/v1/versions/title-12.json",
            params={"part": PART, "issue_date[lte]": SNAPSHOT},
        )
    for response in (xml, html, versions):
        response.raise_for_status()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / XML).write_bytes(xml.content)
    (directory / HTML).write_bytes(html.content)
    write_manifest(
        directory,
        {
            "snapshot": SNAPSHOT,
            "label": "unofficial eCFR snapshot",
            "sources": {XML: str(xml.url), HTML: str(html.url)},
            "latest_amendment_date": versions.json()["meta"]["latest_amendment_date"],
            "pin": {"reason": PIN_REASON, "checked": date.today().isoformat()},
            "files": {name: sha256(directory / name) for name in (XML, HTML)},
        },
    )


def embed_corpus(directory: Path, settings: FoundrySettings) -> None:
    manifest = verified_manifest(directory)
    paragraphs = parse_part(
        (directory / XML).read_bytes(), (directory / HTML).read_bytes()
    )
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
    vectors = embed(settings, [chunk["content"] for chunk in chunks])
    (directory / CHUNKS).write_text(
        "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks),
        encoding="utf-8",
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
    (directory / MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["fetch", "embed", "load"])
    match parser.parse_args().command:
        case "fetch":
            fetch(SNAPSHOT_DIR)
        case "embed":
            embed_corpus(SNAPSHOT_DIR, FoundrySettings())
        case "load":
            engine = PGEngine.from_connection_string(os.environ["DATABASE_URL"])
            load(engine, SNAPSHOT_DIR, FoundrySettings())


if __name__ == "__main__":
    main()
