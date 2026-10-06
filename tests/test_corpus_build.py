import shutil
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from dispute_casework import corpus_build
from dispute_casework.corpus import verified_manifest
from dispute_casework.corpus_build import embed_corpus, fetch
from dispute_casework.foundry import FoundrySettings


def ecfr(renderer_status: int) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("title-12.xml"):
            return httpx.Response(200, content=b"<DIV5/>")
        if request.url.path.endswith("title-12.json"):
            return httpx.Response(
                200, json={"meta": {"latest_amendment_date": "2023-04-19"}}
            )
        if "chapter" not in request.url.params:
            by_chapter = request.url.copy_add_param("chapter", "X")
            return httpx.Response(302, headers={"location": str(by_chapter)})
        return httpx.Response(renderer_status, content=b"<div/>")

    return httpx.MockTransport(respond)


def test_fetch_stores_both_source_files_and_where_they_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=ecfr(200)))
    directory = tmp_path / "snapshot"
    fetch(directory)
    manifest = verified_manifest(directory)
    assert set(manifest["files"]) == {"part-1005.xml", "part-1005.html"}
    assert (directory / "part-1005.xml").read_bytes() == b"<DIV5/>"
    assert (directory / "part-1005.html").read_bytes() == b"<div/>"
    assert manifest["sources"]["part-1005.xml"].endswith("/title-12.xml?part=1005")
    assert "chapter=X" in manifest["sources"]["part-1005.html"]
    assert manifest["latest_amendment_date"] == "2023-04-19"
    assert manifest["pin"]["reason"] == corpus_build.PIN_REASON


def test_fetch_writes_nothing_when_a_source_answers_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=ecfr(503)))
    directory = tmp_path / "snapshot"
    with pytest.raises(httpx.HTTPStatusError):
        fetch(directory)
    assert not directory.exists()


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
