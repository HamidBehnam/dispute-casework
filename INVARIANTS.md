# Invariants

Correctness that can look like complexity. Each item is backed by the named test; until that test exists, the entry reads "pending" with the step that adds it. Removing an item is a bug, not a simplification. An item leaves this list only through a new ADR.

| Invariant | Backing test |
|---|---|
| `interrupt()` is the first statement of the approval node; no side effect precedes it | pending, step 14: `tests/test_graph.py::test_approval_node_interrupts_before_side_effects` |
| Approvals and bank writes carry an idempotency key enforced by a unique constraint | pending, step 15: `tests/test_approval.py::test_duplicate_approval_rejected_by_constraint`; migration assertion in step 7 |
| A fresh `HybridSearchConfig` is built for every retrieval call | `tests/test_retrieve.py::test_hybrid_config_built_per_call`, `tests/test_retrieve.py::test_sequential_searches_use_their_own_keyword_query`; `tests/test_retrieve.py::test_langchain_postgres_still_writes_the_query_into_a_shared_config` fails once the vendor no longer mutates the config |
| Corpus embeddings are loaded only after every snapshot file matches its sha256 in the manifest | `tests/test_corpus.py::test_load_rejects_manifest_mismatch` |
| No checkpoint row holds a token | pending, step 14: `tests/test_graph.py::test_checkpoint_rows_contain_no_token` |
| The token ledger fails closed, including when usage is missing from the response | pending, step 13: `tests/test_ledger.py::test_reserve_denies_without_row`, `tests/test_ledger.py::test_settle_without_usage_fails_closed` |
| `FORCE ROW LEVEL SECURITY` is set on every protected table | pending, step 3: `tests/test_rls.py::test_force_rls_on_protected_tables`; re-asserted in step 7 |
| Cerbos runs with `strictEvaluation` and schema rejection on | pending, step 11: `tests/test_policy.py::test_unknown_attribute_rejected` |
| Every cited paragraph ID is checked against the supplied context set; a miss escalates to the human | pending, step 14: `tests/test_citations.py::test_uncited_paragraph_id_escalates` |
| `stream_usage` is enabled on the chat model | pending, step 14: `tests/test_models.py::test_chat_model_sets_stream_usage` |
| A holiday falling on Saturday is not observed on Friday in the bank calendar | pending, step 10: `tests/test_calendar.py::test_saturday_holiday_not_observed_friday` |
