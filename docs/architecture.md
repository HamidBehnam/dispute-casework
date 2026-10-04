# Architecture notes

The current state of the parts that exist. Decisions and measured results are in `docs/adr/`.

## Corpus

`corpus/ecfr/<snapshot>/` holds one dated snapshot of a regulation part from the eCFR versioner API: the part XML as downloaded, `chunks.jsonl`, `embeddings.npy` and `manifest.json`. The manifest records the snapshot date, the source URL, the latest amendment date, the sha256 of the other three files, the chunk counts, the embedding model and the parser and content-format versions. The eCFR is an unofficial edition of the CFR, and the manifest labels the snapshot as such.

`ecfr.parse_part` rebuilds the paragraph outline from the flat XML and gives every paragraph an ID: `1005.11(c)(2)(i)` for regulation text, `1005.11(c)-3` for a Supplement I comment. Regulation text is tagged `binding` and Supplement I `official_interpretation`. Each paragraph is one chunk. Its content is the paragraph ID, the heading path and the paragraph text on three lines; that one string is embedded, keyword-indexed and reranked.

`python -m dispute_casework.corpus_build` has one command per step: `fetch` downloads the XML, `embed` writes the chunk and embedding files, `load` rebuilds the `chunks` table, and `measure` reports the size of Part 1026 without storing it. `load` checks every file against the manifest before it touches the database.

## Retrieval

`retrieval.py` holds three plain functions. `embed` calls the embedding deployment. `retrieve(store, embedding, keywords)` runs the vendor's hybrid search on Postgres (pgvector cosine distance and a stored `tsvector` column, fused by reciprocal rank fusion) and returns twenty candidates. `rerank` sends candidates to the rerank deployment and returns them in relevance order. Every search builds its own `HybridSearchConfig`; `INVARIANTS.md` names the tests that hold this in place. It takes the query embedding for the dense leg and a short keyword string for the keyword leg, and raises `ValueError` when the keyword string is empty or blank, because the vendor then returns the dense leg without fusion.

`tests/test_exit.py` replays one recorded run of the five retrieval arms over the 30 queries in `tests/queries.json`. The gates on the keyword leg and on recall@20 are asserted. The gate on recall@5 after rerank is not met in the recorded run, 22/26 expected IDs against 23/26 before rerank (ADR 0004); its test is an expected failure that fails the suite once the gate is met.

The database is the single service in `compose.yaml`. Nothing is created when the container starts: the extension and the table are created by `corpus_build load`, and by a session fixture in the tests.

## Recorded model responses

Tests that reach a model replay recorded responses (pytest-recording on vcrpy). The default record mode is `none` and pytest runs with `--block-network`, so a test run makes no model call. A cassette stores the sha256 of each request body in place of the body, so it carries no query or regulation text; authorization headers are removed and response headers are reduced to the content type. A change to a query, a chunk, a retrieval parameter, a model deployment or the request shape of an SDK changes the hash, and the affected test fails until its cassette is recorded again.

Recording a cassette again needs `FOUNDRY_ENDPOINT`, an Azure CLI login with access to the Foundry account and the database from `compose.yaml` running, and is run as `uv run pytest --record-mode=rewrite tests/test_retrieve.py tests/test_exit.py`. Recording the exit test paces its rerank calls under the deployment's rate limit and takes about half an hour; the run is recorded once, and the recall@5 test replays what was recorded.
