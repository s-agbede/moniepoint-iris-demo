"""Source-backed Markdown guidance, published as a versioned Redis corpus."""

from datetime import date
from hashlib import sha256
from importlib.resources import files
from typing import Literal

from pydantic import HttpUrl, TypeAdapter
from redis import Redis

from moniepoint_demo.models import Record
from moniepoint_demo.simulator import DomainError


class PolicySource(Record):
    id: str
    filename: str
    title: str
    source_url: HttpUrl
    retrieved_on: date
    scope: str


class PolicySummary(PolicySource):
    document_kind: Literal["public_guidance_summary"] = "public_guidance_summary"
    version: str


class PolicyDocument(PolicySummary):
    markdown: str


class PolicyCatalog(Record):
    corpus_version: str
    documents: list[PolicySummary]


class PolicyCorpus(Record):
    corpus_version: str
    documents: list[PolicyDocument]


def load_bundled_policies() -> PolicyCorpus:
    directory = files("moniepoint_demo").joinpath("knowledge")
    sources = TypeAdapter(list[PolicySource]).validate_json(
        directory.joinpath("manifest.json").read_text(encoding="utf-8")
    )
    documents: list[PolicyDocument] = []
    for source in sorted(sources, key=lambda item: item.id):
        markdown = directory.joinpath(source.filename).read_text(encoding="utf-8")
        # Metadata changes also invalidate previously cached answers.
        version = sha256((source.model_dump_json() + markdown).encode()).hexdigest()
        documents.append(
            PolicyDocument(
                **source.model_dump(),
                markdown=markdown,
                version=version,
            )
        )
    version = sha256("".join(doc.version for doc in documents).encode()).hexdigest()
    return PolicyCorpus(corpus_version=version, documents=documents)


class PolicyStore:
    def __init__(self, client: Redis, key: str) -> None:
        self.client = client
        self.key = key

    def publish_bundled(self) -> None:
        # One atomic snapshot keeps documents and corpus version consistent.
        # Editable local files are authoritative; HTTP reads come from Redis.
        self.client.set(self.key, load_bundled_policies().model_dump_json())

    def read(self) -> PolicyCorpus:
        raw = self.client.get(self.key)
        if not isinstance(raw, (str, bytes)):
            raise DomainError("Policy corpus unavailable; restart to republish", 503)
        return PolicyCorpus.model_validate_json(raw)

    def catalog(self) -> PolicyCatalog:
        corpus = self.read()
        return PolicyCatalog(
            corpus_version=corpus.corpus_version,
            documents=[
                PolicySummary.model_validate(document.model_dump(exclude={"markdown"}))
                for document in corpus.documents
            ],
        )

    def get(self, policy_id: str) -> PolicyDocument:
        for document in self.read().documents:
            if document.id == policy_id:
                return document
        raise DomainError("Policy document not found", 404)
