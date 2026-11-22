"""Freezes the judge-selection inputs: 25 cases and one gpt-5.4-mini answer each.

Usage: python evals/judge_inputs.py INPUTS ROWS
"""

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from request_log import Attempts, RequestLog, request_row

from dispute_casework.foundry import FoundrySettings, chat_model

REPOSITORY = Path(__file__).resolve().parents[1]
QUERIES = REPOSITORY / "tests" / "queries.json"
CHUNKS = REPOSITORY / "corpus" / "ecfr" / "2023-04-19" / "chunks.jsonl"
CASE_COUNT = 25
ANSWER_DEPLOYMENT = "gpt-5.4-mini"


def build_cases(queries: Path, chunks: Path) -> list[dict[str, Any]]:
    """The first 25 queries, each with its expected paragraphs and their neighbours.

    The context of an expected paragraph is the chunk itself and the chunks
    immediately before and after it in file order.
    """
    with chunks.open(encoding="utf-8") as file:
        contents = [json.loads(line) for line in file]
    position = {chunk["paragraph_id"]: index for index, chunk in enumerate(contents)}
    cases = []
    for number, query in enumerate(
        json.loads(queries.read_text(encoding="utf-8"))[:CASE_COUNT], start=1
    ):
        indexes = sorted(
            {
                neighbour
                for paragraph_id in query["expected"]
                for neighbour in range(
                    position[paragraph_id] - 1, position[paragraph_id] + 2
                )
                if 0 <= neighbour < len(contents)
            }
        )
        cases.append(
            {
                "case_id": f"q{number:02d}",
                "input": query["text"],
                "retrieval_context": [contents[index]["content"] for index in indexes],
            }
        )
    return cases


def answer_prompt(case: dict[str, Any]) -> str:
    context = "\n\n".join(case["retrieval_context"])
    return (
        "Answer the question using only the context below. Cite the paragraph IDs "
        f"you rely on in parentheses.\n\nContext:\n{context}\n\n"
        f"Question: {case['input']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path)
    parser.add_argument("rows", type=Path)
    args = parser.parse_args()
    log = RequestLog(args.rows)
    attempts = Attempts()
    chat = chat_model(
        FoundrySettings(),
        ANSWER_DEPLOYMENT,
        http_client=attempts.sync_client,
        http_async_client=attempts.async_client,
    )
    cases = build_cases(QUERIES, CHUNKS)
    for case in cases:
        prompt = answer_prompt(case)
        attempts.current = []
        started_at = time.time()
        message: AIMessage | None = None
        error: Exception | None = None
        try:
            message = chat.invoke(prompt)
        except Exception as raised:
            error = raised
        log.write(
            request_row(
                case_id=case["case_id"],
                repeat=0,
                step=None,
                prompt=prompt,
                started_at=started_at,
                attempts=attempts.current,
                message=message,
                error=error,
                outcome="failed" if error else "answered",
            )
        )
        if error or message is None:
            raise SystemExit(f"{case['case_id']}: {error}")
        case["actual_output"] = str(message.text)
    frozen = json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n"
    args.inputs.write_text(frozen, encoding="utf-8")
    print(hashlib.sha256(frozen.encode()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
