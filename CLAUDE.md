# Conventions

Working conventions for this repository. Claude Code loads this file at the start of every session and contributors follow the same rules. The system is built with Claude Code under these conventions, and every decision is recorded in `docs/adr/`.

## Project

Casework service for a fictional bank's dispute-operations team. A preparer opens a case; an agent classifies it, routes it to Regulation E or Regulation Z, retrieves the governing paragraphs and role-restricted procedures, reads the synthetic account through MCP tools, computes deadlines with deterministic code, and drafts cited findings for a separate approver. Python 3.13, managed with uv; ruff, mypy and pytest.

## Commands

    uv sync --locked
    uv run ruff check . && uv run ruff format --check .
    uv run mypy
    docker compose up -d --wait
    uv run pytest
    uv run pip-audit

In each of `infra/bootstrap` and `infra/foundry`:

    terraform fmt -check -recursive
    terraform init -backend=false
    terraform validate

## Code

- Maximum simplicity, subject to four constraints holding at once: correct (including failure paths, authorization paths and idempotency of writes); industry-standard (how it is done in production today, never a tutorial pattern); acceptable to a senior reviewer without changes; self-explanatory. When they conflict: correct, then industry-standard, then acceptable, then simple.
- The code carries its own meaning. If a comment is needed to make a block make sense, restructure the block. A comment states only a why the reader could not infer.
- Documentation states the current rule. The history of what changed belongs in the commit message.
- AI capabilities are vendor or framework components. Cosine similarity, vector stores, BM25, rerankers and fusion are never implemented here, including during a simplification pass. Application code, glue and instrumentation are written here.
- Seams are plain functions with one implementation each and no interfaces: parse_token, authorize and allowed_labels, guard, embed, retrieve, rerank, call_model, compute_deadlines.
- Minimum acceptable beats complete and clever. Legibility and defensibility come first.

## Strip list

Remove on sight:

- abstraction with one implementation
- configuration for values never configured
- try/except that adds no information
- docstrings restating the signature, comments narrating the next line, section dividers
- defensive validation of already-validated inputs
- once-called helpers
- type gymnastics where a concrete type is clearer
- logging no one reads
- anything anticipating a requirement not in scope

Never remove:

- trust-boundary checks (token verification, policy calls, row-level security settings)
- the design's named seams
- audit-event writes
- tracing instrumentation
- explicit vendor configuration
- interfaces a framework itself requires
- any component on the project's component table

## Invariants

`INVARIANTS.md` lists correctness that can look like complexity. Every item is backed by the named test. Removing an item is a bug, not a simplification. If an item can be expressed more simply without losing the guarantee, do that; otherwise leave it and make the reason evident from the code.

## Dependencies

- A dependency enters only after its version, license, purpose, required accounts and preconditions have been disclosed and approved. A component outside the agreed design is a stop-and-ask.
- Tools are pinned exactly in `pyproject.toml`. `uv.lock` is committed and CI runs `uv sync --locked`. Upgrades land through Dependabot pull requests or an explicit `uv lock --upgrade-package` commit.
- GitHub Actions are pinned by commit SHA with the version in a comment.

## Git

- Linear `main`. One pull request per step, rebase-merged. No long-lived branches.
- Each step ends with an annotated tag, `step/NN-name` for build steps and `rung/N-name` for measured configurations, with a one-line body. The tag is placed last and never moved; later fixes land on `main`.
- Commits carry a single human author. Author and committer are the repository's configured identity; no co-author trailers and no tool attribution in commit messages or tag bodies.
- Conventional commit messages. Commits stay local until the run report has been reviewed.
- Hooks are never bypassed; `--no-verify` is not used. Prompts, run reports and session transcripts are never committed. `.claude/` is ignored.

## Phase sequence

Each phase is a separate run.

1. Plan: files, tests, sequence and alternatives within the agreed design, approved before implementation.
2. Dependency disclosure and provisioning as code.
3. Implementation with tests.
4. Simplification pass in a fresh session, against the strip list and `INVARIANTS.md`.
5. Audit of the full codebase in a fresh context.
6. Review findings and fixes.
7. ADR rationale recorded.
8. Annotated tag on the final commit.
