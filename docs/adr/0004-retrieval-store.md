# ADR 0004: Retrieval store

Status: Proposed

## Context

The agent retrieves the governing paragraphs of Regulation E before it drafts findings, and every citation it makes is a paragraph ID that must have been retrieved. The design puts hybrid retrieval in the same Postgres that holds the rest of the application data, through the pinned LangChain Postgres package, with a reranker as a second stage and Qdrant as the recorded fallback store. Before anything is built on that choice, a spike had to answer on the real regulation text: can the eCFR XML be turned into stable paragraph IDs; does hybrid search in langchain-postgres 0.0.18 work when configured explicitly, given its open defect with shared configuration; what does each stage contribute on a hand-written query set; and can the whole path be tested without network access.

The following were fixed in the instructions for this step before any run.

- **Query set.** 30 hand-written queries in three strata: bare citations, exact-term queries and paraphrased questions, each with its expected paragraph IDs and a keyword string of three to five terms (the ID itself for a citation).
- **Arms.** Dense search; hybrid search with the question as the keyword query; hybrid search with the keyword string; dense search with rerank; hybrid search with the keyword string and rerank. Rerank takes the arm's twenty candidates and returns five.
- **Gates.** They apply to the exact-term and paraphrased strata (the gate strata). The citation stratum is measured and reported, never gated, because an exact citation is served by the citation lookup in the design.
  - a. Two consecutive searches each use their own keyword query.
  - b. With keyword strings, the keyword leg returns rows for at least 90% of gate-strata queries.
  - c. recall@20 of hybrid search with keyword strings is at least 90%.
  - d. recall@5 after rerank is at least 85% and not below recall@5 of the fused top 5.
  - e. One live double run of the hybrid and rerank arm returns the same top 5; reported, not gated.
- **Fault attribution.** A failed check counts against the store only if the dense leg alone shows the paragraph to be retrievable. Parser, chunking and label faults are fixed in code and do not count against the store.
- **Levers.** `rrf_k`, and the text-search language constant with a table rebuild. A custom fusion function, hand-written keyword SQL and merging in Python are excluded.
- **Flip trigger.** The store moves to Qdrant only if hybrid search with keyword strings has a lower recall@20 than dense search on the exact-term stratum and a bounded sparse-hybrid run on Qdrant beats it on the same queries.
- **Keyword leg.** If dense search with rerank meets the targets on every stratum, the keyword leg is recorded as optional.

## Decision

- **Store.** PostgreSQL with pgvector through langchain-postgres 0.0.18 stays. The flip trigger did not fire: on the exact-term stratum hybrid search with keyword strings and dense search both reach recall@20 of 9/9, so no Qdrant run was made.
- **Gate d.** Recorded as not met, as measured. The thresholds, the chunking, the labels and the recorded run are unchanged. The exit test carries gate d as an expected failure that fails the suite once the gate is met.
- **Corpus snapshot.** 12 CFR Part 1005 with Supplement I from the eCFR versioner API at 2026-09-29, stored as downloaded under `corpus/ecfr/2026-09-29/` with `chunks.jsonl`, `embeddings.npy` and a `manifest.json` that records the source URL, the latest amendment date (2025-10-01), the sha256 of the three files and the label "unofficial eCFR snapshot". The eCFR is not an official legal edition of the CFR. Appendices A to C are not parsed; they are deferred to the corpus step, where the model forms get the legal status `model_form`.
- **Paragraph IDs.** `ecfr.parse_part` gives regulation paragraphs IDs of the form `1005.11(c)(2)(i)` and Supplement I comments `1005.11(c)-3` and `1005.11(c)(4)-5.i`. Comments on Appendix A are `1005.A-1`; that form is provisional until the corpus step. Regulation text is `binding`, Supplement I `official_interpretation`. The parser raises on any shape it does not recognise and on a repeated ID.
- **Chunking.** One chunk per paragraph. The content is the paragraph ID, the heading path and the paragraph text on three lines (content format 1); the same string is embedded, keyword-indexed and reranked.
- **Embeddings.** text-embedding-3-large at 1536 dimensions, generated once into a float32 `.npy` file row-aligned with `chunks.jsonl`. Requests carry raw strings (`check_embedding_ctx_length=False`). The files are committed to plain git; Git LFS starts with the first file over about 50 MB and is set up before that file is added.
- **Table.** Exact search, cosine distance, `PGVectorStore` on the psycopg 3 driver. Table `chunks` is created by the vendor's `init_vectorstore_table` with typed metadata columns (`paragraph_id`, `part`, `section`, `source`, `legal_status`, `snapshot`), a stored `content_tsv` column in `pg_catalog.english`, no JSON metadata column, no vector index and no GIN index at this size. Row IDs are `uuid5(snapshot:paragraph_id)`. The table is created and loaded by an explicit step, after the manifest hashes verify; nothing runs at container start.
- **Hybrid search.** Every search builds a new `HybridSearchConfig`: the stored tsvector column, reciprocal rank fusion with `rrf_k` 60, 40 rows per leg, and `k` 20 candidates passed explicitly because the vendor overwrites `fetch_top_k` with `k`. The keyword query is a separate short string, and `retrieve` raises `ValueError` when it is empty or blank. The text-search language is one constant shared with table creation, so changing it means rebuilding the table.
- **Rerank.** Cohere-rerank-v4.0-fast through the existing client, `max_tokens_per_doc` 512, top 5.
- **Database image.** `pgvector/pgvector:0.8.2-pg17`, pinned in `compose.yaml` by the digest of its multi-platform image index (linux/amd64 and linux/arm64) and used by CI through `docker compose`. The pin follows the PostgreSQL and pgvector versions Azure Database for PostgreSQL flexible server offers, not the newest image, so it is not on the dependency update bot.
- **Recorded responses.** pytest-recording 0.13.4 on vcrpy 8.3.0, record mode `none` and the network blocked by default. A cassette stores the sha256 of each request body, authorization headers are filtered and response headers reduced to the content type. For this step the recorded embedding response is the cache of query embeddings; an evaluation set of real size will want a plain file.
- **Next configuration.** Fixed here, before it is built, for the corpus step, in this order: the label policy for lead-in and parent/child paragraphs is settled first as a domain question (which paragraph is the correct citation); then a chunking rule that can be decided from the XML alone, under which lead-ins and list items carry their context, is applied to every chunk and every label; the corpus is embedded again as a new configuration under its own tag; it is evaluated on new queries written after the rule and before the run; and the result below is kept beside the new one.

## Findings

Measured on 2026-10-01 against PostgreSQL 17.10 with pgvector 0.8.2, SQLAlchemy 2.1.1, psycopg 3.3.6.

**Corpus.** The snapshot (859,423 bytes, sha256 `0669a14f…909a10d0`) has 27 sections with 741 regulation paragraphs, and 1,013 comment paragraphs in Supplement I. Two defects in the source were found: comments 32(b)(1)-2 to -7 are printed twice (12 paragraphs), and the heading of 17(b)(3) lost its tag and sits at the end of the preceding paragraph. The parser keeps the first copy of a repeated comment and recovers the heading, which leaves 1,001 comments and 1,742 chunks, each with a unique ID. The chunks hold 233,832 tokens (cl100k_base), median 105 per chunk. Three chunks exceed 512 tokens (the longest is 602) and are truncated by the reranker. 269 paragraphs have fewer than 25 tokens of their own text.

**Embedding run.** 30 requests of at most 8,000 tokens, one per minute under the deployment's 10,000 tokens-per-minute limit: 233,832 tokens in 29.8 minutes. No rate-limit response and no retry were observed; the gaps between requests stayed at 60 to 62 seconds. At the list price of the model this is about $0.03; the price was not checked again for this record. `embeddings.npy` is 10.7 MB.

**Part 1026.** Fetched and measured, not stored: 3,946,980 bytes, sha256 `d4b9040e…50e500f0`, 9,518 paragraph elements, 802,064 tokens. One vector per paragraph would be about 58 MB, so its embedding file is the first to go to Git LFS.

**langchain-postgres 0.0.18.** Issue 337 and its fix, pull request 338, were both open on 2026-10-01. Line numbers refer to `langchain_postgres/v2/async_vectorstore.py` at 0.0.18.

- A search writes into the config it is given: `fetch_top_k` (line 719) and, when the keyword query is empty, the query text (lines 804 to 806). A test shows that 0.0.18 still writes the query into a shared config and fails once a later version stops doing so. With a config per call, two consecutive searches each use their own keyword query. A metadata filter constrains both legs.
- The keyword leg is `plainto_tsquery` (line 726), which requires every term to match; pull requests 268 and 339, both open, would relax this. Whatever produces the keyword string is therefore part of the retrieval path.
- The keyword leg orders by `ts_rank_cd` with no tie-breaker (line 733). Run directly on the loaded table, 7/30 of the keyword strings return rows with tied scores; the rank within a tie follows the order in which Postgres returns the rows.
- With a config whose keyword query is empty, the search skips the keyword leg and the fusion and returns the dense leg's 40 rows instead of `k` (lines 654 to 656 and 746). `retrieve` rejects an empty keyword string for that reason.
- Each leg runs on its own pooled connection (lines 708 and 734), so a transaction-scoped setting made by the caller does not reach either query. Row-level security for the corpus has to bind at the engine or pool level.
- Both legs select the embedding column (lines 661 to 665): a search fetches 80 vectors it does not use. The cost is latency only.

**Exit test.** 30 queries: 5 bare citations, 9 exact-term queries and 16 paraphrased questions. Every figure below is from the one recorded run that `tests/test_exit.py` replays; the variation seen in a second live run is under Determinism. Recall is micro-averaged over expected IDs: the expected IDs found in the first k results over all expected IDs. One query carries two expected IDs, so the 25 gate-strata queries have 26. "Rescued" counts expected IDs in the hybrid top 20 that the dense top 20 lacks; "lost" is the reverse.

| Stratum (queries, expected IDs) | Arm | Keyword leg non-empty | Rescued | Lost | recall@20 | recall@5 fused | recall@5 after rerank |
|---|---|---|---|---|---|---|---|
| citation (5, 5) | dense | – | – | – | 1/5 | 0/5 | 1/5 |
| citation (5, 5) | hybrid, raw question | 4/5 | 2 | 0 | 3/5 | 3/5 | – |
| citation (5, 5) | hybrid, keyword string | 5/5 | 4 | 0 | 5/5 | 5/5 | 5/5 |
| exact-term (9, 9) | dense | – | – | – | 9/9 | 8/9 | 9/9 |
| exact-term (9, 9) | hybrid, raw question | 6/9 | 0 | 0 | 9/9 | 9/9 | – |
| exact-term (9, 9) | hybrid, keyword string | 9/9 | 0 | 0 | 9/9 | 9/9 | 9/9 |
| paraphrased (16, 17) | dense | – | – | – | 16/17 | 14/17 | 13/17 |
| paraphrased (16, 17) | hybrid, raw question | 0/16 | 0 | 0 | 16/17 | 14/17 | – |
| paraphrased (16, 17) | hybrid, keyword string | 15/16 | 0 | 0 | 16/17 | 14/17 | 13/17 |
| gate strata (25, 26) | dense | – | – | – | 25/26 | 22/26 | 22/26 |
| gate strata (25, 26) | hybrid, raw question | 6/25 | 0 | 0 | 25/26 | 23/26 | – |
| gate strata (25, 26) | hybrid, keyword string | 24/25 | 0 | 0 | 25/26 | 23/26 | 22/26 |

| Gate | Threshold | Result | |
|---|---|---|---|
| a. Consecutive searches use their own keyword query | holds | holds | met |
| b. Keyword leg non-empty with keyword strings, gate strata | at least 90%, which is 23/25 | 24/25 (96%) | met |
| c. recall@20, hybrid with keyword strings, gate strata | at least 90%, which is 24/26 | 25/26 (96%) | met |
| d. recall@5 after rerank, gate strata | at least 85%, which is 23/26, and not below the fused top 5 | 22/26; the fused top 5 holds 23/26 | not met |
| e. Two live runs return the same top 5 | reported | 30/30 identical, see Determinism | – |

Dense search with rerank also reaches 22/26, and finds the same expected IDs as hybrid search with rerank.

**Sample size.** The 95% Wilson interval is 67% to 94% for 22/26 and 71% to 96% for 23/26; the two intervals overlap almost entirely. The per-stratum figures, over 5, 9 and 17 expected IDs, are descriptive only.

**Gate d.** Four expected IDs are missing from the reranked top 5 (queries 15, 18, 26 and 30 below).

- 1005.11(a)(1) and 1005.12(a)(1)(iv) are the same fault: a lead-in chunk that carries none of its paragraph's substance, which sits in its list items. 1005.11(a)(1) was first in the fused list, seven of the twenty candidates were its list items, and the reranked top 5 holds two of those and not the lead-in. 1005.12(a)(1)(iv) was not among the twenty candidates: the keyword leg returned no row and the dense leg ranked it 23rd of its 40; its list item (iv)(A) was sixteenth in the fused list.
- 1005.11(c)(2)(i)(A) and 1005.33(b)(1)(i) are list items whose chunk lacks the sentence of the parent paragraph. They were seventh and third in the fused list. The reranked top 5 holds the parent of the first, 1005.11(c)(2)(i); the parent of the second, 1005.33(b)(1), was sixth in the fused list and is not in the reranked top 5.

Three of the four were among the twenty candidates the store returned, so their miss lies in the order after retrieval; the fourth is a chunking fault. By the fault-attribution rule none counts against the store. The two levers were not run: neither can change the order after retrieval.

**Reranker.** On the gate strata rerank has no measurable effect on recall@5 at this sample size. Between the fused top 5 and the reranked top 5, membership changed for 3 expected IDs, 2 lost and 1 gained (exact McNemar test, p = 1.0). All four missed IDs are a lead-in or a list item; that parent/child paragraph granularity explains the misses is a hypothesis for the corpus step, not a finding. The data supports neither that rerank lowers recall nor that rerank meets the 85% threshold.

**Keyword leg.** On the gate strata the keyword leg returns rows for 6/25 queries when it is given the question as written (0/16 of the paraphrased questions) and for 24/25 when it is given the keyword string. There it rescued 0 expected IDs at 20 and added one to recall@5 before rerank (23/26 against 22/26 for dense search). On the citation stratum it rescued 4/5 expected IDs; dense search finds 1/5. The keyword strings were written by hand before the run, so these figures are an upper bound until strings generated by the triage stage are measured. The measured value of the keyword leg is on citation-shaped queries, which the citation lookup in the design also serves. The keyword leg is not recorded as optional, because dense search with rerank does not meet the gate d target.

Per query: the rank of each expected ID in each arm (– is absent), and the rows returned by the keyword leg for the raw question and for the keyword string.

| # | Stratum | Expected | Keyword rows raw / keyword | Dense | Hybrid raw | Hybrid keyword | Dense + rerank | Hybrid keyword + rerank |
|---|---|---|---|---|---|---|---|---|
| 1 | citation | 1005.6(b)(1) | 19 / 19 | – | 2 | 2 | – | 1 |
| 2 | citation | 1005.11(c)(2)(i) | 2 / 21 | – | – | 5 | – | 1 |
| 3 | citation | 1005.11(c)-3 | 0 / 3 | – | – | 2 | – | 1 |
| 4 | citation | 1005.33(c)(1) | 24 / 24 | – | 5 | 5 | – | 1 |
| 5 | citation | 1005.6(b)-2 | 6 / 5 | 10 | 2 | 1 | 1 | 1 |
| 6 | exact-term | 1005.11(c)(2)(i) | 0 / 2 | 2 | 2 | 1 | 1 | 1 |
| 7 | exact-term | 1005.11(c)(3)(i) | 0 / 3 | 2 | 2 | 2 | 1 | 1 |
| 8 | exact-term | 1005.6(b)(1) | 0 / 7 | 1 | 1 | 2 | 4 | 4 |
| 9 | exact-term | 1005.6(b)(3) | 2 / 5 | 1 | 1 | 1 | 2 | 2 |
| 10 | exact-term | 1005.11(b)(2) | 2 / 4 | 1 | 1 | 1 | 1 | 1 |
| 11 | exact-term | 1005.11(c)(3)(ii)(B) | 2 / 2 | 6 | 1 | 1 | 4 | 4 |
| 12 | exact-term | 1005.10(c)(1) | 1 / 2 | 1 | 1 | 1 | 1 | 1 |
| 13 | exact-term | 1005.13(b)(1) | 1 / 1 | 1 | 1 | 1 | 1 | 1 |
| 14 | exact-term | 1005.2(d) | 4 / 4 | 2 | 2 | 2 | 1 | 1 |
| 15 | paraphrased | 1005.11(a)(1) | 0 / 3 | 2 | 2 | 1 | – | – |
| 16 | paraphrased | 1005.11(b)(1)(i) | 0 / 15 | 1 | 1 | 2 | 1 | 1 |
| 17 | paraphrased | 1005.11(c)(1) | 0 / 5 | 1 | 1 | 1 | 1 | 1 |
| 18 | paraphrased | 1005.11(c)(2)(i)(A) | 0 / 1 | 7 | 7 | 7 | – | – |
| 19 | paraphrased | 1005.11(c)(3)(ii) | 0 / 1 | 1 | 1 | 1 | 1 | 1 |
| 20 | paraphrased | 1005.11(c)(4) | 0 / 2 | 8 | 8 | 8 | 4 | 4 |
| 21 | paraphrased | 1005.11(d)(1) | 0 / 5 | 1 | 1 | 1 | 1 | 1 |
| 22 | paraphrased | 1005.11(d)(2)(ii) | 0 / 1 | 1 | 1 | 1 | 1 | 1 |
| 23 | paraphrased | 1005.11(e) | 0 / 4 | 1 | 1 | 1 | 1 | 1 |
| 24 | paraphrased | 1005.6(b)(4), 1005.6(b)(4)-1 | 0 / 1 | 2, 1 | 2, 1 | 2, 1 | 2, 1 | 2, 1 |
| 25 | paraphrased | 1005.2(m)(1) | 0 / 1 | 5 | 5 | 5 | 2 | 2 |
| 26 | paraphrased | 1005.12(a)(1)(iv) | 0 / 0 | – | – | – | – | – |
| 27 | paraphrased | 1005.6(b)-2 | 0 / 1 | 1 | 1 | 1 | 1 | 1 |
| 28 | paraphrased | 1005.11(c)-3 | 0 / 3 | 1 | 1 | 1 | 1 | 1 |
| 29 | paraphrased | 1005.11(a)-4 | 0 / 1 | 1 | 1 | 1 | 1 | 1 |
| 30 | paraphrased | 1005.33(b)(1)(i) | 0 / 25 | 2 | 2 | 3 | – | – |

**Determinism.** The hybrid-with-keyword-string and rerank arm was run live a second time. The reranked top 5 was identical for 30/30 queries. The twenty candidates were identical, in order, for 28/30; for queries 4 and 25 the candidate lists differed and the top 5 did not. The embedding deployment is the source: 20/30 query vectors differed between the two calls, by at most 0.005 in a component. Query embeddings are not bit-stable across calls, which is why CI replays the recorded ones. No fused score was tied at the cut-off of twenty. One reranked top 5 (query 18) held two equal relevance scores, in the same order in both runs.

**Replay.** The recorded run replays with identical results against the same image on linux/amd64, the architecture CI runs on. vcrpy applies the request filter to the live request before matching, and on one code path twice, so the filter leaves a body that is already a hash unchanged; the default matchers are used.

## Consequences

- Gate d stays not met until the next configuration is measured. Retrieval through this path delivers the expected paragraph in the top 5 for 22/26 expected IDs on this query set, and later steps are built on that figure, not on the threshold.
- Gates fixed before a later run are written with these points, which do not change this result:
  - A gate is stated as a count against the known denominator. The 85% threshold on 26 expected IDs is 23/26, and 22/26 rounds to 85%.
  - A comparison between two arms is paired and carries a stated non-inferiority margin; a comparison of two point values is decided by one query.
  - Store gates are kept apart from rerank-stage gates, and the flip trigger is the operative rule for the store.
  - The relevance policy for hierarchical paragraphs is defined before the labels are written.
  - A clause over "every stratum" names what it means for a stratum without a target; the clause on the optional keyword leg did not, for the citation stratum.
- The citation check and every later step depend on the paragraph IDs. A new snapshot, a parser change or a content-format change produces new chunk files, a new embedding run of about half an hour at the current quota, and new cassettes.
- Hybrid search adds to dense search only when the caller supplies a keyword string. Whatever produces that string for a live query becomes part of the retrieval path and has to be measured with it.
- The per-call `HybridSearchConfig` stays until a langchain-postgres release stops mutating the config; the test that detects that release names what to review.
- The corpus row-level security has to be set on the engine or its pool, because the vendor's searches do not run inside the caller's transaction.
- A dependency update that changes a request body fails the recorded tests until the cassettes are recorded again, which needs the Foundry account, the Azure CLI login and the database, and about half an hour for the exit test at the current rerank quota.
- The database image is updated by hand when the versions offered by Azure Database for PostgreSQL change.
