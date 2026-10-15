# Invariants

Correctness that can look like complexity. Each item is backed by the named test; until that test exists, the entry reads "pending" with the step that adds it. Removing an item is a bug, not a simplification. An item leaves this list only through a new ADR.

| Invariant | Backing test |
|---|---|
| `interrupt()` is the first statement of the approval node; no side effect precedes it | pending, step 14: `tests/test_graph.py::test_approval_node_interrupts_before_side_effects` |
| Approvals and bank writes carry an idempotency key enforced by a unique constraint | pending, step 15: `tests/test_approval.py::test_duplicate_approval_rejected_by_constraint`; migration assertion in step 7 |
| A fresh `HybridSearchConfig` is built for every retrieval call | `tests/test_retrieve.py::test_hybrid_config_built_per_call`, `tests/test_retrieve.py::test_sequential_searches_use_their_own_keyword_query`; `tests/test_retrieve.py::test_langchain_postgres_still_writes_the_query_into_a_shared_config` fails once the vendor no longer mutates the config |
| Corpus embeddings are loaded only after every snapshot file (the XML, the HTML, the chunks and the embeddings) is present on disk, is listed in the manifest and matches its sha256 | `tests/test_corpus.py::test_load_rejects_manifest_mismatch`, `tests/test_corpus.py::test_load_rejects_a_file_the_manifest_does_not_list`, `tests/test_corpus.py::test_load_rejects_a_listed_file_missing_on_disk` |
| Every regulation paragraph ID is an id of the eCFR's rendered file, verbatim | `tests/test_ecfr.py::test_every_regulation_id_is_an_id_of_the_ecfr_file_verbatim` |
| Model clients carry the explicit timeout under a key and under a token credential | `tests/test_foundry.py::test_sdk_clients_carry_the_timeout_under_a_key_and_under_a_token_provider` |
| A rate-limit response is never stored in a cassette | `tests/test_recording.py::test_a_rate_limit_response_is_never_stored` |
| The published results file is what the recorded run produces | `tests/test_exit.py::test_results_file_is_current` |
| DeepEval is switched off before it is imported (no telemetry, no `.env` or key file read, read-only file system), its pytest plugin is not loaded and it reports no Confident AI login | `tests/test_deepeval_quiet.py::test_deepeval_is_switched_off_before_import_and_kept_out_of_pytest` |
| No checkpoint row holds a token | `tests/test_checkpointer.py::test_a_configurable_value_lands_in_checkpoint_metadata_and_nowhere_else`, also observed on Azure Database for PostgreSQL in the step 3 spike (ADR 0005); first version: the checkpointer copies every value of `config["configurable"]` into `checkpoints.metadata`, so the token travels in neither the configurable nor the state; re-asserted over a graph run in step 14: `tests/test_graph.py::test_checkpoint_rows_contain_no_token` |
| The token ledger fails closed, including when usage is missing from the response | pending, step 13: `tests/test_ledger.py::test_reserve_denies_without_row`, `tests/test_ledger.py::test_settle_without_usage_fails_closed` |
| `FORCE ROW LEVEL SECURITY` is set on every protected table | `tests/test_rls.py::test_force_rls_on_protected_tables`, `tests/test_rls.py::test_each_tier_sees_exactly_its_labels`, `tests/test_rls.py::test_concurrent_searches_return_only_the_rows_of_their_tier`, also observed on Azure Database for PostgreSQL in the step 3 spike (ADR 0005); re-asserted in step 7 |
| The loader grant to the login carries `INHERIT FALSE`: the login holds no corpus privilege without a role, including while it holds the loader | `tests/test_rls.py::test_the_login_holds_no_corpus_privilege_without_a_role_while_granted_the_loader` |
| A pooled connection is returned to the role its startup option bound whenever the pool takes it back | `tests/test_pools.py::test_a_tier_engine_connection_returns_to_its_bound_role`, `tests/test_pools.py::test_a_checkpointer_connection_returns_to_its_bound_role` |
| Cerbos runs with `strictEvaluation` and schema rejection on | pending, step 11: `tests/test_policy.py::test_unknown_attribute_rejected` |
| Every cited paragraph ID is checked against the supplied context set; a miss escalates to the human | pending, step 14: `tests/test_citations.py::test_uncited_paragraph_id_escalates` |
| `stream_usage` is enabled on the chat model | pending, step 14: `tests/test_models.py::test_chat_model_sets_stream_usage` |
| A holiday falling on Saturday is not observed on Friday in the bank calendar | pending, step 10: `tests/test_calendar.py::test_saturday_holiday_not_observed_friday` |
