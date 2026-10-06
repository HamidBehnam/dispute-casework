"""One call to each Foundry deployment, or only to the one named on the command line.

Run with FOUNDRY_ENDPOINT set. The credential is FOUNDRY_KEY when set and the
Azure CLI identity otherwise; a run under each covers both.
"""

import argparse
import sys

from dispute_casework.foundry import (
    EMBEDDING_DEPLOYMENT,
    EMBEDDING_DIMENSIONS,
    FoundrySettings,
    chat_model,
    embeddings,
    reranker,
)
from dispute_casework.retrieval import RERANK_DEPLOYMENT

DEPLOYMENTS = (
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "DeepSeek-V4-Pro",
    EMBEDDING_DEPLOYMENT,
    RERANK_DEPLOYMENT,
)
PROMPT = "Reply with the single word: ready"
QUERY = "When must a bank provisionally credit a consumer's account?"
DOCUMENTS = [
    "Regulation E requires provisional credit within 10 business days when the "
    "investigation takes longer.",
    "Regulation Z covers billing error disputes on open-end credit accounts.",
    "The bank calendar treats a Saturday holiday as a business day.",
]


def check_dimensions(vector: list[float]) -> None:
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"expected {EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}"
        )


def check(settings: FoundrySettings, deployment: str) -> str:
    if deployment == EMBEDDING_DEPLOYMENT:
        vector = embeddings(settings).embed_query(PROMPT)
        check_dimensions(vector)
        return f"{len(vector)} dimensions"
    if deployment == RERANK_DEPLOYMENT:
        response = reranker(settings).rerank(
            model=deployment,
            query=QUERY,
            documents=DOCUMENTS,
            top_n=3,
        )
        ranking = [(r.index, round(r.relevance_score, 3)) for r in response.results]
        return f"ranking {ranking}"
    reply = chat_model(settings, deployment).invoke(PROMPT)
    return repr(reply.text.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "deployment",
        nargs="?",
        choices=DEPLOYMENTS,
        help="check only this deployment; all of them when omitted",
    )
    selected = parser.parse_args().deployment
    settings = FoundrySettings()
    auth = "key" if settings.key else "Azure CLI identity"
    print(f"endpoint {settings.endpoint}; credential: {auth}")
    failed = False
    for deployment in [selected] if selected else DEPLOYMENTS:
        try:
            print(f"{deployment}: ok - {check(settings, deployment)}")
        except Exception as exc:
            print(f"{deployment}: failed - {type(exc).__name__}: {str(exc)[:300]}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
