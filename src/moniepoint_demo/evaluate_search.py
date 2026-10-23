"""Run representative retrieval cases against the real embedding model and Redis.

These are evaluation inputs, never agent dialogue or scripted application behavior.
"""

import json
import os
from pathlib import Path

from pydantic import TypeAdapter
from redis import Redis

from moniepoint_demo.models import Record
from moniepoint_demo.policies import PolicyStore
from moniepoint_demo.policy_search import OpenAIEmbeddings, PolicySearch, SearchRequest


class RetrievalCase(Record):
    query: str
    expected: str


def main() -> None:
    cases = TypeAdapter(list[RetrievalCase]).validate_json(
        Path("evals/policy-retrieval.json").read_text()
    )
    with Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6387/0"),
        decode_responses=True,
    ) as client:
        search = PolicySearch(
            PolicyStore(client, "moniepoint-demo:world:policies"), OpenAIEmbeddings()
        )
        if not search.status().ready:
            search.rebuild()
        passed = 0
        for case in cases:
            results = search.search(SearchRequest(query=case.query)).results
            identifiers = [hit.document.id for hit in results]
            found = case.expected in identifiers
            passed += found
            print(
                json.dumps(
                    {
                        "query": case.query,
                        "expected": case.expected,
                        "retrieved": identifiers,
                        "passed": found,
                    }
                )
            )
        print(f"Expected policy in top 3: {passed}/{len(cases)}")
        if passed != len(cases):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
