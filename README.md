# dispute-casework

![ci](https://github.com/HamidBehnam/dispute-casework/actions/workflows/ci.yml/badge.svg)

Reg E / Reg Z error-resolution casework for a dispute-operations team.

Casework service for a fictional bank's dispute-operations team. When a customer reports an error on an account, a preparer opens a case; an agent classifies it, routes it to Regulation E or Regulation Z, retrieves the governing regulation paragraphs and the bank's role-restricted procedures, reads the synthetic account through MCP tools, computes deadlines with deterministic code, and drafts cited findings, a proposed action and a customer letter. A separate approver approves or returns the case with a reason code; approved actions post to a mock bank through idempotent writes, and every event lands in an audit timeline. All data is synthetic and the system is a demonstration, not a bank.

Status: toolchain, Foundry account and model deployments are provisioned; retrieval over a pinned Regulation E snapshot is implemented and measured (ADR 0004); the casework application is not built yet. Decisions are recorded in `docs/adr/`, conventions in `CLAUDE.md`, and correctness invariants in `INVARIANTS.md`.
