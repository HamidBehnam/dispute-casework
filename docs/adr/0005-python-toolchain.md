# ADR 0005: Python toolchain

Status: Proposed

Supersedes 0001

## Context

A greenfield Python service built by one engineer with a coding agent. The repository is public from the first commit, so linting, type checking, secret scanning, a banned-phrase scan and a dependency audit run in CI from the first commit, and every tool version is pinned so that local and CI results match.

## Decision

- Python 3.13, pinned by minor in `.python-version`. Python 3.14 was considered and deferred. Checked at this commit, in a copy outside the repository: every package in `uv.lock` that applies to this platform, grpcio, psycopg-binary and the Azure SDKs among them, installs on Python 3.14.7 from a wheel with no source build; the test suite was not run on 3.14. Python 3.13 receives security fixes until October 2029.
- uv 0.12.21 for dependency resolution, the lockfile, interpreter installation and task running, with `uv_build` as the build backend.
- ruff 0.16.9 for linting and formatting; mypy 2.3.1 in strict mode; pytest 9.1.1; pip-audit 2.10.1.
- gitleaks 8.30.1: the installed binary in the local hooks; in CI the official image `ghcr.io/gitleaks/gitleaks`, pinned by the digest of its multi-platform index and run over the full history.
- GitHub Actions on `ubuntu-24.04` with actions pinned by commit SHA; Dependabot for the uv and github-actions ecosystems.
- Tools are pinned exactly in `pyproject.toml`; runtime dependencies use floors, with `uv.lock` as the exact pin. langchain-postgres is the exception and is pinned exactly; ADR 0004 records the version and what depends on it.

## Rationale

- uv over Poetry or pip-tools: lockfile, interpreter management and task running in one binary, with the fastest resolver.
- ruff over flake8 with black and isort: one tool, one configuration.
- mypy over pyright: strict mode with no Node runtime, and the widest use in Python-only CI.
- pytest: the standard runner.
- gitleaks over trufflehog: a single static binary with a staged-diff mode and a built-in ruleset.
- pip-audit over safety: maintained by the PyPA, Apache-licensed, and usable without an account.
- GitHub Actions over Azure Pipelines: native to the repository, and Dependabot maintains the action pins.
- `uv_build` over hatchling or setuptools: no additional dependency.

## Consequences

- Exact tool pins produce a Dependabot pull request for every tool release; a formatter or linter change is always a visible commit.
- uv is pre-1.0. Its version is pinned in `pyproject.toml` and read by the setup action, so local and CI runs use the same binary.
- The gitleaks image digest in the workflow is updated by hand; Dependabot does not track it.
- pip-audit runs per commit against the locked environment while Dependabot alerts run continuously; the overlap is intended.
- Moving to Python 3.14 is a one-line change plus a superseding ADR; the locked packages already install on it from wheels, and the suite has not been run on it.
